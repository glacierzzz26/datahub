"""定时任务调度（APScheduler）

时间表（见 docs/技术准备文档.md §7.4）：
09:00 股票列表 + 交易日历 | 18:00 财务 + 18:05 回填 | 18:10 当日行情 | 18:15 指数/估值

**Phase 2 采集闸门（两层，"部署零行为"）**：
  - 注册层：`__main__` 经 `_add()` 注册任务前查 `collect_enabled(dataset)`，闸门关 →
    不 `add_job`（默认 ⇒ `scheduler.get_jobs()==[]`）。
  - 调用层：每个 job 经 `@collect_gated(dataset)` 装饰，闸门关 → 立即返回（挡
    `watchdog.startup_catchup` 经 `getattr(tasks, job)` 绕过注册直调）。
  默认全关时**不** `register_catchups()`、**不** `spawn_startup_catchup()` —— 连对
  `trade_calendar` 的补跑探针查询都不发生。
"""
import logging
import time
from datetime import date, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import func, select

from app.collect_config import DAILY_FALLBACK_DAYS, DAILY_SYNC_INTERVAL, collect_enabled
from app.models.tables import DailyPrice
from app.watchdog import (
    guarded,
    healthz_status,
    register_catchup,
    spawn_startup_catchup,
    start_watchdog,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger("tasks")

# job 名 → 采集数据集（闸门白名单键）。job_sync_hotspot/job_consume_remediation 已随
# Phase 2 移除（热点走 datahub external provider；自愈留 steady）。
_JOB_DATASET = {
    "job_sync_stock_list": "stock_basic",
    "job_sync_calendar": "calendar",
    "job_sync_index": "index",
    "job_sync_daily_price": "daily",
    "job_sync_valuation": "valuation",
    "job_sync_finance": "finance",
    "job_nightly_backfill": "daily",
}


def collect_gated(dataset: str):
    """调用层闸门：dataset 未放闸 → 被装饰的 job **立即返回**（不采集、不写库）。

    叠在 `@guarded` **之上**（顺序敏感，外闸内计时）::

        @collect_gated("calendar")
        @guarded
        def job_sync_calendar(): ...

    为什么还要调用层：`watchdog.startup_catchup` 经 `getattr(tasks, job)` 直接拿函数
    调用，绕过注册层；手工 cli 同理。两层都挡才算"部署零行为"。
    """

    def deco(fn):
        import functools

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not collect_enabled(dataset):
                logger.info("采集闸门关闭，跳过 %s（dataset=%s）", fn.__name__, dataset)
                return None
            return fn(*args, **kwargs)

        return wrapper

    return deco


def _any_collect_enabled() -> bool:
    """是否至少有一个采集数据集放闸（任一放闸才注册补跑探针/启动补跑）。"""
    return any(collect_enabled(ds) for ds in set(_JOB_DATASET.values()))


def _start_healthz(service: str, default_port: int, status_provider=None) -> None:
    """启动最小健康端点（Issue #9-3）：backend 容器不挂 docker.sock、无 docker CLI，
    探活改走内网 HTTP —— 本进程是纯 APScheduler 守护，需此 /healthz 供 backend 探测。
    BlockingScheduler 占主线程 → 用守护线程跑 stdlib ThreadingHTTPServer。

    `status_provider`（Issue #14）：返回 (code, payload) 反映**调度器**存活——
    原实现只答静态 ok，job 卡死时照样 200（liveness ≠ freshness）。不传则维持原语义。
    """
    import json
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    port = int(os.environ.get("HEALTH_PORT", default_port))

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server 命名约定)
            if self.path != "/healthz":
                self.send_response(404)
                self.end_headers()
                return
            if status_provider is not None:
                code, payload = status_provider()
                payload = {"service": service, **payload}
            else:
                code, payload = 200, {"status": "ok", "service": service}
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 健康轮询每 ~10s 一次，静默避免刷日志
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("健康端点已启动 :%s/healthz (service=%s)", port, service)


def get_session():
    """DB 会话（各 job 与补跑探针共用）"""
    from app.db import get_session as _get

    return _get()


# ---------- 启动补跑探针（Issue #14）----------
# 每个探针回答「该 job 今日是否已完成」，返回 True = 无需补跑。
# 全部按业务表判据（幂等，不依赖 task_run——采集侧原先不写账本）。

def _probe_daily(_delta: float) -> bool:
    """当日行情是否已同步：daily_price 含今日行（排除 sh% 指数行）"""
    from sqlalchemy import func

    db = get_session()
    try:
        latest = db.execute(
            select(func.max(DailyPrice.trade_date))
            .where(~DailyPrice.code.like("sh%"))
        ).scalar()
        return latest == date.today()
    finally:
        db.close()


def _probe_valuation(_delta: float) -> bool:
    """当日估值是否已同步"""
    from sqlalchemy import func

    from app.models.tables import DailyValuation

    db = get_session()
    try:
        latest = db.execute(select(func.max(DailyValuation.trade_date))).scalar()
        return latest == date.today()
    finally:
        db.close()


def _probe_index(_delta: float) -> bool:
    """当日指数行情是否已同步"""
    from sqlalchemy import func

    db = get_session()
    try:
        latest = db.execute(
            select(func.max(DailyPrice.trade_date))
            .where(DailyPrice.code.like("sh%"))
        ).scalar()
        return latest == date.today()
    finally:
        db.close()


def _never_catchup(_delta: float) -> bool:
    """恒 True：该 job 不参与启动补跑（有自身周期/幂等语义，重跑无益）"""
    return True


@collect_gated("stock_basic")
@guarded
def job_sync_stock_list():
    from app.collectors.stock import StockCollector

    StockCollector(get_session()).run()


@collect_gated("calendar")
@guarded
def job_sync_calendar():
    from app.collectors.calendar import CalendarCollector

    CalendarCollector(get_session()).run()


@collect_gated("index")
@guarded
def job_sync_index():
    from app.collect_config import index_code_list
    from app.collectors.index import IndexCollector

    for symbol in index_code_list():
        IndexCollector(get_session()).run(symbol=symbol)


def _daily_sync_codes(db) -> list[str]:
    """每日同步范围：采集范围内的股票（Issue #13 起走 collect_codes helper）

    旧实现读 `DISTINCT code FROM daily_price`（**不看 universe**）——一旦 daily_price
    扩到 5212，18:10 逐只同步会自动膨胀到 5212 只 × ~2s ≈ 2.9h，且无任何报错
    （Issue #13 R4）。改走 helper：默认 pool 分支 = 现状 800 只，a_share 分支 =
    全量。`include_pool=True` 保证策略股票池兜底在采（data_scope 漂移也不漏采）。
    顺带修既有瑕疵：`sz399106`（深证综指，索引器写入 daily_price）不再被当股票同步。
    """
    from app.collectors.scope import collect_codes

    return collect_codes(db, include_pool=True)


def _latest_close_factor(db, codes: list[str]) -> dict[str, tuple]:
    """每只股票库内最近一行的 (close, adj_factor)（Postgres DISTINCT ON）

    供快照路径：adj_factor 延续 + 除权探测基准。
    """
    from sqlalchemy import select

    rows = db.execute(
        select(DailyPrice.code, DailyPrice.close, DailyPrice.adj_factor)
        .where(DailyPrice.code.in_(codes))
        .distinct(DailyPrice.code)
        .order_by(DailyPrice.code, DailyPrice.trade_date.desc())
    ).all()
    return {code: (close, factor) for code, close, factor in rows}


def _snapshot_sync(db, codes: list[str], end: date) -> list[str]:
    """腾讯批量快照当日同步（~17s 拉全池 raw）→ 返回需逐只回退的股票。

    快照只供不复权 OHLCV，不含复权因子：adj_factor 默认**延续**库内最近值
    （无除权日因子不变）；以下情形回退逐只源链重算（自动过 guard_factor +
    cross_check_splits，自纠）：
      - 库内无历史（新股，无因子可延续）
      - 快照「昨收」≠ 库内最近 close（超 TENCENT_DIV_TOL）→ 疑似除权
      - 库内最近 adj_factor 为空（无可延续值，避免落 NULL 覆盖）

    **deferred 上限**（TENCENT_DEFER_MAX）：扩池首日全池无历史 → 全部 deferred
    → 逐只补 ~2s/只 把快照收益归零（Issue #13 R5）。超上限只回退前 N 只，其余
    留给夜间回填（回填后即有历史，次日快照命中），并告警。上限内的回退不告警。
    """
    from app.collect_config import TENCENT_DEFER_MAX, TENCENT_DIV_TOL
    from app.collectors.daily import upsert_snapshot_rows
    from app.sources import tencent

    latest = _latest_close_factor(db, codes)
    snaps = tencent.snapshot_rows(codes, end)
    by_code = {r["code"]: r for r in snaps}
    deferred: list[str] = []
    rows: list[dict] = []
    for code in codes:
        snap = by_code.get(code)
        base = latest.get(code)
        if snap is None or base is None or base[1] is None:
            deferred.append(code)
            continue
        db_close, db_factor = base
        if db_close and abs(float(snap["prev_close"]) / float(db_close) - 1) > TENCENT_DIV_TOL:
            # 快照昨收 ≠ 库内最近收盘 → 疑似除权/复权口径变动，回退逐只重算因子
            logger.info("快照除权探测命中 %s（昨收 %s vs 库内 %s），回退逐只",
                        code, snap["prev_close"], db_close)
            deferred.append(code)
            continue
        snap["adj_factor"] = float(db_factor)  # 因子延续
        rows.append(snap)
    n = upsert_snapshot_rows(db, rows)
    if len(deferred) > TENCENT_DEFER_MAX:
        logger.warning(
            "腾讯快照回退 %s 只超上限 %s：仅回退前 %s 只，其余交夜间回填"
            "（扩池首日常见，回填后次日即命中快照）",
            len(deferred), TENCENT_DEFER_MAX, TENCENT_DEFER_MAX)
        deferred = deferred[:TENCENT_DEFER_MAX]
    logger.info("腾讯快照入库 %s 只，回退逐只 %s 只", n, len(deferred))
    return deferred


@collect_gated("daily")
@guarded
def job_sync_daily_price():
    """18:10 同步当日行情。

    TENCENT_SNAPSHOT 开启（且 scope 点名 daily）→ 先走腾讯批量快照（全池 ~17s，
    因子延续 + 除权自动回退），仅回退股逐只补；否则全量逐只（BaoStock 主源，
    失败降级 AkShare，见 DailyCollector 源链）。闸门默认关 = 现状。
    """
    from app.collect_config import DAILY_SYNC_INTERVAL, tencent_snapshot_enabled
    from app.collectors.daily import DailyCollector

    db = get_session()
    codes = _daily_sync_codes(db)
    end = date.today()
    logger.info("每日行情同步：%s 只股票", len(codes))
    if tencent_snapshot_enabled():
        codes = _snapshot_sync(db, codes, end)
        logger.info("腾讯快照路径：%s 只需逐只补", len(codes))
    ok = fail = 0
    for code in codes:
        max_d = db.execute(
            select(func.max(DailyPrice.trade_date)).where(DailyPrice.code == code)
        ).scalar()
        start = max_d + timedelta(days=1) if max_d else end - timedelta(days=DAILY_FALLBACK_DAYS)
        if start > end:
            continue
        if DailyCollector(db).run(code, start, end):
            ok += 1
        else:
            fail += 1
        time.sleep(DAILY_SYNC_INTERVAL)
    logger.info("每日行情同步完成：成功 %s，失败 %s", ok, fail)


@collect_gated("finance")
@guarded
def job_sync_finance():
    """18:00 财务数据增量：最近 4 个报告期（覆盖财报季尾部披露）"""
    from app.collect_config import FINANCE_SYNC_QUARTERS
    from app.collectors.finance import FinanceCollector, quarter_ends

    FinanceCollector(get_session()).run(
        report_periods=quarter_ends(FINANCE_SYNC_QUARTERS)
    )


@collect_gated("valuation")
@guarded
def job_sync_valuation():
    """18:15 同步日度估值：BaoStock 主源逐只（阶段 3 开关）；失败/未配置降级
    AkShare 逐只（ValuationCollector 内 BaoStock→AkShare 兜底链）"""
    from app.collectors.scope import collect_codes
    from app.collectors.valuation import ValuationCollector
    from app.models.tables import DailyValuation

    db = get_session()
    codes = collect_codes(db)
    latest = {
        code: max_d
        for code, max_d in db.execute(
            select(DailyValuation.code, func.max(DailyValuation.trade_date))
            .group_by(DailyValuation.code)
        ).all()
    }
    todo = [c for c in codes if latest.get(c) is None or latest[c] < date.today()]
    logger.info("估值同步：%s 只中 %s 只需更新", len(codes), len(todo))
    ok = fail = 0
    for code in todo:
        if ValuationCollector(db).run(code):
            ok += 1
        else:
            fail += 1
        time.sleep(DAILY_SYNC_INTERVAL)
    logger.info("估值同步完成：成功 %s，失败 %s", ok, fail)


@collect_gated("daily")
@guarded
def job_nightly_backfill():
    """18:05 每日缺口回填（仅交易日）：日线补未覆盖到起始日的股票；估值补滞后股票。

    断点续传天然幂等（covered_codes 按 min(trade_date) 判定），正常日只补新股/缺口
    （16:30/16:45 已同步当日行情与估值，正常日 todo 为空，分钟级）；
    首次全量仍由 python -m app.collectors.backfill 手动触发。
    交易日门控：TradeCalendar.is_open != 1（含无记录）→ 跳过，避免周末/节假日无谓拉取。
    """
    from app.collect_config import BACKFILL_START
    from app.collectors.backfill import BackfillJob
    from app.models.tables import TradeCalendar

    db = get_session()
    is_open = db.execute(
        select(TradeCalendar.is_open).where(TradeCalendar.cal_date == date.today())
    ).scalar()
    if not is_open:
        logger.info("非交易日或日历缺失，跳过夜间回填")
        return
    start_date = date.fromisoformat(BACKFILL_START.replace("-", ""))
    BackfillJob(db).daily(start_date, date.today())
    BackfillJob(db).valuation()


def register_catchups() -> None:
    """注册启动补跑探针（Issue #14）：重启后 APScheduler 不会重跑已过去的 cron
    触发点，由 watchdog.startup_catchup 按探针判定并主动补跑，否则进程活着缺口仍在。
    行情/估值/指数按业务表判「今日是否已产出」；其余日频 job 有自身周期与幂等语义，
    不参与补跑。独立成函数便于单测直接调用。

    ⚠️ 必须定义在 `__main__` 块**之前**：`__main__` 里会直接调用它，而模块体是
    顺序执行的——定义在后面会 NameError（已实测：容器启动即崩，见 Issue #14）。

    闸门：只为**已放闸**的数据集注册探针（挡 startup_catchup 经 getattr 直调被停的 job）。
    """
    _probes = {
        "job_sync_daily_price": _probe_daily,
        "job_sync_valuation": _probe_valuation,
        "job_sync_index": _probe_index,
        "job_sync_stock_list": _never_catchup,
        "job_sync_calendar": _never_catchup,
        "job_sync_finance": _never_catchup,
        "job_nightly_backfill": _never_catchup,
    }
    for _j, _probe in _probes.items():
        if collect_enabled(_JOB_DATASET[_j]):
            register_catchup(_j, _probe)


# (job 函数, cron 时, 分) —— 与 watchdog.JOB_SCHEDULE 对齐（当日数据统一 18:00+ BaoStock 产出）。
_JOB_SPECS = [
    (job_sync_stock_list, 9, 0),
    (job_sync_calendar, 9, 5),
    (job_sync_finance, 18, 0),
    (job_nightly_backfill, 18, 5),
    (job_sync_daily_price, 18, 10),
    (job_sync_index, 18, 15),
    (job_sync_valuation, 18, 15),
]
# max_instances=1 + coalesce=True：任务超时（如扩池后回填/同步变长）时**不并发重入**，
# 堆积的触发折叠成一次（Issue #13 R16）。原实现无此约束 → interval 5min 的自愈若跑超
# 5min 会叠起第二个实例，两个同时逐只补数、同时写库。
_JOB_KW = {"max_instances": 1, "coalesce": True}


def register_jobs(scheduler) -> list[str]:
    """注册层闸门：只为**已放闸**的数据集注册 cron 任务，返回已注册的 job 名。

    默认（闸门全关）⇒ 零注册 ⇒ `scheduler.get_jobs()==[]`。独立成函数便于单测直接
    调用（`__main__` 块 pytest 不执行）。
    """
    registered: list[str] = []
    for job, hour, minute in _JOB_SPECS:
        ds = _JOB_DATASET[job.__name__]
        if not collect_enabled(ds):
            logger.info("采集闸门关闭，不注册 %s（dataset=%s）", job.__name__, ds)
            continue
        scheduler.add_job(job, "cron", hour=hour, minute=minute, **_JOB_KW)
        registered.append(job.__name__)
    return registered


if __name__ == "__main__":
    # 请求层超时（Issue #14）：必须在任何网络调用前装，覆盖 AkShare 全部无 timeout 调用。
    # 只在进程入口装，不在 import 期装（否则污染测试集）。
    from app.providers.net import install_http_timeouts

    install_http_timeouts()

    scheduler = BlockingScheduler()

    # 注册层闸门：默认全关 ⇒ 零任务注册（翻闸 = 改 .env + 重启）。
    register_jobs(scheduler)

    _start_healthz("collector", 9200, status_provider=healthz_status)

    # 调用层闸门之外，全关时**连**补跑探针/看门狗都不启（连 trade_calendar 查询都不发生）。
    if _any_collect_enabled():
        register_catchups()
        start_watchdog()
        spawn_startup_catchup()
    else:
        logger.info("采集闸门全关：仅启动调度器与 /healthz，不注册任何任务、不启动看门狗/补跑")
    logger.info("collector 调度器启动，等待定时任务...")
    scheduler.start()

