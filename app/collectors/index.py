"""指数行情采集器：沪深300 / 中证500 日行情，作策略收益基准对比

数据源（阶段 4 起）：**AkShare 主源**——新浪 stock_zh_index_daily（已验证可用，
返回完整历史）；失败且 `baostock_enabled("index")` → **BaoStock 兜底**（保留
"当日未出 → raise"回退，BaoStock 当日约 18:00 后才出）。
存储：stock_basic 中 market='INDEX' 的伪股票 + daily_price 通用表。
"""
import logging
from datetime import date

import akshare as ak
import pandas as pd

from app.collect_config import baostock_enabled
from app.collectors.base import BaseCollector, with_timeout
from app.db import upsert
from app.models.tables import DailyPrice, StockBasic
from app.sources import baostock

logger = logging.getLogger(__name__)

# 指数代码（新浪格式）→ 名称；sz399106 深证综指 = 深市全市场，供 G6 两市成交
INDEX_NAMES = {"sh000001": "上证指数", "sh000300": "沪深300", "sh000905": "中证500",
               "sz399106": "深证综指"}


def build_rows(symbol: str, df: pd.DataFrame,
               start_date: date | None = None,
               end_date: date | None = None) -> list[dict]:
    rows = []
    for _, r in df.iterrows():
        d = date.fromisoformat(str(r["date"]))
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue
        rows.append({
            "code": symbol,
            "trade_date": d,
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": int(r["volume"]) if pd.notna(r["volume"]) else None,
            "amount": None,
            "adj_factor": None,
        })
    return rows


class IndexCollector(BaseCollector):
    """拉取指数日行情，供收益曲线与沪深300 基准对比"""

    def fetch(self, symbol: str = "sh000300", start_date=None, end_date=None,
              *args, **kwargs) -> list[dict]:
        end = end_date if end_date else date.today()
        # 阶段 4：AkShare 主源（新浪 stock_zh_index_daily，全历史）。
        # 失败且源链含 BaoStock → BaoStock 兜底（保留"当日未出 → raise"回退判断）；
        # 链内无 BaoStock 则 raise，触发 base.run 重试。
        try:
            df = with_timeout(ak.stock_zh_index_daily, symbol=symbol)
            rows = build_rows(symbol, df, start_date, end_date)
            if not rows:
                raise RuntimeError(f"{symbol} AkShare 指数未返回数据")
            logger.info("%s AkShare 拉取指数行情 %s 条", symbol, len(rows))
            return rows
        except Exception as e:
            logger.warning("%s AkShare 指数失败(%s)，尝试 BaoStock 兜底", symbol, e)
            if not baostock_enabled("index"):
                raise
        return self._fetch_baostock(symbol, start_date, end_date, end)

    def _fetch_baostock(self, symbol: str, start_date, end_date, end) -> list[dict]:
        """BaoStock 兜底：当日数据约 18:00 后才出，max(trade_date)<end 视为未出 → raise"""
        sess = baostock.get_session()
        if sess is None:
            raise RuntimeError(f"{symbol} BaoStock 不可用且 AkShare 指数失败")
        rows = baostock.index_rows(sess, symbol, start_date, end_date)
        if not rows:
            raise RuntimeError(f"{symbol} BaoStock 指数未返回数据")
        max_d = max(r["trade_date"] for r in rows)
        if max_d < end:
            raise RuntimeError(f"{symbol} BaoStock 当日指数未出(max={max_d})")
        logger.info("%s BaoStock 兜底拉取指数行情 %s 条", symbol, len(rows))
        return rows

    def save(self, data):
        if not data:
            return True
        symbol = data[0]["code"]
        # 1. 确保指数在 stock_basic 中（market='INDEX'，满足 daily_price 外键）
        upsert(
            self.db,
            StockBasic,
            [{
                "code": symbol,
                "name": INDEX_NAMES.get(symbol, symbol),
                "market": "INDEX",
                "status": "L",
            }],
            conflict_cols=["code"],
            update_cols=["name", "market", "status"],
        )
        # 2. 指数日行情入库（与股票共用 daily_price 表）。
        # 源无 amount（AkShare 新浪 stock_zh_index_daily 无成交额列，build_rows 置 None）
        # → 不更新 amount，保留既有（BaoStock 兜底/回填的成交额）。曾踩坑：08-28 index 切
        # AkShare 主源后，16:15 同步全史 upsert 带 amount 列把 BaoStock 回填的成交额全清
        # NULL，致 G6 两市成交断供（market_hotspot.turnover 消失）。源带 amount（BaoStock）
        # → 正常更新。
        cols = ["open", "high", "low", "close", "volume", "adj_factor"]
        if any(r.get("amount") is not None for r in data):
            cols.append("amount")
        upsert(
            self.db,
            DailyPrice,
            data,
            conflict_cols=["code", "trade_date"],
            update_cols=cols,
        )
        logger.info("%s 指数行情入库 %s 条", symbol, len(data))
        return True


def sync_index() -> bool:
    """手动触发入口：同步全部配置指数"""
    from app.collect_config import index_code_list
    from app.db import get_session

    ok = True
    for symbol in index_code_list():
        ok = IndexCollector(get_session()).run(symbol=symbol) and ok
    return ok
