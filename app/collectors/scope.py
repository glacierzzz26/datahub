"""采集范围（data_scope）闸门与代码解析

Issue #13：把「采集哪些股」与「策略选哪只股」拆成两个正交概念。

- `universe`   —— 策略选股域（hs300/zz500），**只有 `factor_service.pool_codes()`
  和 `market_ready` 该读它**；改它就是换策略定义。
- `data_scope` —— 采集范围（'a_share' = market IN ('SH','SZ')，排除北交所/指数），
  由 `stock.py` 每日重标；本次扩池只翻这一列。

`COLLECT_SCOPE` env 是保险丝：默认 `pool`（= 现状，仍按 universe 取 800 只），
翻成 `a_share` 才按全量 5212 只采集。**默认分支与旧代码逐字等价** → 部署本批
代码生产行为零变化（含既有测试不破，见 test_backfill.py）。
"""
import logging

from sqlalchemy import or_, select

from app.collect_config import collect_scope
from app.models.tables import StockBasic

logger = logging.getLogger(__name__)

# COLLECT_SCOPE 取值
SCOPE_POOL = "pool"
SCOPE_A_SHARE = "a_share"

# 策略选股域（与 factor_service.pool_codes() 同字面量；此处只用于默认分支）
_POOL_UNIVERSES = ("hs300", "zz500")


def collect_codes(db, include_pool: bool = False) -> list[str]:
    """当前采集范围下的股票代码列表。

    - `COLLECT_SCOPE=pool`（默认）→ universe IN ('hs300','zz500')（现状 800 只）；
    - `COLLECT_SCOPE=a_share`      → data_scope = 'a_share'（全量 5212 只）。

    `include_pool=True`：a_share 分支下并上股票池（保底口径，用于「每日同步」这类
    一旦漏掉就会造成数据缺口的场景 —— 即便 data_scope 因列表生产者故障而漂移，
    股票池也一定被采）。
    """
    if collect_scope() == SCOPE_A_SHARE:
        cond = StockBasic.data_scope == SCOPE_A_SHARE
        if include_pool:
            # 并上股票池（OR）：data_scope 漂移时股票池仍被采
            cond = or_(cond, StockBasic.universe.in_(_POOL_UNIVERSES))
        rows = db.execute(select(StockBasic.code).where(cond)).scalars().all()
        return sorted(set(rows))

    # 默认 pool：与旧 `universe.in_(("hs300","zz500"))` 逐字等价
    rows = db.execute(
        select(StockBasic.code).where(StockBasic.universe.in_(_POOL_UNIVERSES))
    ).scalars().all()
    return sorted(rows)
