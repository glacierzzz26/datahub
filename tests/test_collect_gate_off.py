"""采集闸门（Phase 2）—— **默认全关** 的行为契约。

默认 env（无 `DATAHUB_COLLECT_ENABLED`）下，部署 datahub 采集栈必须**零行为**：
不注册任何 job、不注册补跑探针、任何 job 调用立即返回且**不触碰 DB/外部源**、cli 拒绝执行。

（放闸分支见 test_collect_gate_dataset.py。）
"""
from app import collect_config as cc
from app import tasks, watchdog

DATASETS = ["stock_basic", "calendar", "index", "daily", "valuation", "finance"]


class _FakeScheduler:
    def __init__(self):
        self.jobs: list[str] = []

    def add_job(self, fn, *a, **kw):
        self.jobs.append(fn.__name__)


def _force_off(monkeypatch):
    monkeypatch.setattr(cc, "COLLECT_ENABLED", False)
    monkeypatch.setattr(cc, "COLLECT_DATASETS", [])


def test_default_gate_off(monkeypatch):
    _force_off(monkeypatch)
    for ds in DATASETS:
        assert cc.collect_enabled(ds) is False
    assert tasks._any_collect_enabled() is False


def test_register_jobs_zero_when_off(monkeypatch):
    """注册层：闸门全关 ⇒ 零 job 注册（scheduler 空转）。"""
    _force_off(monkeypatch)
    sched = _FakeScheduler()
    assert tasks.register_jobs(sched) == []
    assert sched.jobs == []


def test_register_catchups_zero_when_off(monkeypatch):
    """补跑探针层：闸门全关 ⇒ 不注册任何探针（连 trade_calendar 查询都不发生）。"""
    _force_off(monkeypatch)
    watchdog._catchup_probes.clear()
    tasks.register_catchups()
    assert watchdog._catchup_probes == {}


def test_jobs_noop_and_no_db_when_off(monkeypatch):
    """调用层：闸门全关 ⇒ 每个 job 调用立即返回 None，且**不访问 DB/采集器**。"""
    _force_off(monkeypatch)

    def _boom():
        raise AssertionError("闸门关却访问了 DB/采集器")

    monkeypatch.setattr(tasks, "get_session", _boom)
    for job in (tasks.job_sync_stock_list, tasks.job_sync_calendar, tasks.job_sync_index,
                tasks.job_sync_daily_price, tasks.job_sync_valuation,
                tasks.job_sync_finance, tasks.job_nightly_backfill):
        assert job() is None, f"{job.__name__} 闸门关却未立即返回"


def test_cli_rejects_gated_command(monkeypatch):
    """手工 cli：未放闸的数据集直接拒绝（SystemExit 2），不导入采集器。"""
    _force_off(monkeypatch)
    # main() 会装请求层超时补丁；测试里旁路之（否则 conftest 的补丁残留守卫会报错）
    import sys

    import app.providers.net as net
    monkeypatch.setattr(net, "install_http_timeouts", lambda *a, **kw: None)

    import app.cli as cli
    monkeypatch.setattr(sys, "argv", ["quant-collector", "sync-calendar"])
    try:
        cli.main()
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("闸门关时 cli 应拒绝执行（SystemExit 2）")
