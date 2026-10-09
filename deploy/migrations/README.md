# datahub 迁移目录

存放 datahub 库的**幂等**迁移文件（`NNN_*.sql`），由 `scripts/migrate.sh` 按文件名顺序应用，并在 `schema_migrations` 台账登记已应用项。

- **建库初值不在此**：原始表 schema 在 `deploy/postgres/init.sql`（由 `scripts/init-db.sh` 一次性执行）。
- 因 `init.sql` 抽取自 steady HEAD（已含 001–007 迁移的效果），本目录**起始为空**。
- 后续任何列变更：在此新增 `NNN_*.sql`（`ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`），
  并**同步更新** `tests/fixtures/steady_raw_schema.sql`，保持 `tests/test_schema_parity.py` 绿。

> ⚠️ 本目录只用 `.sql` 后缀放迁移——`migrate.sh` 以 `*.sql` 通配，任何 `.sql` 文件都会被尝试应用。
> 说明文档请用 `.md`。
