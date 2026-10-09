"""`db` provider：读 datahub 自有库（Phase 2 raw 数据集）。

与 external provider（抓外部源）对称——本 provider **不触外网**，从自有库按数据集查询、
按 `DatasetSpec` 列对齐返回 `list[dict]`。**批量、无 N+1**（一次查询取整段）。

**空结果是合法值**：库内确无 = 尚未采集，返回 `[]` 即成功；「空即失败」是 external 的
语义，在此不适用（勿让「没采到」误报为「故障」）。

复用 `app.db.get_session`（进程级单例池）——与采集侧共享同一 `app.db` 模块。
"""
import logging
from datetime import date

from sqlalchemy.orm import Session

from app import db
from app.models.tables import TradeCalendar

logger = logging.getLogger(__name__)

name = "db"


def _as_date(value: object) -> date | None:
    """把规整后的 ISO 字符串转 `date`（PG Date 列不接受文本比较）。"""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


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


# dataset id → 查询函数（新增 raw 数据集在此登记）
_FETCHERS = {
    "trade_calendar": _trade_calendar,
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
