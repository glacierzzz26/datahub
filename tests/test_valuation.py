"""估值采集器测试（mock akshare）"""
from datetime import date

import pandas as pd

from app.collectors import valuation as val_mod
from app.collectors.valuation import ValuationCollector, to_rows
from tests.helpers import multi_values, write_execs


def make_valuation_df():
    return pd.DataFrame(
        {
            "数据日期": ["2026-08-19", "2026-08-18", "2026-08-17"],
            "当日收盘价": [1307.88, 1297.99, None],
            "总市值": [1.63e12, 1.62e12, 1.61e12],
            "流通市值": [1.63e12, 1.62e12, 1.61e12],
            "PE(TTM)": [20.077, 19.925, None],
            "PE(静)": [19.86, 19.71, 19.70],
            "市净率": [6.507, 6.458, 6.45],
        }
    )


def test_to_rows_conversion():
    rows = to_rows("600519", make_valuation_df())
    assert len(rows) == 3
    assert rows[0] == {
        "code": "600519",
        "trade_date": date(2026, 8, 19),
        "close": 1307.88,
        "total_mv": 1.63e12,
        "float_mv": 1.63e12,
        "pe_ttm": 20.077,
        "pe_static": 19.86,
        "pb": 6.507,
    }
    # 缺失值 → None（入库为 NULL，正负判断留给因子侧）
    assert rows[2]["close"] is None
    assert rows[2]["pe_ttm"] is None


class FakeSession:
    """记录 execute 调用（单参 = upsert）"""

    def __init__(self):
        self.executed = []

    def execute(self, stmt, params=None):
        self.executed.append(stmt)
        return None

    def commit(self):
        pass


def test_run_upserts_valuation_rows(monkeypatch):
    monkeypatch.setattr(val_mod.ak, "stock_value_em",
                        lambda symbol: make_valuation_df())
    db = FakeSession()
    ok = ValuationCollector(db).run("600519")
    assert ok
    writes = write_execs(db)  # 过滤掉 fetch 里的只读 select
    assert len(writes) == 1
    values = multi_values(writes[0])
    assert values[0]["code"] == "600519"
    assert values[0]["trade_date"] == date(2026, 8, 19)
    assert len(values) == 3


# ---------- 阶段 2：BaoStock 主源 + 同日回退 + 保留既有列 ----------

def _bs_valuation_rows(max_date):
    return [{"code": "600519", "trade_date": max_date,
             "close": 1302.8, "pe_ttm": 25.3, "pb": 8.1}]


def test_akshare_primary_skips_baostock(monkeypatch):
    """阶段 4 主源锁定：AkShare 成功 → BaoStock valuation_rows 不被调用"""
    monkeypatch.setattr(val_mod, "baostock_enabled", lambda *a, **k: True)
    bs_calls = []

    def fake_rows(*a, **k):
        bs_calls.append(a)
        raise AssertionError("不应走到 BaoStock")

    monkeypatch.setattr(val_mod.baostock, "valuation_rows", fake_rows)
    monkeypatch.setattr(val_mod.ak, "stock_value_em", lambda symbol: make_valuation_df())
    rows = ValuationCollector(None).fetch("600519")
    assert len(rows) == 3
    assert bs_calls == []  # BaoStock 未被触碰


def test_fetch_baostock_branch(monkeypatch):
    """阶段 4 AkShare 主源失败 → BaoStock 兜底：当日已出 → 返回 BaoStock 行（无 mv 列）"""
    today = date.today()
    rows = _bs_valuation_rows(today)
    monkeypatch.setattr(val_mod, "baostock_enabled", lambda *a, **k: True)
    monkeypatch.setattr(val_mod.baostock, "get_session", lambda: object())

    def boom(*a, **k):
        raise RuntimeError("AkShare 估值接口失败")

    monkeypatch.setattr(val_mod.ak, "stock_value_em", boom)
    monkeypatch.setattr(val_mod.baostock, "valuation_rows",
                        lambda sess, code, s, e: rows)
    got = ValuationCollector(None).fetch("600519")
    assert got == rows
    assert "total_mv" not in got[0]


def test_akshare_fail_baostock_stale_raises(monkeypatch):
    """阶段 4 BaoStock 兜底当日未出（max < today）→ 抛异常触发重试（不再降级 AkShare）"""
    from datetime import timedelta

    import pytest

    today = date.today()
    monkeypatch.setattr(val_mod, "baostock_enabled", lambda *a, **k: True)
    monkeypatch.setattr(val_mod.baostock, "get_session", lambda: object())
    monkeypatch.setattr(val_mod.baostock, "valuation_rows",
                        lambda sess, code, s, e: _bs_valuation_rows(today - timedelta(days=1)))

    def boom(*a, **k):
        raise RuntimeError("AkShare 估值接口失败")

    monkeypatch.setattr(val_mod.ak, "stock_value_em", boom)
    with pytest.raises(RuntimeError):
        ValuationCollector(None).fetch("600519")


def test_chain_all_fail_raises(monkeypatch):
    """链外无 BaoStock（baostock_enabled=False）且 AkShare 失败 → 抛异常触发重试"""
    import pytest

    monkeypatch.setattr(val_mod, "baostock_enabled", lambda *a, **k: False)

    def boom(*a, **k):
        raise RuntimeError("AkShare 估值接口失败")

    monkeypatch.setattr(val_mod.ak, "stock_value_em", boom)
    with pytest.raises(RuntimeError):
        ValuationCollector(None).fetch("600519")


def test_save_baostock_rows_only_three_cols():
    """BaoStock 行无 total_mv → update_cols 只含 close/pe_ttm/pb（不覆盖既有市值）"""
    from sqlalchemy.dialects.postgresql import dialect as pg_dialect

    db = FakeSession()
    data = [{"code": "600519", "trade_date": date(2026, 8, 19),
             "close": 1302.8, "pe_ttm": 25.3, "pb": 8.1}]
    ValuationCollector(db).save(data)
    sql = str(db.executed[-1].compile(dialect=pg_dialect()))
    assert "ON CONFLICT (code, trade_date) DO UPDATE" in sql
    assert "total_mv" not in sql
    assert "pe_static" not in sql
    assert "pe_ttm" in sql and "pb" in sql and "close" in sql


def test_save_full_cols_when_mv_present():
    """AkShare 行含 total_mv → 沿用原 6 列更新"""
    from sqlalchemy.dialects.postgresql import dialect as pg_dialect

    db = FakeSession()
    data = [{"code": "600519", "trade_date": date(2026, 8, 19), "close": 1302.8,
             "total_mv": 1.63e12, "float_mv": 1.63e12, "pe_ttm": 25.3,
             "pe_static": 24.0, "pb": 8.1}]
    ValuationCollector(db).save(data)
    sql = str(db.executed[-1].compile(dialect=pg_dialect()))
    assert "total_mv" in sql and "float_mv" in sql and "pe_static" in sql
