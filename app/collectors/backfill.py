"""历史数据回填：股票池（沪深300+中证500）分批限速、断点续传

策略（文档 §7.6）：
- 只回填股票池，不拉全市场
- 分批（默认 50 只）、限速（默认 3 秒/只），避免触发 AkShare 限速
- 断点续传：以 daily_price 中 max(trade_date) 为准，已回填到起始日的股票自动跳过，
  中断后重跑即可从上次位置继续
"""
import logging
import time
from datetime import date

from sqlalchemy import func, select

from app.collectors.daily import DailyCollector
from app.collectors.finance import FinanceCollector, quarter_ends
from app.collectors.valuation import ValuationCollector
from app.models.tables import DailyPrice, DailyValuation, StockBasic

logger = logging.getLogger(__name__)


class BackfillJob:
    """历史数据回填任务"""

    def __init__(self, db_session, rate_limit: float = 3.0,
                 batch_size: int = 50, dry_run: bool = False):
        self.db = db_session
        self.rate_limit = rate_limit
        self.batch_size = batch_size
        self.dry_run = dry_run
        self.stats = {"done": 0, "skipped": 0, "failed": 0, "empty": 0}

    # ---------- 基础 ----------

    def pool_codes(self) -> list[str]:
        """股票池代码（沪深300 + 中证500）——策略选股域，**不随采集范围变**"""
        rows = self.db.execute(
            select(StockBasic.code).where(
                StockBasic.universe.in_(("hs300", "zz500"))
            )
        ).scalars().all()
        return sorted(rows)

    def scope_codes(self) -> list[str]:
        """当前采集范围代码（Issue #13）：COLLECT_SCOPE=pool（默认）时等于
        pool_codes()；=a_share 时为全量 5212。历史回填按此扩范围。"""
        from app.collectors.scope import collect_codes

        return collect_codes(self.db, include_pool=True)

    def covered_codes(self, start_date: date) -> set[str]:
        """已完成回填的股票：日线真实覆盖到起始日（min(trade_date) <= start_date）

        注意不能用 max(trade_date) 判断：每日同步任务会给无历史数据的股票
        补最近 30 天，其 max 同样 >= start_date，会被误判为已完成。
        新股（晚于 start_date 上市）min > start_date，每次回填会重拉一次，
        拉不到早期数据、结果相同，仅耗时略增，可接受。
        """
        rows = self.db.execute(
            select(DailyPrice.code, func.min(DailyPrice.trade_date))
            .where(DailyPrice.code.not_like("sh%"))
            .group_by(DailyPrice.code)
        ).all()
        return {code for code, min_d in rows if min_d <= start_date}

    # ---------- 日行情回填 ----------

    def daily(self, start_date: date, end_date: date,
              codes: list[str] | None = None) -> dict:
        """回填股票池日行情（含复权因子），返回统计"""
        codes = codes or self.scope_codes()
        covered = self.covered_codes(start_date)
        todo = [c for c in codes if c not in covered]
        logger.info("股票池 %s 只，已完成 %s 只，待回填 %s 只",
                    len(codes), len(covered), len(todo))
        if self.dry_run:
            logger.info("[dry-run] 回填 %s ~ %s，共 %s 只：%s",
                        start_date, end_date, len(todo), ",".join(todo[:10]))
            return {"todo": todo, "covered": len(covered)}

        for i, code in enumerate(todo, 1):
            ok = DailyCollector(self.db).run(code, start_date, end_date)
            if not ok:
                self.stats["failed"] += 1
                logger.error("回填失败：%s", code)
            else:
                self.stats["done"] += 1
            if i % self.batch_size == 0:
                logger.info("回填进度 %s/%s：%s", i, len(todo), self.stats)
            time.sleep(self.rate_limit)
        self._verify(start_date, end_date)
        return self.stats

    def _verify(self, start_date: date, end_date: date):
        """回填校验：统计库中行数明显偏少的股票（上市/停牌等原因可豁免）"""
        rows = self.db.execute(
            select(DailyPrice.code, func.count(DailyPrice.id))
            .where(DailyPrice.code.not_like("sh%"),
                   DailyPrice.trade_date >= start_date,
                   DailyPrice.trade_date <= end_date)
            .group_by(DailyPrice.code)
        ).all()
        pool = set(self.scope_codes())  # 采集范围（Issue #13：a_share 时含全量）
        low = [(c, n) for c, n in rows if c in pool and n < 10]
        if low:
            logger.warning("回填校验：%s 只股票行数偏少（<10），可能停牌或上市较晚：%s",
                           len(low), [c for c, _ in low[:20]])
        logger.info("回填完成：%s", self.stats)

    # ---------- 估值数据回填 ----------

    def valuation(self, codes: list[str] | None = None) -> dict:
        """回填日度估值（东财，全量拉取 upsert 幂等）

        覆盖判定按 max(trade_date) >= 今日：每日同步会更新当日估值，
        但接口无日期参数，回填本质是"拉全量"，已最新的股票无需重拉。
        """
        from datetime import date

        codes = codes or self.scope_codes()
        latest = {
            code: max_d
            for code, max_d in self.db.execute(
                select(DailyValuation.code, func.max(DailyValuation.trade_date))
                .group_by(DailyValuation.code)
            ).all()
        }
        today = date.today()
        todo = [c for c in codes if latest.get(c) is None or latest[c] < today]
        logger.info("估值回填：股票池 %s 只，已完成 %s 只，待回填 %s 只",
                    len(codes), len(latest), len(todo))
        if self.dry_run:
            logger.info("[dry-run] 估值回填 %s 只：%s", len(todo), ",".join(todo[:10]))
            return {"todo": todo, "covered": len(latest)}
        for i, code in enumerate(todo, 1):
            ok = ValuationCollector(self.db).run(code)
            if not ok:
                self.stats["failed"] += 1
                logger.error("估值回填失败：%s", code)
            else:
                self.stats["done"] += 1
            if i % self.batch_size == 0:
                logger.info("估值回填进度 %s/%s：%s", i, len(todo), self.stats)
            time.sleep(self.rate_limit)
        logger.info("估值回填完成：%s", self.stats)
        return self.stats

    # ---------- 财务数据回填 ----------

    def finance(self, quarters: int, codes: set[str] | None = None) -> int:
        """回填最近 N 个报告期财务数据（含行业回填）

        `codes=None` → **不过滤**：财务接口本就是全市场快照，`FinanceCollector.save`
        只按 `stock_basic` 存在性过滤（见 finance.py:144-152）。旧实现误限 800 池，
        是扩池主体之外的一处不一致（Issue #13：财务已是全市场入库，仅此处误限）。
        """
        periods = quarter_ends(quarters)
        logger.info("财务回填：最近 %s 个报告期 %s", len(periods), periods[:4])
        if self.dry_run:
            return 0
        collector = FinanceCollector(self.db)
        rows = collector.fetch(report_periods=periods)
        if codes is not None:  # 显式指定代码时按指定集过滤
            rows = [r for r in rows if r["code"] in codes]
        collector.save(rows)
        logger.info("财务回填完成：%s 只股票 x %s 期", len(rows), len(periods))
        return len(rows)


def run_backfill(start: str | None, end: str | None, quarters: int | None,
                 dry_run: bool, codes: str | None) -> dict:
    """CLI 入口：执行回填（日行情 + 财务）"""
    from app.collect_config import (
        BACKFILL_BATCH_SIZE,
        BACKFILL_END,
        BACKFILL_FINANCE_QUARTERS,
        BACKFILL_START,
        RATE_LIMIT_SECONDS,
    )
    from app.db import get_session

    start_date = date.fromisoformat((start or BACKFILL_START).replace("-", ""))
    end_date = date.fromisoformat(end or BACKFILL_END or str(date.today()))
    q = quarters or BACKFILL_FINANCE_QUARTERS

    db = get_session()
    job = BackfillJob(db, rate_limit=RATE_LIMIT_SECONDS,
                      batch_size=BACKFILL_BATCH_SIZE, dry_run=dry_run)
    code_list = [c.strip().zfill(6) for c in codes.split(",")] if codes else None
    code_set = set(code_list) if code_list else None
    result = job.daily(start_date, end_date, codes=code_list)
    result["finance"] = job.finance(q, code_set)
    return result


if __name__ == "__main__":
    import argparse

    # 请求层超时（Issue #14）：回填是长时间批量拉取，最需要超时保护
    from app.providers.net import install_http_timeouts

    install_http_timeouts()

    parser = argparse.ArgumentParser(description="历史数据回填")
    parser.add_argument("--start", help="起始日期 YYYYMMDD（默认近10年）")
    parser.add_argument("--end", help="结束日期 YYYYMMDD（默认今天）")
    parser.add_argument("--quarters", type=int, help="财务回填报告期数")
    parser.add_argument("--codes", help="只回填指定代码（逗号分隔，测试用）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划不执行")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_backfill(args.start, args.end, args.quarters, args.dry_run, args.codes)
