"""P14-1 研报数值域 8 张自有表的 db provider 用例。

内存 SQLite 建 8 张真表，真跑过滤/区间/last/投影；断言 provider 输出列 ⊆ DatasetSpec
声明列（契约），且关键口径（news 不外发 content_hash、macro 按 indicator 分区 last、
industry_index asof 截断）成立。
"""
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.datasets import registry
from app.models.tables import (
    AnnouncementByStock,
    CapitalFlow,
    CapitalFlowDetail,
    IndustryIndex,
    IndustrySwCons,
    IndustrySwInfo,
    Macro,
    NewsByStock,
)
from app.providers import db as db_provider

_TABLES = (
    CapitalFlow, CapitalFlowDetail, IndustryIndex, IndustrySwInfo,
    IndustrySwCons, Macro, NewsByStock, AnnouncementByStock,
)

_NEW_DATASETS = (
    "capital_flow", "capital_flow_detail", "industry_index",
    "industry_sw_info", "industry_sw_cons", "macro",
    "news_by_stock", "announcement_by_stock",
)


@pytest.fixture
def research_session(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    for m in _TABLES:
        m.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    s.add_all([
        # 龙虎榜记录：600000 两日 + 600519 一日
        CapitalFlow(id=1, code="600000", trade_date=date(2026, 1, 2), name="浦发",
                    close=10.0, pct_change=9.98, turnover_rate=3.2, reasons="日涨幅偏离值达7%"),
        CapitalFlow(id=2, code="600000", trade_date=date(2026, 1, 3), name="浦发",
                    close=11.0, pct_change=10.0, turnover_rate=5.0,
                    reasons="日涨幅偏离值达7%；连续三个交易日"),
        CapitalFlow(id=3, code="600519", trade_date=date(2026, 1, 3), name="茅台",
                    close=1700.0, pct_change=-2.0, turnover_rate=0.5, reasons="日跌幅偏离值达7%"),
        # 龙虎榜明细
        CapitalFlowDetail(id=1, code="600000", trade_date=date(2026, 1, 3),
                          dealer_name="机构专用", buy_amount=1000000.0, buy_pct=5.0,
                          sell_amount=0.0, sell_pct=0.0, net_amount=1000000.0, reason="买入"),
        CapitalFlowDetail(id=2, code="600000", trade_date=date(2026, 1, 3),
                          dealer_name="某营业部", buy_amount=0.0, buy_pct=0.0,
                          sell_amount=500000.0, sell_pct=2.5, net_amount=-500000.0, reason="卖出"),
        # 申万行业指数
        IndustryIndex(id=1, code="801010", trade_date=date(2026, 1, 2), open=3000.0,
                      high=3050.0, low=2990.0, close=3040.0, pct_change=1.3,
                      amplitude=2.0, chg_amount=40.0),
        IndustryIndex(id=2, code="801010", trade_date=date(2026, 1, 3), open=3040.0,
                      high=3080.0, low=3030.0, close=3070.0, pct_change=1.0,
                      amplitude=1.6, chg_amount=30.0),
        # 申万分类信息
        IndustrySwInfo(id=1, code="801010", level=1, name="农林牧渔", level_label="一级",
                       parent_name=None, member_count=100, pe_static=20.0, pe_ttm=22.0,
                       pb=2.5, dividend_yield=1.0),
        IndustrySwInfo(id=2, code="801012", level=3, name="种植业", level_label="三级",
                       parent_name="农林牧渔", member_count=20, pe_static=18.0, pe_ttm=19.0,
                       pb=2.0, dividend_yield=1.2),
        # 申万三级成分股
        IndustrySwCons(id=1, index_code="801012", stock_code="600000", stock_name="浦发",
                       market_cap=100.0, pe_ttm=5.0, pb=0.6, roe=10.0, dividend_yield=5.0,
                       net_profit_growth=3.0, revenue_growth=2.0),
        IndustrySwCons(id=2, index_code="801012", stock_code="600519", stock_name="茅台",
                       market_cap=20000.0, pe_ttm=30.0, pb=9.0, roe=25.0, dividend_yield=2.0,
                       net_profit_growth=15.0, revenue_growth=12.0),
        # 宏观：cn_cpi 两期 + cn_ppi 一期
        Macro(id=1, indicator="cn_cpi", period_label="2026年07月份",
              period_end=date(2026, 7, 31), cadence="month", level=100.5, yoy=0.5, mom=0.1),
        Macro(id=2, indicator="cn_cpi", period_label="2026年08月份",
              period_end=date(2026, 8, 31), cadence="month", level=100.8, yoy=0.8, mom=0.3),
        Macro(id=3, indicator="cn_ppi", period_label="2026年08月份",
              period_end=date(2026, 8, 31), cadence="month", level=98.0, yoy=-2.0, mom=None),
        # 个股新闻（published_at 含当天更晚时刻，验证 end 含当日）
        NewsByStock(id=1, code="600519", title="茅台发布中报", content="正文A",
                    published_at=datetime(2026, 1, 2, 9, 30), source="证券时报",
                    url="http://x/1", content_hash="h1"),
        NewsByStock(id=2, code="600519", title="茅台召开股东大会", content="正文B",
                    published_at=datetime(2026, 1, 2, 20, 0), source="上证报",
                    url=None, content_hash="h2"),
        # 个股公告
        AnnouncementByStock(id=1, code="600519", title="2025年年度报告",
                            category="年度报告", publish_date=date(2026, 1, 2),
                            url="http://a/1"),
        AnnouncementByStock(id=2, code="600519", title="关于回购股份的公告",
                            category="其他", publish_date=date(2026, 1, 5), url=None),
    ])
    s.commit()
    s.close()
    monkeypatch.setattr(db_provider.db, "get_session", lambda: factory())
    return factory


@pytest.mark.parametrize("dataset_id", _NEW_DATASETS)
def test_output_columns_are_declared(research_session, dataset_id):
    """provider 输出列 ⊆ DatasetSpec 声明列；无 fields 时空参数返回全列。"""
    rows = db_provider.fetch(dataset_id, {})
    assert rows, f"{dataset_id} 空结果 —— fixture 未生效"
    allowed = set(registry.get_spec(dataset_id).column_names)
    for r in rows:
        assert set(r) <= allowed, f"{dataset_id} 输出多出未声明列: {set(r) - allowed}"
    # 投影缺省 = 全列（声明序）
    assert tuple(rows[0]) == registry.get_spec(dataset_id).column_names


def test_capital_flow_range_and_last(research_session):
    rows = db_provider.fetch("capital_flow",
                             {"codes": "600000", "start": "2026-01-03", "end": "2026-01-03"})
    assert [(r["code"], r["trade_date"]) for r in rows] == [("600000", date(2026, 1, 3))]
    assert rows[0]["reasons"] == "日涨幅偏离值达7%；连续三个交易日"
    # last：每码最近 1 行（600000 取 01-03，600519 取 01-03）
    last = db_provider.fetch("capital_flow", {"last": 1})
    assert [r["trade_date"] for r in last] == [date(2026, 1, 3), date(2026, 1, 3)]


def test_capital_flow_detail_exact_date(research_session):
    rows = db_provider.fetch("capital_flow_detail",
                             {"codes": "600000", "date": "2026-01-03"})
    assert len(rows) == 2
    assert {r["reason"] for r in rows} == {"买入", "卖出"}
    # 卖出金额降序：机构(0) 在 某营业部(50w) 之后
    assert rows[0]["dealer_name"] == "某营业部"


def test_industry_index_asof_and_last(research_session):
    asof = db_provider.fetch("industry_index", {"codes": "801010", "asof": "2026-01-02"})
    assert [r["trade_date"] for r in asof] == [date(2026, 1, 2)]
    last = db_provider.fetch("industry_index", {"codes": "801010", "last": 1})
    assert [r["trade_date"] for r in last] == [date(2026, 1, 3)]
    # 不存量价：声明列无 volume/amount/turnover
    declared = set(registry.get_spec("industry_index").column_names)
    assert not ({"volume", "amount", "turnover"} & declared)


def test_industry_sw_info_levels_filter(research_session):
    rows = db_provider.fetch("industry_sw_info", {"levels": "3"})
    assert [r["code"] for r in rows] == ["801012"]
    assert rows[0]["level_label"] == "三级"
    all_levels = db_provider.fetch("industry_sw_info", {})
    assert [r["level"] for r in all_levels] == [1, 3]  # 升序


def test_industry_sw_cons_index_filter(research_session):
    rows = db_provider.fetch("industry_sw_cons", {"index_codes": "801012"})
    assert [r["stock_code"] for r in rows] == ["600519", "600000"]  # 市值降序


def test_macro_last_per_indicator(research_session):
    # 每 indicator 最近 1 行 → cn_cpi 取 08 月、cn_ppi 取 08 月
    rows = db_provider.fetch("macro", {"last": 1})
    by_ind = {r["indicator"]: r["period_label"] for r in rows}
    assert by_ind == {"cn_cpi": "2026年08月份", "cn_ppi": "2026年08月份"}
    # 区间过滤
    rows2 = db_provider.fetch("macro", {"indicator": "cn_cpi", "start": "2026-08-01"})
    assert [r["period_label"] for r in rows2] == ["2026年08月份"]


def test_news_by_stock_hides_hash_and_end_inclusive(research_session):
    rows = db_provider.fetch("news_by_stock", {"codes": "600519", "end": "2026-01-02"})
    # end 含当日全天 → 两条（含 20:00 那条）
    assert len(rows) == 2
    assert "content_hash" not in rows[0]
    last = db_provider.fetch("news_by_stock", {"codes": "600519", "last": 1})
    assert [r["published_at"] for r in last] == [datetime(2026, 1, 2, 20, 0)]


def test_announcement_by_stock_range(research_session):
    rows = db_provider.fetch("announcement_by_stock",
                             {"codes": "600519", "start": "2026-01-01", "end": "2026-01-03"})
    assert [r["publish_date"] for r in rows] == [date(2026, 1, 2)]


def test_new_datasets_have_source_chain_and_self_consistent():
    for dataset_id in _NEW_DATASETS:
        spec = registry.get_spec(dataset_id)
        assert spec is not None and spec.kind == "raw"
        assert registry.source_chain(dataset_id) == ("db",)
