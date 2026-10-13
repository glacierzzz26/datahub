"""ORM 模型（与 deploy/postgres/init.sql 表结构一一对应）。

**搬自 steady `collector/app/models/tables.py`**，仅保留 **datahub 自有库**承载的
原始表子集：`stock_basic` / `daily_price` / `financial_indicator` / `daily_valuation` /
`trade_calendar` / `market_hotspot` / `task_run`。

**移除**（留 steady）：`AppConfig`（采集侧从未引用）、`RemediationTask`（读 steady 专属
自愈队列，随 remediation 一并留 steady）。
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class StockBasic(Base):
    """股票基本信息"""

    __tablename__ = "stock_basic"

    code = Column(String(10), primary_key=True)
    name = Column(String(50), nullable=False)
    market = Column(String(10))  # SH / SZ / BJ
    industry = Column(String(50))
    list_date = Column(Date)
    status = Column(String(10), default="L")  # L=上市 / D=退市
    universe = Column(String(20))  # hs300 / zz500 / NULL=全市场
    # 采集范围（Issue #13）：'a_share' = market IN ('SH','SZ')，NULL = 不采集。
    # 与 universe 正交 —— 前者管「采哪些」，后者管「策略选哪些」。采集侧过滤读此列。
    data_scope = Column(String(16))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=datetime.now)


class DailyPrice(Base):
    """日行情"""

    __tablename__ = "daily_price"
    __table_args__ = (
        {"comment": "日行情，UNIQUE(code, trade_date) 索引见 init.sql"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False)
    trade_date = Column(Date, nullable=False)
    open = Column(Numeric(10, 2))
    high = Column(Numeric(10, 2))
    low = Column(Numeric(10, 2))
    close = Column(Numeric(10, 2))
    volume = Column(BigInteger)  # 成交量（手）
    amount = Column(Numeric(15, 2))  # 成交额（元）
    adj_factor = Column(Numeric(10, 4))  # 复权因子
    # 换手率（%）：腾讯日K[7]/快照[38] 写入；BaoStock/新浪腿无此列（恒 None）。
    # ⚠️ 口径为百分数（0.21 = 0.21%），勿混入新浪小数口径（0.0021）。
    turnover_rate = Column(Numeric(10, 4))


class FinancialIndicator(Base):
    """财务指标（announce_date 公告日，防止未来函数）"""

    __tablename__ = "financial_indicator"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False)
    report_date = Column(Date, nullable=False)  # 报告期
    pe = Column(Numeric(10, 2))
    pb = Column(Numeric(10, 2))
    roe = Column(Numeric(10, 4))
    profit_growth = Column(Numeric(10, 4))
    revenue_growth = Column(Numeric(10, 4))
    debt_ratio = Column(Numeric(10, 4))
    gross_margin = Column(Numeric(10, 4))
    announce_date = Column(Date, nullable=False)  # 公告日
    created_at = Column(DateTime, server_default=func.now())


class FinancialIndicatorExt(Base):
    """财务扩展（datahub **自有**，非 steady 镜像）：net_margin 等 research 侧补口。

    steady 镜像表 financial_indicator 无 net_margin 列；为不破逐字 parity，另立侧表，
    由 `db` provider 的 `_financial_indicator` left-join 出 net_margin。
    """

    __tablename__ = "financial_indicator_ext"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False)
    report_date = Column(Date, nullable=False)  # 报告期
    net_margin = Column(Numeric(10, 4))  # 销售净利率（%）
    created_at = Column(DateTime, server_default=func.now())


class DailyValuation(Base):
    """每日估值（PE(TTM)/PE(静)/PB/市值）"""

    __tablename__ = "daily_valuation"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False)
    trade_date = Column(Date, nullable=False)
    close = Column(Numeric(10, 2))
    total_mv = Column(Numeric(18, 2))  # 总市值（元）
    float_mv = Column(Numeric(18, 2))  # 流通市值（元）
    pe_ttm = Column(Numeric(12, 4))  # 市盈率 TTM
    pe_static = Column(Numeric(12, 4))  # 市盈率（静态）
    pb = Column(Numeric(12, 4))  # 市净率
    created_at = Column(DateTime, server_default=func.now())


class TradeCalendar(Base):
    """交易日历"""

    __tablename__ = "trade_calendar"

    cal_date = Column(Date, primary_key=True)
    is_open = Column(Boolean, nullable=False)
    exchange = Column(String(10), default="SSE")


class TaskRun(Base):
    """任务执行账本。

    采集侧原先**不写** task_run（Issue #14 起由 watchdog 写 `collector_watchdog`
    失败行）。⚠️ 切库后 datahub 的 failed 行写在 **datahub 库**，而 quant-engine 的
    `notify_scheduler._check_task_alerts` 读 **steady 库** —— 过渡期告警链断，
    信号改看 datahub `/healthz`（见 phase2 蓝图）。
    status: success / skipped / failed；UNIQUE(task_name, run_date) 幂等 upsert。
    """

    __tablename__ = "task_run"
    __table_args__ = (UniqueConstraint("task_name", "run_date", name="uq_task_run"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_name = Column(String(64), nullable=False)
    run_date = Column(Date, nullable=False)
    status = Column(String(16), nullable=False)
    message = Column(String)
    detail = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class MarketHotspot(Base):
    """市场热点快照（早盘简报数据源）：每日早晨采集一次。

    sections JSONB 结构：
    { indices: [{name, code, close, change_pct}, ...],        # 隔夜外盘 + A股指数
      sectors_gain: [{name, change_pct, leader}, ...],        # 板块涨幅榜 TOP_N
      sectors_flow: [{name, net_inflow}, ...],                # 板块资金净流入 TOP_N
      hot_stocks: [{rank, code, name, change_pct,
                    board_days?, industry?}, ...] }           # 个股人气榜 TOP_N
    """

    __tablename__ = "market_hotspot"

    spot_date = Column(Date, primary_key=True)
    sections = Column(JSON, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


# ---------- 研报数值域（datahub 自有表，P14-1）----------
# 列逐字对齐 deploy/postgres/init.sql §7；**不**设 FK 到 stock_basic（覆盖全 A 含 BJ/池外码）。
# 列名/类型变更须同步 init.sql + deploy/migrations/002 + tests/test_orm_matches_ddl.py。


class CapitalFlow(Base):
    """龙虎榜记录（每 code 每日一行，多「上榜原因」合并 reasons）。"""

    __tablename__ = "capital_flow"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False)
    trade_date = Column(Date, nullable=False)
    name = Column(String(50))
    close = Column(Numeric(10, 2))                 # 收盘价（元）
    pct_change = Column(Numeric(10, 4))            # 涨跌幅（%），东财「涨跌幅」
    turnover_rate = Column(Numeric(10, 4))         # 换手率（%）
    reasons = Column(String)                       # 上榜原因，「；」连接


class CapitalFlowDetail(Base):
    """龙虎榜明细（营业部级）。"""

    __tablename__ = "capital_flow_detail"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False)
    trade_date = Column(Date, nullable=False)
    dealer_name = Column(String(120), nullable=False)   # 交易营业部名称
    buy_amount = Column(Numeric(18, 2), nullable=False, default=0)   # 买入金额（元）
    buy_pct = Column(Numeric(10, 4))                    # 买入金额-占总成交比例
    sell_amount = Column(Numeric(18, 2), nullable=False, default=0)  # 卖出金额（元）
    sell_pct = Column(Numeric(10, 4))                   # 卖出金额-占总成交比例
    net_amount = Column(Numeric(18, 2))                 # 净额（元）
    reason = Column(String(40))                         # 类型（买入/卖出…）


class IndustryIndex(Base):
    """申万行业指数日线（不存量价）。"""

    __tablename__ = "industry_index"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False)      # 申万行业指数代码（如 801010）
    trade_date = Column(Date, nullable=False)
    open = Column(Numeric(12, 2))
    high = Column(Numeric(12, 2))
    low = Column(Numeric(12, 2))
    close = Column(Numeric(12, 2))
    pct_change = Column(Numeric(10, 4))            # 涨跌幅（%）
    amplitude = Column(Numeric(10, 4))             # 振幅（%）
    chg_amount = Column(Numeric(12, 4))            # 涨跌额


class IndustrySwInfo(Base):
    """申万行业分类信息（一/二/三级快照）。"""

    __tablename__ = "industry_sw_info"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False)      # 行业代码（如 801010）
    level = Column(Integer, nullable=False)        # 1/2/3
    name = Column(String(50))
    level_label = Column(String(20))               # 一级/二级/三级
    parent_name = Column(String(50))               # 上级行业
    member_count = Column(Integer)                 # 成份个数
    pe_static = Column(Numeric(12, 4))             # 静态市盈率
    pe_ttm = Column(Numeric(12, 4))                # TTM(滚动)市盈率
    pb = Column(Numeric(12, 4))                    # 市净率
    dividend_yield = Column(Numeric(10, 4))        # 静态股息率


class IndustrySwCons(Base):
    """申万三级成分股。"""

    __tablename__ = "industry_sw_cons"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    index_code = Column(String(10), nullable=False)    # 申万三级行业代码
    stock_code = Column(String(10), nullable=False)    # 成分股代码
    stock_name = Column(String(50))
    market_cap = Column(Numeric(18, 4))                # 市值（亿元）
    pe_ttm = Column(Numeric(12, 4))                    # 市盈率 TTM
    pb = Column(Numeric(12, 4))                        # 市净率
    roe = Column(Numeric(10, 4))                       # ROE（%）
    dividend_yield = Column(Numeric(10, 4))            # 股息率（%）
    net_profit_growth = Column(Numeric(10, 4))         # 净利润增速（%）
    revenue_growth = Column(Numeric(10, 4))            # 营收增速（%）


class Macro(Base):
    """宏观指标序列（CPI/PPI/M2/GDP）。"""

    __tablename__ = "macro"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    indicator = Column(String(16), nullable=False)     # cn_cpi/cn_ppi/cn_m2/cn_gdp
    period_label = Column(String(40), nullable=False)  # 源文原文（如 2026年08月份）
    period_end = Column(Date, nullable=False)          # 统计期末
    cadence = Column(String(8), nullable=False)        # month/quarter
    level = Column(Numeric(18, 4))                     # 指标值（指数/绝对量）
    yoy = Column(Numeric(10, 4))                       # 同比（%）
    mom = Column(Numeric(10, 4))                       # 环比（%）；源无则 NULL


class NewsByStock(Base):
    """个股新闻（content_hash 去重键，不外发）。"""

    __tablename__ = "news_by_stock"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False)
    title = Column(String)
    content = Column(String)
    published_at = Column(DateTime, nullable=False)    # 发布时间
    source = Column(String(40))                        # 文章来源
    url = Column(String(500))                          # 新闻链接
    content_hash = Column(String(64), nullable=False)


class AnnouncementByStock(Base):
    """个股公告（标题级）。"""

    __tablename__ = "announcement_by_stock"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False)
    title = Column(String(300))
    category = Column(String(40))                      # 公告类型
    publish_date = Column(Date)                        # 公告日期
    url = Column(String(500))                          # 网址
