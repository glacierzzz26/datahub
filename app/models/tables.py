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
