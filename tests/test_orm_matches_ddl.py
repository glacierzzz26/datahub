"""ORM 模型 vs 建库 DDL 对齐测试（Phase 2 PR-2）。

`schema_parity` 只断言 init.sql ↔ steady 冻结副本；本测试补上 **ORM（`app/models/
tables.py`）↔ init.sql**：防止「DDL 改了、ORM 没跟上」导致 `db` provider 读列错位。

**比较口径（有意放宽，标注等价）**：
- 类型族：`JSON`≡`JSONB`、`TIMESTAMP`≡`TIMESTAMPTZ`、`BIGSERIAL`≡`BIGINT`、
  `String()`（无长度）≡`TEXT`。这些是 steady ORM 既有的等价写法（DDL 为真源），
  逐字放宽不隐藏任何**列增删/改名/长度或精度**漂移。
- 列集合、主键必须**精确**相等。
"""
import re

from app.models import tables as orm
from tests.test_schema_parity import INIT_SQL, parse_schema

# 逐表 DDL 类型 → 规范型（与 ORM 侧 _orm_type 对齐）
_DDL_EQUIV = {
    "BIGSERIAL": "BIGINT",
    "TIMESTAMPTZ": "TIMESTAMP",
}


def _ddl_type(sig: str) -> str:
    """从列签名取类型表达式（如 'VARCHAR(10) PRIMARY KEY' → 'VARCHAR(10)'）。"""
    m = re.match(r"([A-Z0-9_]+(?:\([^)]*\))?)", sig.upper())
    tok = m.group(1)
    return _DDL_EQUIV.get(tok, tok)


def _orm_type(col) -> str:
    from sqlalchemy import JSON as SA_JSON
    from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, Numeric, String

    t = col.type
    if isinstance(t, BigInteger):
        return "BIGINT"
    if isinstance(t, Integer):
        return "INTEGER"
    if isinstance(t, Numeric):
        return f"DECIMAL({t.precision},{t.scale})"
    if isinstance(t, String):
        return f"VARCHAR({t.length})" if t.length else "TEXT"
    if isinstance(t, Boolean):
        return "BOOLEAN"
    if isinstance(t, Date):
        return "DATE"
    if isinstance(t, DateTime):
        return "TIMESTAMP"
    if isinstance(t, SA_JSON):
        return "JSONB"
    raise AssertionError(f"未规范的 ORM 类型：{t!r}（表 {col.table.name}.{col.name}）")


def _orm_tables() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for mapper in orm.Base.registry.mappers:
        table = mapper.local_table
        out[table.name] = {c.name: _orm_type(c) for c in table.columns}
    return out


def _ddl_tables() -> dict[str, dict[str, str]]:
    ddl, _, _ = parse_schema(INIT_SQL)
    return {t: {c: _ddl_type(sig) for c, sig in cols.items()} for t, cols in ddl.items()}


def test_orm_tables_are_exactly_raw_subset():
    """ORM 只应含原始表子集（不含 AppConfig / RemediationTask 等 steady 专属表）。"""
    assert set(_orm_tables()) == {
        "stock_basic", "daily_price", "financial_indicator",
        "daily_valuation", "trade_calendar", "market_hotspot", "task_run",
    }


def test_orm_table_names_present_in_ddl():
    ddl = _ddl_tables()
    for name in _orm_tables():
        assert name in ddl, f"ORM 表 {name} 在 init.sql 中不存在"


def test_orm_columns_match_ddl():
    ddl = _ddl_tables()
    for name, cols in _orm_tables().items():
        assert set(cols) == set(ddl[name]), (
            f"{name} 列集合漂移：ORM={sorted(cols)} DDL={sorted(ddl[name])}"
        )


def test_orm_column_types_match_ddl():
    ddl = _ddl_tables()
    for name, cols in _orm_tables().items():
        for col, typ in cols.items():
            assert typ == ddl[name][col], (
                f"{name}.{col} 类型漂移：ORM={typ} DDL={ddl[name][col]}"
            )


def test_orm_primary_keys_match_ddl():
    # DDL 侧：parse_schema 把 'PRIMARY KEY' 留在列签名里
    raw, _, _ = parse_schema(INIT_SQL)
    ddl_pk = {
        t: {c for c, sig in cols.items() if "PRIMARY KEY" in sig}
        for t, cols in raw.items()
    }
    orm_pk = {
        t.name: {c.name for c in t.primary_key.columns}
        for t in (m.local_table for m in orm.Base.registry.mappers)
    }
    assert orm_pk == ddl_pk, f"主键漂移：ORM={orm_pk} DDL={ddl_pk}"
