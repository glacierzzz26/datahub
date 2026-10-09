# datahub（数据接入层 · Phase 1）

独立采集/取数服务：把外部增强数据（热点 / 行业）统一到一个入口，对外经
**MCP（Streamable HTTP）+ 原生 HTTP** 双出口提供，带 **Bearer token 鉴权**。

- **本阶段（Phase 1）**：一个独立服务、**不建自有库**（按需抓 + 缓存）、
  **steady 零改动**（两条线并行、互不依赖）。
- **Phase 2（进行中）**：**建自有库**（复用生产 PG 实例的独立库 `datahub`）+ 搬核心采集栈
  + 逐数据集灰度切换。当前已落地**建库地基**（见「数据库」段），采集栈/raw 出口随后续 PR。
- **上位设计**：steady 仓库 `docs/phase2/design/数据接入层-datahub.md`（总设计）、
  `…-datahub-phase1.md`（本阶段蓝图）、`…-datahub-phase2.md`（建库 + 接管核心采集）。

---

## 出口

| 出口 | 地址 | 鉴权 |
|---|---|---|
| 健康探针 | `GET /v1/healthz` | 免鉴权 |
| 数据集清单 | `GET /v1/datasets` | Bearer |
| 取数 | `GET /v1/datasets/{id}?param=…` | Bearer |
| MCP（Streamable HTTP） | `POST /mcp` | —（见下）|

- **HTTP 响应信封**：`{code, message, data, meta}`（错误同构：`code` 为 HTTP 状态码）。
  `meta` 含 `stale`/`cached`/`rows`。
- **MCP**：每个数据集一个 tool（`hotspot.indices` → `hotspot_indices`）+ `list_datasets`；
  tool 的 `inputSchema` 由数据集 params 机械生成。
- **MCP Host 白名单**：`DATAHUB_MCP_ALLOWED_HOSTS` 须带 `:*`（默认
  `localhost,localhost:*,127.0.0.1,127.0.0.1:*`）——真实客户端 Host 头含端口，SDK 只认
  `host:*` 通配，漏配会一律 **421**（DNS-rebinding 误杀）。跨机暴露时按拓扑追加。
- 两出口共享内核 `service.get_dataset`（**零逻辑重复**）。

### Phase 1 数据集

| id | 参数 | 列 | TTL |
|---|---|---|---|
| `hotspot.indices` | — | name, code, close, change_pct | 300s |
| `hotspot.sectors_gain` | `top` | name, change_pct, leader | 300s |
| `hotspot.sectors_flow` | `top` | name, net_inflow | 300s |
| `hotspot.hot_stocks` | `top` | rank, code, name, change_pct, board_days, industry | 300s |
| `industry.catalog` | — | name, code, change_pct | 86400s |
| `industry.members` | `industry`(必) | code, name | 86400s |

> 列名照 collector `hotspot.py` 实际输出**逐字冻结**。

### 数据源现状（2026-10-09 实测冻结）

- **东财 board 接口在目标环境稳定不可达**（`RemoteDisconnected`），故 `industry.catalog`
  首源取**同花顺**（`stock_board_industry_name_ths` + `stock_board_industry_summary_ths`），
  东财兜底。
- `industry.members` 只有东财 `stock_board_industry_cons_em`（akshare 唯一成分接口），
  无同花顺替代 → **不可达时返回 503**（已知实现受限，非代码缺陷；翻闸前须确认该源可达）。
- **空结果即失败**：数据集整体取空（全源失败）→ 抛错，服务层转 **stale**（有旧值）或
  **503**，**绝不静默返回空数组**（蓝图 §5 / §11）。

---

## 闸门（部署零行为变更）

外部采集由**双闸门**控制，默认**全关** —— 部署本服务不会发任何外部请求：

```
DATAHUB_EXT_ENABLED=            # 总开关（空/0 = 关）
DATAHUB_EXT_DATASETS=           # 数据集白名单（逗号）
```

未翻闸时，对应数据集返回 **503**，且**不发外部请求**。翻闸示例：

```
DATAHUB_EXT_ENABLED=1
DATAHUB_EXT_DATASETS=hotspot.indices,hotspot.sectors_gain,industry.catalog
```

---

## 数据库（Phase 2 · 建库地基）

Phase 2 起 datahub **建自有库**——复用生产 PG 实例里的**独立库 `datahub`**，承载原始采集表。
本轮仅落地**建库地基**（采集栈与 raw 出口在后续 PR）。

- **schema 真源**：`deploy/postgres/init.sql`（**仅原始表**：`stock_basic` / `daily_price` /
  `daily_valuation` / `financial_indicator` / `trade_calendar` / `market_hotspot` / `task_run`）。
  列/类型/主键/唯一键/索引**逐字对齐 steady**，由 `tests/test_schema_parity.py` 对
  `tests/fixtures/steady_raw_schema.sql`（vendor 冻结副本）断言。
- **建库**（一次性，幂等）：`./scripts/init-db.sh`——在 `quant-postgres` 容器上
  `CREATE DATABASE datahub` + 施加 `init.sql`。
- **迁移**（幂等）：`./scripts/migrate.sh` 应用 `deploy/migrations/*.sql`（`schema_migrations` 台账）；
  `./scripts/migrate.sh --check` 仅报告列漂移。
- **DB 环境变量**：`DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME`（默认库名 `datahub`）。
  容器内 `DB_HOST=host.docker.internal`（compose `extra_hosts` 提供），本地开发 `127.0.0.1`。
- ⚠️ **维护约定**：steady 原始表 schema 变更时，须**手工 re-vendor**
  `tests/fixtures/steady_raw_schema.sql`（逐字拷贝 + 更新头注 commit）+ 在 datahub 补一条
  `deploy/migrations/NNN_*.sql`，二者与 parity 测试在同一 PR 内改。

---

## 鉴权

- 请求头 `Authorization: Bearer <DATAHUB_TOKEN>`；缺失/错误 → **401**。
- `DATAHUB_TOKEN` 未配置时**一律拒绝**（fail-closed，不放行空口令）。
- token 属密钥：写进 `.env`（`chmod 600`），不进仓库、不进日志明文。
- `/v1/healthz` 免鉴权；`/v1/datasets*` 与 MCP tool 调用按需收紧（MCP 鉴权面见蓝图 §13.5，Phase 1 先靠网络面 + token）。

---

## 本地起法

### 直接跑
```bash
cp .env.example .env          # 填 DATAHUB_TOKEN
chmod 600 .env
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m app.server          # 默认 0.0.0.0:8100
```

自检：
```bash
T=$(grep -oP 'DATAHUB_TOKEN=\K.*' .env)
curl -s localhost:8100/v1/healthz
curl -s -H "Authorization: Bearer $T" localhost:8100/v1/datasets | head
```

### Docker Compose
```bash
APP_VERSION=v0.0.0 GIT_SHORT=$(git rev-parse --short HEAD) docker compose up -d --build
```

---

## 测试

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q
```

用例覆盖：数据集契约 / 鉴权 / 源链降级与限流冷却 / 缓存 TTL 与单飞 / MCP tool 清单与
端到端 `initialize`。

---

## 目录

```
app/
├── config.py          # DATAHUB_* env（双闸门 ext_enabled）
├── auth.py            # Bearer 鉴权依赖
├── cache.py           # TTL 缓存 + 单飞
├── ratelimit.py       # 信号量 + 最小间隔 + 黑名单冷却
├── service.py         # 共享内核 get_dataset（闸门→缓存→源链降级→兜底）
├── http_api.py        # /v1/... 路由
├── mcp_facade.py      # FastMCP 实例 + 由注册表生成 tools
├── server.py          # 入口：装配 FastAPI + 挂载 MCP + /healthz
├── datasets/          # spec / registry / external（Phase 1 数据集）
└── providers/         # base（with_timeout）/ net（超时补丁）/ registry / ext（取数）

deploy/
├── postgres/init.sql  # 原始表 schema（仅采集表；逐字对齐 steady）
└── migrations/        # 幂等迁移（schema_migrations 台账）
scripts/
├── init-db.sh         # 一次性建库（CREATE DATABASE + 施加 init.sql）
└── migrate.sh         # 迁移应用 + --check 列漂移检测
```

---

## 版本与部署标识

未发版恒为 `v0.0.0`；镜像标签 `<name>:v0.0.0-<短hash>`，镜像内烘焙
`APP_VERSION` + `GIT_SHORT`，生产核验以此二者确认版本。
