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
