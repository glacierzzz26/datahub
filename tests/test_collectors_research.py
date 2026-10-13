"""P14-1 研报数值域采集器测试（mock akshare）。

重点验证**口径红线**（移植自 research provider，不得丢）：
- 龙虎榜记录：同日多「上榜原因」合并为一行、pct 取东财「涨跌幅」。
- 龙虎榜明细：丢弃「连续三个交易日」块、按 (营业部, 买, 卖) 去重。
- 申万指数：不存量价、pct/amplitude 由 prev_close 派生。
- 宏观：level/yoy/mom 抽取映射、期号→期末；CPI「当月」是**指数非百分比**（原样不转）。
- 新闻：content_hash 去重键、列名映射。
"""
from datetime import date, datetime

import pandas as pd
import pytest

from app.collectors import research as r

# ---------- 龙虎榜记录 ----------

def test_capital_flow_merges_reasons_and_uses_em_pct():
    """同日多原因 → 一行；pct_change 取「涨跌幅」而非「对应值」。"""
    df = pd.DataFrame([
        {"代码": "600519", "名称": "贵州茅台", "上榜日": "2026-09-17",
         "收盘价": 1700.0, "涨跌幅": 9.98, "换手率": 3.2, "上榜原因": "日涨幅偏离值达7%",
         "对应值": 35.24},
        {"代码": "600519", "名称": "贵州茅台", "上榜日": "2026-09-17",
         "收盘价": 1700.0, "涨跌幅": 9.98, "换手率": 3.2, "上榜原因": "连续三个交易日",
         "对应值": 35.24},
    ])
    rows = r.build_capital_flow_rows(df)
    assert len(rows) == 1
    assert rows[0]["code"] == "600519"
    assert rows[0]["trade_date"] == date(2026, 9, 17)
    assert rows[0]["pct_change"] == 9.98          # 东财「涨跌幅」，非「对应值」35.24
    assert rows[0]["reasons"] == "日涨幅偏离值达7%；连续三个交易日"


def test_capital_flow_dedup_same_reason():
    """同一原因重复出现只留一次。"""
    df = pd.DataFrame([
        {"代码": "000001", "名称": "平安银行", "上榜日": "20260918",
         "收盘价": 10.0, "涨跌幅": 5.0, "换手率": 1.0, "上榜原因": "日涨幅偏离值达7%"},
        {"代码": "000001", "名称": "平安银行", "上榜日": "20260918",
         "收盘价": 10.0, "涨跌幅": 5.0, "换手率": 1.0, "上榜原因": "日涨幅偏离值达7%"},
    ])
    rows = r.build_capital_flow_rows(df)
    assert len(rows) == 1
    assert rows[0]["reasons"] == "日涨幅偏离值达7%"


# ---------- 龙虎榜明细 ----------

def test_capital_flow_detail_drops_multiday_and_dedups():
    """丢弃「连续三个交易日」块；相同 (营业部, 买, 卖) 只留一行。"""
    buy = pd.DataFrame([
        {"类型": "买入", "交易营业部名称": "机构专用", "买入金额": 1000000.0,
         "买入金额-占总成交比例": 5.0, "卖出金额": 0.0,
         "卖出金额-占总成交比例": 0.0, "净额": 1000000.0},
        {"类型": "连续三个交易日买入", "交易营业部名称": "机构专用",
         "买入金额": 1000000.0, "买入金额-占总成交比例": 5.0,
         "卖出金额": 0.0, "卖出金额-占总成交比例": 0.0, "净额": 1000000.0},
    ])
    # 卖出侧出现与买入侧同 (营业部, 买, 卖) 的重复块（按原因分块重复）
    sell = pd.DataFrame([
        {"类型": "买入", "交易营业部名称": "机构专用", "买入金额": 1000000.0,
         "买入金额-占总成交比例": 5.0, "卖出金额": 0.0,
         "卖出金额-占总成交比例": 0.0, "净额": 1000000.0},
        {"类型": "卖出", "交易营业部名称": "某营业部", "买入金额": 0.0,
         "买入金额-占总成交比例": 0.0, "卖出金额": 500000.0,
         "卖出金额-占总成交比例": 2.5, "净额": -500000.0},
    ])
    rows = r.build_capital_flow_detail_rows(
        "600519", date(2026, 9, 17), {"买入": buy, "卖出": sell})
    # 跨日块被丢、机构专用去重 → 只剩 机构专用 + 某营业部
    assert len(rows) == 2
    dealers = {x["dealer_name"] for x in rows}
    assert dealers == {"机构专用", "某营业部"}
    assert all("连续三个交易日" not in (x["reason"] or "") for x in rows)


def test_capital_flow_detail_missing_amount_defaults_zero():
    """金额字段缺失补 0.0（DDL NOT NULL；镜像 research _to_float）。"""
    df = pd.DataFrame([
        {"类型": "买入", "交易营业部名称": "X", "买入金额": None,
         "买入金额-占总成交比例": None, "卖出金额": None,
         "卖出金额-占总成交比例": None, "净额": None},
    ])
    rows = r.build_capital_flow_detail_rows("600519", date(2026, 9, 17), {"买入": df})
    assert rows[0]["buy_amount"] == 0.0
    assert rows[0]["sell_amount"] == 0.0
    assert rows[0]["net_amount"] == 0.0


# ---------- 申万指数 ----------

def test_industry_index_no_volume_and_derives():
    """不存量价；pct/amplitude/chg 由 prev_close 派生（首行 0）。"""
    df = pd.DataFrame([
        {"日期": "2026-01-02", "开盘": 3000.0, "收盘": 3040.0, "最高": 3050.0, "最低": 2990.0},
        {"日期": "2026-01-03", "开盘": 3040.0, "收盘": 3070.0, "最高": 3080.0, "最低": 3030.0},
    ])
    rows = r.build_industry_index_rows("801010", df)
    assert len(rows) == 2
    assert "volume" not in rows[0] and "amount" not in rows[0]
    assert rows[0]["pct_change"] == 0.0          # 首行无 prev
    assert rows[1]["pct_change"] == pytest.approx((3070 - 3040) / 3040 * 100)


def test_industry_index_date_window():
    df = pd.DataFrame([
        {"日期": "2026-01-01", "开盘": 1.0, "收盘": 1.0, "最高": 1.0, "最低": 1.0},
        {"日期": "2026-01-02", "开盘": 1.0, "收盘": 1.0, "最高": 1.0, "最低": 1.0},
        {"日期": "2026-01-03", "开盘": 1.0, "收盘": 1.0, "最高": 1.0, "最低": 1.0},
    ])
    rows = r.build_industry_index_rows("801010", df,
                                       start_date=date(2026, 1, 2),
                                       end_date=date(2026, 1, 2))
    assert [x["trade_date"] for x in rows] == [date(2026, 1, 2)]


# ---------- 宏观 ----------

def test_build_macro_rows_cpi_level_is_index_not_percent():
    """level 存源文原值（指数 100.8，非 0.8）；period_end 解析正确。"""
    df = pd.DataFrame([
        {"月份": "2026年08月份", "全国-当月": 100.8,
         "全国-同比增长": 0.8, "全国-环比增长": 0.3},
    ])
    rows = r.build_macro_rows("cn_cpi", df, r._MACRO_DIMENSIONS["cn_cpi"])
    assert rows[0]["indicator"] == "cn_cpi"
    assert rows[0]["period_label"] == "2026年08月份"
    assert rows[0]["period_end"] == date(2026, 8, 31)
    assert rows[0]["cadence"] == "month"
    assert rows[0]["level"] == 100.8             # 指数，非百分比
    assert rows[0]["yoy"] == 0.8 and rows[0]["mom"] == 0.3


def test_build_macro_rows_ppi_mom_none():
    """PPI 源无环比 → mom 恒 None。"""
    df = pd.DataFrame([{"月份": "2026年08月份", "当月": 98.0, "当月同比增长": -2.0}])
    rows = r.build_macro_rows("cn_ppi", df, r._MACRO_DIMENSIONS["cn_ppi"])
    assert rows[0]["level"] == 98.0 and rows[0]["yoy"] == -2.0
    assert rows[0]["mom"] is None


def test_parse_period_end():
    assert r.parse_period_end("2026年08月份", "month") == date(2026, 8, 31)
    assert r.parse_period_end("2026年第1季度", "quarter") == date(2026, 3, 31)
    assert r.parse_period_end("2026年第1-2季度", "quarter") == date(2026, 6, 30)


def test_build_macro_rows_bad_period_raises():
    df = pd.DataFrame([{"月份": "2026/08", "全国-当月": 100.0,
                        "全国-同比增长": 0.0, "全国-环比增长": 0.0}])
    try:
        r.build_macro_rows("cn_cpi", df, r._MACRO_DIMENSIONS["cn_cpi"])
    except ValueError:
        return
    raise AssertionError("非法期号应抛 ValueError（不静默当缺失）")


# ---------- 申万分类 / 成分 ----------

def test_build_sw_info_rows():
    df = pd.DataFrame([
        {"行业代码": "801010.SI", "行业名称": "农林牧渔", "上级行业": None,
         "成份个数": "100", "静态市盈率": 20.0, "TTM(滚动)市盈率": 22.0,
         "市净率": 2.5, "静态股息率": 1.0},
        {"行业代码": "801011", "行业名称": "林业", "上级行业": "农林牧渔",
         "成份个数": "N/A", "静态市盈率": "-", "TTM(滚动)市盈率": 19.0,
         "市净率": 2.0, "静态股息率": 1.2},
    ])
    rows = r.build_sw_info_rows(df, 1)
    assert rows[0]["code"] == "801010"           # .SI 去掉
    assert rows[0]["level"] == 1 and rows[0]["level_label"] == "一级"
    assert rows[0]["member_count"] == 100
    assert rows[1]["member_count"] is None        # N/A → None（不补 0）
    assert rows[1]["pe_static"] is None           # '-' → None


def test_build_sw_cons_rows():
    df = pd.DataFrame([
        {"股票代码": "600519.SH", "股票简称": "贵州茅台", "市值": 20000.0,
         "市盈率ttm": 30.0, "市净率": 9.0, "ROE(%)": 25.0, "股息率": 2.0,
         "净利润增速(%)": 15.0, "营收增速(%)": 12.0},
    ])
    rows = r.build_sw_cons_rows(df, "801012")
    assert rows[0]["index_code"] == "801012"
    assert rows[0]["stock_code"] == "600519"
    assert rows[0]["roe"] == 25.0


# ---------- 新闻 / 公告 ----------

def test_build_news_rows_hash_and_rename():
    df = pd.DataFrame([
        {"新闻标题": "茅台发布中报", "新闻内容": "正文A",
         "发布时间": "2026-01-02 09:30:00", "文章来源": "证券时报", "新闻链接": "http://x/1"},
    ])
    rows = r.build_news_rows("600519", df)
    assert rows[0]["title"] == "茅台发布中报"
    assert rows[0]["published_at"] == datetime(2026, 1, 2, 9, 30)
    assert len(rows[0]["content_hash"]) == 64
    # 同内容同 hash；内容变即变
    h1 = rows[0]["content_hash"]
    assert r._content_hash("600519", "茅台发布中报", "正文A") == h1
    assert r._content_hash("600519", "茅台发布中报", "正文B") != h1


def test_build_announcement_rows_rename_and_drop_empty_title():
    df = pd.DataFrame([
        {"公告标题": "2025年年度报告", "公告类型": "年度报告",
         "公告日期": "2026-01-02", "网址": "http://a/1"},
        {"公告标题": "", "公告类型": "其他", "公告日期": "2026-01-03", "网址": None},
    ])
    rows = r.build_announcement_rows("600519", df)
    assert len(rows) == 1
    assert rows[0]["title"] == "2025年年度报告"
    assert rows[0]["publish_date"] == date(2026, 1, 2)
    assert rows[0]["url"] == "http://a/1"


# ---------- fetch 接线（mock akshare）----------

def test_macro_collector_fetch_all_failed_raises(monkeypatch):
    """全部维度拉取失败 → raise（触发 base.run 重试，不静默空成功）。"""

    def _boom(*a, **k):
        raise RuntimeError("source down")

    monkeypatch.setattr(r.ak, "macro_china_cpi", _boom)
    c = r.MacroCollector(None)
    try:
        c.fetch(["cn_cpi"])
    except RuntimeError:
        return
    raise AssertionError("全部维度失败应 raise")


def test_capital_flow_collector_fetch(monkeypatch):
    df = pd.DataFrame([
        {"代码": "600519", "名称": "贵州茅台", "上榜日": "2026-09-17",
         "收盘价": 1700.0, "涨跌幅": 9.98, "换手率": 3.2, "上榜原因": "日涨幅偏离值达7%"},
    ])
    monkeypatch.setattr(r.ak, "stock_lhb_detail_em", lambda **k: df)
    c = r.CapitalFlowCollector(None)
    rows = c.fetch(date(2026, 9, 1), date(2026, 9, 30))
    assert rows[0]["code"] == "600519" and rows[0]["pct_change"] == 9.98
