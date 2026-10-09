#!/usr/bin/env bash
# ===== datahub 数据库迁移脚本（幂等，生产/开发通用）=====
# 用法：
#   ./scripts/migrate.sh             # apply：按序应用 deploy/migrations/*.sql（schema_migrations 台账）
#   ./scripts/migrate.sh --check     # 仅检查：init.sql 定义的列 vs 现库，报告缺列漂移
#
# 移植自 steady scripts/migrate.sh（台账 + 按序 + 单事务 + awk 漂移检测），
# 仅改：默认库名 datahub；INIT_SQL/迁移目录指向 datahub 自己的 deploy/。
#
# 依赖：运行中的 quant-postgres 容器（**复用生产 PG 实例**，datahub 是其中的独立库；
#       docker exec 走容器内本地 trust，免密码）。容器名可用 PG_CONTAINER 覆盖。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ---- 定位资源（本地仓库 / VM 解压目录两种布局）----
if [ -d "$SCRIPT_DIR/../deploy/migrations" ]; then
  MIG_DIR="$SCRIPT_DIR/../deploy/migrations"
elif [ -d "$SCRIPT_DIR/../migrations" ]; then
  MIG_DIR="$SCRIPT_DIR/../migrations"
else
  echo "❌ 找不到 migrations 目录（期望 ../deploy/migrations 或 ../migrations）"; exit 1
fi
if [ -f "$SCRIPT_DIR/../deploy/postgres/init.sql" ]; then
  INIT_SQL="$SCRIPT_DIR/../deploy/postgres/init.sql"
elif [ -f "$SCRIPT_DIR/../postgres/init.sql" ]; then
  INIT_SQL="$SCRIPT_DIR/../postgres/init.sql"
else
  INIT_SQL=""
fi

# ---- 数据库凭据（默认 quant/datahub；有 .env 则读取覆盖）----
for envf in "$PWD/.env" "$SCRIPT_DIR/../.env"; do
  if [ -f "$envf" ]; then
    # shellcheck disable=SC2046
    export $(grep -E '^(DB_USER|DB_NAME)=' "$envf" | xargs)
  fi
done
DB_USER="${DB_USER:-quant}"
# datahub 是复用生产 PG 实例的**独立库**（默认 datahub，非 steady 的 quant_system）
DB_NAME="${DB_NAME:-datahub}"
# 目标 postgres 容器名（复用生产实例；可覆盖以指向一次性测试容器）
PG_CONTAINER="${PG_CONTAINER:-quant-postgres}"

wait_postgres() {
  local i
  for i in $(seq 1 30); do
    if docker exec "$PG_CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "❌ 等待 postgres 就绪超时（容器 $PG_CONTAINER 未运行？库 $DB_NAME 是否存在？）"; return 1
}

check_drift() {
  [ -n "$INIT_SQL" ] || { echo "⚠️ 找不到 init.sql，跳过漂移检查"; return 0; }
  # awk：解析每个 CREATE TABLE IF NOT EXISTS 块内的列名（跳过 CONSTRAINT/PRIMARY/UNIQUE/FOREIGN/CHECK 行），
  #       输出 "表|列"；随后与 information_schema 比对，列出 init.sql 有而现库缺失的列。
  local want have missing=0
  want=$(awk '
    /CREATE TABLE IF NOT EXISTS/ {
      t=$0; sub(/^.*CREATE TABLE IF NOT EXISTS /,"",t); sub(/ .*/,"",t); gsub(/"/,"",t)
      table=t; next
    }
    table != "" {
      line=$0; sub(/^[ \t]+/,"",line)
      if (line ~ /^\)/) { table=""; next }
      first=line; sub(/ .*/,"",first)
      if (first=="CONSTRAINT" || first=="PRIMARY" || first=="UNIQUE" || first=="FOREIGN" || first=="CHECK") next
      col=line; sub(/[ \t].*/,"",col)
      if (col != "") print table "|" col
    }' "$INIT_SQL")
  have=$(docker exec "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -F'|' -c \
    "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public'")
  while IFS='|' read -r t c; do
    [ -z "$t" ] && continue
    if ! grep -qxF "$t|$c" <<<"$have"; then
      echo "  ⚠️ 缺列：$t.$c"
      missing=1
    fi
  done <<<"$want"
  if [ "$missing" = 1 ]; then
    echo "❌ 检测到 schema 漂移：请新增 deploy/migrations/NNN_*.sql 幂等迁移后重跑 apply"
    return 1
  fi
  echo "✅ 无缺列漂移（init.sql 定义的列现库全部存在）"
  return 0
}

apply() {
  wait_postgres
  echo "==> 数据库迁移（$MIG_DIR）"
  docker exec "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q -v ON_ERROR_STOP=1 -c \
    "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
  local applied v ok=0 n=0
  applied=$(docker exec "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -c "SELECT version FROM schema_migrations")
  for f in "$MIG_DIR"/*.sql; do
    [ -f "$f" ] || continue
    v="$(basename "$f")"
    n=$((n+1))
    if grep -qxF "$v" <<<"$applied"; then
      echo "  - $v  已应用，跳过"
      continue
    fi
    echo "  + $v  应用..."
    if cat "$f" | docker exec -i "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" --single-transaction -q -v ON_ERROR_STOP=1; then
      docker exec "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q -v ON_ERROR_STOP=1 -c \
        "INSERT INTO schema_migrations(version) VALUES ('$v')" >/dev/null
      echo "    ✔ 完成"
      ok=$((ok+1))
    else
      echo "    ❌ 应用失败（单事务已回滚），修复 $v 后重跑"
      return 1
    fi
  done
  if [ "$n" = 0 ]; then
    echo "  （migrations 目录为空，无迁移需应用）"
  else
    echo "==> 迁移完成：应用 $ok 条，跳过 $((n-ok)) 条"
  fi
  # 迁移后顺带核验一遍 init.sql 与现库列一致性（有漂移仅告警，不阻塞部署）
  check_drift || true
}

case "${1:-}" in
  --check) check_drift ;;
  "" | apply) apply ;;
  *) echo "用法: $0 [apply|--check]"; exit 1 ;;
esac
