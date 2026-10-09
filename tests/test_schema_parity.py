"""跨仓库 schema 对齐测试（Phase 2 蓝图 §1「列漂移检测」的可执行化）。

datahub 的 CI 读不到 steady 仓库 → 把 steady 原始表 DDL 冻结为
`tests/fixtures/steady_raw_schema.sql`（vendor 快照，见其头注），断言 datahub
自己的 `deploy/postgres/init.sql` 与之**逐字对齐**：表集合、列名/类型/约束签名、
主键/唯一键/索引。

任何一方的原始表 schema 漂移（漏列、改类型、丢索引）都会在此失败——这是
「回填对账可比 + Phase 3 steady 读到等价数据」的护栏。
"""
import re
from pathlib import Path

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "steady_raw_schema.sql"
INIT_SQL = HERE.parent / "deploy" / "postgres" / "init.sql"

# datahub 自有库**只**承载这些原始表（不含 factor_*/strategy_*/account*/… 计算表）
RAW_TABLES = {
    "stock_basic",
    "daily_price",
    "financial_indicator",
    "daily_valuation",
    "trade_calendar",
    "market_hotspot",
    "task_run",
}


def _collapse(path: Path) -> str:
    """去行内注释（`-- …`）后折叠为单空格单行，便于正则解析跨行语句。"""
    lines = [raw.split("--", 1)[0] for raw in path.read_text(encoding="utf-8").splitlines()]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _split_top_level(s: str) -> list[str]:
    """按**顶层**逗号切分（忽略 DECIMAL(10,2) 等括号内逗号）。"""
    out, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _norm(sig: str) -> str:
    return re.sub(r"\s+", " ", sig.strip()).upper()


def parse_schema(path: Path) -> tuple[dict, set, set]:
    """解析 DDL → (tables, uniques, indexes)。

    tables: {表: {列: 签名}}
    uniques: {(表, (列…))}
    indexes: {(名, 表, (列…), is_unique)}
    """
    text = _collapse(path)
    tables: dict[str, dict[str, str]] = {}
    uniques: set = set()
    indexes: set = set()

    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\);", text):
        table, body = m.group(1), m.group(2)
        cols: dict[str, str] = {}
        for part in _split_top_level(body):
            first = part.split(None, 1)[0].upper()
            if first == "UNIQUE":  # 表级唯一约束（如 task_run (task_name, run_date)）
                um = re.match(r"UNIQUE\s*\(([^)]*)\)", part, re.I)
                uniques.add((table, tuple(c.strip() for c in um.group(1).split(","))))
            elif first in ("PRIMARY", "FOREIGN", "CHECK", "CONSTRAINT"):
                continue  # 其它表级约束不参与断言（本 schema 无）
            else:
                col = part.split(None, 1)[0]
                cols[col] = _norm(part[len(col):])
        tables[table] = cols

    for m in re.finditer(
        r"CREATE (UNIQUE )?INDEX IF NOT EXISTS (\w+) ON (\w+)\s*\(([^)]*)\)", text
    ):
        indexes.add((
            m.group(2), m.group(3),
            tuple(c.strip() for c in m.group(4).split(",")),
            bool(m.group(1)),
        ))

    return tables, uniques, indexes


def test_files_exist():
    assert FIXTURE.is_file(), "缺少 vendor 冻结副本"
    assert INIT_SQL.is_file(), "缺少 deploy/postgres/init.sql"


def test_datahub_init_contains_only_raw_tables():
    tables, _, _ = parse_schema(INIT_SQL)
    assert set(tables) == RAW_TABLES, (
        f"init.sql 表集合应恰为原始表；多出 {set(tables) - RAW_TABLES}，"
        f"缺少 {RAW_TABLES - set(tables)}"
    )


def test_fixture_contains_only_raw_tables():
    tables, _, _ = parse_schema(FIXTURE)
    assert set(tables) == RAW_TABLES


def test_columns_match_fixture():
    init_t, _, _ = parse_schema(INIT_SQL)
    fix_t, _, _ = parse_schema(FIXTURE)
    assert init_t == fix_t, "列名/类型/约束签名与 steady 冻结副本漂移"


def test_unique_constraints_match_fixture():
    _, init_u, _ = parse_schema(INIT_SQL)
    _, fix_u, _ = parse_schema(FIXTURE)
    assert init_u == fix_u, f"唯一约束漂移：datahub={init_u} steady={fix_u}"
    # 表级唯一约束（task_run 的幂等键）
    assert ("task_run", ("task_name", "run_date")) in init_u


def test_key_unique_indexes_present():
    """对账 upsert 依赖的唯一**索引**（ON CONFLICT 冲突键）必须存在。"""
    _, _, init_i = parse_schema(INIT_SQL)
    uniq = {(name, table, cols) for name, table, cols, is_uniq in init_i if is_uniq}
    assert ("uq_daily_price_code_date", "daily_price", ("code", "trade_date")) in uniq
    assert ("uq_daily_valuation_code_date", "daily_valuation", ("code", "trade_date")) in uniq
    assert ("uq_financial_code_report", "financial_indicator", ("code", "report_date")) in uniq


def test_indexes_match_fixture():
    _, _, init_i = parse_schema(INIT_SQL)
    _, _, fix_i = parse_schema(FIXTURE)
    assert init_i == fix_i, f"索引漂移：datahub={init_i} steady={fix_i}"
    names = {name for name, *_ in init_i}
    assert {"uq_daily_price_code_date", "idx_daily_price_trade_date",
            "uq_daily_valuation_code_date", "uq_financial_code_report",
            "idx_financial_code_announce", "idx_stock_basic_universe",
            "idx_stock_basic_data_scope"} <= names
