"""交易日历采集器：A股节假日多于周末，调度与回测以此为基准"""
import logging
from datetime import date, datetime, time

import akshare as ak

from app.collect_config import baostock_enabled
from app.collectors.base import BaseCollector
from app.db import upsert
from app.models.tables import TradeCalendar
from app.sources import baostock

logger = logging.getLogger(__name__)

# A 股收盘（北京时间）。now >= 此点才认为「今日」的日线已落定、可写。
MARKET_CLOSE = time(15, 0)


def last_settled_trade_date(db, now: datetime | None = None) -> date:
    """最近一个「已收盘」的交易日（≤ 今日）—— 供日频采集钳制写入 end。

    背景：逐码增量 `start = max_d + 1` 且探针判 `max == today`，故任何一次落库的坏行
    （典型：源未落定时写入的**半日 partial bar**）**永不重采**、永久错（2026-09-23
    全市场半日 bar 事故）。本函数把「今日」钳到最近已收盘交易日，盘中（now < 15:00）
    不回写今日，从源头挡掉今日的半日 bar（与 `daily_valuation` 的「当日未出 → raise」
    守护同意图）。判据同 `tasks.py` / `watchdog.py` 的 `TradeCalendar.is_open`。

      - today 是交易日且 now >= MARKET_CLOSE → today
      - 否则 → today 之前最近一个 is_open 交易日
      - 日历无可用记录（未采/缺失）→ 回退 today（不阻断采集、不引入死循环）
    """
    from sqlalchemy import func, select

    now = now or datetime.now()
    today = now.date()
    is_open_today = db.execute(
        select(TradeCalendar.is_open).where(TradeCalendar.cal_date == today)
    ).scalar()
    if is_open_today and now.time() >= MARKET_CLOSE:
        return today
    prev_open = db.execute(
        select(func.max(TradeCalendar.cal_date)).where(
            TradeCalendar.cal_date < today,
            TradeCalendar.is_open.is_(True),
        )
    ).scalar()
    if prev_open is not None:
        return prev_open
    logger.warning("交易日历无 %s 之前的开市记录，last_settled_trade_date 回退 today", today)
    return today


class CalendarCollector(BaseCollector):
    """拉取 A 股交易日历（新浪源返回全部交易日，全年）"""

    def fetch(self, *args, **kwargs) -> list[dict]:
        # BaoStock 主源（阶段 1 开关）：query_trade_dates（近 2 年 + 未来 1 年，1 次调用）
        if baostock_enabled("calendar"):
            sess = baostock.get_session()
            if sess is not None:
                try:
                    rows = baostock.trade_cal_rows(sess)
                    if not rows:
                        raise RuntimeError("BaoStock 交易日历未返回数据")
                    logger.info("BaoStock 拉取交易日 %s 天", len(rows))
                    return rows
                except Exception as e:
                    logger.warning("BaoStock 交易日历失败(%s)，降级 AkShare", e)
        df = ak.tool_trade_date_hist_sina()
        rows = [
            {
                "cal_date": date.fromisoformat(str(d)),
                "is_open": True,
                "exchange": "SSE",
            }
            for d in df["trade_date"]
        ]
        logger.info("拉取交易日 %s 天", len(rows))
        return rows

    def save(self, data):
        upsert(
            self.db,
            TradeCalendar,
            data,
            conflict_cols=["cal_date"],
            update_cols=["is_open", "exchange"],
        )
        logger.info("交易日历入库 %s 天", len(data))
        return True


def sync_calendar() -> bool:
    """手动触发入口"""
    from app.db import get_session

    return CalendarCollector(get_session()).run()
