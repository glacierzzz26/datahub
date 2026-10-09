"""Provider / 能力层用例：源链降级、缺列不中断、源被限冷却、参数校验。"""
import types

import pandas as pd
import pytest

from app import service
from app.providers import base as prov_base
from app.providers import registry as prov_reg
from app.providers.ext import akshare_hotspot
from app.ratelimit import Blocklist, is_source_blocked

# ---------- 闸门：未翻闸 → 零外部请求 + 503 ----------


def test_gate_off_no_external_call(monkeypatch, no_retry):
    from app import config

    monkeypatch.setattr(config, "EXT_ENABLED", False)
    monkeypatch.setattr(config, "EXT_DATASETS", [])
    called = {"n": 0}

    def _fetch(*a, **k):
        called["n"] += 1
        return []

    monkeypatch.setattr(prov_reg, "get_provider",
                        lambda name: types.SimpleNamespace(fetch=_fetch))
    with pytest.raises(service.DatasetUnavailable):
        service.get_dataset("hotspot.indices", {})
    assert called["n"] == 0


# ---------- 空结果即失败（不静默返回空数组）----------

def test_hot_stocks_all_sources_fail_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("东财不可达")
    monkeypatch.setattr(akshare_hotspot.ak, "stock_hot_rank_em", boom)
    monkeypatch.setattr(akshare_hotspot.ak, "stock_zt_pool_em", boom)
    with pytest.raises(RuntimeError):
        akshare_hotspot.fetch("hotspot.hot_stocks", {})


def test_stale_fallback_when_upstream_fails(monkeypatch, enable_all_ext, no_retry):
    """上游全失败但缓存有旧值 → 返 stale + meta.stale=true（不返回空、不 503）。"""
    ok = types.SimpleNamespace(
        fetch=lambda *a: [{"name": "X", "code": "1", "close": 1.0, "change_pct": 1.0}])
    monkeypatch.setattr(prov_reg, "get_provider", lambda name: ok)
    rows, meta = service.get_dataset("hotspot.indices", {})
    assert rows and meta["stale"] is False

    # 令缓存条目过期（直接操纵内部 clock：expire_at 置 0）
    for entry in service._cache._data.values():
        entry.expire_at = 0.0

    def boom(dataset_id, params):
        raise RuntimeError("全源失败")
    monkeypatch.setattr(prov_reg, "get_provider",
                        lambda name: types.SimpleNamespace(fetch=boom))
    rows, meta = service.get_dataset("hotspot.indices", {})
    assert meta["stale"] is True and rows


# ---------- 源链内降级（主源失败 → 兜底源）----------

def test_sectors_gain_falls_back_to_em(monkeypatch):
    def boom():
        raise RuntimeError("同花顺不可达")
    monkeypatch.setattr(akshare_hotspot.ak, "stock_board_industry_summary_ths", boom)
    em = pd.DataFrame([{"板块名称": "银行", "涨跌幅": 1.2, "领涨股票": "招商银行"}])
    monkeypatch.setattr(akshare_hotspot.ak, "stock_board_industry_name_em", lambda: em)
    rows = akshare_hotspot.fetch("hotspot.sectors_gain", {})
    assert rows == [{"name": "银行", "change_pct": 1.2, "leader": "招商银行"}]


# ---------- 缺列不中断整批 ----------

def test_hot_rank_missing_pct_column_yields_none(monkeypatch):
    rank = pd.DataFrame([{"序号": 1, "代码": "600519", "名称": "贵州茅台"}])  # 无涨跌幅列
    monkeypatch.setattr(akshare_hotspot.ak, "stock_hot_rank_em", lambda: rank)
    rows = akshare_hotspot.fetch("hotspot.hot_stocks", {})
    assert rows[0]["change_pct"] is None
    assert rows[0]["code"] == "600519"


# ---------- 源被限判定與冷却 ----------

def test_is_source_blocked_markers():
    assert is_source_blocked(RuntimeError("HTTP 429 Too Many Requests"))
    assert is_source_blocked(RuntimeError("403 Forbidden"))
    assert is_source_blocked(RuntimeError("腾讯源被限"))
    assert not is_source_blocked(RuntimeError("connection reset"))


def test_blocklist_cooldown_expires():
    t = [0.0]
    bl = Blocklist(cooldown=10, clock=lambda: t[0])
    bl.block("p")
    assert bl.is_blocked("p")
    t[0] = 11.0
    assert not bl.is_blocked("p")


def test_blocked_source_cools_and_skips_next_call(monkeypatch, enable_all_ext, no_retry):
    calls = {"n": 0}

    def boom(dataset_id, params):
        calls["n"] += 1
        raise RuntimeError("HTTP 429 Too Many Requests")

    monkeypatch.setattr(prov_reg, "get_provider",
                        lambda name: types.SimpleNamespace(fetch=boom))

    with pytest.raises(service.DatasetUnavailable):
        service.get_dataset("hotspot.indices", {})
    assert service._blocklist.is_blocked("akshare_hotspot")
    n = calls["n"]
    # 冷却期内再次请求：不再轰击上游
    with pytest.raises(service.DatasetUnavailable):
        service.get_dataset("hotspot.indices", {})
    assert calls["n"] == n


# ---------- 参数校验 ----------

def test_missing_required_param():
    spec = service.registry.get_spec("industry.members")
    with pytest.raises(service.BadParams):
        service.normalize_params(spec, {})


def test_param_default_applied():
    spec = service.registry.get_spec("hotspot.sectors_gain")
    assert service.normalize_params(spec, {})["top"] == 10


def test_int_param_coerced():
    spec = service.registry.get_spec("hotspot.sectors_gain")
    assert service.normalize_params(spec, {"top": "5"})["top"] == 5


def test_bad_int_param():
    spec = service.registry.get_spec("hotspot.sectors_gain")
    with pytest.raises(service.BadParams):
        service.normalize_params(spec, {"top": "abc"})


# ---------- with_timeout 超时兜底 ----------

def test_with_timeout_raises_on_slow():
    import time
    with pytest.raises(TimeoutError):
        prov_base.with_timeout(lambda: time.sleep(2), timeout=0.1)


def test_with_timeout_passthrough():
    assert prov_base.with_timeout(lambda x: x * 2, 21, timeout=1) == 42
