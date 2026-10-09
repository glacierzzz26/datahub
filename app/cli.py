"""采集服务手动入口（日常触发 / 回填）

用法：
    python -m app.cli sync-stock          # 股票列表 + 股票池标记
    python -m app.cli sync-calendar       # 交易日历
    python -m app.cli sync-index          # 指数行情（沪深300/中证500）
    python -m app.cli sync-daily          # 全部待同步股票当日行情（股票池 + 已有数据）
    python -m app.cli sync-daily --code 600519 [--start 20260801] [--end 20260819]
    python -m app.cli sync-finance [--quarters 4]
    python -m app.cli sync-valuation [--codes 600519,000001]
    python -m app.cli backfill [--start 20160801] [--end 20260819] [--quarters 20]
                                [--codes 600519,000001] [--dry-run]
    python -m app.cli backfill-valuation [--codes 600519,000001] [--dry-run]
"""
import argparse
import logging
import sys
from datetime import date, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger("cli")

# cli 子命令 → 采集数据集（闸门白名单键）；audit-tencent 只读不采集，不在表内。
_CMD_DATASET = {
    "sync-stock": "stock_basic",
    "sync-calendar": "calendar",
    "sync-index": "index",
    "sync-daily": "daily",
    "sync-finance": "finance",
    "sync-valuation": "valuation",
    "backfill": "daily",
    "backfill-valuation": "valuation",
}


def _codes_for_daily_sync(db) -> list[str]:
    """每日同步范围：采集范围内的股票（Issue #13 起走 collect_codes helper）

    旧实现 = universe 池 ∪ 已有日行情数据的股票；后者不看 universe（同 tasks
    `_daily_sync_codes` 的 R4 自放大坑），故整体收敛到 helper。默认 pool 分支
    = 现状；`include_pool=True` 保证策略池兜底在采。
    """
    from app.collectors.scope import collect_codes

    return collect_codes(db, include_pool=True)


def cmd_sync_daily(args):
    """增量同步日行情：逐只拉取（上次入库日 → 今天，可用 --start/--end 覆盖）"""
    import time

    from sqlalchemy import func, select

    from app.collect_config import DAILY_FALLBACK_DAYS, DAILY_SYNC_INTERVAL
    from app.collectors.daily import DailyCollector
    from app.db import get_session
    from app.models.tables import DailyPrice

    db = get_session()
    end = date.fromisoformat(args.end) if args.end else date.today()
    if args.code:
        codes = [args.code.zfill(6)]
    else:
        codes = _codes_for_daily_sync(db)
        logger.info("每日同步范围：%s 只", len(codes))
    ok_count = fail_count = 0
    for code in codes:
        if args.start:
            start = date.fromisoformat(args.start)
        else:
            max_d = db.execute(
                select(func.max(DailyPrice.trade_date))
                .where(DailyPrice.code == code)
            ).scalar()
            start = (
                max_d + timedelta(days=1)
                if max_d else end - timedelta(days=DAILY_FALLBACK_DAYS)
            )
        if start > end:
            logger.debug("%s 已是最新，跳过", code)
            continue
        ok = DailyCollector(db).run(code, start, end)
        ok_count += ok
        fail_count += not ok
        time.sleep(DAILY_SYNC_INTERVAL)
    logger.info("每日行情同步完成：成功 %s，失败 %s", ok_count, fail_count)
    return fail_count == 0


def cmd_sync_finance(args):
    from app.collectors.finance import sync_finance
    return sync_finance(quarters=args.quarters)


def cmd_backfill(args):
    from app.collectors.backfill import run_backfill
    run_backfill(args.start, args.end, args.quarters, args.dry_run, args.codes)
    return True


def cmd_sync_valuation(args):
    """同步日度估值（跳过当日已最新的股票）"""
    from app.collectors.valuation import sync_valuation

    codes = None
    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",")]
    return sync_valuation(codes)


def cmd_backfill_valuation(args):
    """估值回填（全量拉取，断点续传按 max(trade_date) 判定）"""
    from app.collect_config import BACKFILL_BATCH_SIZE, RATE_LIMIT_SECONDS
    from app.collectors.backfill import BackfillJob
    from app.db import get_session

    job = BackfillJob(get_session(), rate_limit=RATE_LIMIT_SECONDS,
                      batch_size=BACKFILL_BATCH_SIZE, dry_run=args.dry_run)
    codes = [c.strip().zfill(6) for c in args.codes.split(",")] if args.codes else None
    job.valuation(codes)
    return True


def cmd_audit_tencent(args):
    """只读对账：腾讯日K vs daily_price 的 close/volume/amount 分板中位比值

    翻转前验证单位表（科创板 ÷100、北交所单位当前唯一未实证项）。不写库。
    """
    from sqlalchemy import select

    from app.db import get_session
    from app.models.tables import DailyPrice
    from app.sources import tencent

    db = get_session()
    codes = ([c.strip().zfill(6) for c in args.codes.split(",")]
             if args.codes else None)
    if not codes:
        codes = sorted(db.execute(
            select(DailyPrice.code).where(DailyPrice.code.not_like("sh%"))
            .distinct()).scalars().all())[:20]
    for code in codes:
        q = select(DailyPrice.trade_date, DailyPrice.close, DailyPrice.volume,
                   DailyPrice.amount).where(DailyPrice.code == code)
        if args.start:
            q = q.where(DailyPrice.trade_date >= date.fromisoformat(args.start))
        if args.end:
            q = q.where(DailyPrice.trade_date <= date.fromisoformat(args.end))
        db_rows = {d: (c, v, a) for d, c, v, a in db.execute(q).all()}
        if not db_rows:
            logger.info("%s 库内无数据，跳过", code)
            continue
        start = args.start or min(db_rows).strftime("%Y%m%d")
        end = args.end or max(db_rows).strftime("%Y%m%d")
        try:
            raw = tencent.daily_raw(code, start, end)
        except Exception as e:
            logger.warning("%s 腾讯日K拉取失败：%s", code, e)
            continue
        ratios = {"close": [], "volume": [], "amount": []}
        mismatch = 0
        for _, r in raw.iterrows():
            d = date.fromisoformat(str(r["日期"]))
            if d not in db_rows:
                continue
            dc, dv, da = db_rows[d]
            for key, tv, dbv in (("close", r["收盘"], dc),
                                 ("volume", r["成交量"], dv),
                                 ("amount", r["成交额"], da)):
                if tv and dbv:
                    ratios[key].append(float(tv) / float(dbv))
            if dc and r["收盘"] and abs(float(r["收盘"]) / float(dc) - 1) > 1e-4:
                mismatch += 1
        med = {k: (sorted(v)[len(v) // 2] if v else None) for k, v in ratios.items()}
        logger.info("%s n=%s 中位比值 close=%s volume=%s amount=%s 不一致=%s",
                    code, len(raw), med["close"], med["volume"], med["amount"],
                    mismatch)
        if args.snapshot:
            try:
                snaps = tencent.snapshot_rows([code])
                logger.info("%s 快照：%s", code, snaps[0] if snaps else "缺")
            except Exception as e:
                logger.warning("%s 快照拉取失败：%s", code, e)
    return True


def main():
    # 请求层超时（Issue #14）：手工入口同样要装，覆盖 AkShare 无 timeout 调用
    from app.providers.net import install_http_timeouts

    install_http_timeouts()

    parser = argparse.ArgumentParser(prog="quant-collector")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sync-stock", help="股票列表 + 股票池标记")
    sub.add_parser("sync-calendar", help="交易日历")
    sub.add_parser("sync-index", help="指数行情")

    p_daily = sub.add_parser("sync-daily", help="增量同步日行情")
    p_daily.add_argument("--code", help="只同步指定股票")
    p_daily.add_argument("--start", help="起始日期 YYYYMMDD")
    p_daily.add_argument("--end", help="结束日期 YYYYMMDD")

    p_fin = sub.add_parser("sync-finance", help="增量同步财务数据")
    p_fin.add_argument("--quarters", type=int, default=4, help="报告期数")

    p_back = sub.add_parser("backfill", help="历史回填（分批限速、断点续传）")
    p_back.add_argument("--start", help="起始日期 YYYYMMDD")
    p_back.add_argument("--end", help="结束日期 YYYYMMDD")
    p_back.add_argument("--quarters", type=int, help="财务报告期数")
    p_back.add_argument("--codes", help="只回填指定代码（逗号分隔）")
    p_back.add_argument("--dry-run", action="store_true")

    p_va = sub.add_parser("sync-valuation", help="增量同步日度估值")
    p_va.add_argument("--codes", help="只同步指定代码（逗号分隔）")

    p_bv = sub.add_parser("backfill-valuation", help="估值回填（全量拉取）")
    p_bv.add_argument("--codes", help="只回填指定代码（逗号分隔）")
    p_bv.add_argument("--dry-run", action="store_true")

    p_audit = sub.add_parser("audit-tencent",
                             help="只读对账：腾讯 vs 库内 close/volume/amount")
    p_audit.add_argument("--codes", help="指定代码（逗号分隔）")
    p_audit.add_argument("--start", help="起始日期 YYYYMMDD")
    p_audit.add_argument("--end", help="结束日期 YYYYMMDD")
    p_audit.add_argument("--snapshot", action="store_true",
                         help="同时打印腾讯快照行")

    args = parser.parse_args()

    # 采集闸门（Phase 2，调用层）：手工 cli 同样受闸门约束——未放闸的数据集直接拒绝，
    # 保证「部署即零采集」。要找 datahub 采集，先在 .env 放闸
    # （DATAHUB_COLLECT_ENABLED=1 + DATAHUB_COLLECT_DATASETS=<dataset>）。
    from app.collect_config import collect_enabled

    ds = _CMD_DATASET.get(args.cmd)
    if ds is not None and not collect_enabled(ds):
        logger.error("采集闸门关闭：命令 %s 对应 dataset=%s 未放闸，拒绝执行"
                     "（DATAHUB_COLLECT_ENABLED/DATAHUB_COLLECT_DATASETS）", args.cmd, ds)
        sys.exit(2)

    try:
        if args.cmd == "sync-stock":
            from app.collectors.stock import sync_stock_list
            ok = sync_stock_list()
        elif args.cmd == "sync-calendar":
            from app.collectors.calendar import sync_calendar
            ok = sync_calendar()
        elif args.cmd == "sync-index":
            from app.collectors.index import sync_index
            ok = sync_index()
        elif args.cmd == "sync-daily":
            ok = cmd_sync_daily(args)
        elif args.cmd == "sync-finance":
            ok = cmd_sync_finance(args)
        elif args.cmd == "backfill":
            ok = cmd_backfill(args)
        elif args.cmd == "sync-valuation":
            ok = cmd_sync_valuation(args)
        elif args.cmd == "backfill-valuation":
            ok = cmd_backfill_valuation(args)
        elif args.cmd == "audit-tencent":
            ok = cmd_audit_tencent(args)
        else:
            parser.error(f"未知命令: {args.cmd}")
        sys.exit(0 if ok else 1)
    except KeyboardInterrupt:
        logger.warning("中断，断点续传可安全重跑")
        sys.exit(130)


if __name__ == "__main__":
    main()
