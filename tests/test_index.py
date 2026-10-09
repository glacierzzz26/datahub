"""指数行情采集器测试（mock akshare）"""
from datetime import date

import pandas as pd

from app.collectors import index as idx_mod
from app.collectors.index import IndexCollector, build_rows
from tests.helpers import multi_values


def make_index_df():
    return pd.DataFrame({
        "date": ["2026-08-18", "2026-08-19"],
        "open": [4600.0, 4660.0],
        "high": [4650.0, 4674.0],
        "low": [4590.0, 4568.0],
        "close": [4640.0, 4588.0],
        "volume": [24407009000, 24407009001],
    })


def test_build_rows_and_date_filter():
    rows = build_rows("sh000300", make_index_df(),
                      start_date=date(2026, 8, 19))
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "sh000300"
    assert r["trade_date"] == date(2026, 8, 19)
    assert r["close"] == 4588.0
    assert r["adj_factor"] is None


class FakeSession:
    def __init__(self):
        self.executed = []

    def execute(self, stmt):
        self.executed.append(stmt)

    def commit(self):
        pass


def test_save_inserts_index_stock_and_prices():
    from sqlalchemy.dialects.postgresql import dialect as pg_dialect

    db = FakeSession()
    data = build_rows("sh000300", make_index_df())
    IndexCollector(db).save(data)
    # 2 次 execute：stock_basic 伪股票 + daily_price
    assert len(db.executed) == 2
    stock_stmt = db.executed[0]
    stock_row = multi_values(stock_stmt)[0]
    assert stock_row["code"] == "sh000300"
    assert stock_row["market"] == "INDEX"
    assert stock_row["name"] == "沪深300"
    sql = str(db.executed[1].compile(dialect=pg_dialect()))
    assert "ON CONFLICT (code, trade_date) DO UPDATE" in sql


# ---------- 阶段 2：BaoStock 主源 + 同日回退 ----------

def _bs_index_rows(max_date):
    return [{"code": "sh000300", "trade_date": max_date, "open": 4600.0,
             "high": 4650.0, "low": 4590.0, "close": 4640.0,
             "volume": 100, "amount": 1e9, "adj_factor": None}]


def test_akshare_primary_skips_baostock(monkeypatch):
    """阶段 4 主源锁定：AkShare 成功 → BaoStock index_rows 不被调用"""
    monkeypatch.setattr(idx_mod, "baostock_enabled", lambda *a, **k: True)
    bs_calls = []

    def fake_rows(*a, **k):
        bs_calls.append(a)
        raise AssertionError("不应走到 BaoStock")

    monkeypatch.setattr(idx_mod.baostock, "index_rows", fake_rows)
    monkeypatch.setattr(idx_mod.ak, "stock_zh_index_daily", lambda symbol: make_index_df())
    rows = IndexCollector(None).fetch("sh000300")
    assert len(rows) == 2
    assert bs_calls == []  # BaoStock 未被触碰


def test_fetch_baostock_branch(monkeypatch):
    """阶段 4 AkShare 主源失败 → BaoStock 兜底：当日已出 → 返回 BaoStock 指数行"""
    today = date.today()
    rows = _bs_index_rows(today)
    monkeypatch.setattr(idx_mod, "baostock_enabled", lambda *a, **k: True)
    monkeypatch.setattr(idx_mod.baostock, "get_session", lambda: object())

    def boom(*a, **k):
        raise RuntimeError("AkShare 指数接口失败")

    monkeypatch.setattr(idx_mod.ak, "stock_zh_index_daily", boom)
    monkeypatch.setattr(idx_mod.baostock, "index_rows",
                        lambda sess, symbol, s, e: rows)
    got = IndexCollector(None).fetch("sh000300")
    assert got == rows


def test_akshare_fail_baostock_stale_raises(monkeypatch):
    """阶段 4 BaoStock 兜底当日未出（max < end）→ 抛异常触发重试（不再降级 AkShare）"""
    from datetime import timedelta

    import pytest

    today = date.today()
    monkeypatch.setattr(idx_mod, "baostock_enabled", lambda *a, **k: True)
    monkeypatch.setattr(idx_mod.baostock, "get_session", lambda: object())
    monkeypatch.setattr(idx_mod.baostock, "index_rows",
                        lambda sess, symbol, s, e: _bs_index_rows(today - timedelta(days=1)))

    def boom(*a, **k):
        raise RuntimeError("AkShare 指数接口失败")

    monkeypatch.setattr(idx_mod.ak, "stock_zh_index_daily", boom)
    with pytest.raises(RuntimeError):
        IndexCollector(None).fetch("sh000300")


def test_chain_all_fail_raises(monkeypatch):
    """链外无 BaoStock（baostock_enabled=False）且 AkShare 失败 → 抛异常触发重试"""
    import pytest

    monkeypatch.setattr(idx_mod, "baostock_enabled", lambda *a, **k: False)

    def boom(*a, **k):
        raise RuntimeError("AkShare 指数接口失败")

    monkeypatch.setattr(idx_mod.ak, "stock_zh_index_daily", boom)
    with pytest.raises(RuntimeError):
        IndexCollector(None).fetch("sh000300")


def _upsert_set_cols(stmt) -> set[str]:
    """从 upsert 语句提取 ON CONFLICT SET 的列名集合（无 WHERE 时到行尾）"""
    import re

    from sqlalchemy.dialects.postgresql import dialect as pg_dialect
    sql = str(stmt.compile(dialect=pg_dialect()))
    m = re.search(r"DO UPDATE SET (.*)$", sql, re.S)
    set_clause = m.group(1) if m else ""
    return {part.split("=")[0].strip() for part in set_clause.split(",") if "=" in part}


def test_save_akshare_path_preserves_amount():
    """AkShare 路径（build_rows amount=None）：update 不含 amount，保留既有成交额"""

    db = FakeSession()
    data = build_rows("sh000300", make_index_df())
    assert all(r["amount"] is None for r in data)  # 前提：AkShare 无 amount
    IndexCollector(db).save(data)
    cols = _upsert_set_cols(db.executed[1])
    assert "amount" not in cols
    assert {"open", "high", "low", "close", "volume", "adj_factor"} <= cols


def test_save_baostock_path_writes_amount():
    """BaoStock 路径（行含 amount）：update 含 amount，正常写成交额"""
    db = FakeSession()
    data = build_rows("sh000300", make_index_df())
    for r in data:
        r["amount"] = 123456789.0
    IndexCollector(db).save(data)
    assert "amount" in _upsert_set_cols(db.executed[1])
