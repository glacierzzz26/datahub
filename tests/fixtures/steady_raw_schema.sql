-- ============================================================
-- 冻结副本：steady **原始表** DDL（跨仓库 schema 对齐的基准）
-- ============================================================
-- 来源：steady 仓库 deploy/postgres/init.sql + deploy/migrations/007_data_scope_and_turnover.sql
-- steady commit：c910bc0    抽取日期：2026-10-09
--
-- 本文件是 **vendor 的冻结快照**——datahub 的 CI 读不到 steady 仓库，故把
-- steady 原始表 DDL 逐字拷来，供 tests/test_schema_parity.py 断言
-- datahub deploy/postgres/init.sql 与之**逐字对齐**（列/类型/主键/唯一键/索引）。
--
-- ⚠️ **维护约定**：当 steady 的原始表 schema 发生变更时，须**手工**重新同步本文件
--    （逐字拷贝）+ 在 datahub 补一条 deploy/migrations/NNN_*.sql，二者在同一 PR 内完成。
--    同步后同步更新上面的 steady commit 与抽取日期。
--
-- 纪律：只含**原始采集表**。不含 factor_*/strategy_*/account*/backtest_*/remediation_task/
--       app_config/morning_brief/notify_config（那些留 steady）。
-- ============================================================

-- ------------------------------------------------------------
-- stock_basic
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_basic (
    code       VARCHAR(10)  PRIMARY KEY,
    name       VARCHAR(50)  NOT NULL,
    market     VARCHAR(10),                -- SH / SZ / BJ
    industry   VARCHAR(50),
    list_date  DATE,
    status     VARCHAR(10)  DEFAULT 'L',   -- L=上市 / D=退市
    universe   VARCHAR(20),                -- hs300 / zz500 / NULL=全市场（**策略选股域**）
    data_scope VARCHAR(16),                -- a_share=SH+SZ 采集范围；NULL=不采集(BJ/INDEX)
    created_at TIMESTAMP    DEFAULT NOW(),
    updated_at TIMESTAMP    DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stock_basic_universe ON stock_basic (universe);
CREATE INDEX IF NOT EXISTS idx_stock_basic_data_scope ON stock_basic (data_scope);

-- ------------------------------------------------------------
-- daily_price
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_price (
    id         BIGSERIAL     PRIMARY KEY,
    code       VARCHAR(10)   NOT NULL REFERENCES stock_basic (code),
    trade_date DATE          NOT NULL,
    open       DECIMAL(10,2),
    high       DECIMAL(10,2),
    low        DECIMAL(10,2),
    close      DECIMAL(10,2),
    volume     BIGINT,                       -- 成交量（手）
    amount     DECIMAL(15,2),                -- 成交额（元）
    adj_factor DECIMAL(10,4),                -- 复权因子
    turnover_rate DECIMAL(10,4)              -- 换手率（%），腾讯日K[7]/快照[38]
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_price_code_date
    ON daily_price (code, trade_date);
CREATE INDEX IF NOT EXISTS idx_daily_price_trade_date
    ON daily_price (trade_date);

-- ------------------------------------------------------------
-- financial_indicator
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS financial_indicator (
    id             BIGSERIAL     PRIMARY KEY,
    code           VARCHAR(10)   NOT NULL REFERENCES stock_basic (code),
    report_date    DATE          NOT NULL,   -- 报告期
    pe             DECIMAL(10,2),
    pb             DECIMAL(10,2),
    roe            DECIMAL(10,4),
    profit_growth  DECIMAL(10,4),
    revenue_growth DECIMAL(10,4),
    debt_ratio     DECIMAL(10,4),
    gross_margin   DECIMAL(10,4),
    announce_date  DATE          NOT NULL,   -- 公告日（财报实际披露日）
    created_at     TIMESTAMP     DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_financial_code_report
    ON financial_indicator (code, report_date);
CREATE INDEX IF NOT EXISTS idx_financial_code_announce
    ON financial_indicator (code, announce_date);

-- ------------------------------------------------------------
-- daily_valuation
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_valuation (
    id         BIGSERIAL     PRIMARY KEY,
    code       VARCHAR(10)   NOT NULL REFERENCES stock_basic (code),
    trade_date DATE          NOT NULL,
    close      DECIMAL(10,2),                -- 收盘价（元）
    total_mv   DECIMAL(18,2),                -- 总市值（元）
    float_mv   DECIMAL(18,2),                -- 流通市值（元）
    pe_ttm     DECIMAL(12,4),                -- 市盈率 TTM
    pe_static  DECIMAL(12,4),                -- 市盈率（静态）
    pb         DECIMAL(12,4),                -- 市净率
    created_at TIMESTAMP     DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_valuation_code_date
    ON daily_valuation (code, trade_date);

-- ------------------------------------------------------------
-- trade_calendar
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_calendar (
    cal_date DATE         PRIMARY KEY,
    is_open  BOOLEAN      NOT NULL,
    exchange VARCHAR(10)  DEFAULT 'SSE'
);

-- ------------------------------------------------------------
-- market_hotspot
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_hotspot (
    spot_date DATE        PRIMARY KEY,
    sections  JSONB       NOT NULL,   -- {indices, sectors_gain, sectors_flow, hot_stocks}
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------
-- task_run
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_run (
    id         BIGSERIAL   PRIMARY KEY,
    task_name  VARCHAR(64) NOT NULL,
    run_date   DATE        NOT NULL,
    status     VARCHAR(16) NOT NULL,           -- success / skipped / failed
    message    TEXT,
    detail     JSONB,                          -- 结构化明细（后续供大模型消费）
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (task_name, run_date)
);
