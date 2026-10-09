"""任务级看门狗测试（Issue #14 / L3，**验收 #3**）。

覆盖三件事：
  1. `overdue_jobs` 纯函数的判定（越界/新鲜/env 覆盖）——这是"要不要自杀"的**唯一**依据；
  2. `alert_and_exit` 的告警落地（写 failed task_run → 进飞书红卡通道）+ 退出预算防抖；
  3. `startup_catchup` 的四态门控（非交易日/时间窗外/已完成/该补跑）。

**不碰真库**：告警写账本用假 session 断言**语句内容**（`pg_insert(...)` 在 sqlite 上必然
失败——`TaskRun.id` 是 BigInteger 主键，sqlite 不自动递增）；
交易日历判定用假 session 回放。`exit_fn` 注入 → 不会真的退出进程。
`startup_catchup` 的时间窗依赖"现在几点"，故显式传入固定 `now`，任何钟点跑都稳定。
"""
import time
from datetime import date, datetime

import pytest
from sqlalchemy.sql import Select

from app import watchdog
from app.collectors import base

# 固定时刻：计划 18:10 之后（在 6h 窗口内）/ 之前
NOW_PAST_PLAN = datetime(2026, 9, 16, 20, 0)
NOW_BEFORE_PLAN = datetime(2026, 9, 16, 8, 0)
PLAN_TIME = datetime(2026, 9, 16, 18, 10)  # JOB_SCHEDULE 里 job_sync_daily_price


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后清空全局态（_running / 探针 / 泄漏计数）"""
    def _reset():
        with watchdog._lock:
            watchdog._running.clear()
        watchdog._catchup_probes.clear()
        with base._leak_lock:
            base._leaked_workers = 0
    _reset()
    yield
    _reset()


class FakeSession:
    """记录 execute 的语句；select 回放给定值（供交易日历判定）。不会真的入库。"""

    def __init__(self, is_open=None):
        self.executed = []
        self._is_open = is_open

    def execute(self, stmt, params=None):
        self.executed.append(stmt)
        if isinstance(stmt, Select):
            return _Scalar(self._is_open)
        return None

    def close(self):
        pass


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


def _values_of(stmt) -> dict:
    """pg_insert(...).values(...) 的「列→值」dict（键为 Column，转字符串键）"""
    return {getattr(k, "key", k): getattr(v, "value", v)
            for k, v in stmt._values.items()}


@pytest.fixture
def session(monkeypatch):
    """告警路径的假 session"""
    s = FakeSession()
    monkeypatch.setattr("app.db.get_session", lambda: s)
    return s


@pytest.fixture
def trading_session(monkeypatch):
    """交易日判定用假 session：`is_open` 由用例给定（True/False/None）"""
    def _make(is_open):
        s = FakeSession(is_open=is_open)
        monkeypatch.setattr("app.db.get_session", lambda: s)
        return s
    return _make


@pytest.fixture
def exit_file(tmp_path, monkeypatch):
    """退出计数文件指向 tmp（防污染真 /app/logs）"""
    p = tmp_path / ".watchdog_exits"
    monkeypatch.setattr(watchdog, "EXIT_THRESHOLD_FILE", str(p))
    return p


# ---------- overdue_jobs：纯函数 ----------

def test_overdue_detects_exceeded(monkeypatch):
    monkeypatch.setitem(watchdog.JOB_LIMITS, "job_x", 100)
    with watchdog.job_guard("job_x"):
        assert watchdog.overdue_jobs(time.monotonic()) == []
    # job_guard 退出即摘除 → 不再算越界
    assert watchdog.overdue_jobs(time.monotonic()) == []


def test_overdue_reports_elapsed(monkeypatch):
    monkeypatch.setitem(watchdog.JOB_LIMITS, "job_x", 100)
    with watchdog._lock:
        watchdog._running["job_x"] = 1000.0
    assert watchdog.overdue_jobs(now=1150.0) == [("job_x", 150.0)]


def test_overdue_not_yet(monkeypatch):
    monkeypatch.setitem(watchdog.JOB_LIMITS, "job_x", 100)
    with watchdog._lock:
        watchdog._running["job_x"] = 1000.0
    assert watchdog.overdue_jobs(now=1050.0) == []


def test_overdue_ignores_unknown_job_under_default(monkeypatch):
    """未登记上限的 job 走 DEFAULT_LIMIT，而非被当作 0 秒立即判越界"""
    with watchdog._lock:
        watchdog._running["job_never_seen"] = time.monotonic()
    assert watchdog.overdue_jobs() == []


def test_limit_env_override(monkeypatch):
    monkeypatch.setitem(watchdog.JOB_LIMITS, "job_x", 100)
    monkeypatch.setenv("COLLECTOR_JOB_LIMIT_JOB_X", "5")
    assert watchdog._limit("job_x") == 5.0


def test_limit_env_invalid_falls_back(monkeypatch):
    monkeypatch.setitem(watchdog.JOB_LIMITS, "job_x", 100)
    monkeypatch.setenv("COLLECTOR_JOB_LIMIT_JOB_X", "abc")
    assert watchdog._limit("job_x") == 100.0


def test_limit_default_for_unknown_job(monkeypatch):
    monkeypatch.setenv("COLLECTOR_JOB_LIMIT_DEFAULT", "77")
    assert watchdog._limit("job_unknown") == 77.0


def test_limit_unknown_without_env():
    assert watchdog._limit("job_unknown") == float(watchdog.DEFAULT_LIMIT)


def test_guarded_tracks_run_and_clears(monkeypatch):
    """@guarded：运行期可被 overdue 看到（job 名 = 函数名），退出即摘除"""
    monkeypatch.setitem(watchdog.JOB_LIMITS, "f", 1)
    seen = {}

    @watchdog.guarded
    def f():
        seen["during"] = [j for j, _ in watchdog.overdue_jobs(now=time.monotonic() + 60)]
        return "r"

    assert f() == "r"
    assert seen["during"] == ["f"], "运行中的 job 应被 overdue_jobs 看到"
    assert watchdog.overdue_jobs() == [], "退出后应摘除"
    assert watchdog.is_running("f") is False


# ---------- 泄漏触发 ----------

def test_leaked_over_threshold(monkeypatch):
    monkeypatch.setattr(watchdog, "_max_leaked", lambda: 2)
    with base._leak_lock:
        base._leaked_workers = 3
    assert base.leaked_workers() > watchdog._max_leaked()


# ---------- healthz_status ----------

def test_healthz_ok():
    code, payload = watchdog.healthz_status()
    assert code == 200 and payload["status"] == "ok"


def test_healthz_overdue_returns_503(monkeypatch):
    monkeypatch.setitem(watchdog.JOB_LIMITS, "job_x", 1)
    with watchdog._lock:
        watchdog._running["job_x"] = 0.0
    code, payload = watchdog.healthz_status()
    assert code == 503 and payload["status"] == "stale"
    assert payload["job"] == "job_x" and payload["overdue"] == 1


def test_healthz_leak_returns_503(monkeypatch):
    monkeypatch.setattr(watchdog, "_max_leaked", lambda: 1)
    with base._leak_lock:
        base._leaked_workers = 2
    code, payload = watchdog.healthz_status()
    assert code == 503 and payload["reason"] == "leaked_workers"


# ---------- alert_and_exit：告警 + 退出 + 防抖 ----------

def _capture_exit():
    calls = []
    return calls, (lambda code: calls.append(code))


def test_alert_writes_failed_task_run_and_exits(session, exit_file):
    calls, exit_fn = _capture_exit()
    watchdog.alert_and_exit("运行超时 >100s", "job_x", 150.0, exit_fn=exit_fn)

    assert calls == [1], "应在预算内调用 os._exit(1)"
    assert len(session.executed) == 1, "应写一行账本"
    vals = _values_of(session.executed[0])
    assert vals["task_name"] == "collector_watchdog"
    assert vals["run_date"] == date.today()
    assert vals["status"] == "failed"
    assert "job_x" in vals["message"] and "150" in vals["message"]
    detail = vals["detail"]
    assert detail["job"] == "job_x"
    assert detail["exit_count"] == 1
    assert detail["leaked_workers"] == 0
    assert detail["reason"] == "运行超时 >100s"
    assert "pid" in detail and "at" in detail


def test_alert_upserts_on_name_and_run_date(session, exit_file):
    """冲突列必须是 (task_name, run_date)——否则同日二次越界会插重复行而非更新"""
    calls, exit_fn = _capture_exit()
    watchdog.alert_and_exit("原因", "job_x", 1.0, exit_fn=exit_fn)
    stmt = session.executed[0]
    assert [getattr(e, "key", e)
            for e in stmt._post_values_clause.inferred_target_elements] \
        == ["task_name", "run_date"]


def test_alert_writes_file_log(session, exit_file):
    calls, exit_fn = _capture_exit()
    watchdog.alert_and_exit("原因", "job_x", 10.0, exit_fn=exit_fn)
    log = exit_file.parent / "watchdog.log"
    assert log.exists() and "job_x" in log.read_text()


def test_exit_budget_alert_only_after_max(session, exit_file):
    """超过 MAX_EXITS_PER_DAY 后只告警不退出（防 restart:unless-stopped 无退避抖动）"""
    calls, exit_fn = _capture_exit()
    for i in range(1, watchdog.MAX_EXITS_PER_DAY + 1):
        watchdog.alert_and_exit(f"第{i}次", "job_x", 1.0, exit_fn=exit_fn)
    assert calls == [1] * watchdog.MAX_EXITS_PER_DAY, "预算内每次都应退出"

    calls.clear()
    session.executed.clear()
    watchdog.alert_and_exit("超预算", "job_x", 1.0, exit_fn=exit_fn)
    assert calls == [], "超预算后不得再退出"
    vals = _values_of(session.executed[0])
    assert "超今日重启预算" in vals["message"], "应标注未自动重启"
    assert vals["detail"]["exit_count"] == watchdog.MAX_EXITS_PER_DAY + 1


def test_exits_counter_survives_and_resets_per_day(exit_file):
    assert watchdog._exits_today() == 0
    assert watchdog._bump_exits() == 1
    assert watchdog._bump_exits() == 2
    assert watchdog._exits_today() == 2
    # 换成「旧的日期」→ 计数归零（新的一天重新给预算）
    exit_file.write_text("2020-01-01 9")
    assert watchdog._exits_today() == 0
    assert watchdog._bump_exits() == 1


def test_exits_counter_ignores_corrupt_file(exit_file):
    exit_file.write_text("garbage")
    assert watchdog._exits_today() == 0
    exit_file.write_text("")
    assert watchdog._exits_today() == 0


def test_alert_db_failure_does_not_prevent_exit(exit_file, monkeypatch):
    """DB 卡死正是要自杀的场景——写账本失败绝不能拦住退出"""
    def boom():
        raise RuntimeError("DB 挂了")
    monkeypatch.setattr("app.db.get_session", boom)
    calls, exit_fn = _capture_exit()
    watchdog.alert_and_exit("DB 卡死", "job_x", 1.0, exit_fn=exit_fn)
    assert calls == [1]
    assert (exit_file.parent / "watchdog.log").exists()


# ---------- startup_catchup：门控（固定 now → 任意钟点稳定）----------

def test_catchup_skipped_on_non_trading_day(trading_session):
    trading_session(False)
    watchdog.register_catchup("job_sync_daily_price", lambda d: False)
    assert watchdog.startup_catchup(now=NOW_PAST_PLAN) == []


def test_catchup_skipped_when_calendar_missing(trading_session):
    trading_session(None)  # 日历无今日行
    watchdog.register_catchup("job_sync_daily_price", lambda d: False)
    assert watchdog.startup_catchup(now=NOW_PAST_PLAN) == []


def test_catchup_skipped_when_already_done(trading_session, monkeypatch):
    """探针返回 True（今日已完成）→ 不补跑"""
    trading_session(True)
    watchdog.register_catchup("job_sync_daily_price", lambda d: True)
    ran = []
    monkeypatch.setattr("app.tasks.job_sync_daily_price", lambda: ran.append(1))
    assert watchdog.startup_catchup(now=NOW_PAST_PLAN) == []
    assert ran == []


def test_catchup_runs_when_overdue_and_missing(trading_session, monkeypatch):
    """核心验收：交易日 + 计划时刻已过且在窗口内 + 今日未完成 → 立即补跑"""
    trading_session(True)
    watchdog.register_catchup("job_sync_daily_price", lambda d: False)
    ran = []
    monkeypatch.setattr("app.tasks.job_sync_daily_price", lambda: ran.append(1))
    assert watchdog.startup_catchup(now=NOW_PAST_PLAN) == ["job_sync_daily_price"]
    assert ran == [1]


def test_catchup_skipped_before_planned_time(trading_session, monkeypatch):
    """计划时刻还在未来（delta<0）→ 不补跑"""
    trading_session(True)
    watchdog.register_catchup("job_sync_daily_price", lambda d: False)
    ran = []
    monkeypatch.setattr("app.tasks.job_sync_daily_price", lambda: ran.append(1))
    assert watchdog.startup_catchup(now=NOW_BEFORE_PLAN) == []
    assert ran == []


def test_catchup_skipped_outside_window(trading_session, monkeypatch):
    """计划时刻过去太久（>window_hours）→ 不补跑（免得凌晨重启去跑昨天的活）"""
    trading_session(True)
    watchdog.register_catchup("job_sync_daily_price", lambda d: False)
    ran = []
    monkeypatch.setattr("app.tasks.job_sync_daily_price", lambda: ran.append(1))
    # 计划 18:10，now 次日 03:00 → delta ≈ 8.8h > 6h
    assert watchdog.startup_catchup(now=datetime(2026, 9, 17, 3, 0)) == []
    assert ran == []


def test_catchup_skips_running_job(trading_session, monkeypatch):
    """该 job 正在跑 → 不干扰"""
    trading_session(True)
    watchdog.register_catchup("job_sync_daily_price", lambda d: False)
    ran = []
    monkeypatch.setattr("app.tasks.job_sync_daily_price", lambda: ran.append(1))
    with watchdog.job_guard("job_sync_daily_price"):
        assert watchdog.startup_catchup(now=NOW_PAST_PLAN) == []
    assert ran == []


def test_catchup_probe_failure_is_isolated(trading_session, monkeypatch):
    """探针抛异常 → 跳过该 job，不影响其它"""
    trading_session(True)

    def boom(_d):
        raise RuntimeError("探针炸了")
    watchdog.register_catchup("job_sync_daily_price", boom)
    watchdog.register_catchup("job_sync_valuation", lambda d: False)
    ran = []
    monkeypatch.setattr("app.tasks.job_sync_valuation", lambda: ran.append("valuation"))
    assert watchdog.startup_catchup(now=NOW_PAST_PLAN) == ["job_sync_valuation"]
    assert ran == ["valuation"]


def test_catchup_job_exception_is_isolated(trading_session, monkeypatch):
    """补跑本身抛异常 → 只记日志，不影响其它 job"""
    trading_session(True)

    def boom():
        raise RuntimeError("补跑失败")
    watchdog.register_catchup("job_sync_daily_price", lambda d: False)
    watchdog.register_catchup("job_sync_valuation", lambda d: False)
    ran = []
    monkeypatch.setattr("app.tasks.job_sync_daily_price", boom)
    monkeypatch.setattr("app.tasks.job_sync_valuation", lambda: ran.append("valuation"))
    watchdog.startup_catchup(now=NOW_PAST_PLAN)
    assert ran == ["valuation"]


def test_catchup_probe_receives_delta_hours(trading_session):
    """探针拿到的是「距计划时刻的小时数」——供其自行判窗口内是否合理"""
    trading_session(True)
    got = []
    watchdog.register_catchup("job_sync_daily_price", lambda d: got.append(d) or True)
    watchdog.startup_catchup(now=NOW_PAST_PLAN)
    assert len(got) == 1
    want = (NOW_PAST_PLAN - PLAN_TIME).total_seconds() / 3600
    assert abs(got[0] - want) < 1e-6


def test_catchup_unmapped_job_ignored(trading_session):
    """未列入 JOB_SCHEDULE 的 job 不参与补跑（无从判定计划时刻）"""
    trading_session(True)
    watchdog.register_catchup("job_not_in_schedule", lambda d: False)
    assert watchdog.startup_catchup(now=NOW_PAST_PLAN) == []


# ---------- 真实探针接线 ----------

def test_register_catchups_wires_real_probes(monkeypatch):
    """register_catchups 把真实探针接上（行情/估值/指数 + 4 个不补跑）"""
    from app import tasks

    monkeypatch.setattr(tasks, "collect_enabled", lambda ds: True)  # 放闸（否则零注册）
    watchdog._catchup_probes.clear()
    tasks.register_catchups()
    assert set(watchdog._catchup_probes) == {
        "job_sync_daily_price", "job_sync_valuation", "job_sync_index",
        "job_sync_stock_list", "job_sync_calendar", "job_sync_finance",
        "job_nightly_backfill"}
    assert watchdog._catchup_probes["job_sync_stock_list"](0) is True  # _never_catchup
    # 注册了探针却没排进 JOB_SCHEDULE → 永远轮不到补跑（静默失效），必须钉住
    assert set(watchdog._catchup_probes) <= set(watchdog.JOB_SCHEDULE)


def test_catchup_probes_cover_declared_schedules(monkeypatch):
    """反向：JOB_SCHEDULE 里的每个 job 都得有探针，否则那条时间窗是死配置"""
    from app import tasks

    monkeypatch.setattr(tasks, "collect_enabled", lambda ds: True)  # 放闸
    watchdog._catchup_probes.clear()
    tasks.register_catchups()
    assert set(watchdog.JOB_SCHEDULE) <= set(watchdog._catchup_probes)
