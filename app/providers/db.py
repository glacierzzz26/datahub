"""`db` provider：读 datahub 自有库（Phase 2 raw 数据集）。

与 external provider（抓外部源）对称——本 provider **不触外网**，从自有库按数据集查询、
按 `DatasetSpec` 列对齐返回 `list[dict]`。**批量、无 N+1**（一次查询取整段）。

**空结果是合法值**：库内确无 = 尚未采集，返回 `[]` 即成功；「空即失败」是 external 的
语义，在此不适用（勿让「没采到」误报为「故障」）。

复用 `app.db.get_session`（进程级单例池）——与采集侧共享同一 `app.db` 模块。
"""
import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import db
from app.models.tables import (
    DailyPrice,
    DailyValuation,
    FinancialIndicator,
    StockBasic,
    TradeCalendar,
)

logger = logging.getLogger(__name__)

name = "db"


def _as_date(value: object) -> date | None:
    """把规整后的 ISO 字符串转 `date`（PG Date 列不接受文本比较）。"""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _as_int(value: object) -> int | None:
    """转 int（空 → None）。`last`/`limit` 等正整数参数用。"""
    if value in (None, ""):
        return None
    return int(value)


def _as_bool(value: object, default: bool = False) -> bool:
    """转 bool（空 → default）。HTTP query 传 "" / "true" / "1" 等。"""
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


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


# ---------- 三张价格/财务表 + 小数据集（Phase 3）----------
#
# 通用：codes 逗号 IN；start/end 区间；date 精确单日；asof 上界；last 每 code 最近 N 行
# （服务端 ROW_NUMBER，把窗口/聚合读点变有界批量）；fields 列投影（保持声明序）。

_PRICE_COLS = ("code", "trade_date", "open", "high", "low", "close", "volume",
               "amount", "adj_factor", "turnover_rate")
_VALUATION_COLS = ("code", "trade_date", "close", "total_mv", "float_mv",
                   "pe_ttm", "pe_static", "pb")
_FINANCIAL_COLS = ("code", "report_date", "announce_date", "pe", "pb", "roe",
                   "profit_growth", "revenue_growth", "debt_ratio", "gross_margin")


def _project(rows, fields: list[str], all_cols: tuple[str, ...]) -> list[dict]:
    """按 fields 投影（保持 all_cols 声明序，忽略未知列）；fields 空 → 全列。

    **数值列归一为 `float`**：`NUMERIC` 列经 ORM 映射成 `Decimal`，而 pydantic v2 会把
    `Decimal` 序列化成 JSON **字符串**（保精度）——这与 `ColumnSpec` 声明的 `float`
    类型不符，且让 Go 侧 `float64` DTO 反序列化直接失败
    （`json: cannot unmarshal string into float64`）。故此处按契约把 `Decimal` 转
    `float`（`int` 列 ORM 本就是 `int`，不动）。
    """
    wanted = [c for c in all_cols if not fields or c in fields]
    out: list[dict] = []
    for r in rows:
        row: dict = {}
        for k in wanted:
            v = getattr(r, k)
            row[k] = float(v) if isinstance(v, Decimal) else v
        out.append(row)
    return out


def _apply_last(q, model, order_by, last: int):
    """每 code 最近 N 行：内层 `ROW_NUMBER() OVER (PARTITION BY code ORDER BY <order_by>)`，
    外层 join 回取 `rn <= last`。把「每码最新一条 / top-2 / 21 根窗」变成有界批量。"""
    rn = func.row_number().over(partition_by=model.code, order_by=order_by).label("rn")
    sub = q.with_entities(model.id, rn).subquery()
    return q.join(sub, model.id == sub.c.id).filter(sub.c.rn <= last)


def _daily_price(session: Session, params: dict) -> list[dict]:
    """日行情（含指数行）：codes IN、start/end 区间、date 精确、asof 上界、
    adj_not_null、last 窗口、fields 投影。升序（code, trade_date）。"""
    q = session.query(DailyPrice)
    codes = _split_csv(params.get("codes"))
    if codes:
        q = q.filter(DailyPrice.code.in_(codes))
    exact = _as_date(params.get("date"))
    if exact is not None:
        q = q.filter(DailyPrice.trade_date == exact)
    else:
        start = _as_date(params.get("start"))
        end = _as_date(params.get("end"))
        if start is not None:
            q = q.filter(DailyPrice.trade_date >= start)
        if end is not None:
            q = q.filter(DailyPrice.trade_date <= end)
    asof = _as_date(params.get("asof"))
    if asof is not None:
        q = q.filter(DailyPrice.trade_date <= asof)
    if _as_bool(params.get("adj_not_null")):
        q = q.filter(DailyPrice.adj_factor.isnot(None))
    last = _as_int(params.get("last"))
    if last:
        q = _apply_last(q, DailyPrice, [DailyPrice.trade_date.desc()], last)
    q = q.order_by(DailyPrice.code, DailyPrice.trade_date)
    return _project(q.all(), _split_csv(params.get("fields")), _PRICE_COLS)


def _daily_valuation(session: Session, params: dict) -> list[dict]:
    q = session.query(DailyValuation)
    codes = _split_csv(params.get("codes"))
    if codes:
        q = q.filter(DailyValuation.code.in_(codes))
    start = _as_date(params.get("start"))
    end = _as_date(params.get("end"))
    if start is not None:
        q = q.filter(DailyValuation.trade_date >= start)
    if end is not None:
        q = q.filter(DailyValuation.trade_date <= end)
    asof = _as_date(params.get("asof"))
    if asof is not None:
        q = q.filter(DailyValuation.trade_date <= asof)
    last = _as_int(params.get("last"))
    if last:
        q = _apply_last(q, DailyValuation, [DailyValuation.trade_date.desc()], last)
    q = q.order_by(DailyValuation.code, DailyValuation.trade_date)
    return _project(q.all(), _split_csv(params.get("fields")), _VALUATION_COLS)


def _financial_indicator(session: Session, params: dict) -> list[dict]:
    """财务指标：codes IN、as_of（announce_date ≤，防未来函数）、last 窗口、fields。"""
    q = session.query(FinancialIndicator)
    codes = _split_csv(params.get("codes"))
    if codes:
        q = q.filter(FinancialIndicator.code.in_(codes))
    as_of = _as_date(params.get("as_of"))
    if as_of is not None:
        q = q.filter(FinancialIndicator.announce_date <= as_of)
    last = _as_int(params.get("last"))
    if last:
        q = _apply_last(q, FinancialIndicator,
                        [FinancialIndicator.announce_date.desc(),
                         FinancialIndicator.report_date.desc()], last)
    q = q.order_by(FinancialIndicator.code, FinancialIndicator.report_date)
    return _project(q.all(), _split_csv(params.get("fields")), _FINANCIAL_COLS)


def _latest_trade_date(session: Session, params: dict) -> list[dict]:
    """最新交易日（MAX）。`exclude_index` 默认 true（qe 口径 `code NOT LIKE 'sh%'`）；
    backend `GetLatestTradeDate` 不过滤 → 传 false。`before` 给定时取 `trade_date < before`
    的最近一日（严格小于；qe data_quality 的 prev 推导用）。空 → `[]`（合法值）。"""
    model = DailyValuation if params.get("table") == "daily_valuation" else DailyPrice
    q = session.query(func.max(model.trade_date))
    if _as_bool(params.get("exclude_index"), default=True):
        q = q.filter(~model.code.like("sh%"))
    before = _as_date(params.get("before"))
    if before is not None:
        q = q.filter(model.trade_date < before)
    d = q.scalar()
    return [{"trade_date": d}] if d else []


def _daily_price_days(session: Session, params: dict) -> list[dict]:
    """区间内「有行情的交易日」去重列表（升序）。`exclude_index` 默认 true（qe 口径
    `code NOT LIKE 'sh%'`）。空 → `[]`（合法值）。供 data_quality._check_missing_days。"""
    q = session.query(DailyPrice.trade_date).distinct()
    start = _as_date(params.get("start"))
    end = _as_date(params.get("end"))
    if start is not None:
        q = q.filter(DailyPrice.trade_date >= start)
    if end is not None:
        q = q.filter(DailyPrice.trade_date <= end)
    if _as_bool(params.get("exclude_index"), default=True):
        q = q.filter(~DailyPrice.code.like("sh%"))
    q = q.order_by(DailyPrice.trade_date)
    return [{"trade_date": d} for (d,) in q.all()]


def _market_ready(session: Session, params: dict) -> list[dict]:
    """池就绪度：池内 code 数 `total`，当日有行情者 `covered`，覆盖率 ≥ 阈值 → ready。
    对齐 qe `tasks.market_ready`（池 = `universe IN (...)`，阈值默认 0.9）。"""
    universes = _split_csv(params.get("universe")) or ["hs300", "zz500"]
    threshold = float(params.get("threshold") or 0.9)
    d = _as_date(params.get("date"))
    pool_codes = [c for (c,) in session.query(StockBasic.code)
                  .filter(StockBasic.universe.in_(universes)).all()]
    total = len(pool_codes)
    covered = 0
    if d is not None and total:
        covered = (session.query(func.count(func.distinct(DailyPrice.code)))
                   .filter(DailyPrice.trade_date == d,
                           DailyPrice.code.in_(pool_codes)).scalar()) or 0
    ready = total > 0 and covered / total >= threshold
    return [{"ready": ready, "covered": covered, "total": total}]


_DUP_KEYS = {
    "daily_price": (DailyPrice, (DailyPrice.code, DailyPrice.trade_date)),
    "daily_valuation": (DailyValuation, (DailyValuation.code, DailyValuation.trade_date)),
    "financial_indicator": (FinancialIndicator,
                            (FinancialIndicator.code, FinancialIndicator.report_date)),
}


def _dup_keys(session: Session, params: dict) -> list[dict]:
    """重复唯一键组（>1 条）。datahub 有 UNIQUE 索引 → 恒空；作为「零重复」断言。
    对齐 qe `data_quality._check_duplicates`（价格/估值用 trade_date，财务用 report_date）。"""
    model, key_cols = _DUP_KEYS.get(params.get("table") or "daily_price",
                                    _DUP_KEYS["daily_price"])
    rows = (session.query(*key_cols, func.count())
            .group_by(*key_cols).having(func.count() > 1).all())
    return [{"key": "|".join(str(v) for v in r[:-1]), "n": r[-1]} for r in rows]


# dataset id → 查询函数（新增 raw 数据集在此登记）
_FETCHERS = {
    "trade_calendar": _trade_calendar,
    "stock_basic": _stock_basic,
    "daily_price": _daily_price,
    "daily_valuation": _daily_valuation,
    "financial_indicator": _financial_indicator,
    "latest_trade_date": _latest_trade_date,
    "daily_price_days": _daily_price_days,
    "market_ready": _market_ready,
    "dup_keys": _dup_keys,
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
