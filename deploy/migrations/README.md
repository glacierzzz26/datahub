# datahub 迁移目录

存放 datahub 库的**幂等**迁移文件（`NNN_*.sql`），由 `scripts/migrate.sh` 按文件名顺序应用，并在 `schema_migrations` 台账登记已应用项。

- **建库初值不在此**：原始表 schema 在 `deploy/postgres/init.sql`（由 `scripts/init-db.sh` 一次性执行）。
- 因 `init.sql` 抽取自 steady HEAD（已含 001–007 迁移的效果），本目录**起始为空**。
- 后续列变更：在此新增 `NNN_*.sql`（`ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`）。
  **按表分类**：
  - **steady 镜像表**（`stock_basic`/`daily_price`/`daily_valuation`/`financial_indicator`/
    `trade_calendar`/`market_hotspot`/`task_run`）：**改列**须**同步更新**
    `tests/fixtures/steady_raw_schema.sql`，保持 `tests/test_schema_parity.py` 绿
    （该表集 = `STEADY_MIRRORED_TABLES`）。
    - **例外——纯性能索引**：镜像表上只加索引（与数据等价无关）时，可**只走迁移、不写 `init.sql`**
      （否则须同步改 fixture，而 fixture 是 steady 冻结副本，得 steady 也有该索引才诚实）。
      此时不破逐字 parity（比对的是 `init.sql`，其未变）。先例 `003`（`daily_valuation` 的
      `trade_date` 索引）。
  - **datahub 自有表**（如 `financial_indicator_ext`）：steady **无**对应表，**不动** fixture
    （不纳入 parity 比对），仅由 `tests/test_orm_matches_ddl.py` 守 ORM↔DDL。
    `init.sql` 表集 == `STEADY_MIRRORED_TABLES | DATAHUB_OWNED_TABLES`。

> ⚠️ 本目录只用 `.sql` 后缀放迁移——`migrate.sh` 以 `*.sql` 通配，任何 `.sql` 文件都会被尝试应用。
> 说明文档请用 `.md`。
