-- daily_valuation 独立 trade_date 索引（读侧加固）
--
-- 背景：`dup_keys(daily_valuation)` 及估值窗查询按 `trade_date` 过滤，而 daily_valuation
-- 只有唯一键 `uq_daily_valuation_code_date (code, trade_date)`（前导列 code），
-- 无独立 trade_date 索引 → 纯日期窗（无 codes）退化为 9.28M 行 seq scan（冷扫 ~5.5s）。
-- daily_price 早有同类索引 `idx_daily_price_trade_date`（由 steady 迁移 007 引入并镜像），
-- 本迁移补齐 daily_valuation 侧，使估值窗也走索引。
--
-- 口径（**重要**）：daily_valuation 属 STEADY_MIRRORED_TABLES，`deploy/postgres/init.sql`
-- 的镜像表索引与 `tests/fixtures/steady_raw_schema.sql` 逐字比对（tests/test_schema_parity.py
-- 的 test_indexes_match_fixture）。故**刻意不写进 init.sql**：纯性能索引只经本迁移施加，
-- 既不破逐字 parity、也不涉 steady 仓。deploy 恒跑 `scripts/migrate.sh apply`，新装/升级均补齐。
--
-- 幂等：IF NOT EXISTS，可重复执行（见 deploy/migrations/README.md）。
-- 注意：`migrate.sh apply` 单事务执行，故用普通 `CREATE INDEX`（非 CONCURRENTLY）；
--       施加时取 SHARE 锁会**暂阻写**，请在低写窗执行（避开 18:10 采集）。
CREATE INDEX IF NOT EXISTS idx_daily_valuation_trade_date
    ON daily_valuation (trade_date);
