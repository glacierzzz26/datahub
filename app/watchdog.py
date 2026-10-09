"""任务级看门狗（Issue #14）：识别「进程活着但 job 卡死」并自愈 + 告警 + 补跑。

**为什么需要**：现有 `/healthz` 由独立 daemon 线程提供，**调度器卡死时照样 200**；
容器 `restart: unless-stopped` **只在进程退出时生效**，卡死不退出就永不重启；
且容器内无 docker.sock/CLI，应用无法重启别的容器。→ 09-08 采集卡死后无人知晓，
09-09 被 APScheduler `skipped`，行情静默断档两日。本模块补上这一环。

**自愈方式**：`os._exit(1)` 让进程退出 → Docker `restart: unless-stopped` 拉起新容器。
⚠️ **必须 `os._exit`，不能 `sys.exit`**：SystemExit 会触发 atexit /
`concurrent.futures.thread._python_exit`，后者 join 每个 worker 线程 → 正是要逃的死锁
（卡死 worker 不可杀，`sys.exit` 会卡在退不出去）。

**为何用独立 daemon 线程而非 APScheduler job**：APScheduler 默认 executor 仅 10 worker，
若多个 job 卡死占满，看门狗自身就永无机会跑。独立线程不受 executor / max_instances /
调度器主循环阻塞影响。（本线程自身卡死是唯一覆盖不到的情形，交给宿主侧哨兵与 /healthz。）

**告警通道（零新增管道）**：退出前写一行 `task_run(status='failed')`，quant-engine 的
`notify_scheduler._check_task_alerts` 每 1 分钟扫当日 failed 行推飞书红卡——且它**不受
数据新鲜度门控**（对比：09-09 静默的真因是 `_schedule_matches` 的 trading_day 门控）。

**重启补跑（`startup_catchup`）**：APScheduler 用 MemoryJobStore，重启**不会**重跑已过去的
cron 触发点（也不能靠 misfire_grace_time——它存不住重启前的运行）。故重启后需主动补跑，
否则进程活着但当天缺口仍在。
"""
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime

logger = logging.getLogger("watchdog")

# 每个 job 的运行时长上限（秒）；env `COLLECTOR_JOB_LIMIT_<JOB 大写>` 可逐个覆盖。
# 默认按 **800 只** 校准——扩池（Issue #13）翻 a_share 前必须重新标定，
# 否则 18:10 单跑 90min–3h 会必然触发误判重启。
JOB_LIMITS = {
    "job_sync_stock_list": 1800,
    "job_sync_calendar": 900,
    "job_sync_index": 900,
    "job_sync_finance": 3600,
    "job_nightly_backfill": 14400,
    "job_sync_daily_price": 10800,
    "job_sync_valuation": 10800,
}
DEFAULT_LIMIT = 3600
# 各 job 的今日计划时刻 (时, 分) —— 与 tasks.py 的 add_job 保持一致，供启动补跑判定时间窗。
# 仅含「当日必须产出」的 job。
JOB_SCHEDULE = {
    "job_sync_stock_list": (9, 0),
    "job_sync_calendar": (9, 5),
    "job_sync_finance": (18, 0),
    "job_nightly_backfill": (18, 5),
    "job_sync_daily_price": (18, 10),
    "job_sync_index": (18, 15),
    "job_sync_valuation": (18, 15),
}
# 退出计数（挂载卷 → 跨容器重启留存）：防 `restart: unless-stopped` 无退避的抖动环路
EXIT_THRESHOLD_FILE = os.getenv("COLLECTOR_WATCHDOG_EXIT_FILE", "/app/logs/.watchdog_exits")
MAX_EXITS_PER_DAY = 3
# 巡检间隔（秒）
SCAN_INTERVAL = 60

_running: dict[str, float] = {}  # job 名 → monotonic 起始时刻
_lock = threading.Lock()
# 供 startup_catchup 判定「今日是否已完成」；由 tasks.py 注册（避免本模块 import 采集器）
_catchup_probes: dict[str, callable] = {}


def _limit(job: str) -> float:
    """job 的运行时长上限：env 覆盖 > JOB_LIMITS > DEFAULT_LIMIT"""
    env = os.getenv(f"COLLECTOR_JOB_LIMIT_{job.upper()}")
    if env:
        try:
            return float(env)
        except ValueError:
            logger.warning("环境变量 COLLECTOR_JOB_LIMIT_%s=%r 非法，忽略", job.upper(), env)
    env_default = os.getenv("COLLECTOR_JOB_LIMIT_DEFAULT")
    if env_default and job not in JOB_LIMITS:
        try:
            return float(env_default)
        except ValueError:
            pass
    return float(JOB_LIMITS.get(job, DEFAULT_LIMIT))


def register_catchup(job: str, probe) -> None:
    """注册「该 job 今日是否已完成」的判定函数（返回 True = 已完成，无需补跑）"""
    _catchup_probes[job] = probe


@contextmanager
def job_guard(job: str):
    """包住 job 体：登记起始时刻，退出即摘除（供 overdue_jobs 判定）"""
    with _lock:
        _running[job] = time.monotonic()
    try:
        yield
    finally:
        with _lock:
            _running.pop(job, None)


def is_running(job: str) -> bool:
    return job in _running


def guarded(fn):
    """装饰器：给 job 计时（job 名 = 函数名，与 JOB_LIMITS 键一致）。

    优于在各 job 体内手写 `with job_guard(...)`——不必重排缩进，且 `startup_catchup`
    经 `getattr(tasks, job)` 拿到的就是包装后的函数，两条路径都受计时保护。
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with job_guard(fn.__name__):
            return fn(*args, **kwargs)

    return wrapper


def overdue_jobs(now: float | None = None) -> list[tuple[str, float]]:
    """超过各自上限仍在跑的 job 列表 [(job, 已运行秒数)]。**纯函数**，无 DB 无线程。"""
    if now is None:
        now = time.monotonic()
    with _lock:
        snapshot = dict(_running)
    out = []
    for job, started in snapshot.items():
        elapsed = now - started
        if elapsed > _limit(job):
            out.append((job, elapsed))
    return out


def _max_leaked() -> int:
    from app.collectors.base import MAX_LEAKED_WORKERS

    env = os.getenv("COLLECTOR_MAX_LEAKED_WORKERS")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return MAX_LEAKED_WORKERS


def _exits_today() -> int:
    """今日已自杀次数（从挂载卷文件读；跨重启留存）。文件按日期分隔。"""
    try:
        with open(EXIT_THRESHOLD_FILE, encoding="utf-8") as f:
            day, n = f.read().strip().split()
            return int(n) if day == date.today().isoformat() else 0
    except Exception:
        return 0


def _bump_exits() -> int:
    n = _exits_today() + 1
    try:
        os.makedirs(os.path.dirname(EXIT_THRESHOLD_FILE), exist_ok=True)
        with open(EXIT_THRESHOLD_FILE, "w", encoding="utf-8") as f:
            f.write(f"{date.today().isoformat()} {n}")
    except Exception:
        logger.warning("写退出计数文件失败（不影响退出）: %s", EXIT_THRESHOLD_FILE)
    return n


def _record_failure(job: str, message: str, detail: dict) -> None:
    """有界 best-effort 写 task_run failed 行（告警通道）。失败只记日志。"""
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from app.db import get_session
        from app.models.tables import TaskRun

        db = get_session()
        try:
            stmt = pg_insert(TaskRun).values(
                task_name="collector_watchdog", run_date=date.today(),
                status="failed", message=message, detail=detail,
            ).on_conflict_do_update(
                index_elements=["task_name", "run_date"],
                set_={"status": "failed", "message": message, "detail": detail},
            )
            db.execute(stmt)
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("写 watchdog task_run 失败（告警将退回文件日志）")


def alert_and_exit(reason: str, job: str, elapsed: float = 0.0,
                   exit_fn=os._exit) -> None:
    """告警 + 退出进程（exit_fn 可注入便于测试）。

    顺序刻意如此（`os._exit` 不刷新缓冲）：
      1. logger.critical（StreamHandler 逐条 flush，最后一行必留）
      2. 写 /app/logs/watchdog.log（DB 卡死时仍留证）
      3. best-effort 写 task_run failed（→ quant-engine 推飞书红卡）
      4. 退出
    """
    from app.collectors.base import leaked_workers

    n = _bump_exits()
    detail = {
        "reason": reason, "job": job, "elapsed": round(elapsed, 1),
        "leaked_workers": leaked_workers(), "exit_count": n,
        "pid": os.getpid(), "at": datetime.now().isoformat(timespec="seconds"),
    }
    message = (f"采集任务卡死：{job} 已运行 {round(elapsed)}s（{reason}），"
               f"触发容器重启（今日第 {n} 次）")
    logger.critical("%s — 详情 %s", message, detail)

    try:
        log_path = os.path.join(os.path.dirname(EXIT_THRESHOLD_FILE), "watchdog.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{detail['at']} {message} {detail}\n")
    except Exception:
        pass

    if n <= MAX_EXITS_PER_DAY:
        _record_failure(job, message, detail)
        logger.critical("看门狗触发进程退出（os._exit(1)）→ 依赖 Docker restart 拉起")
        exit_fn(1)
    else:
        # 超今日退出预算：只告警不退出，防 restart:unless-stopped 无退避的抖动环路
        logger.critical(
            "今日已退出 %s 次，超上限 %s —— 仅告警不再自动重启（需人工介入，"
            "或由宿主侧看门狗处置）", n, MAX_EXITS_PER_DAY)
        _record_failure(job, message + "（超今日重启预算，未自动重启）", detail)


def healthz_status() -> tuple[int, dict]:
    """给 /healthz 用：有 job 越界或泄漏超上限 → (503, ...)，否则 (200, ok)"""
    from app.collectors.base import leaked_workers

    overdue = overdue_jobs()
    leaks = leaked_workers()
    if overdue:
        job, elapsed = overdue[0]
        return 503, {"status": "stale", "job": job,
                     "elapsed": round(elapsed, 1), "overdue": len(overdue)}
    if leaks > _max_leaked():
        return 503, {"status": "stale", "reason": "leaked_workers", "leaked": leaks}
    return 200, {"status": "ok", "running": len(_running), "leaked": leaks}


def startup_catchup(window_hours: float = 6.0,
                    now: datetime | None = None) -> list[str]:
    """重启后补跑错过的任务（APScheduler MemoryJobStore 不会重跑过去的触发点）。

    门控（全部满足才补跑）：
      - 交易日（trade_calendar.is_open == 1）
      - 距该 job 今日计划时刻在 window_hours 内（避免凌晨重启去跑昨天的活）
      - 该 job 当前未在跑（不干扰正在执行的）
      - 注册的 probe 判定「今日未完成」

    `now` 可注入（默认当前时刻）——时间窗判定依赖"现在几点"，不注入就没法在
    任意钟点稳定测试。

    返回补跑的 job 名列表。由 tasks.py 传入 job 调用函数（本模块不 import 采集器）。
    """
    from sqlalchemy import select

    from app.db import get_session
    from app.models.tables import TradeCalendar

    db = get_session()
    try:
        is_open = db.execute(
            select(TradeCalendar.is_open).where(TradeCalendar.cal_date == date.today())
        ).scalar()
    finally:
        db.close()
    if not is_open:
        logger.info("非交易日或日历缺失，跳过启动补跑")
        return []

    if now is None:
        now = datetime.now()
    ran: list[str] = []
    for job, probe in _catchup_probes.items():
        if is_running(job):
            continue
        hh, mm = JOB_SCHEDULE.get(job, (None, None))
        if hh is None:
            continue
        planned = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = (now - planned).total_seconds() / 3600.0
        if not (0 <= delta <= window_hours):
            continue
        try:
            if probe(delta):
                continue
        except Exception:
            logger.exception("补跑探针失败，跳过 %s", job)
            continue
        logger.warning("启动补跑：%s 今日计划 %02d:%02d 未完成，立即补跑", job, hh, mm)
        from app import tasks as _tasks

        fn = getattr(_tasks, job, None)
        if fn is None:
            continue
        try:
            fn()
            ran.append(job)
        except Exception:
            logger.exception("启动补跑失败：%s", job)
    return ran


def _watchdog_loop() -> None:
    """看门狗主循环（独立 daemon 线程）：每 SCAN_INTERVAL 秒巡检一次。"""
    from app.collectors.base import leaked_workers

    while True:
        time.sleep(SCAN_INTERVAL)
        try:
            overdue = overdue_jobs()
            if overdue:
                job, elapsed = max(overdue, key=lambda x: x[1])
                alert_and_exit(f"运行超时 >{_limit(job)}s", job, elapsed)
            leaks = leaked_workers()
            if leaks > _max_leaked():
                alert_and_exit(f"遗弃 worker 线程 {leaks} > {_max_leaked()}", "-", 0.0)
        except Exception:
            # 看门狗自身异常不得让它停摆——记录后继续下一轮
            logger.exception("看门狗巡检异常（继续）")


def start_watchdog() -> threading.Thread:
    """启动看门狗 daemon 线程（幂等）"""
    t = threading.Thread(target=_watchdog_loop, name="watchdog", daemon=True)
    t.start()
    logger.info("看门狗已启动（巡检间隔 %ss，退出预算 %s/日，计数文件 %s）",
                SCAN_INTERVAL, MAX_EXITS_PER_DAY, EXIT_THRESHOLD_FILE)
    return t


def spawn_startup_catchup(delay: int = 60) -> threading.Thread:
    """延迟 delay 秒后执行一次启动补跑（等调度器就绪）"""
    def _go():
        time.sleep(delay)
        try:
            ran = startup_catchup()
            if ran:
                logger.info("启动补跑完成：%s", ran)
        except Exception:
            logger.exception("启动补跑异常")

    t = threading.Thread(target=_go, name="startup_catchup", daemon=True)
    t.start()
    return t
