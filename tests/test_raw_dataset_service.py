"""raw 数据集（trade_calendar）服务层语义用例。

判定「读自有库」的 raw 通路与 external 通路的差异：
- raw 不受 `DATAHUB_EXT_*` 闸门约束（读自有库，不发外部请求）；
- raw 空结果是**合法值**（不转 503）；
- raw **不缓存**（每次实查）。
回归：ext 闸门关时 external 仍 503（kind 分派未破坏 Phase 1 语义）。
"""
import types

import pytest

from app import config, service
from app.datasets import registry
from app.providers import registry as prov_reg


def _fake_db(rows):
    """把 `db` provider 换成返回固定行的假 provider。"""
    return lambda name: types.SimpleNamespace(
        fetch=lambda dataset_id, params: list(rows))


def test_trade_calendar_declared_raw():
    spec = registry.get_spec("trade_calendar")
    assert spec is not None
    assert spec.kind == "raw"
    assert registry.source_chain("trade_calendar") == ("db",)
    assert spec.ttl_seconds is None
    assert spec.column_names == ("cal_date", "is_open", "exchange")


def test_raw_readable_when_ext_gate_off(monkeypatch, no_retry):
    monkeypatch.setattr(config, "EXT_ENABLED", False)
    monkeypatch.setattr(config, "EXT_DATASETS", [])
    monkeypatch.setattr(prov_reg, "get_provider", _fake_db(
        [{"cal_date": "2026-01-02", "is_open": True, "exchange": "SSE"}]))
    rows, meta = service.get_dataset("trade_calendar", {})
    assert rows[0]["is_open"] is True
    assert meta == {"stale": False, "cached": False, "rows": 1}


def test_external_still_503_when_ext_gate_off(monkeypatch, no_retry):
    """回归：kind 分派不得放宽 external 的闸门。"""
    monkeypatch.setattr(config, "EXT_ENABLED", False)
    monkeypatch.setattr(config, "EXT_DATASETS", [])
    with pytest.raises(service.DatasetUnavailable):
        service.get_dataset("hotspot.indices", {})


def test_raw_empty_result_is_ok(monkeypatch, no_retry):
    """库内确无 = 尚未采集 → 返回空数组即成功，不转 503。"""
    monkeypatch.setattr(prov_reg, "get_provider", _fake_db([]))
    rows, meta = service.get_dataset("trade_calendar", {"is_open": "false"})
    assert rows == []
    assert meta["rows"] == 0 and meta["stale"] is False


def test_raw_not_cached(monkeypatch, no_retry):
    calls = {"n": 0}

    def _fetch(dataset_id, params):
        calls["n"] += 1
        return [{"cal_date": "2026-01-02", "is_open": True, "exchange": "SSE"}]

    monkeypatch.setattr(prov_reg, "get_provider",
                        lambda name: types.SimpleNamespace(fetch=_fetch))
    service.get_dataset("trade_calendar", {})
    service.get_dataset("trade_calendar", {})
    assert calls["n"] == 2


def test_bad_date_param_is_badparams():
    with pytest.raises(service.BadParams):
        service.get_dataset("trade_calendar", {"start": "2026/01/02"})


def test_is_open_default_is_true(monkeypatch, no_retry):
    """契约默认 is_open=true —— 未传参时 provider 收到 True。"""
    seen = {}

    def _fetch(dataset_id, params):
        seen.update(params)
        return []

    monkeypatch.setattr(prov_reg, "get_provider",
                        lambda name: types.SimpleNamespace(fetch=_fetch))
    service.get_dataset("trade_calendar", {})
    assert seen.get("is_open") is True
