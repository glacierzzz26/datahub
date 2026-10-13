"""采集闸门（Phase 2）—— **放闸某数据集** 的行为契约。

`DATAHUB_COLLECT_ENABLED=1` + `DATAHUB_COLLECT_DATASETS=calendar` ⇒ 只 `calendar`
放闸：仅 `job_sync_calendar` 注册/放行，其余一律照旧关。
"""
from app import collect_config as cc
from app import tasks, watchdog


class _FakeScheduler:
    def __init__(self):
        self.jobs: list[str] = []

    def add_job(self, fn, *a, **kw):
        self.jobs.append(fn.__name__)


def _enable(monkeypatch, datasets):
    monkeypatch.setattr(cc, "COLLECT_ENABLED", True)
    monkeypatch.setattr(cc, "COLLECT_DATASETS", list(datasets))


def test_only_named_dataset_enabled(monkeypatch):
    _enable(monkeypatch, ["calendar"])
    assert cc.collect_enabled("calendar") is True
    assert cc.collect_enabled("daily") is False
    assert cc.collect_enabled("stock_basic") is False
    assert tasks._any_collect_enabled() is True


def test_register_jobs_only_calendar(monkeypatch):
    _enable(monkeypatch, ["calendar"])
    sched = _FakeScheduler()
    assert tasks.register_jobs(sched) == ["job_sync_calendar"]
    assert sched.jobs == ["job_sync_calendar"]


def test_register_catchups_only_calendar(monkeypatch):
    _enable(monkeypatch, ["calendar"])
    watchdog._catchup_probes.clear()
    tasks.register_catchups()
    assert set(watchdog._catchup_probes) == {"job_sync_calendar"}


def test_calendar_job_runs_when_enabled(monkeypatch):
    """放闸后 job_sync_calendar 真的调用 CalendarCollector；其它 job 仍立即返回。"""
    _enable(monkeypatch, ["calendar"])
    import app.collectors.calendar as cal_mod

    calls = []

    class _FakeCalendar:
        def __init__(self, db):
            self.db = db

        def run(self):
            calls.append("run")

    monkeypatch.setattr(cal_mod, "CalendarCollector", _FakeCalendar)
    monkeypatch.setattr(tasks, "get_session", lambda: object())

    assert tasks.job_sync_calendar() is None
    assert calls == ["run"]
    # 未放闸的 daily 仍立即返回（不碰 DB）
    monkeypatch.setattr(tasks, "get_session",
                        lambda: (_ for _ in ()).throw(AssertionError("daily 不应访问 DB")))
    assert tasks.job_sync_daily_price() is None


def test_register_jobs_only_capital_flow(monkeypatch):
    """放闸 P14-1 单个数据集 → 只注册其对应 job（其余（含核心域）不注册）。"""
    _enable(monkeypatch, ["capital_flow"])
    sched = _FakeScheduler()
    assert tasks.register_jobs(sched) == ["job_sync_capital_flow"]
    assert sched.jobs == ["job_sync_capital_flow"]


def test_capital_flow_job_runs_when_enabled(monkeypatch):
    """放闸后 job_sync_capital_flow 真的调用 CapitalFlowCollector；detail 未放闸即返回。"""
    _enable(monkeypatch, ["capital_flow"])
    import app.collectors.research as res_mod

    calls = []

    class _FakeCapitalFlow:
        def __init__(self, db):
            self.db = db

        def run(self, start, end):
            calls.append((start, end))

    monkeypatch.setattr(res_mod, "CapitalFlowCollector", _FakeCapitalFlow)
    monkeypatch.setattr(tasks, "get_session", lambda: object())

    assert tasks.job_sync_capital_flow() is None
    assert len(calls) == 1
    # detail 未放闸 → 立即返回（不碰 DB）
    monkeypatch.setattr(tasks, "get_session",
                        lambda: (_ for _ in ()).throw(AssertionError("detail 不应访问 DB")))
    assert tasks.job_sync_capital_flow_detail() is None
