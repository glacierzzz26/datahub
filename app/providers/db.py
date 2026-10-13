"""`db` provider：读 datahub 自有库（Phase 2 raw 数据集）。

与 external provider（抓外部源）对称——本 provider **不触外网**，从自有库按数据集查询、
按 `DatasetSpec` 列对齐返回 `list[dict]`。**批量、无 N+1**（一次查询取整段）。

**空结果是合法值**：库内确无 = 尚未采集，返回 `[]` 即成功；「空即失败」是 external 的
语义，在此不适用（勿让「没采到」误报为「故障」）。

复用 `app.db.get_session`（进程级单例池）——与采集侧共享同一 `app.db` 模块。
"""
import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app import db
from app.models.tables import (
    AnnouncementByStock,
    CapitalFlow,
    CapitalFlowDetail,
    DailyPrice,
    DailyValuation,
    FinancialIndicator,
    FinancialIndicatorExt,
    IndustryIndex,
    IndustrySwCons,
    IndustrySwInfo,
    Macro,
    NewsByStock,
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
                   "profit_growth", "revenue_growth", "debt_ratio", "gross_margin",
                   "net_margin")


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


def _apply_last(q, model, order_by, last: int, partition_by=None):
    """每 <partition_by> 最近 N 行：内层 `ROW_NUMBER() OVER (PARTITION BY <col>
    ORDER BY <order_by>)`，外层 join 回取 `rn <= last`。把「每码最新一条 / top-2 / 21 根窗」
    变成有界批量。`partition_by` 默认 `model.code`；`macro` 等无 code 列的表显式传。"""
    part = partition_by if partition_by is not None else model.code
    rn = func.row_number().over(partition_by=part, order_by=order_by).label("rn")
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
    """财务指标 + 侧表 net_margin：codes IN、as_of（announce_date ≤，防未来函数）、
    last 窗口、fields 投影（含 net_margin）。

    net_margin 来自 datahub 自有侧表 `financial_indicator_ext`（steady 镜像表无此列），
    按 (code, report_date) left-join；未采集到该行时 net_margin=NULL（合法）。
    """
    q = (
        session.query(FinancialIndicator, FinancialIndicatorExt.net_margin)
        .outerjoin(
            FinancialIndicatorExt,
            and_(
                FinancialIndicatorExt.code == FinancialIndicator.code,
                FinancialIndicatorExt.report_date == FinancialIndicator.report_date,
            ),
        )
    )
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
    fields = _split_csv(params.get("fields"))
    wanted = [c for c in _FINANCIAL_COLS if not fields or c in fields]
    out: list[dict] = []
    for fi, net_margin in q.all():
        vals = {
            "code": fi.code, "report_date": fi.report_date, "announce_date": fi.announce_date,
            "pe": fi.pe, "pb": fi.pb, "roe": fi.roe, "profit_growth": fi.profit_growth,
            "revenue_growth": fi.revenue_growth, "debt_ratio": fi.debt_ratio,
            "gross_margin": fi.gross_margin, "net_margin": net_margin,
        }
        out.append({k: (float(vals[k]) if isinstance(vals[k], Decimal) else vals[k])
                    for k in wanted})
    return out


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
    对齐 qe `data_quality._check_duplicates`（价格/估值用 trade_date，财务用 report_date）。

    `start`/`end` 收窄**键日期**（价格/估值为 trade_date，财务为 report_date）：
    全表 `GROUP BY`（daily_price 10.26M / daily_valuation 9.28M 行）需 ~10–55s，会超
    消费方（qe）10s 读超时 → 质检 job 假失败；qe 只断言「近期零重复」，传近窗即可（秒级）。
    """
    model, key_cols = _DUP_KEYS.get(params.get("table") or "daily_price",
                                    _DUP_KEYS["daily_price"])
    q = session.query(*key_cols, func.count())
    date_col = key_cols[1]  # 键日期列：trade_date / report_date
    start = _as_date(params.get("start"))
    end = _as_date(params.get("end"))
    if start is not None:
        q = q.filter(date_col >= start)
    if end is not None:
        q = q.filter(date_col <= end)
    rows = q.group_by(*key_cols).having(func.count() > 1).all()
    return [{"key": "|".join(str(v) for v in r[:-1]), "n": r[-1]} for r in rows]


# ---------- P14-1 研报数值域（datahub 自有表）----------
#
# 通用：codes 逗号 IN、start/end 区间、date 精确单日、asof 上界、last 每 code 最近 N 行、
# fields 列投影（保持声明序）。全部重复上文既有函数的口径，无新单位约定。

_CAPITAL_FLOW_COLS = ("code", "trade_date", "name", "close", "pct_change",
                      "turnover_rate", "reasons")
_CAPITAL_FLOW_DETAIL_COLS = ("code", "trade_date", "dealer_name", "buy_amount",
                             "buy_pct", "sell_amount", "sell_pct", "net_amount",
                             "reason")
_INDUSTRY_INDEX_COLS = ("code", "trade_date", "open", "high", "low", "close",
                        "pct_change", "amplitude", "chg_amount")
_INDUSTRY_SW_INFO_COLS = ("code", "level", "name", "level_label", "parent_name",
                          "member_count", "pe_static", "pe_ttm", "pb",
                          "dividend_yield")
_INDUSTRY_SW_CONS_COLS = ("index_code", "stock_code", "stock_name", "market_cap",
                          "pe_ttm", "pb", "roe", "dividend_yield",
                          "net_profit_growth", "revenue_growth")
_MACRO_COLS = ("indicator", "period_label", "period_end", "cadence", "level",
               "yoy", "mom")
# news 不暴露 content_hash（去重键，内部用）
_NEWS_COLS = ("code", "title", "content", "published_at", "source", "url")
_ANNOUNCEMENT_COLS = ("code", "title", "category", "publish_date", "url")


def _capital_flow(session: Session, params: dict) -> list[dict]:
    """龙虎榜记录：codes IN、start/end 区间、last 每码最近 N、fields 投影。"""
    q = session.query(CapitalFlow)
    codes = _split_csv(params.get("codes"))
    if codes:
        q = q.filter(CapitalFlow.code.in_(codes))
    start = _as_date(params.get("start"))
    end = _as_date(params.get("end"))
    if start is not None:
        q = q.filter(CapitalFlow.trade_date >= start)
    if end is not None:
        q = q.filter(CapitalFlow.trade_date <= end)
    last = _as_int(params.get("last"))
    if last:
        q = _apply_last(q, CapitalFlow, [CapitalFlow.trade_date.desc()], last)
    q = q.order_by(CapitalFlow.code, CapitalFlow.trade_date)
    return _project(q.all(), _split_csv(params.get("fields")), _CAPITAL_FLOW_COLS)


def _capital_flow_detail(session: Session, params: dict) -> list[dict]:
    """龙虎榜明细：codes IN、date 精确或 start/end 区间、fields 投影。"""
    q = session.query(CapitalFlowDetail)
    codes = _split_csv(params.get("codes"))
    if codes:
        q = q.filter(CapitalFlowDetail.code.in_(codes))
    exact = _as_date(params.get("date"))
    if exact is not None:
        q = q.filter(CapitalFlowDetail.trade_date == exact)
    else:
        start = _as_date(params.get("start"))
        end = _as_date(params.get("end"))
        if start is not None:
            q = q.filter(CapitalFlowDetail.trade_date >= start)
        if end is not None:
            q = q.filter(CapitalFlowDetail.trade_date <= end)
    q = q.order_by(CapitalFlowDetail.code, CapitalFlowDetail.trade_date,
                   CapitalFlowDetail.sell_amount.desc().nulls_last())
    return _project(q.all(), _split_csv(params.get("fields")),
                    _CAPITAL_FLOW_DETAIL_COLS)


def _industry_index(session: Session, params: dict) -> list[dict]:
    """申万行业指数日线：codes IN、start/end 区间、asof 上界、last、fields。升序。"""
    q = session.query(IndustryIndex)
    codes = _split_csv(params.get("codes"))
    if codes:
        q = q.filter(IndustryIndex.code.in_(codes))
    start = _as_date(params.get("start"))
    end = _as_date(params.get("end"))
    if start is not None:
        q = q.filter(IndustryIndex.trade_date >= start)
    if end is not None:
        q = q.filter(IndustryIndex.trade_date <= end)
    asof = _as_date(params.get("asof"))
    if asof is not None:
        q = q.filter(IndustryIndex.trade_date <= asof)
    last = _as_int(params.get("last"))
    if last:
        q = _apply_last(q, IndustryIndex, [IndustryIndex.trade_date.desc()], last)
    q = q.order_by(IndustryIndex.code, IndustryIndex.trade_date)
    return _project(q.all(), _split_csv(params.get("fields")), _INDUSTRY_INDEX_COLS)


def _industry_sw_info(session: Session, params: dict) -> list[dict]:
    """申万行业分类信息：levels IN、codes IN、parent 精确、fields 投影。"""
    q = session.query(IndustrySwInfo)
    levels = [int(x) for x in _split_csv(params.get("levels")) if x.isdigit()]
    if levels:
        q = q.filter(IndustrySwInfo.level.in_(levels))
    codes = _split_csv(params.get("codes"))
    if codes:
        q = q.filter(IndustrySwInfo.code.in_(codes))
    if params.get("parent"):
        q = q.filter(IndustrySwInfo.parent_name == params["parent"])
    q = q.order_by(IndustrySwInfo.level, IndustrySwInfo.code)
    return _project(q.all(), _split_csv(params.get("fields")),
                    _INDUSTRY_SW_INFO_COLS)


def _industry_sw_cons(session: Session, params: dict) -> list[dict]:
    """申万三级成分股：index_codes IN、codes（成分股）IN、fields 投影。市值降序。"""
    q = session.query(IndustrySwCons)
    index_codes = _split_csv(params.get("index_codes"))
    if index_codes:
        q = q.filter(IndustrySwCons.index_code.in_(index_codes))
    codes = _split_csv(params.get("codes"))
    if codes:
        q = q.filter(IndustrySwCons.stock_code.in_(codes))
    q = q.order_by(IndustrySwCons.index_code,
                   IndustrySwCons.market_cap.desc().nulls_last())
    return _project(q.all(), _split_csv(params.get("fields")),
                    _INDUSTRY_SW_CONS_COLS)


def _macro(session: Session, params: dict) -> list[dict]:
    """宏观指标序列：indicator（逗号 IN）、period_end 区间、last 每指标最近 N、fields。"""
    q = session.query(Macro)
    indicators = _split_csv(params.get("indicator"))
    if indicators:
        q = q.filter(Macro.indicator.in_(indicators))
    start = _as_date(params.get("start"))
    end = _as_date(params.get("end"))
    if start is not None:
        q = q.filter(Macro.period_end >= start)
    if end is not None:
        q = q.filter(Macro.period_end <= end)
    last = _as_int(params.get("last"))
    if last:
        q = _apply_last(q, Macro, [Macro.period_end.desc()], last,
                        partition_by=Macro.indicator)
    q = q.order_by(Macro.indicator, Macro.period_end)
    return _project(q.all(), _split_csv(params.get("fields")), _MACRO_COLS)


def _news_by_stock(session: Session, params: dict) -> list[dict]:
    """个股新闻：codes IN、start/end（published_at 区间，end 含当日）、last、fields。
    content_hash 为内部去重键，不返回。"""
    q = session.query(NewsByStock)
    codes = _split_csv(params.get("codes"))
    if codes:
        q = q.filter(NewsByStock.code.in_(codes))
    start = _as_date(params.get("start"))
    if start is not None:
        q = q.filter(NewsByStock.published_at >= start)
    end = _as_date(params.get("end"))
    if end is not None:
        # end 含当日全天：published_at < 次日零点
        q = q.filter(NewsByStock.published_at < end + timedelta(days=1))
    last = _as_int(params.get("last"))
    if last:
        q = _apply_last(q, NewsByStock, [NewsByStock.published_at.desc()], last)
    q = q.order_by(NewsByStock.code, NewsByStock.published_at.desc())
    return _project(q.all(), _split_csv(params.get("fields")), _NEWS_COLS)


def _announcement_by_stock(session: Session, params: dict) -> list[dict]:
    """个股公告：codes IN、start/end（publish_date 区间）、last、fields。"""
    q = session.query(AnnouncementByStock)
    codes = _split_csv(params.get("codes"))
    if codes:
        q = q.filter(AnnouncementByStock.code.in_(codes))
    start = _as_date(params.get("start"))
    end = _as_date(params.get("end"))
    if start is not None:
        q = q.filter(AnnouncementByStock.publish_date >= start)
    if end is not None:
        q = q.filter(AnnouncementByStock.publish_date <= end)
    last = _as_int(params.get("last"))
    if last:
        q = _apply_last(q, AnnouncementByStock,
                        [AnnouncementByStock.publish_date.desc()], last)
    q = q.order_by(AnnouncementByStock.code,
                   AnnouncementByStock.publish_date.desc())
    return _project(q.all(), _split_csv(params.get("fields")),
                    _ANNOUNCEMENT_COLS)


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
    "capital_flow": _capital_flow,
    "capital_flow_detail": _capital_flow_detail,
    "industry_index": _industry_index,
    "industry_sw_info": _industry_sw_info,
    "industry_sw_cons": _industry_sw_cons,
    "macro": _macro,
    "news_by_stock": _news_by_stock,
    "announcement_by_stock": _announcement_by_stock,
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
