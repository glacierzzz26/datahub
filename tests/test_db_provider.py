"""`db` provider 用例（假会话 + 内存 SQLite 真表）。

- 内存 SQLite 建 `trade_calendar` 真表 → 真跑过滤/排序（start/end/is_open）。
- 假会话 → 断言连接在取数后被 `close()`、未知数据集报错。
"""
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.tables import TradeCalendar
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
