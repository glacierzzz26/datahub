"""数据集契约用例：DatasetSpec 自洽 + 列名与 provider 实际输出一致。"""
import pandas as pd
import pytest

from app.datasets import registry
from app.datasets.external import EXTERNAL_DATASETS
from app.providers.ext import akshare_hotspot, akshare_industry

_ALLOWED_TYPES = {"str", "int", "float", "bool", "date"}


def test_registry_keys_match_ids():
    assert set(registry.DATASET_REGISTRY) == {d.id for d in EXTERNAL_DATASETS}


def test_every_dataset_self_consistent():
    for d in EXTERNAL_DATASETS:
        assert d.id and d.title and d.kind in ("external", "raw")
        assert d.source, f"{d.id} 缺 source"
        assert d.columns, f"{d.id} 无列定义"
        names = [c.name for c in d.columns]
        assert len(names) == len(set(names)), f"{d.id} 列名重复"
        for c in d.columns:
            assert c.type in _ALLOWED_TYPES, f"{d.id}.{c.name} 未知类型 {c.type}"
        pnames = [p.name for p in d.params]
        assert len(pnames) == len(set(pnames)), f"{d.id} 参数名重复"
        for p in d.params:
            if p.required:
                assert p.default is None, f"{d.id}.{p.name} 必填不应有默认值"


def test_every_dataset_has_source_chain():
    for d in EXTERNAL_DATASETS:
        assert registry.source_chain(d.id), f"{d.id} 无源链"


# ---------- 列名与 provider 实际输出逐字对齐 ----------

def _assert_columns(spec_id: str, rows: list[dict]):
    allowed = set(registry.get_spec(spec_id).column_names)
    for r in rows:
        extra = set(r) - allowed
        assert not extra, f"{spec_id} provider 输出多出未声明列: {extra}"


def test_hotspot_indices_columns(monkeypatch):
    us = pd.DataFrame({"close": [100.0, 101.0]})
    monkeypatch.setattr(akshare_hotspot.ak, "index_us_stock_sina", lambda symbol: us)
    spot = pd.DataFrame([
        {"名称": "上证指数", "代码": "000001", "最新价": 3000.0, "涨跌幅": 0.5},
        {"名称": "深证成指", "代码": "399001", "最新价": 9000.0, "涨跌幅": -0.3},
    ])
    monkeypatch.setattr(akshare_hotspot.ak, "stock_zh_index_spot_em", lambda symbol: spot)
    rows = akshare_hotspot.fetch("hotspot.indices", {})
    assert rows and rows[0]["name"] == "道琼斯"
    _assert_columns("hotspot.indices", rows)


def test_hotspot_sectors_gain_columns(monkeypatch):
    ths = pd.DataFrame([
        {"板块": "白酒", "涨跌幅": 3.1, "净流入": 2.5, "领涨股": "贵州茅台"},
        {"板块": "银行", "涨跌幅": 1.2, "净流入": 1.1, "领涨股": "招商银行"},
    ])
    monkeypatch.setattr(akshare_hotspot.ak, "stock_board_industry_summary_ths", lambda: ths)
    rows = akshare_hotspot.fetch("hotspot.sectors_gain", {"top": 1})
    assert rows == [{"name": "白酒", "change_pct": 3.1, "leader": "贵州茅台"}]
    _assert_columns("hotspot.sectors_gain", rows)


def test_hotspot_sectors_flow_columns(monkeypatch):
    ths = pd.DataFrame([
        {"板块": "白酒", "涨跌幅": 3.1, "净流入": 2.5, "领涨股": "贵州茅台"},
    ])
    monkeypatch.setattr(akshare_hotspot.ak, "stock_board_industry_summary_ths", lambda: ths)
    rows = akshare_hotspot.fetch("hotspot.sectors_flow", {})
    assert rows == [{"name": "白酒", "net_inflow": "2.50亿"}]
    _assert_columns("hotspot.sectors_flow", rows)


def test_hotspot_hot_stocks_columns(monkeypatch):
    rank = pd.DataFrame([
        {"序号": 1, "代码": "600519", "名称": "贵州茅台", "涨跌幅": 2.0},
    ])
    monkeypatch.setattr(akshare_hotspot.ak, "stock_hot_rank_em", lambda: rank)
    rows = akshare_hotspot.fetch("hotspot.hot_stocks", {})
    assert rows[0]["code"] == "600519" and rows[0]["rank"] == 1
    _assert_columns("hotspot.hot_stocks", rows)


def test_industry_catalog_from_ths(monkeypatch):
    """主源同花顺：name_ths 取 name/code + summary_ths 按名补涨跌幅。"""
    names = pd.DataFrame([{"name": "白酒", "code": "881273"}])
    summary = pd.DataFrame([{"板块": "白酒", "涨跌幅": 3.1}])
    monkeypatch.setattr(akshare_industry.ak, "stock_board_industry_name_ths", lambda: names)
    monkeypatch.setattr(akshare_industry.ak, "stock_board_industry_summary_ths", lambda: summary)
    rows = akshare_industry.fetch("industry.catalog", {})
    assert rows == [{"name": "白酒", "code": "881273", "change_pct": 3.1}]
    _assert_columns("industry.catalog", rows)


def test_industry_catalog_falls_back_to_em(monkeypatch):
    def boom():
        raise RuntimeError("同花顺不可达")
    monkeypatch.setattr(akshare_industry.ak, "stock_board_industry_name_ths", boom)
    em = pd.DataFrame([{"板块名称": "银行", "板块代码": "BK0477", "涨跌幅": 1.2}])
    monkeypatch.setattr(akshare_industry.ak, "stock_board_industry_name_em", lambda: em)
    rows = akshare_industry.fetch("industry.catalog", {})
    assert rows == [{"name": "银行", "code": "BK0477", "change_pct": 1.2}]


def test_industry_catalog_ths_without_summary(monkeypatch):
    """概览失败时目录仍在，涨跌幅留空（不阻断）。"""
    names = pd.DataFrame([{"name": "白酒", "code": "881273"}])
    monkeypatch.setattr(akshare_industry.ak, "stock_board_industry_name_ths", lambda: names)

    def boom():
        raise RuntimeError("概览不可达")
    monkeypatch.setattr(akshare_industry.ak, "stock_board_industry_summary_ths", boom)
    rows = akshare_industry.fetch("industry.catalog", {})
    assert rows == [{"name": "白酒", "code": "881273", "change_pct": None}]


def test_industry_members_requires_param(monkeypatch):
    with pytest.raises(ValueError):
        akshare_industry.fetch("industry.members", {})


def test_industry_members_columns(monkeypatch):
    df = pd.DataFrame([{"代码": "600519", "名称": "贵州茅台"}])
    monkeypatch.setattr(akshare_industry.ak, "stock_board_industry_cons_em",
                        lambda symbol: df)
    rows = akshare_industry.fetch("industry.members", {"industry": "白酒"})
    assert rows == [{"code": "600519", "name": "贵州茅台"}]
    _assert_columns("industry.members", rows)
