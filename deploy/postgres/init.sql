-- ============================================================
-- datahub 数据库初始化脚本（**原始表子集**）
-- PostgreSQL 16
--
-- 来源：逐字抽取自 steady `deploy/postgres/init.sql`（@c910bc0）
--       与 `deploy/migrations/007_data_scope_and_turnover.sql`。
-- **只含原始采集表**（stock_basic / daily_price / daily_valuation /
-- financial_indicator / trade_calendar / market_hotspot）+ 采集台账 task_run；
-- 不含 factor_*/strategy_*/account*/backtest_*/morning_brief/notify_config/app_config
-- 等计算/配置表（那些留在 steady）。
--
-- 纪律：列名/类型/主键/唯一键/索引**逐字对齐 steady**——回填对账与 Phase 3
--       「steady 读到等价数据」都依赖它。任何列变更须同步更新
--       tests/fixtures/steady_raw_schema.sql 并让 tests/test_schema_parity.py 保持绿。
--
-- schema_migrations 台账由 scripts/migrate.sh 建立，**不**写在本文件。
-- 本文件仅首次建库时执行（scripts/init-db.sh）；后续 schema 变更走
-- deploy/migrations/NNN_*.sql（幂等），由 scripts/migrate.sh 补齐。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 股票基本信息
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
-- 2. 日行情
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
-- 3. 财务指标（含公告日，防止未来函数）
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
-- 3.5 每日估值（日度 PE/PB/市值）
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
-- 4. 交易日历
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_calendar (
    cal_date DATE         PRIMARY KEY,
    is_open  BOOLEAN      NOT NULL,
    exchange VARCHAR(10)  DEFAULT 'SSE'
);

-- ------------------------------------------------------------
-- 5. 市场热点快照（早盘简报用）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_hotspot (
    spot_date DATE        PRIMARY KEY,
    sections  JSONB       NOT NULL,   -- {indices, sectors_gain, sectors_flow, hot_stocks}
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------
-- 6. 采集台账（同构 steady：task_name+run_date 幂等、detail 结构化）
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
