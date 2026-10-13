-- P14-1（research 数值域）：financial_indicator_ext 侧表
--
-- datahub **自有**表（非 steady 镜像）：承载 research 需要的 net_margin（销售净利率）。
-- steady 镜像表 financial_indicator 无此列，为保 steady 逐字 parity，另立侧表，
-- 读时由 financial_indicator 数据集的 db provider left-join 出 net_margin。
--
-- 幂等：ADD/IF NOT EXISTS，可重复执行（见 deploy/migrations/README.md）。
CREATE TABLE IF NOT EXISTS financial_indicator_ext (
    id          BIGSERIAL     PRIMARY KEY,
    code        VARCHAR(10)   NOT NULL REFERENCES stock_basic (code),
    report_date DATE          NOT NULL,
    net_margin  DECIMAL(10,4),
    created_at  TIMESTAMP     DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_financial_ext_code_report
    ON financial_indicator_ext (code, report_date);
