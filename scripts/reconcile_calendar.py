"""calendar 对账（datahub Phase 2 §5）：datahub.trade_calendar vs steady.trade_calendar。

用途：灰度切 calendar 前的放行门——datahub 采集落库后，与 steady 既有日历**逐位比对**，
零偏差（或偏差已 classify 归因并知情接受）才把 calendar 的采集权切给 datahub。

**PG 不能跨库查询**：脚本开**两个引擎**（各库一连接），分别取最近 N 行，按 `cal_date`
在 Python 侧比对。窗口错位（一侧多/少）即体现为 `false_pos` / `db_anomaly` 分类。

用法（仓库根目录）：
    DB_HOST=127.0.0.1 DB_USER=quant DB_PASSWORD=... DB_NAME=datahub \
    STEADY_DB_NAME=quant_system \
    python scripts/reconcile_calendar.py [--limit 60]

- datahub 侧读 `DB_*`（默认库 `datahub`）；
- steady 侧读 `STEADY_DB_*`（默认库 `quant_system`）；未设的项回退到 `DB_*`
  （同实例不同库的常见拓扑）。
- 任一侧有非 `accepted` 分类 → 退出码 1（放行门）；`--allow-drift` 强制 0。

**历史拉取不占当日采集窗、不与 steady 争抢上游**（蓝图 §5）。
"""
import argparse
import os
import sys
from collections import Counter, defaultdict

# 对账分类词表（对齐仓库 rewrite_adj_factor.classify 的五类范式）：
#   accepted   → 双侧都有且逐位相等（可切）
#   drifted    → 双侧都有但值不同（口径/源分歧）
#   db_anomaly → 仅 steady 有（datahub 缺 = 抽取 bug）
#   false_pos  → 仅 datahub 有（多产）
#   rejected   → 不可比（值缺失/无法解析）
CATEGORIES = ("accepted", "drifted", "db_anomaly", "false_pos", "rejected")


def classify_calendar(dh, steady) -> str:
    """逐日分类（纯函数，便于单测）。

    dh/steady：`(is_open, exchange)` 元组；缺该日 = None。
    """
    if dh is None and steady is None:
        return "rejected"           # 不可比（理论不出现：遍历的是并集）
    if dh is None:
        return "db_anomaly"         # 仅 steady 有 → datahub 缺（抽取 bug）
    if steady is None:
        return "false_pos"          # 仅 datahub 有 → 多产
    if dh == steady:
        return "accepted"
    return "drifted"


def _dsn(prefix: str, default_name: str) -> str:
    """构造 DSN；未设项回退到 `DB_*`（同实例不同库拓扑）。"""
    host = os.getenv(prefix + "HOST") or os.getenv("DB_HOST", "localhost")
    port = os.getenv(prefix + "PORT") or os.getenv("DB_PORT", "5432")
    user = os.getenv(prefix + "USER") or os.getenv("DB_USER", "quant")
    password = os.getenv(prefix + "PASSWORD") or os.getenv("DB_PASSWORD", "")
    name = os.getenv(prefix + "NAME", default_name)
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


def fetch_recent(engine, limit: int) -> dict:
    """取该库最近 `limit` 行 → {cal_date: (is_open, exchange)}。"""
    from sqlalchemy import text

    with engine.connect() as c:
        rows = c.execute(
            text("SELECT cal_date, is_open, exchange FROM trade_calendar"
                 " ORDER BY cal_date DESC LIMIT :n"),
            {"n": limit},
        ).all()
    return {r[0]: (bool(r[1]), r[2]) for r in rows}


def compare(dh_rows: dict, steady_rows: dict) -> tuple[Counter, dict]:
    """按 cal_date 并集逐位分类 → (计数, {分类: [(日期, dh, steady)]})。"""
    counts: Counter = Counter()
    detail: dict = defaultdict(list)
    for cal_date in sorted(set(dh_rows) | set(steady_rows)):
        cat = classify_calendar(dh_rows.get(cal_date), steady_rows.get(cal_date))
        counts[cat] += 1
        if cat != "accepted":
            detail[cat].append((cal_date, dh_rows.get(cal_date),
                                steady_rows.get(cal_date)))
    return counts, detail


def main() -> int:
    ap = argparse.ArgumentParser(description="calendar 对账（datahub vs steady）")
    ap.add_argument("--limit", type=int, default=60, help="各库取最近 N 行（默认 60）")
    ap.add_argument("--allow-drift", action="store_true",
                    help="有非 accepted 分类也返回 0（仅报告）")
    args = ap.parse_args()

    from sqlalchemy import create_engine

    dh_engine = create_engine(_dsn("DB_", "datahub"))
    steady_engine = create_engine(_dsn("STEADY_DB_", "quant_system"))
    dh_rows = fetch_recent(dh_engine, args.limit)
    steady_rows = fetch_recent(steady_engine, args.limit)

    counts, detail = compare(dh_rows, steady_rows)
    print(f"datahub={len(dh_rows)} 行  steady={len(steady_rows)} 行  "
          f"窗口={args.limit}")
    for cat in CATEGORIES:
        print(f"  {cat:10s} {counts.get(cat, 0)}")
    for cat, rows in detail.items():
        for cal_date, dh, steady in rows[:20]:
            print(f"  [{cat}] {cal_date}  datahub={dh}  steady={steady}")

    bad = sum(counts.get(c, 0) for c in CATEGORIES if c != "accepted")
    if bad and not args.allow_drift:
        print(f"对账未通过：{bad} 条非 accepted", file=sys.stderr)
        return 1
    print("对账通过（窗口内逐位一致）" if not bad else "存在偏差（--allow-drift）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
