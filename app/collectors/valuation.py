"""日度估值采集器：东财 stock_value_em（PE(TTM)/PE(静)/PB/市值）

数据源（阶段 4 起）：**AkShare 主源**——东财 stock_value_em 单次返回全历史
（约 2018 年至今，逐日一行），无日期参数，故全量拉取 + upsert 幂等；失败且
`baostock_enabled("valuation")` → **BaoStock 兜底**（保留"当日未出 → raise"回退，
BaoStock 当日约 18:00 后才出）。PE/PB 缺失（停牌/亏损无值）原样存 NULL，
正负值判断留给因子计算侧（pe<=0 不参与价值横截面）。
"""
import logging
from datetime import date, timedelta

import akshare as ak
import pandas as pd

from app.collect_config import baostock_enabled
from app.collectors.base import BaseCollector, with_timeout
from app.db import upsert
from app.models.tables import DailyValuation
from app.sources import baostock

logger = logging.getLogger(__name__)


def _num(v) -> float | None:
    """NaN/空 → None，其余转 float（与 finance.py 一致）"""
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
        return None
    return float(v)


def to_rows(code: str, df: pd.DataFrame) -> list[dict]:
    """估值表 → 入库行（列名与 DailyValuation 对齐）"""
    rows = []
    for _, r in df.iterrows():
        if pd.isna(r.get("数据日期")):
            continue
        rows.append(
            {
                "code": code,
                "trade_date": pd.Timestamp(r["数据日期"]).date(),
                "close": _num(r.get("当日收盘价")),
                "total_mv": _num(r.get("总市值")),
                "float_mv": _num(r.get("流通市值")),
                "pe_ttm": _num(r.get("PE(TTM)")),
                "pe_static": _num(r.get("PE(静)")),
                "pb": _num(r.get("市净率")),
            }
        )
    return rows


class ValuationCollector(BaseCollector):
    """日度估值采集（东财，按股票全量拉取）"""

    def fetch(self, code: str, *args, **kwargs) -> list[dict]:
        today = date.today()
        # 阶段 4：AkShare 主源（东财 stock_value_em，单次返回全历史）。
        # 失败且源链含 BaoStock → BaoStock 兜底（保留"当日未出 → raise"回退判断）；
        # 链内无 BaoStock 则 raise，触发 base.run 重试。
        try:
            df = with_timeout(ak.stock_value_em, symbol=code)
            rows = to_rows(code, df)
            if not rows:
                raise RuntimeError(f"{code} AkShare 估值未返回数据")
            logger.info("AkShare 返回 %s 估值 %s 条", code, len(rows))
            return rows
        except Exception as e:
            logger.warning("%s AkShare 估值失败(%s)，尝试 BaoStock 兜底", code, e)
            if not baostock_enabled("valuation"):
                raise
        return self._fetch_baostock(code, today)

    def _fetch_baostock(self, code: str, today) -> list[dict]:
        """BaoStock 兜底：当日数据约 18:00 后才出，max(trade_date)<today 视为未出 → raise"""
        sess = baostock.get_session()
        if sess is None:
            raise RuntimeError(f"{code} BaoStock 不可用且 AkShare 估值失败")
        rows = baostock.valuation_rows(
            sess, code, today - timedelta(days=3650), today)
        if not rows:
            raise RuntimeError(f"{code} BaoStock 估值未返回数据")
        max_d = max(r["trade_date"] for r in rows)
        if max_d < today:
            raise RuntimeError(f"{code} BaoStock 当日估值未出(max={max_d})")
        logger.info("%s BaoStock 兜底拉取 %s 条估值", code, len(rows))
        return rows

    def save(self, data):
        # BaoStock 分支不产出 total_mv/float_mv/pe_static（保留既有值：无消费者，
        # 不覆盖 AkShare 的市值与静态 PE）；含这三列的行沿用原 6 列更新。
        if data and "total_mv" in data[0]:
            update_cols = ["close", "total_mv", "float_mv",
                           "pe_ttm", "pe_static", "pb"]
        else:
            update_cols = ["close", "pe_ttm", "pb"]
        upsert(
            self.db,
            DailyValuation,
            data,
            conflict_cols=["code", "trade_date"],
            update_cols=update_cols,
        )
        return True


def sync_valuation(codes: list[str] | None = None, rate_limit: float = 1.0) -> bool:
    """手动触发入口：同步日度估值（跳过当日已最新的股票）"""
    import time
    from datetime import date

    from sqlalchemy import func, select

    from app.db import get_session

    db = get_session()
    if codes is None:
        from app.collectors.scope import collect_codes

        codes = collect_codes(db, include_pool=True)
    latest = {
        code: max_d
        for code, max_d in db.execute(
            select(DailyValuation.code, func.max(DailyValuation.trade_date))
            .group_by(DailyValuation.code)
        ).all()
    }
    todo = [c for c in codes if latest.get(c) is None or latest[c] < date.today()]
    logger.info("估值同步：%s 只中 %s 只需更新", len(codes), len(todo))
    ok_count = fail_count = 0
    for code in todo:
        ok = ValuationCollector(db).run(code)
        ok_count += ok
        fail_count += not ok
        time.sleep(rate_limit)
    logger.info("估值同步完成：成功 %s，失败 %s", ok_count, fail_count)
    return fail_count == 0
