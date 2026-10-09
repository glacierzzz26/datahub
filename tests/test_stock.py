"""股票列表采集器测试（mock akshare）"""
import pandas as pd
import pytest

from app.collectors import stock as stock_mod
from app.collectors.stock import StockCollector, infer_market, normalize_stock_rows
from tests.helpers import multi_values, write_execs


@pytest.fixture(autouse=True)
def _no_bj_net(monkeypatch):
    """北交所接口默认不可用（离线测试不触网）→ fetch_bj_rows 走失败分支返回空；
    测 bj 合并/取数的用例各自覆盖 ak 或 fetch_bj_rows"""
    monkeypatch.setattr(
        stock_mod.ak, "stock_info_bj_name_code",
        lambda: (_ for _ in ()).throw(ConnectionError("offline")),
    )


def test_infer_market():
    assert infer_market("600519") == "SH"
    assert infer_market("688001") == "SH"
    assert infer_market("000001") == "SZ"
    assert infer_market("300001") == "SZ"
    assert infer_market("830001") == "BJ"
    assert infer_market("430001") == "BJ"


def test_normalize_stock_rows():
    df = pd.DataFrame(
        {"code": ["600519", "000001", "300750", "830001"],
         "name": ["贵州茅台", "平安银行", "宁德时代", "某退市股"]}
    )
    rows = normalize_stock_rows(df)
    assert rows[0] == {"code": "600519", "name": "贵州茅台",
                       "market": "SH", "status": "L"}
    # 退市标记
    assert rows[3]["status"] == "D"
    assert rows[3]["market"] == "BJ"


class FakeSession:
    """记录 execute 调用，验证入库语句（stmt 单参 = upsert，双参 = 原生 UPDATE）"""

    def __init__(self):
        self.executed = []

    def execute(self, stmt, params=None):
        self.executed.append(stmt)
        return None

    def commit(self):
        pass


def test_save_marks_universe(monkeypatch):
    """save：入库列表 → 标记 universe → 补全上市日期/行业"""
    called = []

    def fake_list():
        called.append("list")
        return pd.DataFrame({"code": ["600519", "000001"],
                             "name": ["贵州茅台", "平安银行"]})

    def fake_cons(symbol):
        called.append(symbol)
        return pd.DataFrame({"成分券代码": ["600519"]})

    monkeypatch.setattr(stock_mod.ak, "stock_info_a_code_name", fake_list)
    monkeypatch.setattr(stock_mod.ak, "index_stock_cons_csindex", fake_cons)
    monkeypatch.setattr(stock_mod, "fetch_list_dates",
                        lambda: {"600519": "2001-08-27"})
    monkeypatch.setattr(stock_mod, "fetch_industries",
                        lambda: {"600519": "白酒"})

    db = FakeSession()
    ok = StockCollector(db).run()
    assert ok
    # 5 次写入：upsert 列表 + upsert 股票池 + UPDATE 采集范围(data_scope)
    # + UPDATE 上市日期 + UPDATE 行业（Fetch 里的只读 select 被 write_execs 过滤）
    assert len(write_execs(db)) == 5
    assert called == ["list", "000300", "000905"]


def test_universe_marking_values(monkeypatch):
    monkeypatch.setattr(
        stock_mod.ak, "stock_info_a_code_name",
        lambda: pd.DataFrame({"code": ["600519", "000001"],
                              "name": ["a", "b"]}),
    )
    monkeypatch.setattr(
        stock_mod.ak, "index_stock_cons_csindex",
        lambda symbol: pd.DataFrame({"成分券代码": ["600519"] if symbol == "000300" else []}),
    )
    monkeypatch.setattr(stock_mod, "fetch_list_dates", lambda: {})
    monkeypatch.setattr(stock_mod, "fetch_industries", lambda: {})
    db = FakeSession()
    StockCollector(db).run()
    # 第二个写入（upsert 股票池）应含 universe 标记
    values = multi_values(write_execs(db)[1])
    by_code = {r["code"]: r for r in values}
    assert by_code["600519"]["universe"] == "hs300"
    assert by_code["000001"]["universe"] is None


def test_fetch_list(monkeypatch):
    monkeypatch.setattr(
        stock_mod.ak, "stock_info_a_code_name",
        lambda: pd.DataFrame({"code": ["600519"], "name": ["贵州茅台"]}),
    )
    rows = StockCollector(None).fetch()
    assert rows[0]["code"] == "600519"
    assert rows[0]["market"] == "SH"


def test_fetch_list_empty(monkeypatch):
    monkeypatch.setattr(
        stock_mod.ak, "stock_info_a_code_name",
        lambda: pd.DataFrame({"code": [], "name": []}),
    )
    assert StockCollector(None).fetch() == []


def test_fetch_baostock_merges_bj(monkeypatch):
    """阶段 3：BaoStock 主源（SH/SZ）+ AkShare bj 合并（BaoStock 无北交所）"""
    from datetime import date

    rows_in = [{"code": "600519", "name": "贵州茅台", "market": "SH",
                "status": "L", "list_date": date(2001, 8, 27)}]
    monkeypatch.setattr(stock_mod, "baostock_enabled", lambda *a, **k: True)
    monkeypatch.setattr(stock_mod.baostock, "get_session", lambda: object())
    monkeypatch.setattr(stock_mod.baostock, "stock_basic_rows",
                        lambda sess: list(rows_in))  # 副本：fetch 内 rows += bj 会原地变异
    monkeypatch.setattr(stock_mod, "fetch_bj_rows",
                        lambda: [{"code": "830001", "name": "某北交所股",
                                  "market": "BJ", "status": "L"}])
    rows = StockCollector(None).fetch()
    assert rows == rows_in + [{"code": "830001", "name": "某北交所股",
                               "market": "BJ", "status": "L"}]


def test_fetch_baostock_branch(monkeypatch):
    """BAOSTOCK_SOURCES 含 stock_basic → BaoStock 主源返回全量列表（无 industry）"""
    from datetime import date

    rows_in = [{"code": "600519", "name": "贵州茅台", "market": "SH",
                "status": "L", "list_date": date(2001, 8, 27)}]
    monkeypatch.setattr(stock_mod, "baostock_enabled", lambda *a, **k: True)
    monkeypatch.setattr(stock_mod.baostock, "get_session", lambda: object())
    monkeypatch.setattr(stock_mod.baostock, "stock_basic_rows", lambda sess: rows_in)
    rows = StockCollector(None).fetch()
    assert rows == rows_in


def test_fetch_bj_rows(monkeypatch):
    """fetch_bj_rows：北交所列名 → 入库行（market=BJ，退市标记）"""
    bj_df = pd.DataFrame({
        "证券代码": ["830001", "920002"],
        "证券简称": ["某北交所股", "某退市股"],
    })
    monkeypatch.setattr(stock_mod.ak, "stock_info_bj_name_code", lambda: bj_df)
    rows = stock_mod.fetch_bj_rows()
    assert rows == [
        {"code": "830001", "name": "某北交所股", "market": "BJ", "status": "L"},
        {"code": "920002", "name": "某退市股", "market": "BJ", "status": "D"},
    ]


def test_fetch_bj_rows_failure(monkeypatch):
    """bj 接口失败 → 返回空（SH/SZ 主源不受影响，仅告警）"""
    monkeypatch.setattr(stock_mod.ak, "stock_info_bj_name_code",
                        lambda: (_ for _ in ()).throw(ConnectionError("bse down")))
    assert stock_mod.fetch_bj_rows() == []


def test_pool_without_cons_api_failure(monkeypatch):
    """成分股接口失败时 run 应返回 False（基类重试后）"""
    monkeypatch.setattr(
        stock_mod.ak, "stock_info_a_code_name",
        lambda: pd.DataFrame({"code": ["600519"], "name": ["贵州茅台"]}),
    )
    monkeypatch.setattr(
        stock_mod.ak, "index_stock_cons_csindex",
        lambda symbol: (_ for _ in ()).throw(ConnectionError("boom")),
    )
    ok = StockCollector(FakeSession()).run()
    assert not ok
