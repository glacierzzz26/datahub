"""P14-1 研报数值域采集器（8 张 datahub 自有表）。

`research/src/providers/*` 现直调 akshare 的 8 个数值域；本模块把**同一批** akshare
调用搬到 datahub，写入 `deploy/postgres/init.sql` §7 的 8 张自有表。表结构见
`app/models/tables.py`，读出口见 `app/datasets/raw.py`。

**口径红线**（逐条照 research provider 移植，不得丢）：

- **龙虎榜记录**（`capital_flow`）：同交易日多「上榜原因」**合并为一行**（`reasons`
  用「；」连接、同因去重）；`pct_change` 取**东财「涨跌幅」**——不是新浪「对应值」
  （后者含义随上榜原因变化：换手率榜是换手率、振幅榜是振幅）。
- **龙虎榜明细**（`capital_flow_detail`）：**丢弃**「连续三个交易日」聚合块（跨日口径，
  非当日成交）；买入/卖出两侧合并后按 `(营业部, 买入金额, 卖出金额)` 去重（同一笔交易
  按上榜原因分块重复出现）；金额类字段缺失补 **0.0**（镜像 research `_to_float`，且
  DDL 中 buy/sell_amount NOT NULL）。
- **申万指数**（`industry_index`）：**不存** `volume/amount/turnover`（申万该字段量纲与
  A 股不兼容）；`pct_change/amplitude/chg_amount` 由**序列内** `prev_close` 派生（窗口
  首个行 prev 不存在 → 0），与原 provider 逐字同法。
- **宏观**（`macro`）：只存 `level/yoy/mom`（源列名→值的最小抽取映射）；`unit/
  level_label/caliber` 等**语义表留 PIKS/Python**（R2 决定），datahub 不内嵌、不解释。
  `period_label` 存**源文原文**（如 `2026年08月份`）；CPI/PPI 的 `level` 是**指数
  （上年同月=100）非百分比** —— 原样落库，量纲说明在消费侧。

全部采集经 `tasks.py` 双闸门（`DATAHUB_COLLECT_ENABLED × DATAHUB_COLLECT_DATASETS`），
默认全关 —— 部署本模块零采集行为变更（不注册任务、不发请求、不写行）。
"""
import hashlib
import logging
import re
import time
from datetime import date, datetime

import akshare as ak
import pandas as pd

from app.collect_config import RESEARCH_SYNC_INTERVAL
from app.collectors.base import BaseCollector, with_timeout
from app.db import upsert
from app.models.tables import (
    AnnouncementByStock,
    CapitalFlow,
    CapitalFlowDetail,
    IndustryIndex,
    IndustrySwCons,
    IndustrySwInfo,
    Macro,
    NewsByStock,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 通用取值/清洗助手
# ---------------------------------------------------------------------------

def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    """列名去首尾空白（东财接口偶带空格）"""
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _num(v) -> float | None:
    """数值字段：NaN/None/空串 → None，其余转 float（与 finance.py 同约定）。

    ⚠️ 与 `_to_float` 的区别：本函数**保留缺失为 None**；`_to_float` 缺失补 0.0。
    """
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _to_float(v) -> float:
    """金额/比例字段：缺失或不可解析 → **0.0**（镜像 research `_to_float`）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f != f else f


def _opt_float(v) -> float | None:
    """申万快照的 `N/A` / 空串 / `-` 一律 None —— 不补 0（缺失≠为零）。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "nan", "NaN", "None", "null"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _opt_int(v) -> int | None:
    f = _opt_float(v)
    return int(f) if f is not None else None


def _short(code) -> str:
    """'801010.SI' → '801010'"""
    return str(code).split(".")[0].strip()


def _trunc(v, n: int):
    """超长字符串截断到 n（DDL 列宽有限；None 原样返回）"""
    if v is None:
        return None
    s = str(v)
    return s[:n]


def _retry(fn, retries: int = 3, delay: float = 0.8):
    """页面解析偶发失败时重试兜底（镜像 research 各 provider 的 `_with_retry`）。"""
    last: Exception | None = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 —— 逐次重试，最后一次抛出原异常
            last = e
            time.sleep(delay * (i + 1))
    raise last if last else RuntimeError("retry 未执行")


def _parse_date(value) -> date | None:
    """东财「上榜日」形如 `2026-09-17`；解析不出返回 None（不猜）。"""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 宏观（macro）
# ---------------------------------------------------------------------------

# 源列名 → 语义名的最小抽取映射（**唯一**的接口名来源；语义/量纲表在 PIKS）。
# 键与 datahub `macro.indicator` 列一致（cn_cpi/cn_ppi/cn_m2/cn_gdp）。
_MACRO_DIMENSIONS: dict[str, dict] = {
    "cn_cpi": {
        "ak_name": "macro_china_cpi", "cadence": "month", "label": "月份",
        "level": "全国-当月", "yoy": "全国-同比增长", "mom": "全国-环比增长",
    },
    "cn_ppi": {
        "ak_name": "macro_china_ppi", "cadence": "month", "label": "月份",
        "level": "当月", "yoy": "当月同比增长", "mom": None,
    },
    "cn_m2": {
        "ak_name": "macro_china_money_supply", "cadence": "month", "label": "月份",
        "level": "货币和准货币(M2)-数量(亿元)",
        "yoy": "货币和准货币(M2)-同比增长",
        "mom": "货币和准货币(M2)-环比增长",
    },
    "cn_gdp": {
        "ak_name": "macro_china_gdp", "cadence": "quarter", "label": "季度",
        "level": "国内生产总值-绝对值", "yoy": "国内生产总值-同比增长", "mom": None,
    },
}

_MONTH_RE = re.compile(r"^(\d{4})年(\d{1,2})月份$")
_QUARTER_RE = re.compile(r"^(\d{4})年第(\d)季度$")
_QUARTER_RANGE_RE = re.compile(r"^(\d{4})年第(\d)-(\d)季度$")


def _month_end(year: int, month: int) -> date:
    """统计期末 = 该月最后一天（不引 calendar，手算闰年）。"""
    if month == 12:
        return date(year, 12, 31)
    first_of_next = date(year, month + 1, 1)
    return date.fromordinal(first_of_next.toordinal() - 1)


def parse_period_end(label, cadence: str) -> date:
    """源站期号 → 统计期末。无法解析即抛（不静默当缺失）。"""
    s = str(label).strip()
    m = _MONTH_RE.match(s)
    if m:
        return _month_end(int(m.group(1)), int(m.group(2)))
    m = _QUARTER_RANGE_RE.match(s)
    if m:
        return _month_end(int(m.group(1)), int(m.group(3)) * 3)
    m = _QUARTER_RE.match(s)
    if m:
        return _month_end(int(m.group(1)), int(m.group(2)) * 3)
    raise ValueError(
        f"无法解析源站期号: {label!r}（cadence={cadence}，既非「YYYY年MM月份」"
        "也非「YYYY年第N季度」）")


def build_macro_rows(indicator: str, df: pd.DataFrame, spec: dict) -> list[dict]:
    """一个宏观维度的源 DataFrame → 入库行（level/yoy/mom 三语义列）。"""
    label_col = spec["label"]
    if label_col not in df.columns:
        raise ValueError(
            f"接口 {spec['ak_name']} 缺期号列 {label_col!r}（列={list(df.columns)}）"
            "—— 源站结构调整，需重新核对后再改本模块")
    rows = []
    for _, r in df.iterrows():
        label = str(r.get(label_col)).strip()
        rows.append({
            "indicator": indicator,
            "period_label": _trunc(label, 40),
            "period_end": parse_period_end(label, spec["cadence"]),
            "cadence": spec["cadence"],
            "level": _num(r.get(spec["level"])),
            "yoy": _num(r.get(spec["yoy"])),
            "mom": (_num(r.get(spec["mom"])) if spec.get("mom") else None),
        })
    return rows


class MacroCollector(BaseCollector):
    """宏观指标全史刷新（CPI/PPI/M2/GDP，每指标一次调用，小数据量）。"""

    def fetch(self, indicators: list[str] | None = None, *args, **kwargs) -> list[dict]:
        keys = indicators or list(_MACRO_DIMENSIONS)
        out: list[dict] = []
        ok = 0
        for key in keys:
            spec = _MACRO_DIMENSIONS.get(key)
            if spec is None:
                logger.warning("未知宏观维度 %s，跳过（不臆造）", key)
                continue
            try:
                df = _retry(lambda spec=spec: with_timeout(
                    getattr(ak, spec["ak_name"])))
            except Exception as e:  # noqa: BLE001
                logger.warning("宏观 %s（%s）拉取失败：%s", key, spec["ak_name"], e)
                continue
            if df is None or df.empty:
                logger.warning("宏观 %s 无数据", key)
                continue
            out.extend(build_macro_rows(key, df, spec))
            ok += 1
        if keys and ok == 0:
            raise RuntimeError("全部宏观维度拉取失败")
        return out

    def save(self, data):
        upsert(
            self.db, Macro, data,
            conflict_cols=["indicator", "period_label"],
            update_cols=["period_end", "cadence", "level", "yoy", "mom"],
        )
        logger.info("宏观入库 %s 条", len(data))
        return True


# ---------------------------------------------------------------------------
# 申万行业分类快照（industry_sw_info）
# ---------------------------------------------------------------------------

# 层级 → 表函数名 / 中文标签（datahub 存短形「一级/二级/三级」，PIKS 映射侧补「申万」前缀）
_SW_LEVEL_TABLES = {1: "sw_index_first_info", 2: "sw_index_second_info",
                    3: "sw_index_third_info"}
_SW_LEVEL_LABELS = {1: "一级", 2: "二级", 3: "三级"}


def build_sw_info_rows(df: pd.DataFrame, level: int) -> list[dict]:
    """申万行业表 → industry_sw_info 入库行。"""
    rows = []
    for _, r in df.iterrows():
        code = _short(r.get("行业代码"))
        if not code:
            continue
        rows.append({
            "code": _trunc(code, 10),
            "level": level,
            "name": _trunc(str(r.get("行业名称") or ""), 50),
            "level_label": _SW_LEVEL_LABELS[level],
            "parent_name": (_trunc(str(r.get("上级行业")), 50)
                            if pd.notna(r.get("上级行业")) else None),
            "member_count": _opt_int(r.get("成份个数")),
            "pe_static": _opt_float(r.get("静态市盈率")),
            "pe_ttm": _opt_float(r.get("TTM(滚动)市盈率")),
            "pb": _opt_float(r.get("市净率")),
            "dividend_yield": _opt_float(r.get("静态股息率")),
        })
    return rows


class IndustrySwInfoCollector(BaseCollector):
    """申万行业分类**当期快照**（一/二/三级）：upsert 刷新，每日一次。

    ⚠️ 快照语义：只 upsert 不删 —— 某日官网 508 时保留上一日行（DB 即缓存），
    退市行业不会自动清除（申万分类稳定，可接受）。
    """

    def fetch(self, levels=(1, 2, 3), *args, **kwargs) -> list[dict]:
        out: list[dict] = []
        ok = 0
        for level in levels:
            table = _SW_LEVEL_TABLES[level]
            try:
                df = _retry(lambda table=table: with_timeout(getattr(ak, table)))
            except Exception as e:  # noqa: BLE001
                logger.warning("申万 %s 级分类拉取失败：%s", level, e)
                continue
            if df is None or df.empty:
                logger.warning("申万 %s 级分类无数据", level)
                continue
            out.extend(build_sw_info_rows(df, level))
            ok += 1
        if levels and ok == 0:
            raise RuntimeError("申万行业分类全部层级拉取失败")
        return out

    def save(self, data):
        upsert(
            self.db, IndustrySwInfo, data,
            conflict_cols=["level", "code"],
            update_cols=["name", "level_label", "parent_name", "member_count",
                         "pe_static", "pe_ttm", "pb", "dividend_yield"],
        )
        logger.info("申万行业分类入库 %s 条", len(data))
        return True


# ---------------------------------------------------------------------------
# 申万三级成分股快照（industry_sw_cons）
# ---------------------------------------------------------------------------

# 成分接口连续失败阈值（镜像 research 熔断：官网 508 期间不逐个硬试）
_CONS_FAIL_THRESHOLD = 3


def build_sw_cons_rows(df: pd.DataFrame, index_code: str) -> list[dict]:
    """申万三级成分表 → industry_sw_cons 入库行。"""
    rows = []
    for _, r in df.iterrows():
        stock = _short(r.get("股票代码"))
        if not stock:
            continue
        rows.append({
            "index_code": _trunc(index_code, 10),
            "stock_code": _trunc(stock, 10),
            "stock_name": _trunc(str(r.get("股票简称") or ""), 50),
            "market_cap": _opt_float(r.get("市值")),
            "pe_ttm": _opt_float(r.get("市盈率ttm")),
            "pb": _opt_float(r.get("市净率")),
            "roe": _opt_float(r.get("ROE(%)")),
            "dividend_yield": _opt_float(r.get("股息率")),
            "net_profit_growth": _opt_float(r.get("净利润增速(%)")),
            "revenue_growth": _opt_float(r.get("营收增速(%)")),
        })
    return rows


class IndustrySwConsCollector(BaseCollector):
    """申万三级成分股**快照**：upsert 刷新，每日一次。

    codes 缺省 = `industry_sw_info` 中 level=3 的全部行业码（申万**只有三级**有成分接口）
    —— 故依赖 `industry_sw_info` 先采（tasks 调度上 sw_info 先于 sw_cons）。
    """

    def _third_level_codes(self) -> list[str]:
        from sqlalchemy import select

        rows = self.db.execute(
            select(IndustrySwInfo.code).where(IndustrySwInfo.level == 3)
        ).scalars().all()
        return sorted(set(rows))

    def fetch(self, index_codes: list[str] | None = None, *args, **kwargs) -> list[dict]:
        codes = index_codes if index_codes is not None else self._third_level_codes()
        if not codes:
            logger.info("申万三级成分：无行业码（industry_sw_info 未采？），跳过")
            return []
        out: list[dict] = []
        ok = fail = 0
        for code in codes:
            if fail >= _CONS_FAIL_THRESHOLD:
                logger.warning("成分接口连续失败 %s 次，熔断余下 %s 个三级行业",
                               fail, len(codes) - ok - fail)
                break
            try:
                df = _retry(lambda code=code: with_timeout(
                    ak.sw_index_third_cons, symbol=f"{code}.SI"), retries=2)
            except Exception as e:  # noqa: BLE001
                fail += 1
                logger.warning("申万三级成分 %s 拉取失败：%s", code, e)
                continue
            fail = 0
            if df is not None and not df.empty:
                out.extend(build_sw_cons_rows(df, code))
            ok += 1
            time.sleep(RESEARCH_SYNC_INTERVAL)
        if codes and ok == 0:
            raise RuntimeError("申万三级成分全部拉取失败")
        return out

    def save(self, data):
        upsert(
            self.db, IndustrySwCons, data,
            conflict_cols=["index_code", "stock_code"],
            update_cols=["stock_name", "market_cap", "pe_ttm", "pb", "roe",
                         "dividend_yield", "net_profit_growth", "revenue_growth"],
        )
        logger.info("申万三级成分入库 %s 条", len(data))
        return True


# ---------------------------------------------------------------------------
# 申万行业指数日线（industry_index）
# ---------------------------------------------------------------------------

def build_industry_index_rows(code: str, df: pd.DataFrame,
                              start_date: date | None = None,
                              end_date: date | None = None) -> list[dict]:
    """申万指数日线 → industry_index 入库行。

    ⚠️ **不存量价**（见模块 docstring）；pct_change/amplitude/chg_amount 由**窗口内**
    序列的 prev_close 派生（首个窗口行 prev 不存在 → 三者置 0），与原 provider 同法。
    """
    df = df.copy()
    df["日期"] = pd.to_datetime(df["日期"])
    mask = pd.Series(True, index=df.index)
    if start_date is not None:
        mask &= df["日期"].dt.date >= start_date
    if end_date is not None:
        mask &= df["日期"].dt.date <= end_date
    df = df.loc[mask].sort_values("日期")
    rows: list[dict] = []
    prev_close: float | None = None
    for _, r in df.iterrows():
        close = _num(r.get("收盘"))
        if close is None:
            continue
        high, low, open_ = _num(r.get("最高")), _num(r.get("最低")), _num(r.get("开盘"))
        if prev_close and prev_close > 0:
            pct = (close - prev_close) / prev_close * 100
            amplitude = ((high - low) / prev_close * 100
                         if high is not None and low is not None else None)
            chg = close - prev_close
        else:
            pct = amplitude = chg = 0.0
        rows.append({
            "code": _trunc(code, 10),
            "trade_date": r["日期"].date(),
            "open": open_, "high": high, "low": low, "close": close,
            "pct_change": pct, "amplitude": amplitude, "chg_amount": chg,
        })
        prev_close = close
    return rows


class IndustryIndexCollector(BaseCollector):
    """申万行业指数日线（全史）。codes 缺省 = `industry_sw_info` 全部行业码。"""

    def _index_codes(self) -> list[str]:
        from sqlalchemy import select

        rows = self.db.execute(select(IndustrySwInfo.code)).scalars().all()
        return sorted(set(rows))

    def fetch(self, codes: list[str] | None = None, start_date: date | None = None,
              end_date: date | None = None, *args, **kwargs) -> list[dict]:
        codes = codes if codes is not None else self._index_codes()
        if not codes:
            logger.info("申万指数：无行业码（industry_sw_info 未采？），跳过")
            return []
        out: list[dict] = []
        ok = 0
        for code in codes:
            try:
                df = _retry(lambda code=code: with_timeout(
                    ak.index_hist_sw, symbol=code, period="day"))
            except Exception as e:  # noqa: BLE001
                logger.warning("申万指数 %s 拉取失败：%s", code, e)
                continue
            if df is not None and not df.empty:
                out.extend(build_industry_index_rows(code, df, start_date, end_date))
            ok += 1
            time.sleep(RESEARCH_SYNC_INTERVAL)
        if codes and ok == 0:
            raise RuntimeError("申万指数全部代码拉取失败")
        return out

    def save(self, data):
        upsert(
            self.db, IndustryIndex, data,
            conflict_cols=["code", "trade_date"],
            update_cols=["open", "high", "low", "close", "pct_change",
                         "amplitude", "chg_amount"],
        )
        logger.info("申万指数入库 %s 条", len(data))
        return True


# ---------------------------------------------------------------------------
# 龙虎榜记录（capital_flow）
# ---------------------------------------------------------------------------

def build_capital_flow_rows(df: pd.DataFrame) -> list[dict]:
    """东财龙虎榜区间表 → capital_flow 入库行（按 (code, 上榜日) 聚合、原因合并）。

    - `pct_change` 取**东财「涨跌幅」**（非新浪「对应值」）。
    - 同交易日多条（多个上榜原因）→ **合并为一行**，reasons 用「；」连接、同因去重。
    """
    df = _clean_columns(df)
    if "代码" not in df.columns:
        return []
    by: dict[tuple, dict] = {}
    for _, r in df.iterrows():
        code = str(r.get("代码")).zfill(6)
        d = _parse_date(r.get("上榜日"))
        if d is None:
            continue
        key = (code, d)
        reason = str(r.get("上榜原因", "") or "").strip()
        if key not in by:
            by[key] = {
                "code": _trunc(code, 10),
                "trade_date": d,
                "name": _trunc(str(r.get("名称", "") or ""), 50),
                "close": _num(r.get("收盘价")),
                "pct_change": _num(r.get("涨跌幅")),
                "turnover_rate": _num(r.get("换手率")),
                "reasons": [],
            }
        if reason and reason not in by[key]["reasons"]:
            by[key]["reasons"].append(reason)
    return [{**v, "reasons": "；".join(v["reasons"])} for v in by.values()]


class CapitalFlowCollector(BaseCollector):
    """龙虎榜记录（东财区间接口，**一次取全市场**，存全部 code）。"""

    def fetch(self, start: date, end: date, *args, **kwargs) -> list[dict]:
        df = with_timeout(
            ak.stock_lhb_detail_em,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            logger.info("龙虎榜区间 %s~%s 无数据", start, end)
            return []
        rows = build_capital_flow_rows(df)
        logger.info("龙虎榜 %s~%s 拉取 %s 条（聚合后）", start, end, len(rows))
        return rows

    def save(self, data):
        upsert(
            self.db, CapitalFlow, data,
            conflict_cols=["code", "trade_date"],
            update_cols=["name", "close", "pct_change", "turnover_rate", "reasons"],
        )
        logger.info("龙虎榜记录入库 %s 条", len(data))
        return True


# ---------------------------------------------------------------------------
# 龙虎榜明细（capital_flow_detail）
# ---------------------------------------------------------------------------

_MULTI_DAY_REASON = "连续三个交易日"


def build_capital_flow_detail_rows(code: str, trade_date: date,
                                   frames: dict[str, pd.DataFrame]) -> list[dict]:
    """买入/卖出两 flag 的席位明细 → capital_flow_detail 入库行。

    - **丢弃**「连续三个交易日」块（跨日口径，非当日成交）；
    - 按 `(营业部, 买入金额, 卖出金额)` 去重（同一笔交易按上榜原因分块重复出现）；
    - 金额字段缺失补 0.0（镜像 research `_to_float`）。
    """
    seen: set = set()
    rows: list[dict] = []
    for _flag, df in frames.items():
        df = _clean_columns(df)
        for _, r in df.iterrows():
            reason = str(r.get("类型", "") or "")
            if _MULTI_DAY_REASON in reason:
                continue
            dealer = str(r.get("交易营业部名称", "") or "")
            buy = _to_float(r.get("买入金额"))
            sell = _to_float(r.get("卖出金额"))
            key = (dealer, buy, sell)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "code": _trunc(code, 10),
                "trade_date": trade_date,
                "dealer_name": _trunc(dealer, 120),
                "buy_amount": buy,
                "buy_pct": _to_float(r.get("买入金额-占总成交比例")),
                "sell_amount": sell,
                "sell_pct": _to_float(r.get("卖出金额-占总成交比例")),
                "net_amount": _to_float(r.get("净额")),
                "reason": _trunc(reason, 40),
            })
    return rows


class CapitalFlowDetailCollector(BaseCollector):
    """龙虎榜营业部明细。

    pairs 缺省 = `capital_flow` 表在 [start, end] 内的全部 `(code, trade_date)`
    —— 明细是记录的钻取，依赖记录先采（tasks 调度上 capital_flow 先于 detail）。
    """

    def _pairs(self, start: date | None, end: date | None) -> list[tuple[str, date]]:
        from sqlalchemy import select

        q = select(CapitalFlow.code, CapitalFlow.trade_date)
        if start is not None:
            q = q.where(CapitalFlow.trade_date >= start)
        if end is not None:
            q = q.where(CapitalFlow.trade_date <= end)
        return sorted(set(self.db.execute(q).all()))

    def fetch(self, pairs=None, start: date | None = None, end: date | None = None,
              *args, **kwargs) -> list[dict]:
        if pairs is None:
            pairs = self._pairs(start, end)
        if not pairs:
            logger.info("龙虎榜明细：无 (code, 日期) 对（capital_flow 未采？），跳过")
            return []
        out: list[dict] = []
        ok = 0
        for code, d in pairs:
            frames: dict[str, pd.DataFrame] = {}
            for flag in ("买入", "卖出"):
                try:
                    df = with_timeout(ak.stock_lhb_stock_detail_em,
                                      symbol=code, date=d.strftime("%Y%m%d"),
                                      flag=flag)
                except Exception as e:  # noqa: BLE001
                    logger.warning("龙虎榜明细 %s %s %s 拉取失败：%s", code, d, flag, e)
                    continue
                if df is not None and not df.empty:
                    frames[flag] = df
            out.extend(build_capital_flow_detail_rows(code, d, frames))
            ok += 1
            time.sleep(RESEARCH_SYNC_INTERVAL)
        if pairs and ok == 0:
            raise RuntimeError("龙虎榜明细全部 (code, 日期) 对拉取失败")
        return out

    def save(self, data):
        upsert(
            self.db, CapitalFlowDetail, data,
            conflict_cols=["code", "trade_date", "dealer_name",
                           "buy_amount", "sell_amount"],
            update_cols=["buy_pct", "sell_pct", "net_amount", "reason"],
        )
        logger.info("龙虎榜明细入库 %s 条", len(data))
        return True


# ---------------------------------------------------------------------------
# 个股新闻（news_by_stock）
# ---------------------------------------------------------------------------

def _content_hash(code: str, title: str, content: str) -> str:
    """去重键：sha256(code + 标题 + 正文) 的十六进制（64 字符，落 VARCHAR(64)）。"""
    h = hashlib.sha256()
    h.update((code or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((title or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((content or "").encode("utf-8"))
    return h.hexdigest()


def build_news_rows(code: str, df: pd.DataFrame) -> list[dict]:
    """东财个股新闻表 → news_by_stock 入库行（列名映射同 research）。"""
    df = df.rename(columns={
        "新闻标题": "title", "新闻内容": "content", "发布时间": "published",
        "文章来源": "source", "新闻链接": "url",
    })
    rows = []
    for _, r in df.iterrows():
        pub = pd.to_datetime(r.get("published"), errors="coerce")
        if pd.isna(pub):
            continue
        title = str(r.get("title", "") or "")
        content = str(r.get("content", "") or "")
        rows.append({
            "code": _trunc(code, 10),
            "title": title,
            "content": content,
            "published_at": pub.to_pydatetime(),
            "source": _trunc(str(r.get("source", "") or ""), 40),
            "url": (_trunc(str(r.get("url")), 500)
                    if pd.notna(r.get("url")) else None),
            "content_hash": _content_hash(code, title, content),
        })
    return rows


class NewsByStockCollector(BaseCollector):
    """个股新闻（`stock_news_em` 返回该票近期新闻；upsert 幂等）。"""

    def fetch(self, codes: list[str], *args, **kwargs) -> list[dict]:
        if not codes:
            return []
        out: list[dict] = []
        ok = 0
        for code in codes:
            try:
                df = with_timeout(ak.stock_news_em, symbol=code)
            except Exception as e:  # noqa: BLE001
                logger.warning("个股新闻 %s 拉取失败：%s", code, e)
                continue
            if df is not None and not df.empty:
                out.extend(build_news_rows(code, df))
            ok += 1
            time.sleep(RESEARCH_SYNC_INTERVAL)
        if codes and ok == 0:
            raise RuntimeError("个股新闻全部代码拉取失败")
        return out

    def save(self, data):
        upsert(
            self.db, NewsByStock, data,
            conflict_cols=["code", "published_at", "content_hash"],
            update_cols=["title", "content", "source", "url"],
        )
        logger.info("个股新闻入库 %s 条", len(data))
        return True


# ---------------------------------------------------------------------------
# 个股公告（announcement_by_stock）
# ---------------------------------------------------------------------------

def build_announcement_rows(code: str, df: pd.DataFrame) -> list[dict]:
    """东财个股公告表 → announcement_by_stock 入库行（列名映射同 research）。"""
    df = df.rename(columns={
        "公告标题": "title", "公告类型": "category",
        "公告日期": "publish_date", "网址": "url",
    })
    rows = []
    for _, r in df.iterrows():
        pub = pd.to_datetime(r.get("publish_date"), errors="coerce")
        if pd.isna(pub):
            continue
        title = str(r.get("title", "") or "")
        if not title:
            continue
        rows.append({
            "code": _trunc(code, 10),
            "title": _trunc(title, 300),
            "category": _trunc(str(r.get("category", "") or ""), 40),
            "publish_date": pub.date(),
            "url": (_trunc(str(r.get("url")), 500)
                    if pd.notna(r.get("url")) else None),
        })
    return rows


class AnnouncementByStockCollector(BaseCollector):
    """个股公告（`stock_individual_notice_report` 返回该票全部历史；upsert 幂等）。"""

    def fetch(self, codes: list[str], *args, **kwargs) -> list[dict]:
        if not codes:
            return []
        out: list[dict] = []
        ok = 0
        for code in codes:
            try:
                df = with_timeout(ak.stock_individual_notice_report, security=code)
            except Exception as e:  # noqa: BLE001
                logger.warning("个股公告 %s 拉取失败：%s", code, e)
                continue
            if df is not None and not df.empty:
                out.extend(build_announcement_rows(code, df))
            ok += 1
            time.sleep(RESEARCH_SYNC_INTERVAL)
        if codes and ok == 0:
            raise RuntimeError("个股公告全部代码拉取失败")
        return out

    def save(self, data):
        upsert(
            self.db, AnnouncementByStock, data,
            conflict_cols=["code", "publish_date", "title"],
            update_cols=["category", "url"],
        )
        logger.info("个股公告入库 %s 条", len(data))
        return True
