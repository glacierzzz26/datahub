"""18:10 当日同步路径测试（Issue #13）：快照 deferred 上限。

扩池首日全池无历史 → 全部 deferred → 逐只补把快照收益归零（R5）。上限内的
回退不告警；超上限只回退前 N，其余交夜间回填。
"""
from datetime import date

from app import tasks as tasks_mod


class _FakeDB:
    """_snapshot_sync 只在 upsert_snapshot_rows 里写库；mock 掉 upsert 后
    db 无实际用途（_latest_close_factor 被 monkeypatch）。"""


def test_snapshot_defer_capped(monkeypatch):
    """deferred 超 TENCENT_DEFER_MAX → 只回退前 N 只"""
    from app.collectors import daily as daily_mod

    codes = [f"60{i:04d}" for i in range(400)]   # 400 只，全部无历史
    monkeypatch.setattr(tasks_mod, "_latest_close_factor",
                        lambda db, cs: {})       # 全部无库内历史 → 全部 deferred

    # 快照返回行，逐只进入 deferred 分支
    from app.sources import tencent
    monkeypatch.setattr(tencent, "snapshot_rows",
                        lambda cs, d: [{"code": c, "prev_close": 1.0} for c in cs])
    monkeypatch.setattr(daily_mod, "upsert_snapshot_rows", lambda db, rows: len(rows))

    # _snapshot_sync 内 `from app.collect_config import TENCENT_DEFER_MAX` 按名取值，
    # patch 模块属性即生效
    from app import collect_config as cfg_mod
    monkeypatch.setattr(cfg_mod, "TENCENT_DEFER_MAX", 300)

    out = tasks_mod._snapshot_sync(_FakeDB(), codes, date(2026, 9, 17))
    assert len(out) == 300                        # 截到上限
    assert out == codes[:300]


def test_snapshot_defer_under_cap_uncapped(monkeypatch):
    """deferred 在上限内 → 不截断"""
    from app.collectors import daily as daily_mod
    from app.sources import tencent

    codes = [f"60{i:04d}" for i in range(10)]
    monkeypatch.setattr(tasks_mod, "_latest_close_factor", lambda db, cs: {})
    monkeypatch.setattr(tencent, "snapshot_rows",
                        lambda cs, d: [{"code": c, "prev_close": 1.0} for c in cs])
    monkeypatch.setattr(daily_mod, "upsert_snapshot_rows", lambda db, rows: len(rows))
    from app import collect_config as cfg_mod
    monkeypatch.setattr(cfg_mod, "TENCENT_DEFER_MAX", 300)

    out = tasks_mod._snapshot_sync(_FakeDB(), codes, date(2026, 9, 17))
    assert out == codes
