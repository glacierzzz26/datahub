"""热点取数（move 自 collector/app/collectors/hotspot.py 的纯取数函数）。

Phase 1 是「复制过来先跑通」；steady 侧对应代码在 **Phase 3 收尾时删除**
（登记为待删，见 Phase 1 蓝图 §12）。

列名防御：AkShare 版本演进列名可能变化，`_pick` 按候选名做「精确 → 子串」匹配，
缺列/异常时该项返回空列表，绝不抛异常中断整批（热点是增强数据，失败可降级）。

⚠️ 与 collector 的差异：`top` 由数据集参数传入（原为模块级 HOTSPOT_TOP_N 常量）；
`_fetch_turnover`（两市成交，需读 daily_price）**未搬**——它是派生数据，留待 Phase 2。
"""
import logging
from datetime import date

import akshare as ak

from app import config
from app.providers.ext.util import f as _f
from app.providers.ext.util import pick as _pick

logger = logging.getLogger(__name__)

name = "akshare_hotspot"

# 美股指数代码 → 中文名（新浪格式 .DJI/.IXIC/.INX/.NDX）
US_INDEX_NAMES = {".DJI": "道琼斯", ".IXIC": "纳斯达克", ".INX": "标普500", ".NDX": "纳指100"}
# A股重要指数（东财 spot 名称）
CN_INDEX_NAMES = ["上证指数", "深证成指", "创业板指"]


def _fmt_flow(v) -> str | None:
    """资金净流入格式化：元 → 'X.XX亿'；空返回 None"""
    v = _f(v)
    if v is None:
        return None
    return f"{v / 1e8:.2f}亿"


def _fmt_yi(v) -> str | None:
    """格式化「已是亿元」的值 → 'X.XX亿'（同花顺板块概览净流入单位即亿）"""
    v = _f(v)
    if v is None:
        return None
    return f"{v:.2f}亿"


# ---------- 分项抓取（各自 try/except，失败返回 [] 不中断整批） ----------

def _fetch_indices() -> list[dict]:
    rows = []
    for symbol in config.hotspot_index_list():
        try:
            df = ak.index_us_stock_sina(symbol=symbol)
            if df is None or len(df) < 2:
                logger.warning("外盘 %s 无数据", symbol)
                continue
            last, prev = df.iloc[-1], df.iloc[-2]
            close = _f(last.get("close"))
            pc = _f(prev.get("close"))
            rows.append({
                "name": US_INDEX_NAMES.get(symbol, symbol),
                "code": symbol,
                "close": close,
                "change_pct": round((close / pc - 1) * 100, 2) if close and pc else None,
            })
        except Exception as e:
            logger.warning("外盘 %s 采集失败: %s", symbol, e)
    rows.extend(_cn_indices_from_em() or _cn_indices_from_sina())
    return rows


def _cn_indices_from_em() -> list[dict]:
    """A股指数主源：东财 沪深京指数 spot。失败/缺列返回 []（由调用方降级新浪）。"""
    try:
        df = ak.stock_zh_index_spot_em(symbol="沪深重要指数")
        name_col, px_col, pct_col = (
            _pick(df, "名称"), _pick(df, "最新价", "最新"), _pick(df, "涨跌幅"))
        if not (name_col and px_col and pct_col):
            logger.warning("东财A股指数列缺失，降级新浪")
            return []
        out = []
        for _, r in df.iterrows():
            if str(r[name_col]) in CN_INDEX_NAMES:
                out.append({
                    "name": str(r[name_col]), "code": str(r.get("代码", "")),
                    "close": _f(r[px_col]), "change_pct": _f(r[pct_col]),
                })
        return out
    except Exception as e:
        logger.warning("东财A股指数采集失败(%s)，降级新浪", e)
        return []


def _cn_indices_from_sina() -> list[dict]:
    """A股指数兜底：新浪 指数快照 spot（prod 东财不可达时生效；列结构同族，_pick 兼容）。"""
    try:
        df = ak.stock_zh_index_spot_sina()
        name_col, px_col, pct_col = (
            _pick(df, "名称"), _pick(df, "最新价", "最新"), _pick(df, "涨跌幅"))
        if not (name_col and px_col and pct_col):
            logger.warning("新浪A股指数列缺失，跳过")
            return []
        out = []
        for _, r in df.iterrows():
            if str(r[name_col]) in CN_INDEX_NAMES:
                out.append({
                    "name": str(r[name_col]), "code": str(r.get("代码", "")),
                    "close": _f(r[px_col]), "change_pct": _f(r[pct_col]),
                })
        return out
    except Exception as e:
        logger.warning("新浪A股指数采集失败: %s", e)
        return []


def _fetch_ths_sectors() -> list[dict] | None:
    """同花顺行业板块概览一次拉取，返回归一化 [{name, change_pct, leader, net_inflow}]。
    失败返回 None（由调用方回退）。"""
    try:
        df = ak.stock_board_industry_summary_ths()
        name_col = _pick(df, "板块")
        pct_col = _pick(df, "涨跌幅")
        flow_col = _pick(df, "净流入")
        leader_col = _pick(df, "领涨股")
        if name_col is None:
            return None
        out = []
        for _, r in df.iterrows():
            raw_flow = _f(r[flow_col]) if flow_col else None
            out.append({
                "name": str(r[name_col]),
                "change_pct": _f(r[pct_col]) if pct_col else None,
                "leader": str(r[leader_col]) if leader_col else None,
                "net_inflow": _fmt_yi(raw_flow),   # 展示用：格式化字符串
                "net_inflow_raw": raw_flow,        # 排序用：原始数值（单位已是亿）
            })
        return out
    except Exception as e:
        logger.warning("同花顺板块概览失败: %s", e)
        return None


def _sectors_gain_from(ths: list[dict] | None, top: int) -> list[dict]:
    """板块涨幅 TOP_N：主源同花顺（涨跌幅列），回退东财行业板块"""
    if ths:
        rows = sorted(ths, key=lambda x: x["change_pct"] or -999, reverse=True)[:top]
        return [{k: r[k] for k in ("name", "change_pct", "leader")} for r in rows]
    try:
        df = ak.stock_board_industry_name_em()
        name_col, pct_col, leader_col = (
            _pick(df, "板块名称", "名称"), _pick(df, "涨跌幅"), _pick(df, "领涨股票"))
        if not (name_col and pct_col):
            logger.warning("东财板块涨幅列缺失，跳过")
            return []
        out = []
        for _, r in df.sort_values(pct_col, ascending=False).head(top).iterrows():
            out.append({
                "name": str(r[name_col]),
                "change_pct": _f(r[pct_col]),
                "leader": str(r[leader_col]) if leader_col else None,
            })
        return out
    except Exception as e:
        logger.warning("东财板块涨幅榜失败: %s", e)
        return []


def _sectors_flow_from(ths: list[dict] | None, top: int) -> list[dict]:
    """板块资金净流入 TOP_N：主源同花顺（净流入列），回退东财主力净流入，
    再回退同花顺行业资金流（即时）。"""
    if ths:
        rows = sorted(ths, key=lambda x: x.get("net_inflow_raw") or -999,
                      reverse=True)[:top]
        return [{"name": r["name"], "net_inflow": r["net_inflow"]} for r in rows
                if r["net_inflow"] is not None]
    try:
        df = ak.stock_board_industry_name_em()
        name_col = _pick(df, "板块名称", "名称")
        flow_col = _pick(df, "主力净流入", "净流入")
        if name_col and flow_col:
            out = []
            for _, r in df.sort_values(flow_col, ascending=False).head(top).iterrows():
                out.append({"name": str(r[name_col]), "net_inflow": _fmt_flow(r[flow_col])})
            return out
    except Exception as e:
        logger.warning("东财板块资金流失败: %s", e)
    try:
        df = ak.stock_fund_flow_industry(symbol="即时")
        name_col = _pick(df, "行业")
        flow_col = _pick(df, "净额", "净流入", "净流出")
        if name_col and flow_col:
            out = []
            for _, r in df.sort_values(flow_col, ascending=False).head(top).iterrows():
                out.append({"name": str(r[name_col]), "net_inflow": _fmt_flow(r[flow_col])})
            return out
    except Exception as e:
        logger.warning("同花顺资金流采集失败: %s", e)
    return []


def _fetch_hot_rank(top: int) -> list[dict]:
    try:
        df = ak.stock_hot_rank_em()
        rank_col = _pick(df, "序号", "排名")
        code_col = _pick(df, "代码", "股票代码")
        name_col = _pick(df, "名称", "股票名称")
        pct_col = _pick(df, "涨跌幅")
        if not (name_col and code_col):
            logger.warning("人气榜列缺失，跳过")
            return []
        out = []
        for i, (_, r) in enumerate(df.head(top).iterrows(), 1):
            out.append({
                "rank": int(r[rank_col]) if rank_col and _f(r[rank_col]) is not None else i,
                "code": str(r[code_col]),
                "name": str(r[name_col]),
                "change_pct": _f(r[pct_col]) if pct_col else None,
            })
        return out
    except Exception as e:
        logger.warning("人气榜采集失败: %s", e)
        return []


def _fetch_zt_pool(spot_date: date, top: int) -> list[dict]:
    """涨停股池兜底：早盘时段 date 取当日，接口返回最近交易日涨停个股
    （「昨日涨停·今日关注」），语义契合早报活跃个股。失败/缺列返回 []。"""
    try:
        df = ak.stock_zt_pool_em(date=spot_date.strftime("%Y%m%d"))
        code_col = _pick(df, "代码", "股票代码")
        name_col = _pick(df, "名称", "股票名称")
        pct_col = _pick(df, "涨跌幅")
        board_col = _pick(df, "连板数")
        ind_col = _pick(df, "所属行业")
        if not (name_col and code_col):
            logger.warning("涨停池列缺失，跳过")
            return []
        out = []
        for i, (_, r) in enumerate(df.head(top).iterrows(), 1):
            days = _f(r[board_col]) if board_col else None
            pct = _f(r[pct_col]) if pct_col else None
            out.append({
                "rank": i,
                "code": str(r[code_col]),
                "name": str(r[name_col]).replace(" ", ""),  # 源数据偶含空格（中 关 村）
                "change_pct": round(pct, 2) if pct is not None else None,
                "board_days": int(days) if days is not None else 1,  # 连板数
                "industry": str(r[ind_col]) if ind_col else None,
            })
        return out
    except Exception as e:
        logger.warning("涨停池采集失败: %s", e)
        return []


def _fetch_hot_stocks(spot_date: date, top: int) -> list[dict]:
    """活跃个股：人气榜为主，涨停池兜底（人气榜网络不可达时降级）"""
    rows = _fetch_hot_rank(top)
    if rows:
        return rows
    return _fetch_zt_pool(spot_date, top)


# ---------- 数据集分发 ----------

def fetch(dataset_id: str, params: dict) -> list[dict]:
    """按数据集 id 分发到对应取数函数（供 providers/registry 调用）。"""
    top = int(params.get("top") or config.HOTSPOT_TOP_N)
    if dataset_id == "hotspot.indices":
        return _fetch_indices()
    if dataset_id == "hotspot.sectors_gain":
        return _sectors_gain_from(_fetch_ths_sectors(), top)
    if dataset_id == "hotspot.sectors_flow":
        return _sectors_flow_from(_fetch_ths_sectors(), top)
    if dataset_id == "hotspot.hot_stocks":
        return _fetch_hot_stocks(date.today(), top)
    raise ValueError(f"akshare_hotspot 不支持的数据集: {dataset_id}")
