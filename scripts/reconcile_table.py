"""按表对账（datahub Phase 3）：datahub.<table> vs steady.<table> 逐位比对。

用途：**底种/切换放行门**。理想底种是**直接库拷贝**（`pg_dump --data-only | psql`，
见 `scripts/seed_from_steady.sh`）——此时两侧应**逐位全等**（`--mode copy`）。
若以 steady 旧值为基线校验**回采**产物（换了源/口径），用 `--mode recollect` + 数值容差。

**PG 不能跨库查询**：脚本开**两个引擎**（各库一连接），分别按锚点取窗口，在 Python 侧按
复合主键比对。窗口错位（一侧多/少）即体现为 `db_anomaly` / `false_pos`。

分类（五类，对齐 `reconcile_calendar.py`）：
  accepted   → 双侧都有且（容差内）相等
  drifted    → 双侧都有但值超容差
  db_anomaly → 仅 steady 有（datahub 缺 = 抽取/拷贝 bug）
  false_pos  → 仅 datahub 有（多产）
  rejected   → 不可比（值缺失/无法解析）

用法（仓库根目录）：
    DB_HOST=127.0.0.1 DB_USER=quant DB_PASSWORD=... DB_NAME=datahub \
    STEADY_DB_NAME=quant_system \
    python scripts/reconcile_table.py --table daily_price \
        --key code,trade_date --vals close,adj_factor,turnover_rate \
        [--numeric-tol close:0.005] [--date-col trade_date] \
        [--limit 60 | --all] [--end YYYY-MM-DD] [--mode copy|recollect]

- datahub 侧读 `DB_*`（默认库 `datahub`）；steady 侧读 `STEADY_DB_*`（默认库 `quant_system`），
  未设项回退 `DB_*`（同实例不同库拓扑）。
- **两侧必须锚同一 `--end`**：(日期类表) 各取「≤ end 的最近 N 行」，否则窗口错位全成伪偏离。
- 任一侧非 `accepted` → 退出码 1（放行门）；`--allow-drift` 强制 0（仅报告）。

**模式**：`copy`（默认，容差恒 0，逐位全等）/ `recollect`（允许 `--numeric-tol` 数值容差）。
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

CATEGORIES = ("accepted", "drifted", "db_anomaly", "false_pos", "rejected")


def _to_float(v):
    """数值化（Decimal/str/int/float → float）；不可解析 → None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    try:
        return float(str(v))
    except (TypeError, ValueError, InvalidOperation):
        return None


def classify_row(dh, steady, val_cols: list[str],
                 tolerances: dict[str, float]) -> str:
    """按复合键逐列分类（纯函数，便于单测）。

    dh/steady：值元组（列序同 `val_cols`）；缺该键 = None。
    val_cols：值列名（与元组一一对应，用于把 `tolerances` 按**列名**取容差）。
    tolerances：{列名: 容差}；命中则按 |a-b| ≤ tol 判等（数值列），否则严格相等。
    """
    if dh is None and steady is None:
        return "rejected"
    if dh is None:
        return "db_anomaly"
    if steady is None:
        return "false_pos"
    if len(dh) != len(steady) or len(dh) != len(val_cols):
        return "rejected"
    for vcol, a, b in zip(val_cols, dh, steady, strict=True):
        tol = tolerances.get(vcol)
        if tol is not None:
            fa, fb = _to_float(a), _to_float(b)
            if fa is None and fb is None:
                continue
            if fa is None or fb is None:
                return "rejected"
            if abs(fa - fb) > tol:
                return "drifted"
        elif a != b:
            return "drifted"
    return "accepted"


def _dsn(prefix: str, default_name: str) -> str:
    """构造 DSN；未设项回退到 `DB_*`（同实例不同库拓扑）。"""
    host = os.getenv(prefix + "HOST") or os.getenv("DB_HOST", "localhost")
    port = os.getenv(prefix + "PORT") or os.getenv("DB_PORT", "5432")
    user = os.getenv(prefix + "USER") or os.getenv("DB_USER", "quant")
    password = os.getenv(prefix + "PASSWORD") or os.getenv("DB_PASSWORD", "")
    name = os.getenv(prefix + "NAME", default_name)
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


def fetch_rows(engine, table: str, key_cols: list[str], val_cols: list[str],
               date_col: str | None, limit: int | None, end=None) -> dict:
    """取该表「截至 `end`（含）的最近 `limit` 行」 → {key 元组: 值元组}。

    `limit=None` → 全量（`--all`，Phase 3 读切换前的全区间放行门）。
    `date_col` 给定时按它锚定窗口并降序取；否则按 key 降序。
    """
    from sqlalchemy import text

    cols = ", ".join(key_cols + val_cols)
    sql = f"SELECT {cols} FROM {table}"  # noqa: S608 —— 表/列名来自 CLI，非用户输入拼值
    params: dict = {}
    if date_col is not None and end is not None:
        sql += f" WHERE {date_col} <= :end"
        params["end"] = end
    if limit is not None:
        order = (f"{date_col} DESC, " if date_col else "") + ", ".join(
            f"{k} DESC" for k in key_cols)
        sql += f" ORDER BY {order} LIMIT :n"
        params["n"] = limit
    nk = len(key_cols)
    with engine.connect() as c:
        rows = c.execute(text(sql), params).all()
    return {tuple(r[:nk]): tuple(r[nk:]) for r in rows}


def compare(dh_rows: dict, steady_rows: dict, val_cols: list[str],
            tolerances: dict[str, float]):
    """按复合键并集逐行分类 → (计数, {分类: [(key, dh, steady)]})。"""
    counts: Counter = Counter()
    detail: dict = defaultdict(list)
    for key in sorted(set(dh_rows) | set(steady_rows), key=lambda k: tuple(map(str, k))):
        cat = classify_row(dh_rows.get(key), steady_rows.get(key), val_cols, tolerances)
        counts[cat] += 1
        if cat != "accepted":
            detail[cat].append((key, dh_rows.get(key), steady_rows.get(key)))
    return counts, detail


def _parse_tols(spec: str) -> dict[str, float]:
    """`close:0.005,pe:0.02` → {close:0.005, pe:0.02}；空 → {}。"""
    out: dict[str, float] = {}
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        col, _, tol = part.partition(":")
        out[col.strip()] = float(tol)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="按表对账（datahub vs steady）")
    ap.add_argument("--table", required=True, help="表名（如 daily_price）")
    ap.add_argument("--key", required=True, help="复合主键列，逗号（如 code,trade_date）")
    ap.add_argument("--vals", required=True, help="比对值列，逗号（如 close,adj_factor）")
    ap.add_argument("--numeric-tol", default="",
                    help="数值容差 col:tol[,...]（如 close:0.005）；默认空=严格相等")
    ap.add_argument("--date-col", default=None,
                    help="窗口锚列（如 trade_date）；给定则按它锚端并降序取")
    ap.add_argument("--limit", type=int, default=60, help="锚点前 N 行（默认 60）")
    ap.add_argument("--all", action="store_true", help="全量比对（忽略 --limit）")
    ap.add_argument("--end", type=str, default=None,
                    help="窗口上界（含，YYYY-MM-DD）；两侧按此锚对齐")
    ap.add_argument("--mode", choices=("copy", "recollect"), default="copy",
                    help="copy=底种逐位全等（默认，容差恒 0）；recollect=回采校验（允许容差）")
    ap.add_argument("--allow-drift", action="store_true",
                    help="有非 accepted 分类也返回 0（仅报告）")
    args = ap.parse_args()

    from sqlalchemy import create_engine

    key_cols = [c.strip() for c in args.key.split(",") if c.strip()]
    val_cols = [c.strip() for c in args.vals.split(",") if c.strip()]
    tolerances = _parse_tols(args.numeric_tol) if args.mode == "recollect" else {}
    limit = None if args.all else args.limit

    dh_engine = create_engine(_dsn("DB_", "datahub"))
    steady_engine = create_engine(_dsn("STEADY_DB_", "quant_system"))
    dh_rows = fetch_rows(dh_engine, args.table, key_cols, val_cols,
                         args.date_col, limit, args.end)
    steady_rows = fetch_rows(steady_engine, args.table, key_cols, val_cols,
                             args.date_col, limit, args.end)

    counts, detail = compare(dh_rows, steady_rows, val_cols, tolerances)
    print(f"table={args.table} mode={args.mode} "
          f"datahub={len(dh_rows)} 行  steady={len(steady_rows)} 行  "
          f"锚点={args.end or '无'} 窗口={'全量' if args.all else args.limit}")
    for cat in CATEGORIES:
        print(f"  {cat:10s} {counts.get(cat, 0)}")
    for cat, rows in detail.items():
        for key, dh, steady in rows[:20]:
            print(f"  [{cat}] {key}  datahub={dh}  steady={steady}")

    bad = sum(counts.get(c, 0) for c in CATEGORIES if c != "accepted")
    if bad and not args.allow_drift:
        print(f"对账未通过：{bad} 条非 accepted", file=sys.stderr)
        return 1
    print("对账通过（窗口内逐位一致）" if not bad else "存在偏差（--allow-drift）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
