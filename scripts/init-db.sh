#!/usr/bin/env bash
# ===== datahub 建库（一次性，幂等）=====
# 在**复用生产 PG 实例**上创建 datahub 独立库，并施加原始表 schema（deploy/postgres/init.sql）。
#
# 用法（生产，经 ssh；或本地开发对一次性测试容器）：
#   ./scripts/init-db.sh
#
# 背景：init.sql 只在 initdb 时自动跑于**全新**集群；生产集群是已存在的 →
#       须显式 CREATE DATABASE + 施加 schema。两者都幂等（IF NOT EXISTS），可重复执行。
# 依赖：运行中的 quant-postgres 容器（PG_CONTAINER 可覆盖）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

for envf in "$PWD/.env" "$SCRIPT_DIR/../.env"; do
  if [ -f "$envf" ]; then
    # shellcheck disable=SC2046
    export $(grep -E '^(DB_USER|DB_NAME)=' "$envf" | xargs)
  fi
done
DB_USER="${DB_USER:-quant}"
DB_NAME="${DB_NAME:-datahub}"
PG_CONTAINER="${PG_CONTAINER:-quant-postgres}"
INIT_SQL="$SCRIPT_DIR/../deploy/postgres/init.sql"

[ -f "$INIT_SQL" ] || { echo "❌ 找不到 $INIT_SQL"; exit 1; }

# 1. 建库（幂等）。CREATE DATABASE 不能处于事务块中 —— 故走单独 -c（psql 默认 autocommit）。
if docker exec "$PG_CONTAINER" psql -U "$DB_USER" -d postgres -tAc \
     "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
  echo "  - 库 $DB_NAME 已存在，跳过创建"
else
  echo "  + 创建库 $DB_NAME（OWNER $DB_USER）..."
  docker exec "$PG_CONTAINER" psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "CREATE DATABASE \"$DB_NAME\" OWNER \"$DB_USER\""
fi

# 2. 施加原始表 schema（全 IF NOT EXISTS，幂等）
echo "  + 施加原始表 schema（$INIT_SQL）"
cat "$INIT_SQL" | docker exec -i "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q -v ON_ERROR_STOP=1
echo "✅ 建库完成：$DB_NAME（容器 $PG_CONTAINER）。后续 schema 变更走 scripts/migrate.sh"
