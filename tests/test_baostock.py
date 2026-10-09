"""BaoStock 适配层测试（mock 协议函数，无网络）

覆盖：代码转 bs 格式 / ResultData 翻页转 DataFrame / daily_pairs 的单位与列对齐 /
后复权缺失报错 / 交易日历过滤 / 会话重连重试（连接级错误码）/ 单例 / bs 缺失退化。
"""
from datetime import date

import baostock as baostock_mod
import pandas as pd
import pytest

from app.sources import baostock

# ---------- 工具 ----------

def _rs(fields, rows, error_code="0", error_msg=""):
    """baostock ResultData 桩：fields + 翻页"""
    class _RS:
        def __init__(self):
            self.fields = fields
            self._rows = list(rows)
            self.error_code = error_code
            self.error_msg = error_msg
            self._i = -1

        def next(self):
            if self._i + 1 < len(self._rows):
                self._i += 1
                return True
            return False

        def get_row_data(self):
            return list(self._rows[self._i])

    return _RS()


class FakeBS:
    """baostock 协议函数桩：登录/登出 + 可编程的日线/交易日历/列表/财务查询"""

    def __init__(self, login_code="0"):
        self.login_code = login_code
        self.login_calls = 0
        self.logout_calls = 0
        self.queries = []          # 记录 (qfunc, args, kwargs)
        self.hist_responses = {}   # adjustflag -> list of _rs 或 callable
        self.trade_cal_resp = None
        self.stock_basic_resp = None
        self.profit_resp = None    # 或 callable(year, quarter)
        self.growth_resp = None
        self.balance_resp = None
        self.last_adjustflag = None

    def login(self):
        self.login_calls += 1
        return _rs([], [], error_code=self.login_code)

    def logout(self):
        self.logout_calls += 1

    def query_history_k_data_plus(self, code, fields, **kw):
        self.queries.append((code, fields, kw))
        af = kw.get("adjustflag")
        self.last_adjustflag = af
        resp = self.hist_responses.get(af)
        if callable(resp):
            return resp(kw)
        return resp if resp is not None else _rs([], [])

    def query_trade_dates(self, **kw):
        self.queries.append(("trade_dates", None, kw))
        return self.trade_cal_resp if self.trade_cal_resp is not None else _rs([], [])

    def query_stock_basic(self):
        self.queries.append(("stock_basic", None, {}))
        return self.stock_basic_resp if self.stock_basic_resp is not None else _rs([], [])

    def query_profit_data(self, code, year, quarter):
        self.queries.append(("profit", (code, year, quarter), {}))
        if callable(self.profit_resp):
            return self.profit_resp(year, quarter)
        return self.profit_resp if self.profit_resp is not None else _rs([], [])

    def query_growth_data(self, code, year, quarter):
        self.queries.append(("growth", (code, year, quarter), {}))
        return self.growth_resp if self.growth_resp is not None else _rs([], [])

    def query_balance_data(self, code, year, quarter):
        self.queries.append(("balance", (code, year, quarter), {}))
        return self.balance_resp if self.balance_resp is not None else _rs([], [])


@pytest.fixture(autouse=True)
def fake_bs(monkeypatch):
    """每测试隔离：替换 baostock 模块的协议函数，并重置单例"""
    fbs = FakeBS()
    monkeypatch.setattr(baostock_mod, "login", fbs.login)
    monkeypatch.setattr(baostock_mod, "logout", fbs.logout)
    monkeypatch.setattr(baostock_mod, "query_history_k_data_plus", fbs.query_history_k_data_plus)
    monkeypatch.setattr(baostock_mod, "query_trade_dates", fbs.query_trade_dates)
    monkeypatch.setattr(baostock_mod, "query_stock_basic", fbs.query_stock_basic)
    monkeypatch.setattr(baostock_mod, "query_profit_data", fbs.query_profit_data)
    monkeypatch.setattr(baostock_mod, "query_growth_data", fbs.query_growth_data)
    monkeypatch.setattr(baostock_mod, "query_balance_data", fbs.query_balance_data)
    baostock._reset_session()
    yield fbs
    baostock._reset_session()


# ---------- 代码 / 日期工具 ----------

def test_bs_code():
    assert baostock.bs_code("600519") == "sh.600519"
    assert baostock.bs_code("000001") == "sz.000001"
    assert baostock.bs_code("300750") == "sz.300750"
    assert baostock.bs_code("830001") == "bj.830001"
    assert baostock.bs_code("430047") == "bj.430047"
    assert baostock.bs_code("920001") == "bj.920001"


def test_is_source_blocked():
    """自愈 stage1 分流：源被限（封禁/黑名单冷却）vs 瞬时错误"""
    # 源被限：黑名单冷却 / 登录封禁 10001011
    assert baostock.is_source_blocked(
        RuntimeError("BaoStock 封禁冷却中（900s 后重试）"))
    assert baostock.is_source_blocked(
        RuntimeError("BaoStock 登录失败(10001011:too many request)"))
    # 瞬时错误：网络超时（10002003/10002006/10002008）不视为源被限
    assert not baostock.is_source_blocked(
        RuntimeError("BaoStock query 失败(10002003:网络连接超时)"))
    assert not baostock.is_source_blocked(
        RuntimeError("BaoStock 登录失败(10001001:未登录)"))
    assert not baostock.is_source_blocked(TimeoutError("connect timed out"))
    assert not baostock.is_source_blocked(RuntimeError("其他错误"))


def test_ymd():
    assert baostock._ymd(date(2026, 8, 26)) == "2026-08-26"
    assert baostock._ymd("2026/08/26") == "2026-08-26"


# ---------- ResultData → DataFrame ----------

def test_rs_to_df_paginates():
    rs = _rs(["date", "close"], [["2026-08-26", "10.5"], ["2026-08-25", "10.0"]])
    df = baostock._rs_to_df(rs)
    assert list(df.columns) == ["date", "close"]
    assert df.iloc[0]["close"] == "10.5"          # 原样字符串（build_rows 负责 float）


def test_rs_to_df_empty():
    assert baostock._rs_to_df(None).empty
    assert baostock._rs_to_df(_rs([], [])).empty


# ---------- daily_pairs：单位/列对齐/后复权缺失 ----------

def test_daily_pairs_units_and_columns(fake_bs):
    fake_bs.hist_responses = {
        "3": _rs(["date", "open", "high", "low", "close", "volume", "amount"],
                 [["2026-08-26", "10.0", "10.5", "9.9", "10.2", "1000000", "10200000"]]),
        "1": _rs(["date", "close"], [["2026-08-26", "68.9"]]),
    }
    sess = baostock.get_session()
    raw, hfq = baostock.daily_pairs(sess, "600519", "2026-08-26", "2026-08-26")
    assert list(raw.columns) == ["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]
    # 成交量 股 → 手（÷100），成交额 元 原样
    assert raw.iloc[0]["成交量"] == 10000.0
    assert float(raw.iloc[0]["成交额"]) == 10200000.0  # 成交额留字符串，build_rows 负责 float
    assert hfq.iloc[0]["收盘"] == "68.9"
    # 请求用 bs 格式代码 + 后复权 adjustflag=1
    assert fake_bs.queries[0][0] == "sh.600519"
    assert fake_bs.queries[0][2]["adjustflag"] == "3"
    assert fake_bs.queries[1][2]["adjustflag"] == "1"


def test_daily_pairs_hfq_empty_raises(fake_bs):
    fake_bs.hist_responses = {
        "3": _rs(["date", "close"], [["2026-08-26", "10.2"]]),
        "1": _rs([], []),  # 后复权缺失
    }
    sess = baostock.get_session()
    with pytest.raises(RuntimeError, match="后复权未返回数据"):
        baostock.daily_pairs(sess, "600519", "2026-08-26", "2026-08-26")


def test_daily_pairs_raw_empty_returns_empty(fake_bs):
    fake_bs.hist_responses = {"3": _rs([], [])}
    sess = baostock.get_session()
    raw, hfq = baostock.daily_pairs(sess, "600519", "2026-08-26", "2026-08-26")
    assert raw.empty and hfq.empty


def test_daily_pairs_empty_strings_coerced(fake_bs):
    # 回归：BaoStock 偶发返回空串（如 000066 某批成交量）→ 转 NaN 不崩溃；
    # 无收盘的坏条丢弃；空成交量行保留为 NaN（cleaner 按停牌语义丢弃）
    fake_bs.hist_responses = {
        "3": _rs(["date", "open", "high", "low", "close", "volume", "amount"],
                 [["2026-08-26", "10.0", "10.5", "9.9", "10.2", "", ""],
                  ["2026-08-25", "9.8", "10.1", "9.7", "", "1000000", ""],
                  ["2026-08-24", "9.5", "9.9", "9.4", "9.6", "800000", "7680000"]]),
        "1": _rs(["date", "close"], [["2026-08-26", "68.9"],
                                     ["2026-08-25", "66.0"],
                                     ["2026-08-24", "63.5"]]),
    }
    sess = baostock.get_session()
    raw, hfq = baostock.daily_pairs(sess, "600519", "2026-08-24", "2026-08-26")
    # 08-25 无收盘坏条被丢弃 → 剩 2 行
    assert list(raw["日期"]) == ["2026-08-26", "2026-08-24"]
    # 空成交量/成交额 → NaN（不崩溃）
    r26 = raw.loc[raw["日期"] == "2026-08-26"].iloc[0]
    assert pd.isna(r26["成交量"]) and pd.isna(r26["成交额"])
    # 正常行保留，成交量 股 → 手
    r24 = raw.loc[raw["日期"] == "2026-08-24"].iloc[0]
    assert r24["成交量"] == 8000.0 and float(r24["成交额"]) == 7680000.0


# ---------- 交易日历 ----------

def test_trade_cal_rows_filters_trading_days(fake_bs):
    fake_bs.trade_cal_resp = _rs(
        ["calendar_date", "is_trading_day"],
        [["2026-08-26", "1"], ["2026-08-27", "0"], ["2026-08-28", "1"]])
    sess = baostock.get_session()
    rows = baostock.trade_cal_rows(sess, "2026-08-26", "2026-08-28")
    assert [r["cal_date"] for r in rows] == [date(2026, 8, 26), date(2026, 8, 28)]
    assert all(r["is_open"] for r in rows)
    assert all(r["exchange"] == "SSE" for r in rows)


# ---------- 阶段 2：股票列表 / 估值 / 指数 / 财务（同形状输出） ----------

def test_stock_basic_rows_shape(fake_bs):
    """query_stock_basic 带交易所前缀 → 剥离成 6 位纯数字；过滤非股票；北交所 BJ"""
    fake_bs.stock_basic_resp = _rs(
        ["code", "code_name", "ipoDate", "outDate", "type", "status"],
        [
            ["sh.600519", "贵州茅台", "2001-08-27", "", "1", "1"],
            ["sz.000001", "平安银行", "1991-04-03", "", "1", "1"],
            ["bj.830001", "北 交 新 三", "2020-06-01", "", "1", "0"],
            ["sh.000001", "上证指数", "", "", "2", "1"],   # 非股票 type≠1：过滤
        ])
    sess = baostock.get_session()
    rows = baostock.stock_basic_rows(sess)
    assert len(rows) == 3
    assert rows[0] == {"code": "600519", "name": "贵州茅台", "market": "SH",
                       "status": "L", "list_date": date(2001, 8, 27)}
    # 带空格名称去除；退市状态 D；北交所 8xx → BJ
    assert rows[2]["name"] == "北交新三"
    assert rows[2]["status"] == "D"
    assert rows[2]["market"] == "BJ"
    assert rows[1]["market"] == "SZ"
    # 无 industry（留 AkShare 补全）
    assert "industry" not in rows[0]


def test_stock_basic_rows_empty(fake_bs):
    sess = baostock.get_session()
    assert baostock.stock_basic_rows(sess) == []


def test_valuation_rows_shape(fake_bs):
    """估值行：close/pe_ttm/pb，不含 total_mv/float_mv/pe_static（保留既有值）"""
    fake_bs.hist_responses["3"] = _rs(
        ["date", "close", "peTTM", "pbMRQ"],
        [["2026-08-26", "1302.8", "25.3", "8.1"], ["2026-08-25", "1297.99", "25.1", ""]])
    sess = baostock.get_session()
    rows = baostock.valuation_rows(sess, "600519", "2026-08-25", "2026-08-26")
    assert rows[0] == {"code": "600519", "trade_date": date(2026, 8, 26),
                       "close": 1302.8, "pe_ttm": 25.3, "pb": 8.1}
    assert "total_mv" not in rows[0] and "pe_static" not in rows[0]
    # 空串 → None
    assert rows[1]["pb"] is None
    # 请求用 bs 格式代码 + 不复权
    assert fake_bs.queries[0][0] == "sh.600519"
    assert fake_bs.queries[0][2]["adjustflag"] == "3"


def test_index_bs_code():
    assert baostock.index_bs_code("sh000001") == "sh.000001"
    assert baostock.index_bs_code("sz399106") == "sz.399106"
    assert baostock.index_bs_code("sh000300") == "sh.000300"


def test_index_rows_shape(fake_bs):
    """指数行：code 保留新浪码；volume 股→手；adj_factor None"""
    fake_bs.hist_responses["3"] = _rs(
        ["date", "open", "high", "low", "close", "volume", "amount"],
        [["2026-08-26", "3350.1", "3360.5", "3340.0", "3355.2", "400000000", "5e11"],
         ["2026-08-25", "3340.2", "3355.0", "3330.1", "3348.9", "380000000", "4.8e11"]])
    sess = baostock.get_session()
    rows = baostock.index_rows(sess, "sh000001", "2026-08-25", "2026-08-26")
    assert rows[0]["code"] == "sh000001"
    assert rows[0]["trade_date"] == date(2026, 8, 26)
    assert rows[0]["volume"] == 4000000        # 股 → 手（÷100）
    assert rows[0]["amount"] == 5e11           # 元 原样
    assert rows[0]["adj_factor"] is None
    # 指数码直接映射 sh.000001（不是 sz.000001）
    assert fake_bs.queries[0][0] == "sh.000001"


def test_financial_rows_shape_and_debt_unit(fake_bs):
    """财务行：roe/gross_margin/profit_growth/debt_ratio 统一 ×100 单位校准
    （BaoStock 小数比例 → 库存百分数，茅台/浦发真实 API 实证）+
    营收增速两期 MBRevenue 同比 + announce_date"""
    fake_bs.profit_resp = _rs(
        ["code", "pubDate", "statDate", "roeAvg", "gpMargin", "MBRevenue"],
        [["sh.600519", "2026-08-15", "2026-06-30", "0.179543", "0.895552", "9e10"]])
    fake_bs.growth_resp = _rs(
        ["code", "YOYNI"], [["sh.600519", "-0.02029"]])
    fake_bs.balance_resp = _rs(
        ["code", "liabilityToAsset"], [["sh.600519", "0.151931"]])
    sess = baostock.get_session()
    row = baostock.financial_rows(sess, "600519", 2026, 2)
    assert row is not None
    assert row["code"] == "600519"
    assert row["report_date"] == date(2026, 6, 30)
    assert row["roe"] == 17.9543            # 0.179543 ×100
    assert row["gross_margin"] == 89.5552   # 0.895552 ×100
    assert row["profit_growth"] == -2.029   # 来自 growth_data YOYNI ×100
    assert row["debt_ratio"] == 15.1931     # 0.151931 ×100（不是 ×10000！）
    assert row["announce_date"] == date(2026, 8, 15)
    # 请求代码 bs 格式
    assert fake_bs.queries[0][1] == ("sh.600519", 2026, 2)


def test_financial_rows_revenue_growth_two_period(fake_bs):
    """营收增速 = (本期 MBRevenue − 上年同期 MBRevenue) / 上年同期 ×100"""
    def profit_resp(year, quarter):
        if year == 2026:
            return _rs(["code", "MBRevenue"], [["sz.000001", "6e10"]])
        return _rs(["code", "MBRevenue"], [["sz.000001", "5e10"]])
    fake_bs.profit_resp = profit_resp
    sess = baostock.get_session()
    row = baostock.financial_rows(sess, "000001", 2026, 1)
    assert row["revenue_growth"] == 20.0         # (6-5)/5 ×100
    assert row["report_date"] == date(2026, 3, 31)


def test_financial_rows_empty_profit_returns_none(fake_bs):
    """报告期未披露：profit_data 空 → 返回 None（采集器跳过该期）"""
    sess = baostock.get_session()
    assert baostock.financial_rows(sess, "600000", 2026, 1) is None


# ---------- 会话：连接级错误重连重试 ----------

def test_query_retries_on_conn_error(monkeypatch, fake_bs):
    """首查返回连接级错误码 → 重连重试 → 成功；sleep 只发生一次"""
    sleeps = []
    monkeypatch.setattr(baostock.time, "sleep", lambda s: sleeps.append(s))
    fake_bs.hist_responses["3"] = _rs([], [], error_code="10002002", error_msg="网络连接失败")
    # 第一次 error，第二次成功
    state = {"n": 0}

    def flip(af):
        state["n"] += 1
        if state["n"] == 1:
            return _rs([], [], error_code="10002002", error_msg="网络连接失败")
        return _rs(["date", "close"], [["2026-08-26", "10.2"]])

    fake_bs.hist_responses["3"] = flip
    sess = baostock.BaoStockSession(retries=1, retry_delay=0.5)
    rs = sess.query(fake_bs.query_history_k_data_plus, "sh.600519", "date,close",
                    start_date="2026-08-26", end_date="2026-08-26",
                    frequency="d", adjustflag="3")
    assert rs.error_code == "0"
    assert state["n"] == 2          # 重试一次
    assert sleeps == [0.5]          # 只有一次重试等待
    assert fake_bs.login_calls >= 2  # 连接失效后重新登录


def test_query_non_conn_error_no_retry(monkeypatch, fake_bs):
    """非连接级错误（参数/数据错）直接抛，不重试"""
    sleeps = []
    monkeypatch.setattr(baostock.time, "sleep", lambda s: sleeps.append(s))
    fake_bs.hist_responses["3"] = _rs([], [], error_code="10003001", error_msg="参数错误")
    sess = baostock.BaoStockSession(retries=2, retry_delay=0.5)
    with pytest.raises(RuntimeError, match="参数错误"):
        sess.query(fake_bs.query_history_k_data_plus, "sh.600519", "date,close",
                   start_date="2026-08-26", end_date="2026-08-26",
                   frequency="d", adjustflag="3")
    assert sleeps == []


def test_query_raises_when_bs_none(monkeypatch):
    """baostock 未安装：get_session() 返回 None，查询直接抛"""
    monkeypatch.setattr(baostock, "bs", None)
    assert baostock.get_session() is None


# ---------- 单例 ----------

def test_get_session_singleton():
    s1 = baostock.get_session()
    s2 = baostock.get_session()
    assert s1 is s2
    baostock._reset_session()
    assert baostock.get_session() is not s1


# ---------- 阶段 2：源级门控 ----------

def test_baostock_enabled_scope_gating(monkeypatch):
    """BAOSTOCK_SOURCES 决定哪些 scope 走 BaoStock；无参 = scopes 非空"""
    from app import collect_config as config
    monkeypatch.setattr(config, "BAOSTOCK_ENABLED", True)
    monkeypatch.setattr(config, "BAOSTOCK_SOURCES", ["daily", "calendar", "stock_basic"])
    assert config.baostock_enabled("daily") is True
    assert config.baostock_enabled("stock_basic") is True
    assert config.baostock_enabled("valuation") is False
    assert config.baostock_enabled("index") is False
    assert config.baostock_enabled() is True
    # 开关关闭：全部 False
    monkeypatch.setattr(config, "BAOSTOCK_ENABLED", False)
    assert config.baostock_enabled("daily") is False
    assert config.baostock_enabled() is False
    # scopes 空：无参返回 False（不误判主源启用）
    monkeypatch.setattr(config, "BAOSTOCK_ENABLED", True)
    monkeypatch.setattr(config, "BAOSTOCK_SOURCES", [])
    assert config.baostock_enabled() is False


# ---------- 阶段 4：黑名单冷却（08-28 事故：封禁期逐股登录锤） ----------

def test_login_blacklist_sets_cooldown(monkeypatch, fake_bs):
    """命中 10001011 黑名单 → 进程内冷却；冷却期内不再触发登录"""
    fake_bs.login_code = "10001011"
    sess = baostock.BaoStockSession(retries=0, retry_delay=0.01)
    with pytest.raises(RuntimeError, match="BaoStock 登录失败"):
        sess.query(fake_bs.query_history_k_data_plus, "sh.600519", "date,close",
                   start_date="2026-08-26", end_date="2026-08-26",
                   frequency="d", adjustflag="3")
    assert fake_bs.login_calls == 1     # 首查登录一次并置位冷却
    assert sess._ban_until > 0

    # 冷却期内：即使服务端已恢复（login_code="0"），也不再实际登录
    fake_bs.login_code = "0"
    before = fake_bs.login_calls
    with pytest.raises(RuntimeError, match="封禁冷却中"):
        sess.query(fake_bs.query_history_k_data_plus, "sh.600519", "date,close",
                   start_date="2026-08-26", end_date="2026-08-26",
                   frequency="d", adjustflag="3")
    assert fake_bs.login_calls == before  # 未再调用登录
