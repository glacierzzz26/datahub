"""`db` provider：读 datahub 自有库（Phase 2 raw 数据集）。

与 external provider（抓外部源）对称——本 provider **不触外网**，从自有库按数据集查询、
按 `DatasetSpec` 列对齐返回 `list[dict]`。**批量、无 N+1**（一次查询取整段）。

**空结果是合法值**：库内确无 = 尚未采集，返回 `[]` 即成功；「空即失败」是 external 的
语义，在此不适用（勿让「没采到」误报为「故障」）。

复用 `app.db.get_session`（进程级单例池）——与采集侧共享同一 `app.db` 模块。
"""
import logging
from datetime import date

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import db
from app.models.tables import StockBasic, TradeCalendar

logger = logging.getLogger(__name__)

name = "db"


def _as_date(value: object) -> date | None:
    """把规整后的 ISO 字符串转 `date`（PG Date 列不接受文本比较）。"""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _split_csv(value: object) -> list[str]:
    """逗号分隔参数 → 去空列表（空 → []，表示不加过滤）。"""
    if value in (None, ""):
        return []
    return [s.strip() for s in str(value).split(",") if s.strip()]


def _trade_calendar(session: Session, params: dict) -> list[dict]:
    q = session.query(TradeCalendar)
    start = _as_date(params.get("start"))
    end = _as_date(params.get("end"))
    if start is not None:
        q = q.filter(TradeCalendar.cal_date >= start)
    if end is not None:
        q = q.filter(TradeCalendar.cal_date <= end)
    if params.get("is_open") is not None:
        q = q.filter(TradeCalendar.is_open == bool(params["is_open"]))
    q = q.order_by(TradeCalendar.cal_date)
    return [
        {"cal_date": r.cal_date, "is_open": r.is_open, "exchange": r.exchange}
        for r in q.all()
    ]


# 排序白名单（非法值回退 code）——与 backend `stockSortColumns` 对齐。
_STOCK_SORT = ("code", "name", "list_date", "market", "industry")


def _stock_basic(session: Session, params: dict) -> list[dict]:
    """股票列表：支持 code/market/universe/scope 白名单（逗号 IN）、行业精确、
    关键词模糊（大小写不敏感）、白名单排序（NULLS LAST）、分页。**一次查询，无 N+1**。"""
    q = session.query(StockBasic)
    codes = _split_csv(params.get("codes"))
    if codes:
        q = q.filter(StockBasic.code.in_(codes))
    markets = _split_csv(params.get("market"))
    if markets:
        q = q.filter(StockBasic.market.in_(markets))
    if params.get("industry"):
        q = q.filter(StockBasic.industry == params["industry"])
    universes = _split_csv(params.get("universe"))
    if universes:
        q = q.filter(StockBasic.universe.in_(universes))
    scopes = _split_csv(params.get("scope"))
    if scopes:
        q = q.filter(StockBasic.data_scope.in_(scopes))
    keyword = params.get("keyword")
    if keyword:
        pat = f"%{keyword}%"
        q = q.filter(or_(StockBasic.name.ilike(pat), StockBasic.code.ilike(pat)))

    col = params.get("sort") if params.get("sort") in _STOCK_SORT else "code"
    column = getattr(StockBasic, col)
    if params.get("order") == "desc":
        q = q.order_by(column.desc().nulls_last())
    else:
        q = q.order_by(column.asc().nulls_last())

    if params.get("limit"):
        q = q.limit(int(params["limit"]))
    if params.get("offset"):
        q = q.offset(int(params["offset"]))
    return [
        {"code": r.code, "name": r.name, "market": r.market, "industry": r.industry,
         "list_date": r.list_date, "status": r.status, "universe": r.universe,
         "data_scope": r.data_scope}
        for r in q.all()
    ]


# dataset id → 查询函数（新增 raw 数据集在此登记）
_FETCHERS = {
    "trade_calendar": _trade_calendar,
    "stock_basic": _stock_basic,
}


def fetch(dataset_id: str, params: dict) -> list[dict]:
    fn = _FETCHERS.get(dataset_id)
    if fn is None:
        raise ValueError(f"db provider 未支持数据集: {dataset_id}")
    session = db.get_session()
    try:
        return fn(session, params)
    finally:
        session.close()
