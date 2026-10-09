"""腾讯行情源适配层（gtimg / proxy.finance.qq.com）

设计原则（镜像 `sources/baostock.py` 的「同形状输出」）：
- 各查询返回与 AkShare **同形状**的数据（中文列名），上层 build_rows / save /
  清洗 / 重试逻辑完全不动；
- 无 token、无外部 SDK，纯 HTTP（requests）+ 进程级 Session + 限速 +
  冷却，按「非官方抓取端点」姿态接入，不当稳定 API 依赖。

⚠️ **硬结论：腾讯 hfq（后复权）永久禁用于派生 adj_factor。**
实测（2026-09-16，见 docs/phase2/design/数据源评估-Tencent.md §3.1）腾讯 hfq/raw
比值**单调漂移**（600519 60 日：7.006→7.061），无除权日的日收益也偏差
0.09–0.34%，同一日内 hfq_open/raw_open ≠ hfq_close/raw_close —— 不是合法的
等比复权序列。复权因子只能走新浪腿（hfq/raw 恒 8.8825 = 库内口径）或
BaoStock 派生。`daily_pairs()` 以显式 tripwire 抛异常封死，防后人「优化」踩坑。

单位（实测定表，见设计文档 §2.3）：
- 行结构 `[0]=date [1]=open [2]=CLOSE [3]=high [4]=low [5]=volume
  [6]={} [7]=turnover_pct [8]=amount(万元)` —— **close 在下标 2，不是 4**；
- 成交量：主板/创业板 = 手（原样）；科创板 688/689 = 股（÷100）；
  北交所 43/83/87/92 = 暂按手（**未实证**，由 `audit-tencent` 定案）；
- 成交额：万元 → ×10000 元。
"""
import logging
import threading
import time
from datetime import date, timedelta

import pandas as pd
import requests

try:
    from app.collect_config import TENCENT_BATCH_SIZE, TENCENT_RATE_LIMIT, TENCENT_TIMEOUT
except ImportError:  # 旧部署无这些配置项：用内置默认值（本层保持独立可跑）
    TENCENT_BATCH_SIZE = 50
    TENCENT_RATE_LIMIT = 0.2
    TENCENT_TIMEOUT = 10

logger = logging.getLogger(__name__)

_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
_QUOTE_URL = "https://qt.gtimg.cn/q="

# 单次日K请求返回行数上限（实测 2000；>2000 时 data 退化为 list 报错结构）
_MAX_ROWS_PER_PAGE = 2000
# 连接级失败重试次数与间隔
_RETRIES = 1
_RETRY_DELAY = 1.0

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
}
# 腾讯日K列名（输出对齐 BaoStock/AkShare 的中文列）
_KLINE_COLS = ["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额", "换手率"]

_session: requests.Session | None = None
_session_lock = threading.Lock()


def is_source_blocked(exc: Exception) -> bool:
    """判定异常是否为「源被限」而非瞬时错误（自愈 stage1 据此分流）。

    腾讯限频/封禁 → 任务置 source_blocked 不再重试，绝不反复轰被限源；
    瞬时错误（连接超时/解析失败）→ 允许 attempts 重试。
    """
    return "腾讯源被限" in str(exc)


def _get_session() -> requests.Session:
    """进程级 Session 单例（复用连接）"""
    global _session
    with _session_lock:
        if _session is None:
            _session = requests.Session()
            _session.headers.update(_HEADERS)
        return _session


def _decode(content: bytes) -> str:
    """腾讯响应编码：日K JSON 为 UTF-8、快照 JS 行为 GBK —— 先 UTF-8 再 GBK"""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("gbk", errors="replace")


def _get_text(url: str) -> str:
    """GET 一次并返回解码文本；403/429 → 抛「源被限」，其余连接错误重试后抛"""
    last: Exception | None = None
    for attempt in range(_RETRIES + 1):
        try:
            resp = _get_session().get(url, timeout=TENCENT_TIMEOUT)
        except Exception as e:  # 连接/超时异常
            last = e
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY)
                continue
            raise RuntimeError(f"腾讯请求失败: {e}") from e
        if resp.status_code in (403, 429):
            raise RuntimeError(f"腾讯源被限(HTTP {resp.status_code})")
        try:
            resp.raise_for_status()
        except Exception as e:
            last = e
            if attempt < _RETRIES:
                time.sleep(_RETRY_DELAY)
                continue
            raise RuntimeError(f"腾讯请求失败: {e}") from e
        time.sleep(TENCENT_RATE_LIMIT)
        return _decode(resp.content)
    raise RuntimeError(f"腾讯请求失败: {last}")


# ---------- 代码 / 单位 ----------

def tx_code(code: str) -> str:
    """股票代码 → 腾讯带市场前缀格式（600519→sh600519 / 000001→sz000001 /
    830001→bj830001）"""
    code = str(code)
    prefix = "sh" if code.startswith("6") else (
        "bj" if code.startswith(("8", "4", "9")) else "sz")
    return prefix + code


def volume_divisor(code: str) -> int:
    """成交量原始单位 → 手的除数：科创板 688/689 = 股（÷100），其余 = 手（÷1）。

    北交所 43/83/87/92 当前按手处理（**未实证**，由 `audit-tencent` 定案）。
    """
    return 100 if str(code).startswith(("688", "689")) else 1


def _to_lots(code: str, raw_vol) -> int | None:
    """腾讯原始成交量 → 手（按板块归一）"""
    if raw_vol is None or raw_vol == "":
        return None
    try:
        v = float(raw_vol)
    except (TypeError, ValueError):
        return None
    return int(v / volume_divisor(code))


def _to_yuan(raw_amount) -> float | None:
    """成交额：万元 → 元"""
    if raw_amount is None or raw_amount == "":
        return None
    try:
        return float(raw_amount) * 10000
    except (TypeError, ValueError):
        return None


def _to_pct(raw) -> float | None:
    """腾讯换手率原始值 → 百分数（**源侧已是 %，不做换算**）。

    日K `[7]` 与快照 `[38]` 均为「换手率 %」（如 0.21 = 0.21%），实测与
    成交量/流通股本自洽（见 docs/phase2/design/数据源评估-全量股票池.md §3.3）。
    ⚠️ 新浪 `turnover` 是小数（0.0021）——不可混入本列（100× 静默错位）。
    """
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _ymd(d) -> str:
    """date / str → 腾讯需要的 YYYY-MM-DD（兼容 YYYYMMDD 紧凑式）"""
    if isinstance(d, date):
        return d.strftime("%Y-%m-%d")
    s = str(d).replace("/", "-")
    if len(s) == 8 and s.isdigit():  # AkShare 风格 YYYYMMDD → 补连字符
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


# ---------- 日K线（不复权） ----------

def _kline_page(symbol: str, end: str, count: int) -> list[list]:
    """取截至 end 的最近 count 行日K（腾讯忽略 start，按 end 向前截取）"""
    import json

    url = f"{_KLINE_URL}?param={symbol},day,,{end},{count},"
    text = _get_text(url)
    try:
        payload = json.loads(text)
    except ValueError as e:
        raise RuntimeError(f"腾讯日K响应非 JSON: {e}") from e
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("腾讯日K返回异常结构（data 非对象）")
    node = data.get(symbol)
    if not isinstance(node, dict):
        return []
    return node.get("day") or []


def daily_raw(code: str, start_date, end_date) -> pd.DataFrame:
    """不复权日行情 → 东财格式中文列 DataFrame（日期窗口翻页，向后取）

    腾讯 `count` 上限 2000 且 `start` 被忽略 —— 以 `end` 向前截取最近 N 行，
    不足时把 end 移到已取最早行的前一日继续，直到覆盖 start。

    单位归一在此完成：成交量 股→手（科创板 ÷100），成交额 万元→元。
    """
    symbol = tx_code(code)
    start, end = _ymd(start_date), _ymd(end_date)
    collected: list[list] = []
    cur_end = end
    for _ in range(64):  # 防御上限：64 页 × 2000 行 ≈ 128 年，绝不会触及
        page = _kline_page(symbol, cur_end, _MAX_ROWS_PER_PAGE)
        if not page:
            break
        earliest = str(page[0][0])
        # 未向更早推进（页首不比已取更早）→ 停止，防重复累积
        if collected and earliest >= str(collected[0][0]):
            break
        collected = page + collected
        if earliest <= start:
            break
        cur_end = (date.fromisoformat(earliest) - timedelta(days=1)).isoformat()
    if not collected:
        return pd.DataFrame(columns=_KLINE_COLS)

    recs = []
    for r in collected:
        if not isinstance(r, (list, tuple)) or len(r) < 9:
            logger.warning("%s 腾讯日K畸形行（len=%s），丢弃",
                           code, len(r) if isinstance(r, (list, tuple)) else "?")
            continue
        d = str(r[0])
        if d < start or d > end:
            continue
        try:
            recs.append({
                "日期": d,
                "开盘": float(r[1]),
                "最高": float(r[3]),
                "最低": float(r[4]),
                "收盘": float(r[2]),  # close 在下标 2
                "成交量": _to_lots(code, r[5]),
                "成交额": _to_yuan(r[8]),
                "换手率": _to_pct(r[7]),
            })
        except (TypeError, ValueError):
            logger.warning("%s 腾讯日K数值异常行 %s，丢弃", code, d)
    return pd.DataFrame(recs, columns=_KLINE_COLS)


def daily_pairs(*args, **kwargs):
    """⛔ tripwire：腾讯 hfq 非恒定比例，禁止用于派生 adj_factor。

    见模块 docstring 与 docs/phase2/design/数据源评估-Tencent.md §3.1。
    复权因子走新浪 hfq（恒等比 = 库内口径）或 BaoStock 派生。
    """
    raise RuntimeError(
        "腾讯 hfq 非恒定比例（实测 hfq/raw 单调漂移 7.006→7.061），"
        "禁止用于派生 adj_factor；复权因子走新浪 hfq 或 BaoStock 派生")


# ---------- 批量快照 ----------

def quote_batch(symbols: list[str]) -> dict[str, list[str]]:
    """批量快照 → {symbol: [~分隔字段]}。

    响应为 GBK 编码的 JS 行（`v_<symbol>="...";`，非 JSON）。
    **容忍部分代码缺失**（实测 58 请求 / 48 返回）——调用方须按缺失处理，
    绝不把部分成功当整体成功。
    """
    out: dict[str, list[str]] = {}
    for i in range(0, len(symbols), TENCENT_BATCH_SIZE):
        chunk = symbols[i:i + TENCENT_BATCH_SIZE]
        text = _get_text(_QUOTE_URL + ",".join(chunk))
        for line in text.split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, val = line.partition("=")
            sym = key.strip()
            if sym.startswith("v_"):
                sym = sym[2:]
            fields = val.strip().strip('"').split("~")
            if len(fields) < 6:  # 畸形行：丢弃，不崩
                logger.warning("腾讯快照畸形行 %s（字段数 %s）", sym, len(fields))
                continue
            out[sym] = fields
    return out


def _quote_date(fields: list[str], fallback: date) -> date:
    """快照行日期：取时间戳 [30]=YYYYMMDDHHMMSS 的日期段（避免用调用日致
    非交易日错标当日）；解析失败回退 fallback"""
    if len(fields) > 30:
        ts = str(fields[30])
        if len(ts) >= 8 and ts[:8].isdigit():
            try:
                return date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))
            except ValueError:
                pass
    return fallback


def snapshot_rows(codes: list[str], trade_date: date | None = None) -> list[dict]:
    """批量快照 → DailyPrice 形状行（含 prev_close）。

    快照只供当日不复权 OHLCV，**不含复权因子** —— 返回行 `adj_factor=None`，
    由调用方按「因子延续 / 除权回退」填充（见 tasks.job_sync_daily_price）。
    字段下标：`[3]`=现价 `[4]`=昨收 `[5]`=开盘 `[6]`=成交量 `[33]`=最高
    `[34]`=最低 `[37]`=成交额(万元) `[38]`=换手率(%) `[30]`=时间戳。
    """
    if not codes:
        return []
    fallback = trade_date or date.today()
    sym2code = {tx_code(c): c for c in codes}
    quotes = quote_batch(list(sym2code))
    missing = [sym2code[s] for s in sym2code if s not in quotes]
    if missing:
        logger.warning("腾讯快照缺 %s 只（%s…），须按缺失处理",
                       len(missing), missing[:5])
    rows = []
    for sym, f in quotes.items():
        code = sym2code[sym]
        if len(f) < 39:  # 需读 [38] 换手率
            logger.warning("腾讯快照 %s 字段不足（%s），丢弃", code, len(f))
            continue
        try:
            close = float(f[3])
            if close <= 0:  # 停牌/无成交 → 交由 cleaner 语义（volume<=0）丢弃
                continue
            rows.append({
                "code": code,
                "trade_date": _quote_date(f, fallback),
                "open": float(f[5]),
                "high": float(f[33]),
                "low": float(f[34]),
                "close": close,
                "volume": _to_lots(code, f[6]),
                "amount": _to_yuan(f[37]),
                "turnover_rate": _to_pct(f[38]),
                "adj_factor": None,
                "prev_close": float(f[4]),
            })
        except (TypeError, ValueError):
            logger.warning("腾讯快照 %s 数值异常，丢弃", code)
    return rows
