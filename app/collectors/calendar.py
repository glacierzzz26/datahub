"""交易日历采集器：A股节假日多于周末，调度与回测以此为基准"""
import logging
from datetime import date

import akshare as ak

from app.collect_config import baostock_enabled
from app.collectors.base import BaseCollector
from app.db import upsert
from app.models.tables import TradeCalendar
from app.sources import baostock

logger = logging.getLogger(__name__)


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
