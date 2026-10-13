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
-- §7 段另有 **datahub 自有**研报表（capital_flow / industry_index / macro / …，
-- 见 §7 与 deploy/migrations/002_research_datasets.sql）——**非** steady 镜像，
-- 不进 tests/fixtures/steady_raw_schema.sql，逐字 parity 不适用（见 tests/test_schema_parity.py
-- 的 STEADY_MIRRORED_TABLES / DATAHUB_OWNED_TABLES 划分）。
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
-- 3.2 财务扩展（datahub 自有，**非** steady 镜像）：research 数值域补口
--     net_margin（销售净利率）等 —— steady 镜像表 financial_indicator 无此列，
--     为不破 steady 逐字 parity，另立 datahub 自有侧表，读时 left-join 出 net_margin。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS financial_indicator_ext (
    id          BIGSERIAL     PRIMARY KEY,
    code        VARCHAR(10)   NOT NULL REFERENCES stock_basic (code),
    report_date DATE          NOT NULL,   -- 报告期（对齐 financial_indicator.report_date）
    net_margin  DECIMAL(10,4),            -- 销售净利率（%）
    created_at  TIMESTAMP     DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_financial_ext_code_report
    ON financial_indicator_ext (code, report_date);

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

-- ============================================================
-- 7. 研报数值域（datahub **自有**表，非 steady 镜像）
--    P14-1 ≡ phase13 P13-1：research/src/providers/* 现直调 akshare 的
--    8 个数据域在此落表，datahub 供数、research 退为消费方。
--    列**照 research 现用 akshare 输出冻结**（见各 provider 的 rename/取值列）。
--    各表**不**设 FK 到 stock_basic —— 龙虎榜/新闻/公告/申万成分覆盖全 A（含 BJ
--    与不在采集池内的码），FK 会误拒。单位一律显式（见列注）。
-- ============================================================

-- 7.1 龙虎榜记录（每 code 每日一行，多「上榜原因」合并为 reasons）
CREATE TABLE IF NOT EXISTS capital_flow (
    id            BIGSERIAL     PRIMARY KEY,
    code          VARCHAR(10)   NOT NULL,
    trade_date    DATE          NOT NULL,
    name          VARCHAR(50),
    close         DECIMAL(10,2),                 -- 收盘价（元）
    pct_change    DECIMAL(10,4),                 -- 涨跌幅（%），东财「涨跌幅」
    turnover_rate DECIMAL(10,4),                 -- 换手率（%）
    reasons       TEXT                           -- 上榜原因，多因以「；」连接
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_capital_flow_code_date
    ON capital_flow (code, trade_date);

-- 7.2 龙虎榜明细（营业部级；丢弃「连续三个交易日」聚合块，按 (dealer,buy,sell) 去重）
CREATE TABLE IF NOT EXISTS capital_flow_detail (
    id          BIGSERIAL     PRIMARY KEY,
    code        VARCHAR(10)   NOT NULL,
    trade_date  DATE          NOT NULL,
    dealer_name VARCHAR(120)  NOT NULL,          -- 交易营业部名称
    buy_amount  DECIMAL(18,2) NOT NULL DEFAULT 0, -- 买入金额（元）
    buy_pct     DECIMAL(10,4),                   -- 买入金额-占总成交比例
    sell_amount DECIMAL(18,2) NOT NULL DEFAULT 0, -- 卖出金额（元）
    sell_pct    DECIMAL(10,4),                   -- 卖出金额-占总成交比例
    net_amount  DECIMAL(18,2),                   -- 净额（元）
    reason      VARCHAR(40)                      -- 类型（买入/卖出…）
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_capital_flow_detail
    ON capital_flow_detail (code, trade_date, dealer_name, buy_amount, sell_amount);

-- 7.3 申万行业指数日线（**不存量价**：SW 指数量纲与 A 股不兼容，恒 0，不落库）
CREATE TABLE IF NOT EXISTS industry_index (
    id         BIGSERIAL     PRIMARY KEY,
    code       VARCHAR(10)   NOT NULL,           -- 申万行业指数代码（如 801010）
    trade_date DATE          NOT NULL,
    open       DECIMAL(12,2),
    high       DECIMAL(12,2),
    low        DECIMAL(12,2),
    close      DECIMAL(12,2),
    pct_change DECIMAL(10,4),                     -- 涨跌幅（%）
    amplitude  DECIMAL(10,4),                     -- 振幅（%）
    chg_amount DECIMAL(12,4)                      -- 涨跌额
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_industry_index_code_date
    ON industry_index (code, trade_date);

-- 7.4 申万行业分类信息（一/二/三级快照）
CREATE TABLE IF NOT EXISTS industry_sw_info (
    id             BIGSERIAL     PRIMARY KEY,
    code           VARCHAR(10)   NOT NULL,       -- 行业代码（如 801010）
    level          INTEGER       NOT NULL,       -- 1/2/3
    name           VARCHAR(50),
    level_label    VARCHAR(20),                  -- 一级/二级/三级
    parent_name    VARCHAR(50),                  -- 上级行业
    member_count   INTEGER,                      -- 成份个数
    pe_static      DECIMAL(12,4),                -- 静态市盈率
    pe_ttm         DECIMAL(12,4),                -- TTM(滚动)市盈率
    pb             DECIMAL(12,4),                -- 市净率
    dividend_yield DECIMAL(10,4)                 -- 静态股息率
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_industry_sw_info_level_code
    ON industry_sw_info (level, code);

-- 7.5 申万三级成分股（research 行业横截面/估值分位用）
CREATE TABLE IF NOT EXISTS industry_sw_cons (
    id                BIGSERIAL     PRIMARY KEY,
    index_code        VARCHAR(10)   NOT NULL,    -- 申万三级行业代码
    stock_code        VARCHAR(10)   NOT NULL,    -- 成分股代码
    stock_name        VARCHAR(50),
    market_cap        DECIMAL(18,4),             -- 市值（亿元）
    pe_ttm            DECIMAL(12,4),             -- 市盈率 TTM
    pb                DECIMAL(12,4),             -- 市净率
    roe               DECIMAL(10,4),             -- ROE（%）
    dividend_yield    DECIMAL(10,4),             -- 股息率（%）
    net_profit_growth DECIMAL(10,4),             -- 净利润增速（%）
    revenue_growth    DECIMAL(10,4)              -- 营收增速（%）
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_industry_sw_cons
    ON industry_sw_cons (index_code, stock_code);

-- 7.6 宏观指标序列（CPI/PPI/M2/GDP）
CREATE TABLE IF NOT EXISTS macro (
    id           BIGSERIAL     PRIMARY KEY,
    indicator    VARCHAR(16)   NOT NULL,         -- cn_cpi/cn_ppi/cn_m2/cn_gdp
    period_label VARCHAR(40)   NOT NULL,         -- 源文原文（如 2026年08月份）
    period_end   DATE          NOT NULL,         -- 统计期末
    cadence      VARCHAR(8)    NOT NULL,         -- month/quarter
    level        DECIMAL(18,4),                  -- 指标值（指数/绝对量；口径见 research）
    yoy          DECIMAL(10,4),                  -- 同比（%）
    mom          DECIMAL(10,4)                   -- 环比（%）；源无则 NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_macro_indicator_period
    ON macro (indicator, period_label);

-- 7.7 个股新闻（content_hash = sha256(title|content) 去重键，不外发）
CREATE TABLE IF NOT EXISTS news_by_stock (
    id           BIGSERIAL     PRIMARY KEY,
    code         VARCHAR(10)   NOT NULL,
    title        TEXT,
    content      TEXT,
    published_at TIMESTAMP     NOT NULL,         -- 发布时间
    source       VARCHAR(40),                    -- 文章来源
    url          VARCHAR(500),                   -- 新闻链接
    content_hash VARCHAR(64)   NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_news_by_stock
    ON news_by_stock (code, published_at, content_hash);

-- 7.8 个股公告（标题级；不进 LLM）
CREATE TABLE IF NOT EXISTS announcement_by_stock (
    id           BIGSERIAL     PRIMARY KEY,
    code         VARCHAR(10)   NOT NULL,
    title        VARCHAR(300),
    category     VARCHAR(40),                    -- 公告类型
    publish_date DATE,                           -- 公告日期
    url          VARCHAR(500)                    -- 网址
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_announcement_by_stock
    ON announcement_by_stock (code, publish_date, title);
