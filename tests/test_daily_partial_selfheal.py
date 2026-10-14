"""daily_price 半日 partial bar 自愈 + 盘中护栏 单测。

根因：逐码增量 `start = max_d + 1` 且探针判 `max == today` → 任何已落库的坏行
（源未落定时写入的半日 partial bar，如 2026-09-23 全市场事故）永不重采、永久错。

本测覆盖两处修法：
  - `trailing_refresh_start`：补新日期之外回看尾窗 → 坏行次日被 upsert 覆盖。
  - `last_settled_trade_date`：未收盘不回写「今日」→ 从源头挡掉今日半日 bar。
"""
from datetime import date, datetime

from app.collectors.calendar import MARKET_CLOSE, last_settled_trade_date
from app.collectors.daily import trailing_refresh_start


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeDB:
    """按调用顺序吐出预置 scalar 值的假会话（last_settled_trade_date 只读 scalar）。"""

    def __init__(self, scalars):
        self._scalars = list(scalars)
        self.executed = []

    def execute(self, stmt):
        self.executed.append(stmt)
        return _FakeResult(self._scalars.pop(0))


# ---------- trailing_refresh_start ----------

END = date(2026, 10, 13)


def test_start_recent_max_d_replays_tail_window():
    """今日已有行（max_d == end，可能正是坏行）→ 起点回看尾窗，覆盖当日。"""
    assert trailing_refresh_start(END, END, refresh_days=5, fallback_days=30) == date(2026, 10, 8)


def test_start_within_refresh_replays_tail_window():
    """缺口 ≤ 尾窗 → 起点 = end - refresh（重采，覆盖窗内潜在坏行）。"""
    assert trailing_refresh_start(date(2026, 10, 11), END, 5, 30) == date(2026, 10, 8)


def test_start_far_gap_only_fills_gap():
    """缺口 > 尾窗 → 起点 = max_d + 1（只补缺口，不越界重拉历史）。"""
    assert trailing_refresh_start(date(2026, 9, 1), END, 5, 30) == date(2026, 9, 2)


def test_start_no_history_uses_fallback():
    assert trailing_refresh_start(None, END, 5, 30) == date(2026, 9, 13)


def test_start_refresh_zero_is_legacy():
    """refresh=0 → 退化为旧语义 max_d + 1（当日回滚闸门）。"""
    assert trailing_refresh_start(END, END, 0, 30) == date(2026, 10, 14)


# ---------- last_settled_trade_date ----------

def test_settled_today_after_close():
    """交易日且已过收盘 → 今日（正常 18:10 采集路径）。"""
    db = _FakeDB([True])
    now = datetime(2026, 9, 23, 18, 10)
    assert last_settled_trade_date(db, now=now) == date(2026, 9, 23)
    assert len(db.executed) == 1  # 命中即返回，不再查上一交易日


def test_settled_today_before_close_falls_back():
    """交易日但盘中（now < 15:00）→ 回退上一交易日，不写今日半日 bar。"""
    db = _FakeDB([True, date(2026, 9, 22)])
    now = datetime(2026, 9, 23, 11, 0)
    assert last_settled_trade_date(db, now=now) == date(2026, 9, 22)
    assert now.time() < MARKET_CLOSE


def test_settled_non_trading_day_falls_back():
    """非交易日（如周末）→ 回退上一交易日。"""
    db = _FakeDB([False, date(2026, 9, 18)])
    now = datetime(2026, 9, 19, 20, 0)
    assert last_settled_trade_date(db, now=now) == date(2026, 9, 18)


def test_settled_calendar_missing_falls_back_to_today():
    """日历无记录（未采/缺失）→ 回退 today，不阻断采集。"""
    db = _FakeDB([None, None])
    now = datetime(2026, 9, 23, 18, 10)
    assert last_settled_trade_date(db, now=now) == date(2026, 9, 23)
