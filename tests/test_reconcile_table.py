"""按表对账分类器单测（纯函数 + sqlite 真跑 SQL，不连 PG）。

分类键语义（详见 scripts/reconcile_table.py classify_row docstring）：
  accepted / drifted / db_anomaly / false_pos / rejected
"""
import importlib.util
import pathlib

_SCRIPT = (pathlib.Path(__file__).resolve().parents[1]
           / "scripts" / "reconcile_table.py")
_spec = importlib.util.spec_from_file_location("reconcile_table", _SCRIPT)
_rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rt)

_VALS = ["close", "adj_factor"]          # 值列（元组按此列序）


def test_both_equal_accepted():
    v = (10.0, 1.0)
    assert _rt.classify_row(v, v, _VALS, {}) == "accepted"


def test_both_present_but_differ_drifted():
    assert _rt.classify_row((10.0, 1.0), (11.0, 1.0), _VALS, {}) == "drifted"


def test_only_steady_is_db_anomaly():
    """datahub 缺该键 = 抽取/拷贝 bug。"""
    assert _rt.classify_row(None, (10.0, 1.0), _VALS, {}) == "db_anomaly"


def test_only_datahub_is_false_pos():
    """datahub 多产该键。"""
    assert _rt.classify_row((10.0, 1.0), None, _VALS, {}) == "false_pos"


def test_both_missing_rejected():
    assert _rt.classify_row(None, None, _VALS, {}) == "rejected"


def test_length_mismatch_rejected():
    assert _rt.classify_row((10.0,), (10.0, 1.0), _VALS, {}) == "rejected"


def test_tolerance_by_column_name_not_position():
    """回归：容差按**列名**对齐，不按位置。列序反了也不得张冠李戴。

    close 有容差、adj_factor 无；两列都在阈值内的差（close 差 0.001）应 accepted。
    """
    tols = {"close": 0.005}
    assert _rt.classify_row((10.000, 1.0), (10.001, 1.0), _VALS, tols) == "accepted"
    # 超容差 → drifted
    assert _rt.classify_row((10.0, 1.0), (10.01, 1.0), _VALS, tols) == "drifted"
    # adj_factor 无容差：0.001 差即 drifted（证明未误套 close 的容差）
    assert _rt.classify_row((10.0, 1.0), (10.0, 1.001), _VALS, tols) == "drifted"


def test_tolerance_decimal_vs_float():
    """PG 侧返回 Decimal，容差比较须先数值化。"""
    from decimal import Decimal
    tols = {"close": 0.005}
    assert _rt.classify_row(
        (Decimal("10.000"), Decimal("1.0")),
        (10.001, 1.0), _VALS, tols) == "accepted"


def test_tolerance_non_numeric_rejected():
    """容差列出现无法数值化的值 → rejected（不可比）。"""
    assert _rt.classify_row((None, 1.0), (10.0, 1.0), _VALS, {"close": 0.005}) == "rejected"


def test_compare_counts_and_detail():
    dh = {("A", "2026-01-01"): (10.0, 1.0),
          ("A", "2026-01-02"): (11.0, 1.0),
          ("A", "2026-01-04"): (12.0, 1.0)}
    steady = {("A", "2026-01-01"): (10.0, 1.0),
              ("A", "2026-01-02"): (11.5, 1.0),
              ("A", "2026-01-03"): (13.0, 1.0)}
    counts, detail = _rt.compare(dh, steady, _VALS, {})
    assert counts["accepted"] == 1           # 01-01
    assert counts["drifted"] == 1            # 01-02
    assert counts["db_anomaly"] == 1         # 01-03 仅 steady
    assert counts["false_pos"] == 1          # 01-04 仅 datahub
    assert counts["rejected"] == 0
    assert detail["drifted"][0][0] == ("A", "2026-01-02")


def test_compare_zero_drift_passes():
    dh = {("A", "2026-01-01"): (10.0, 1.0), ("B", "2026-01-01"): (20.0, 1.0)}
    counts, detail = _rt.compare(dh, dict(dh), _VALS, {})
    assert counts["accepted"] == 2
    assert detail == {}


def test_fetch_rows_anchors_at_end():
    """窗口上界必须锚定同一 end —— 否则两侧各取「最近 N 行」取到不重叠段。"""
    from sqlalchemy import create_engine, text
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE daily_price (code TEXT, trade_date DATE, close REAL)"))
        for d in ("2026-01-01", "2026-01-02", "2027-06-01", "2027-06-02"):
            c.execute(text(
                "INSERT INTO daily_price VALUES ('A', :d, 10)"), {"d": d})

    got = _rt.fetch_rows(engine, "daily_price", ["code", "trade_date"],
                         ["close"], "trade_date", 2, "2026-12-31")
    assert set(got) == {("A", "2026-01-01"), ("A", "2026-01-02")}

    # 无锚点：最近 2 行落在 2027
    assert set(_rt.fetch_rows(engine, "daily_price", ["code", "trade_date"],
                              ["close"], "trade_date", 2)) == {
        ("A", "2027-06-01"), ("A", "2027-06-02")}


def test_fetch_rows_none_limit_returns_all():
    """limit=None（--all）→ 不设行数上限，返回锚点前**全部**行。"""
    from sqlalchemy import create_engine, text
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE daily_price (code TEXT, trade_date DATE, close REAL)"))
        for d in ("2026-01-01", "2026-01-02", "2026-01-05", "2027-06-01"):
            c.execute(text(
                "INSERT INTO daily_price VALUES ('A', :d, 10)"), {"d": d})
    assert len(_rt.fetch_rows(engine, "daily_price", ["code", "trade_date"],
                              ["close"], "trade_date", None)) == 4
    assert len(_rt.fetch_rows(engine, "daily_price", ["code", "trade_date"],
                              ["close"], "trade_date", None, "2026-12-31")) == 3


def test_fetch_rows_maps_key_to_vals():
    """返回结构 == {key 元组: 值元组}（键列数由 key_cols 决定）。"""
    from sqlalchemy import create_engine, text
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE daily_valuation (code TEXT, trade_date DATE, close REAL, pe REAL)"))
        c.execute(text(
            "INSERT INTO daily_valuation VALUES ('A', '2026-01-01', 10.0, 12.5)"))
    got = _rt.fetch_rows(engine, "daily_valuation", ["code", "trade_date"],
                         ["close", "pe"], "trade_date", 1)
    assert got == {("A", "2026-01-01"): (10.0, 12.5)}


def test_parse_tols():
    assert _rt._parse_tols("") == {}
    assert _rt._parse_tols("close:0.005,pe_ttm:0.02") == {"close": 0.005, "pe_ttm": 0.02}
    assert _rt._parse_tols("close:0.005,") == {"close": 0.005}


def test_dsn_defaults_and_fallback(monkeypatch):
    for k in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME",
              "STEADY_DB_NAME", "STEADY_DB_HOST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_USER", "quant")
    monkeypatch.setenv("DB_PASSWORD", "secret")
    assert _rt._dsn("DB_", "datahub") == (
        "postgresql+psycopg2://quant:secret@127.0.0.1:5432/datahub")
    # steady 侧未设 → 回退 DB_* 主机/凭据，库名默认 quant_system
    assert _rt._dsn("STEADY_DB_", "quant_system") == (
        "postgresql+psycopg2://quant:secret@127.0.0.1:5432/quant_system")
    monkeypatch.setenv("STEADY_DB_NAME", "quant_prod")
    assert _rt._dsn("STEADY_DB_", "quant_system").endswith("/quant_prod")
