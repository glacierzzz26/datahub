"""共享内核：`get_dataset(id, params)`（Phase 1 蓝图 §7）。

HTTP 与 MCP 两个出口都调本函数 —— **出口层零业务逻辑**。
职责：闸门判定 → 参数规整 → 缓存/单飞 → 源链降级 → 限流/冷却 → 降级兜底。
"""
import json
import logging
from datetime import date

from app import config
from app.cache import TTLCache
from app.datasets import registry
from app.providers import base as prov_base
from app.providers import registry as prov_reg
from app.ratelimit import Blocklist, RateLimiter, is_source_blocked

logger = logging.getLogger(__name__)

_cache = TTLCache()
_limiter = RateLimiter(config.RATE_LIMIT, config.RATE_CONCURRENCY)
_blocklist = Blocklist(config.BLOCK_COOLDOWN)


class UnknownDataset(Exception):
    """datasetId 未注册 → 404"""


class DatasetUnavailable(Exception):
    """数据集未翻闸 / 上游全失败且无缓存 → 503"""


class BadParams(Exception):
    """参数缺失/类型错 → 400"""


def _canonical(params: dict) -> str:
    return json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)


def normalize_params(spec, raw: dict) -> dict:
    """按 DatasetSpec 规整参数：类型转换 + 必填校验 + enum 校验。"""
    out: dict = {}
    for p in spec.params:
        val = raw.get(p.name)
        if val is None or (isinstance(val, str) and val.strip() == ""):
            if p.required:
                raise BadParams(f"缺少必需参数: {p.name}")
            if p.default is not None:
                out[p.name] = p.default
            continue
        if p.type == "int":
            try:
                out[p.name] = int(val)
            except (TypeError, ValueError):
                raise BadParams(f"参数 {p.name} 需为整数") from None
        elif p.type == "bool":
            out[p.name] = str(val).strip().lower() in ("1", "true", "yes", "on")
        elif p.type == "date":
            s = str(val).strip()
            try:
                date.fromisoformat(s)
            except ValueError:
                raise BadParams(f"参数 {p.name} 需为 YYYY-MM-DD") from None
            out[p.name] = s
        else:  # str / code / enum
            out[p.name] = str(val)
        if p.enum and out[p.name] not in p.enum:
            raise BadParams(f"参数 {p.name} 取值须为 {p.enum}")
    return out


def _fetch_from_chain(spec, params: dict) -> list[dict]:
    """按源链左→右尝试；命中源被限则冷却该 provider；全失败 raise。"""
    chain = registry.source_chain(spec.id) or ((spec.source,) if spec.source else ())
    if not chain:
        raise DatasetUnavailable(f"{spec.id} 无可用源链")
    errors: list[str] = []
    for pname in chain:
        provider = prov_reg.get_provider(pname)
        if provider is None:
            errors.append(f"{pname}: 未注册 provider")
            continue
        if _blocklist.is_blocked(pname):
            errors.append(f"{pname}: 冷却中（剩 {_blocklist.remaining(pname):.0f}s）")
            continue
        try:
            with _limiter.slot():
                rows = prov_base.call_with_retries(
                    provider.fetch, spec.id, params, name=f"{pname}:{spec.id}")
            logger.info("%s 取数成功：%s 行（源 %s）", spec.id, len(rows), pname)
            return rows
        except Exception as e:  # noqa: BLE001
            if is_source_blocked(e):
                _blocklist.block(pname)
                logger.warning("%s 源 %s 被限，冷却 %.0fs: %s",
                               spec.id, pname, config.BLOCK_COOLDOWN, e)
            else:
                logger.warning("%s 源 %s 失败: %s", spec.id, pname, e)
            errors.append(f"{pname}: {e}")
    raise DatasetUnavailable(f"{spec.id} 源链全部失败（{'；'.join(errors)}）")


def get_dataset(dataset_id: str, params: dict) -> tuple[list[dict], dict]:
    """返回 (rows, meta)。meta 含 stale/cached/rows 计数。

    异常语义：UnknownDataset → 404；BadParams → 400；DatasetUnavailable → 503。
    """
    spec = registry.get_spec(dataset_id)
    if spec is None:
        raise UnknownDataset(dataset_id)
    nparams = normalize_params(spec, params)

    # raw（读自有库）：不受外部采集闸门约束，不缓存、无 stale 兜底——
    # 本地库权威且廉价；**空结果合法**（库内确无 = 尚未采集），不转 503。
    if spec.kind == "raw":
        rows = _fetch_from_chain(spec, nparams)
        return rows, {"stale": False, "cached": False, "rows": len(rows)}

    # 闸门在缓存之前：external 未翻闸 → 零外部请求、直接 503
    if not config.ext_enabled(spec.id):
        raise DatasetUnavailable(f"{spec.id} 未翻闸（DATAHUB_EXT_ENABLED × DATAHUB_EXT_DATASETS）")

    key = f"{spec.id}:{_canonical(nparams)}"
    ttl = spec.ttl_seconds if spec.ttl_seconds is not None else config.CACHE_TTL_DEFAULT

    entry = _cache.peek(key)
    if entry is not None and entry[1]:  # 新鲜命中
        return entry[0], {"stale": False, "cached": True, "rows": len(entry[0])}

    try:
        rows = _cache.get_or_create(key, ttl, lambda: _fetch_from_chain(spec, nparams))
        return rows, {"stale": False, "cached": False, "rows": len(rows)}
    except DatasetUnavailable:
        # 上游全失败：有旧值 → stale 兜底；无 → 抛 503
        entry = _cache.peek(key)
        if entry is not None:
            return entry[0], {"stale": True, "cached": True, "rows": len(entry[0])}
        raise


def reset_state() -> None:
    """清空缓存与冷却（**仅测试用**）。"""
    _cache.clear()
    _blocklist.clear()
