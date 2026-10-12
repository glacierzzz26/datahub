#!/usr/bin/env bash
# ===== 历史底种：steady → datahub 直接库拷贝（幂等）=====
#
# 背景：datahub 库与 steady 库**同处一个 PG 实例**（容器 quant-postgres）→ 历史全量用
#       `pg_dump --data-only | psql` 直接拷贝，**逐位精确、分钟级、零对账漂移**，
#       不做逐股回采。采集器此后只负责①切换后每日增量；②可选回采近端校验。
#
# 用法（生产，经 ssh 到 PG 主机）：
#   ./scripts/seed_from_steady.sh                # 拷贝全部 4 张表（默认）
#   SEED_TABLES="daily_price daily_valuation" ./scripts/seed_from_steady.sh
#   DRY_RUN=1 ./scripts/seed_from_steady.sh      # 只打印将执行的 SQL，不落库
#
# 语义：
#   ① 目标库（datahub）`TRUNCATE <表…> CASCADE` —— **只动目标库**，绝不碰源库；
#   ② **FK 顺序**：先 stock_basic（含 market='INDEX' 伪行，满足子表 FK），再子表；
#      逐表 `pg_dump --data-only --no-owner --table=public.<t>`（COPY **显式列名**，抗列序漂移）
#      → `psql` 导入；
#   ③ 三张 BIGSERIAL 表 `setval(pg_get_serial_sequence(...), MAX(id)+1, false)` —— 漏做则
#      后续采集 insert 主键冲突（stock_basic 无序列，跳过）。
#
# 依赖：运行中的 quant-postgres 容器（PG_CONTAINER 可覆盖）、容器内 `pg_dump`/`psql`。
# 幂等：可重复执行（先 TRUNCATE 再导入，结果恒等）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

for envf in "$PWD/.env" "$SCRIPT_DIR/../.env"; do
  if [ -f "$envf" ]; then
    # shellcheck disable=SC2046
    export $(grep -E '^(DB_USER)=' "$envf" | xargs)
  fi
done

DB_USER="${DB_USER:-quant}"
SRC_DB="${STEADY_DB_NAME:-quant_system}"   # 源：拷贝自
DST_DB="${DB_NAME:-datahub}"               # 目标：拷贝到
PG_CONTAINER="${PG_CONTAINER:-quant-postgres}"
DRY_RUN="${DRY_RUN:-}"

# 顺序关键：stock_basic 先（子表 FK 依赖），其余为 BIGSERIAL 子表。
SEED_TABLES="${SEED_TABLES:-stock_basic daily_price daily_valuation financial_indicator}"
SERIAL_TABLES="daily_price daily_valuation financial_indicator"  # 需 setval 的表

[ "$SRC_DB" != "$DST_DB" ] || { echo "❌ 源库与目标库同为 $SRC_DB，拒绝执行"; exit 1; }

_psql() {  # $1=库名，其余=psql 参数
  local database="$1"; shift
  docker exec -i "$PG_CONTAINER" psql -U "$DB_USER" -d "$database" -v ON_ERROR_STOP=1 -q "$@"
}

run() {  # DRY_RUN 时只回显
  if [ -n "$DRY_RUN" ]; then echo "  [dry-run] $*"; else eval "$@"; fi
}

echo "底种：$SRC_DB → $DST_DB（容器 $PG_CONTAINER，用户 $DB_USER）"
echo "表（FK 顺序）：$SEED_TABLES"

# 1. 清空目标库的表（CASCADE 处理彼此 FK；只锁目标库）
TRUNCATE_SQL="TRUNCATE $(echo "$SEED_TABLES" | tr ' ' ',') CASCADE;"
echo "  + TRUNCATE 目标库：$TRUNCATE_SQL"
run "_psql '$DST_DB' -c \"$TRUNCATE_SQL\""

# 2. 逐表拷贝（COPY 显式列名 → 源/目标列序漂移也不串列）
for t in $SEED_TABLES; do
  echo "  + $SRC_DB.$t → $DST_DB.$t"
  run "docker exec '$PG_CONTAINER' pg_dump -U '$DB_USER' -d '$SRC_DB' \
        --data-only --no-owner --no-acl --table='public.$t' \
       | docker exec -i '$PG_CONTAINER' psql -U '$DB_USER' -d '$DST_DB' -v ON_ERROR_STOP=1 -q"
done

# 3. 重置 BIGSERIAL 序列（空表 → 下次 nextval=1；非空 → MAX(id)+1）
for t in $SERIAL_TABLES; do
  case " $SEED_TABLES " in *" $t "*) ;; *) continue ;; esac
  SETVAL_SQL="SELECT setval(pg_get_serial_sequence('$t','id'), COALESCE((SELECT MAX(id) FROM $t), 0) + 1, false);"
  echo "  + setval $t.id"
  run "_psql '$DST_DB' -c \"$SETVAL_SQL\""
done

echo "✅ 底种完成。下一步：对账放行门 →"
echo "   STEADY_DB_NAME=$SRC_DB python scripts/reconcile_table.py \\"
for t in $SEED_TABLES; do echo "     --table $t --key ... --vals ...   # 逐表放行"; done
echo "   （copy 模式须逐位全等，零偏差方可翻闸）"
