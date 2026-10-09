"""calendar 对账分类器单测（纯函数，不连库）。

分类键语义（详见 scripts/reconcile_calendar.py classify_calendar docstring）：
  accepted / drifted / db_anomaly / false_pos / rejected
"""
import importlib.util
import pathlib

_SCRIPT = (pathlib.Path(__file__).resolve().parents[1]
           / "scripts" / "reconcile_calendar.py")
_spec = importlib.util.spec_from_file_location("reconcile_calendar", _SCRIPT)
_rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rc)

_OPEN = (True, "SSE")
_CLOSED = (False, "SSE")


def test_both_equal_accepted():
    assert _rc.classify_calendar(_OPEN, _OPEN) == "accepted"


def test_both_present_but_differ_drifted():
    assert _rc.classify_calendar(_OPEN, _CLOSED) == "drifted"
    assert _rc.classify_calendar((True, "SSE"), (True, "SZSE")) == "drifted"


def test_only_steady_is_db_anomaly():
    """datahub 缺该日 = 抽取 bug。"""
    assert _rc.classify_calendar(None, _OPEN) == "db_anomaly"


def test_only_datahub_is_false_pos():
    """datahub 多产该日。"""
    assert _rc.classify_calendar(_OPEN, None) == "false_pos"


def test_both_missing_rejected():
    assert _rc.classify_calendar(None, None) == "rejected"


def test_compare_counts_and_detail():
    dh = {"2026-01-01": _OPEN, "2026-01-02": _CLOSED, "2026-01-04": _OPEN}
    steady = {"2026-01-01": _OPEN, "2026-01-02": _OPEN, "2026-01-03": _OPEN}
    counts, detail = _rc.compare(dh, steady)
    assert counts["accepted"] == 1          # 01-01
    assert counts["drifted"] == 1           # 01-02 值不同
    assert counts["db_anomaly"] == 1        # 01-03 仅 steady
    assert counts["false_pos"] == 1         # 01-04 仅 datahub
    assert counts["rejected"] == 0
    assert [d for d, _, _ in detail["drifted"]] == ["2026-01-02"]


def test_compare_zero_drift_passes():
    dh = {"2026-01-01": _OPEN, "2026-01-02": _CLOSED}
    counts, detail = _rc.compare(dh, dict(dh))
    assert counts["accepted"] == 2
    assert detail == {}


def test_fetch_recent_anchors_at_end():
    """窗口上界必须锚定同一 end —— 否则两侧各取「最近 N 行」会取到不重叠段。

    回归场景：steady 日历预载到 2027、datahub 只到 2026-12，各取最近 2 行
    → 全成假偏离；锚 end=2026-12-31 后两侧窗口可比。用 sqlite in-memory 真跑 SQL。
    """
    from sqlalchemy import create_engine, text
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE trade_calendar (cal_date DATE, is_open BOOLEAN, exchange TEXT)"))
        for d in ("2026-01-01", "2026-01-02", "2027-06-01", "2027-06-02"):
            c.execute(text(
                "INSERT INTO trade_calendar VALUES (:d, 1, 'SSE')"), {"d": d})

    # 无 end：最近 2 行落在 2027
    assert set(_rc.fetch_recent(engine, 2)) == {"2027-06-01", "2027-06-02"}
    # 锚 end=2026-12-31：最近 2 行落在 2026（与 2027 段不重叠——修复前此处取到 2027）
    assert set(_rc.fetch_recent(engine, 2, "2026-12-31")) == {
        "2026-01-01", "2026-01-02"}


def test_dsn_defaults_and_fallback(monkeypatch):
    for k in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME",
              "STEADY_DB_NAME", "STEADY_DB_HOST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_USER", "quant")
    monkeypatch.setenv("DB_PASSWORD", "secret")
    assert _rc._dsn("DB_", "datahub") == (
        "postgresql+psycopg2://quant:secret@127.0.0.1:5432/datahub")
    # steady 侧未设 → 回退 DB_* 主机/凭据，库名默认 quant_system
    assert _rc._dsn("STEADY_DB_", "quant_system") == (
        "postgresql+psycopg2://quant:secret@127.0.0.1:5432/quant_system")
    # 显式覆盖库名
    monkeypatch.setenv("STEADY_DB_NAME", "quant_prod")
    assert _rc._dsn("STEADY_DB_", "quant_system").endswith("/quant_prod")
