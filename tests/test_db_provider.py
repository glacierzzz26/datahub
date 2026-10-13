"""`db` provider 用例（假会话 + 内存 SQLite 真表）。

- 内存 SQLite 建 `trade_calendar` 真表 → 真跑过滤/排序（start/end/is_open）。
- 假会话 → 断言连接在取数后被 `close()`、未知数据集报错。
"""
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.tables import StockBasic, TradeCalendar
from app.providers import db as db_provider


@pytest.fixture
def cal_session(monkeypatch):
    """内存 SQLite（StaticPool 共享同一库）+ 把 get_session 指向它。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    TradeCalendar.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    s.add_all([
        TradeCalendar(cal_date=date(2026, 1, 1), is_open=False, exchange="SSE"),
        TradeCalendar(cal_date=date(2026, 1, 2), is_open=True, exchange="SSE"),
        TradeCalendar(cal_date=date(2026, 1, 3), is_open=False, exchange="SSE"),
        TradeCalendar(cal_date=date(2026, 1, 4), is_open=True, exchange="SSE"),
        TradeCalendar(cal_date=date(2026, 1, 5), is_open=True, exchange="SSE"),
    ])
    s.commit()
    s.close()
    monkeypatch.setattr(db_provider.db, "get_session", lambda: factory())
    return factory


def test_returns_rows_aligned_to_columns(cal_session):
    rows = db_provider.fetch("trade_calendar", {"is_open": True})
    assert rows[0] == {"cal_date": date(2026, 1, 2), "is_open": True,
                       "exchange": "SSE"}
    assert [r["cal_date"] for r in rows] == [date(2026, 1, 2), date(2026, 1, 4),
                                             date(2026, 1, 5)]


def test_filters_by_range_and_is_open(cal_session):
    rows = db_provider.fetch("trade_calendar",
                             {"start": "2026-01-02", "end": "2026-01-04",
                              "is_open": False})
    assert [r["cal_date"] for r in rows] == [date(2026, 1, 3)]


def test_no_is_open_returns_all(cal_session):
    rows = db_provider.fetch("trade_calendar", {})
    assert len(rows) == 5


def test_range_bounds_inclusive(cal_session):
    rows = db_provider.fetch("trade_calendar",
                             {"start": "2026-01-01", "end": "2026-01-02"})
    assert [r["cal_date"] for r in rows] == [date(2026, 1, 1), date(2026, 1, 2)]


def test_unknown_dataset_raises():
    with pytest.raises(ValueError):
        db_provider.fetch("nope", {})


def test_session_closed_after_fetch(monkeypatch):
    class _Q:
        def filter(self, *a, **k):
            return self

        def order_by(self, *a):
            return self

        def all(self):
            return []

    class _S:
        def __init__(self):
            self.closed = False

        def query(self, model):
            return _Q()

        def close(self):
            self.closed = True

    session = _S()
    monkeypatch.setattr(db_provider.db, "get_session", lambda: session)
    db_provider.fetch("trade_calendar", {})
    assert session.closed is True


# ---------- stock_basic ----------

@pytest.fixture
def stock_session(monkeypatch):
    """内存 SQLite 建 stock_basic 真表，真跑过滤/排序/分页。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    StockBasic.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    s.add_all([
        StockBasic(code="000001", name="平安银行", market="SZ", industry="银行",
                   list_date=date(1991, 4, 3), status="L", universe="hs300",
                   data_scope="a_share"),
        StockBasic(code="600000", name="浦发银行", market="SH", industry="银行",
                   list_date=date(1999, 11, 10), status="L", universe="zz500",
                   data_scope="a_share"),
        StockBasic(code="600519", name="贵州茅台", market="SH", industry="白酒",
                   list_date=date(2001, 8, 27), status="L", universe="hs300",
                   data_scope="a_share"),
        StockBasic(code="920000", name="退市示例", market="BJ", industry=None,
                   list_date=None, status="D", universe=None, data_scope=None),
    ])
    s.commit()
    s.close()
    monkeypatch.setattr(db_provider.db, "get_session", lambda: factory())
    return factory


def test_stock_basic_columns_and_order(stock_session):
    rows = db_provider.fetch("stock_basic", {})
    # 默认 code asc，返回全列
    assert [r["code"] for r in rows] == ["000001", "600000", "600519", "920000"]
    assert set(rows[0]) == {"code", "name", "market", "industry", "list_date",
                            "status", "universe", "data_scope"}


def test_stock_basic_filter_by_codes(stock_session):
    rows = db_provider.fetch("stock_basic", {"codes": "000001,600519"})
    assert [r["code"] for r in rows] == ["000001", "600519"]


def test_stock_basic_filter_by_universe_list(stock_session):
    # universe 逗号 = IN（qe pool_codes 用 hs300,zz500）
    rows = db_provider.fetch("stock_basic", {"universe": "hs300,zz500"})
    assert [r["code"] for r in rows] == ["000001", "600000", "600519"]


def test_stock_basic_filter_by_scope_market_industry(stock_session):
    assert [r["code"] for r in db_provider.fetch("stock_basic", {"scope": "a_share"})] \
        == ["000001", "600000", "600519"]
    assert [r["code"] for r in db_provider.fetch("stock_basic", {"market": "SH"})] \
        == ["600000", "600519"]
    assert [r["code"] for r in db_provider.fetch("stock_basic", {"industry": "银行"})] \
        == ["000001", "600000"]


def test_stock_basic_keyword_case_insensitive(stock_session):
    assert [r["code"] for r in db_provider.fetch("stock_basic", {"keyword": "600519"})] \
        == ["600519"]
    # 名称模糊（英文不敏感；SQLite/SA 走 lower LIKE）
    assert [r["code"] for r in db_provider.fetch("stock_basic", {"keyword": "茅台"})] \
        == ["600519"]


def test_stock_basic_sort_desc_nulls_last(stock_session):
    rows = db_provider.fetch("stock_basic", {"sort": "list_date", "order": "desc"})
    # 9000-... 无 list_date 的排在最后（NULLS LAST），而非最前
    assert [r["code"] for r in rows] == ["600519", "600000", "000001", "920000"]


def test_stock_basic_pagination(stock_session):
    rows = db_provider.fetch("stock_basic", {"sort": "code", "order": "asc",
                                             "limit": 2, "offset": 1})
    assert [r["code"] for r in rows] == ["600000", "600519"]


def test_stock_basic_unknown_sort_falls_back_to_code(stock_session):
    rows = db_provider.fetch("stock_basic", {"sort": "drop_table"})
    assert [r["code"] for r in rows] == ["000001", "600000", "600519", "920000"]


# ---------- daily_price / daily_valuation / financial_indicator（Phase 3） ----------

from app.models.tables import (  # noqa: E402
    DailyPrice,
    DailyValuation,
    FinancialIndicator,
    FinancialIndicatorExt,
)


@pytest.fixture
def price_session(monkeypatch):
    """内存 SQLite 建价格/财务真表 + 财务侧表，真跑过滤/窗口/投影。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    for m in (StockBasic, DailyPrice, DailyValuation, FinancialIndicator,
              FinancialIndicatorExt):
        m.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    s.add_all([
        StockBasic(code="600000", name="浦发", market="SH", universe="hs300",
                   data_scope="a_share"),
        StockBasic(code="600519", name="茅台", market="SH", universe="hs300",
                   data_scope="a_share"),
        # 行情：600000 三日 + 指数 sh000300 两日；600000 首日 adj_factor 为空
        DailyPrice(id=1, code="600000", trade_date=date(2026, 1, 2), close=10.0,
                   adj_factor=None),
        DailyPrice(id=2, code="600000", trade_date=date(2026, 1, 3), close=10.5,
                   adj_factor=1.2),
        DailyPrice(id=3, code="600000", trade_date=date(2026, 1, 5), close=11.0,
                   adj_factor=1.2),
        DailyPrice(id=4, code="sh000300", trade_date=date(2026, 1, 2), close=4000.0),
        DailyPrice(id=5, code="sh000300", trade_date=date(2026, 1, 3), close=4010.0),
        # 估值：600000 两日
        DailyValuation(id=1, code="600000", trade_date=date(2026, 1, 2), close=10.0,
                       pe_ttm=8.0, pb=1.1),
        DailyValuation(id=2, code="600000", trade_date=date(2026, 1, 3), close=10.5,
                       pe_ttm=8.2, pb=1.2),
        # 财务：600000 两期（含公告日）
        FinancialIndicator(id=1, code="600000", report_date=date(2025, 9, 30),
                           announce_date=date(2025, 10, 20), roe=9.0),
        FinancialIndicator(id=2, code="600000", report_date=date(2025, 12, 31),
                           announce_date=date(2026, 1, 25), roe=9.5),
        # 财务侧表：仅 2025-12-31 期有 net_margin；2025-09-30 期无（验证 left-join NULL）
        FinancialIndicatorExt(id=1, code="600000", report_date=date(2025, 12, 31),
                              net_margin=12.34),
    ])
    s.commit()
    s.close()
    monkeypatch.setattr(db_provider.db, "get_session", lambda: factory())
    return factory


def test_daily_price_range_asc_and_full_columns(price_session):
    rows = db_provider.fetch("daily_price",
                             {"codes": "600000", "start": "2026-01-02",
                              "end": "2026-01-05"})
    assert [(r["code"], r["trade_date"]) for r in rows] == [
        ("600000", date(2026, 1, 2)), ("600000", date(2026, 1, 3)),
        ("600000", date(2026, 1, 5))]
    assert set(rows[0]) == set(db_provider._PRICE_COLS)


def test_daily_price_exact_date(price_session):
    rows = db_provider.fetch("daily_price", {"date": "2026-01-03"})
    assert sorted(r["code"] for r in rows) == ["600000", "sh000300"]


def test_daily_price_last_window_per_code(price_session):
    # 每 code 最近 2 行：600000 → 01-03,01-05；sh000300 → 01-02,01-03
    rows = db_provider.fetch("daily_price", {"last": 2})
    by = {}
    for r in rows:
        by.setdefault(r["code"], []).append(r["trade_date"])
    assert by["600000"] == [date(2026, 1, 3), date(2026, 1, 5)]
    assert by["sh000300"] == [date(2026, 1, 2), date(2026, 1, 3)]


def test_daily_price_asof_and_fields_projection(price_session):
    rows = db_provider.fetch("daily_price",
                             {"codes": "600000", "asof": "2026-01-03",
                              "fields": "trade_date,close"})
    assert rows == [{"code": "600000", "trade_date": date(2026, 1, 2), "close": 10.0},
                    {"code": "600000", "trade_date": date(2026, 1, 3), "close": 10.5}] \
        or rows == [{"trade_date": date(2026, 1, 2), "close": 10.0},
                    {"trade_date": date(2026, 1, 3), "close": 10.5}]


def test_daily_price_adj_not_null(price_session):
    rows = db_provider.fetch("daily_price",
                             {"codes": "600000", "adj_not_null": True,
                              "last": 1})
    assert len(rows) == 1 and rows[0]["trade_date"] == date(2026, 1, 5)
    assert float(rows[0]["adj_factor"]) == 1.2


def test_daily_price_empty_is_legal(price_session):
    assert db_provider.fetch("daily_price", {"codes": "999999"}) == []


def test_daily_valuation_last_one(price_session):
    rows = db_provider.fetch("daily_valuation", {"codes": "600000", "last": 1})
    assert len(rows) == 1
    assert rows[0]["trade_date"] == date(2026, 1, 3) and float(rows[0]["pe_ttm"]) == 8.2


def test_numeric_columns_serialize_as_json_numbers(price_session):
    """契约：`ColumnSpec.type == "float"` 的列，wire 必须是 JSON **数字**。

    回归守卫：ORM 把 `NUMERIC` 映射为 `Decimal`，pydantic v2 又把 `Decimal`
    序列化成 JSON **字符串** —— 与声明的 `float` 不符，且让 Go 侧 `float64`
    DTO 反序列化失败（`json: cannot unmarshal string into float64`）。生产
    `daily_valuation` 读切换（首个数值数据集）暴露该缺陷。
    """
    import json
    from decimal import Decimal

    val = db_provider.fetch("daily_valuation", {"codes": "600000", "last": 1})[0]
    price = db_provider.fetch("daily_price", {"codes": "600000", "last": 1})[0]

    for row, cols in ((val, ("close", "pe_ttm", "pb")),
                      (price, ("close", "adj_factor"))):
        for col in cols:
            v = row[col]
            assert v is not None
            assert isinstance(v, float) and not isinstance(v, (Decimal, str, bool)), \
                (col, type(v))

    # 真正走一遍 JSON：数值列不得被引号包成字符串（date 列不可 json 序列化，故单取数值）
    s = json.dumps({k: val[k] for k in ("close", "pe_ttm", "pb")})
    assert '"pe_ttm":"' not in s and '"close":"' not in s
    assert json.loads(s)["pe_ttm"] == val["pe_ttm"]


def test_daily_valuation_asof(price_session):
    rows = db_provider.fetch("daily_valuation",
                             {"codes": "600000", "asof": "2026-01-02"})
    assert [r["trade_date"] for r in rows] == [date(2026, 1, 2)]


def test_financial_as_of_guards_future(price_session):
    # as_of=2025-12-31 → 只暴露 2025-10-20 公告的那期（01-25 公告的不给）
    rows = db_provider.fetch("financial_indicator",
                             {"codes": "600000", "as_of": "2025-12-31"})
    assert [r["report_date"] for r in rows] == [date(2025, 9, 30)]


def test_financial_last_window_announce_desc(price_session):
    rows = db_provider.fetch("financial_indicator", {"codes": "600000", "last": 1})
    assert len(rows) == 1 and rows[0]["report_date"] == date(2025, 12, 31)


def test_financial_net_margin_left_join(price_session):
    # 侧表 financial_indicator_ext 有 12-31 期 → net_margin 带出；09-30 期无 → NULL
    rows = db_provider.fetch("financial_indicator", {"codes": "600000"})
    by_report = {r["report_date"]: r["net_margin"] for r in rows}
    assert by_report[date(2025, 12, 31)] == 12.34
    assert by_report[date(2025, 9, 30)] is None
    # 投影含 net_margin，且数值是 float（非 Decimal→字符串）
    assert "net_margin" in rows[0]
    proj = db_provider.fetch("financial_indicator",
                             {"codes": "600000", "fields": "net_margin,roe"})
    assert set(proj[0]) == {"net_margin", "roe"}


def test_latest_trade_date_excludes_index_by_default(price_session):
    # sh000300 有 01-03，但 600000 有 01-05 → 默认排除指数 → 01-05
    rows = db_provider.fetch("latest_trade_date", {})
    assert rows == [{"trade_date": date(2026, 1, 5)}]


def test_latest_trade_date_include_index(price_session):
    rows = db_provider.fetch("latest_trade_date", {"exclude_index": False})
    assert rows == [{"trade_date": date(2026, 1, 5)}]


def test_latest_trade_date_no_rows_is_empty(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    DailyPrice.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(db_provider.db, "get_session", lambda: factory())
    assert db_provider.fetch("latest_trade_date", {}) == []


def test_latest_trade_date_before_strict(price_session):
    # 600000 有 01-05；before=01-05（严格 <）→ 01-03（非 01-05）；before=01-03 → 01-02
    assert db_provider.fetch("latest_trade_date", {"before": "2026-01-05"}) == \
        [{"trade_date": date(2026, 1, 3)}]
    assert db_provider.fetch("latest_trade_date", {"before": "2026-01-03"}) == \
        [{"trade_date": date(2026, 1, 2)}]


def test_latest_trade_date_before_with_index(price_session):
    # 指数行更晚（sh000300 06-30）：exclude_index=false 纳入、true 排除（before 组合）
    s = price_session()
    s.add(DailyPrice(id=10, code="sh000300", trade_date=date(2026, 6, 30), close=4100.0))
    s.commit()
    s.close()
    assert db_provider.fetch("latest_trade_date",
                             {"before": "2026-12-31", "exclude_index": False}) == \
        [{"trade_date": date(2026, 6, 30)}]
    assert db_provider.fetch("latest_trade_date",
                             {"before": "2026-12-31", "exclude_index": True}) == \
        [{"trade_date": date(2026, 1, 5)}]


def test_daily_price_days_distinct_asc(price_session):
    # 600000 三日 + sh000300 两日；默认排指数、去重、升序
    rows = db_provider.fetch("daily_price_days",
                             {"start": "2026-01-01", "end": "2026-01-31"})
    assert [r["trade_date"] for r in rows] == \
        [date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 5)]


def test_daily_price_days_include_index_and_window(price_session):
    # 含指数 + 窗口 [01-03, 01-31] → 01-03,01-05（01-02 被 start 排除）
    rows = db_provider.fetch("daily_price_days",
                             {"start": "2026-01-03", "end": "2026-01-31",
                              "exclude_index": False})
    assert [r["trade_date"] for r in rows] == \
        [date(2026, 1, 3), date(2026, 1, 5)]


def test_daily_price_days_empty_is_legal(price_session):
    assert db_provider.fetch("daily_price_days",
                             {"start": "2030-01-01", "end": "2030-12-31"}) == []


def test_market_ready_ratio(price_session):
    # 池 2 只（600000,600519），当日仅 600000 有行情 → covered=1/total=2=0.5 < 0.9
    rows = db_provider.fetch("market_ready", {"date": "2026-01-03", "threshold": "0.9"})
    assert rows == [{"ready": False, "covered": 1, "total": 2}]
    # 阈值下调 → ready
    rows = db_provider.fetch("market_ready", {"date": "2026-01-03", "threshold": "0.5"})
    assert rows[0]["ready"] is True


def test_dup_keys_empty_when_unique(price_session):
    assert db_provider.fetch("dup_keys", {"table": "daily_price"}) == []


def test_dup_keys_detects_duplicate(price_session):
    s = price_session()
    s.add(DailyPrice(id=99, code="600000", trade_date=date(2026, 1, 5), close=11.0))
    s.commit()
    s.close()
    rows = db_provider.fetch("dup_keys", {"table": "daily_price"})
    assert rows == [{"key": "600000|2026-01-05", "n": 2}]


def test_dup_keys_window_filters_by_key_date(price_session):
    # 两处重复：01-02 与 01-05；窗口 [01-03, 01-31] 只应命中 01-05（01-02 被 start 排除）
    s = price_session()
    s.add_all([
        DailyPrice(id=99, code="600000", trade_date=date(2026, 1, 5), close=11.0),
        DailyPrice(id=100, code="600000", trade_date=date(2026, 1, 2), close=10.0),
    ])
    s.commit()
    s.close()
    assert {r["key"] for r in db_provider.fetch("dup_keys", {"table": "daily_price"})} == \
        {"600000|2026-01-02", "600000|2026-01-05"}
    assert db_provider.fetch("dup_keys", {"table": "daily_price",
                                          "start": "2026-01-03", "end": "2026-01-31"}) == \
        [{"key": "600000|2026-01-05", "n": 2}]
    # end 收窄同样生效：≤01-03 只见 01-02 的重复
    assert db_provider.fetch("dup_keys", {"table": "daily_price",
                                          "end": "2026-01-03"}) == \
        [{"key": "600000|2026-01-02", "n": 2}]
