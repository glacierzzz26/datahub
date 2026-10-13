-- P14-1（research 数值域）第二批：8 张 datahub **自有**表
--
-- research/src/providers/* 现直调 akshare 的 8 个数据域，在此落表（非 steady 镜像，
-- 不进 tests/fixtures/steady_raw_schema.sql）。列**照 research 现用 akshare 输出冻结**。
-- 各表**不**设 FK 到 stock_basic（覆盖全 A 含 BJ 与池外码，FK 会误拒）。
--
-- 与 deploy/postgres/init.sql 的 §7 段一致（首次建库走 init.sql，存量库走本迁移）。
-- 幂等：全部 IF NOT EXISTS，可重复执行（见 deploy/migrations/README.md）。

-- 7.1 龙虎榜记录
CREATE TABLE IF NOT EXISTS capital_flow (
    id            BIGSERIAL     PRIMARY KEY,
    code          VARCHAR(10)   NOT NULL,
    trade_date    DATE          NOT NULL,
    name          VARCHAR(50),
    close         DECIMAL(10,2),
    pct_change    DECIMAL(10,4),
    turnover_rate DECIMAL(10,4),
    reasons       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_capital_flow_code_date
    ON capital_flow (code, trade_date);

-- 7.2 龙虎榜明细
CREATE TABLE IF NOT EXISTS capital_flow_detail (
    id          BIGSERIAL     PRIMARY KEY,
    code        VARCHAR(10)   NOT NULL,
    trade_date  DATE          NOT NULL,
    dealer_name VARCHAR(120)  NOT NULL,
    buy_amount  DECIMAL(18,2) NOT NULL DEFAULT 0,
    buy_pct     DECIMAL(10,4),
    sell_amount DECIMAL(18,2) NOT NULL DEFAULT 0,
    sell_pct    DECIMAL(10,4),
    net_amount  DECIMAL(18,2),
    reason      VARCHAR(40)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_capital_flow_detail
    ON capital_flow_detail (code, trade_date, dealer_name, buy_amount, sell_amount);

-- 7.3 申万行业指数日线（不存量价）
CREATE TABLE IF NOT EXISTS industry_index (
    id         BIGSERIAL     PRIMARY KEY,
    code       VARCHAR(10)   NOT NULL,
    trade_date DATE          NOT NULL,
    open       DECIMAL(12,2),
    high       DECIMAL(12,2),
    low        DECIMAL(12,2),
    close      DECIMAL(12,2),
    pct_change DECIMAL(10,4),
    amplitude  DECIMAL(10,4),
    chg_amount DECIMAL(12,4)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_industry_index_code_date
    ON industry_index (code, trade_date);

-- 7.4 申万行业分类信息
CREATE TABLE IF NOT EXISTS industry_sw_info (
    id             BIGSERIAL     PRIMARY KEY,
    code           VARCHAR(10)   NOT NULL,
    level          INTEGER       NOT NULL,
    name           VARCHAR(50),
    level_label    VARCHAR(20),
    parent_name    VARCHAR(50),
    member_count   INTEGER,
    pe_static      DECIMAL(12,4),
    pe_ttm         DECIMAL(12,4),
    pb             DECIMAL(12,4),
    dividend_yield DECIMAL(10,4)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_industry_sw_info_level_code
    ON industry_sw_info (level, code);

-- 7.5 申万三级成分股
CREATE TABLE IF NOT EXISTS industry_sw_cons (
    id                BIGSERIAL     PRIMARY KEY,
    index_code        VARCHAR(10)   NOT NULL,
    stock_code        VARCHAR(10)   NOT NULL,
    stock_name        VARCHAR(50),
    market_cap        DECIMAL(18,4),
    pe_ttm            DECIMAL(12,4),
    pb                DECIMAL(12,4),
    roe               DECIMAL(10,4),
    dividend_yield    DECIMAL(10,4),
    net_profit_growth DECIMAL(10,4),
    revenue_growth    DECIMAL(10,4)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_industry_sw_cons
    ON industry_sw_cons (index_code, stock_code);

-- 7.6 宏观指标序列
CREATE TABLE IF NOT EXISTS macro (
    id           BIGSERIAL     PRIMARY KEY,
    indicator    VARCHAR(16)   NOT NULL,
    period_label VARCHAR(40)   NOT NULL,
    period_end   DATE          NOT NULL,
    cadence      VARCHAR(8)    NOT NULL,
    level        DECIMAL(18,4),
    yoy          DECIMAL(10,4),
    mom          DECIMAL(10,4)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_macro_indicator_period
    ON macro (indicator, period_label);

-- 7.7 个股新闻
CREATE TABLE IF NOT EXISTS news_by_stock (
    id           BIGSERIAL     PRIMARY KEY,
    code         VARCHAR(10)   NOT NULL,
    title        TEXT,
    content      TEXT,
    published_at TIMESTAMP     NOT NULL,
    source       VARCHAR(40),
    url          VARCHAR(500),
    content_hash VARCHAR(64)   NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_news_by_stock
    ON news_by_stock (code, published_at, content_hash);

-- 7.8 个股公告
CREATE TABLE IF NOT EXISTS announcement_by_stock (
    id           BIGSERIAL     PRIMARY KEY,
    code         VARCHAR(10)   NOT NULL,
    title        VARCHAR(300),
    category     VARCHAR(40),
    publish_date DATE,
    url          VARCHAR(500)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_announcement_by_stock
    ON announcement_by_stock (code, publish_date, title);
