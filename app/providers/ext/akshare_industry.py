"""行业目录 / 成分取数。

**首源经实测冻结（2026-10-09）**：
- `industry.catalog` → 主源**同花顺**（`stock_board_industry_name_ths` 取 name/code，
  `stock_board_industry_summary_ths` 取涨跌幅）；东财 `stock_board_industry_name_em`
  兜底。**东财 board 接口在目标环境实测稳定不可达（RemoteDisconnected）**，故不能作首源。
- `industry.members` → 只有东财 `stock_board_industry_cons_em`（akshare 唯一成分接口），
  无同花顺替代。**该接口不可达时本数据集返回 503**——已知实现受限，非代码缺陷；
  需目标环境实测可达方可翻闸（蓝图 §13.2）。

列名用 `_pick` 容错（AkShare 版本演进列名可能变化）。
"""
import logging

import akshare as ak

from app.providers.ext.util import f as _f
from app.providers.ext.util import pick as _pick

logger = logging.getLogger(__name__)

name = "akshare_industry"


def _catalog_from_ths() -> list[dict]:
    """同花顺行业目录：name/code（name_ths）+ change_pct（summary_ths 按名合并）。"""
    names = ak.stock_board_industry_name_ths()
    ncn, ncc = _pick(names, "name", "名称"), _pick(names, "code", "代码")
    if not ncn:
        logger.warning("同花顺行业目录列缺失")
        return []
    pct: dict[str, float | None] = {}
    try:
        summary = ak.stock_board_industry_summary_ths()
        scn, scp = _pick(summary, "板块", "name"), _pick(summary, "涨跌幅")
        if scn and scp:
            pct = {str(r[scn]): _f(r[scp]) for _, r in summary.iterrows()}
    except Exception as e:  # noqa: BLE001 —— 涨跌幅缺失不阻断目录
        logger.warning("同花顺板块概览失败（涨跌幅留空）: %s", e)
    return [
        {"name": str(r[ncn]),
         "code": str(r[ncc]) if ncc else None,
         "change_pct": pct.get(str(r[ncn]))}
        for _, r in names.iterrows()
    ]


def _catalog_from_em() -> list[dict]:
    """东财行业目录（兜底；东财不可达时抛异常由调用方降级）。"""
    df = ak.stock_board_industry_name_em()
    name_col = _pick(df, "板块名称", "名称")
    code_col = _pick(df, "板块代码", "代码")
    pct_col = _pick(df, "涨跌幅")
    if not name_col:
        logger.warning("东财行业目录列缺失")
        return []
    return [
        {"name": str(r[name_col]),
         "code": str(r[code_col]) if code_col else None,
         "change_pct": _f(r[pct_col]) if pct_col else None}
        for _, r in df.iterrows()
    ]


def _fetch_catalog() -> list[dict]:
    """行业目录：主源同花顺，失败降级东财。"""
    try:
        rows = _catalog_from_ths()
        if rows:
            return rows
    except Exception as e:  # noqa: BLE001
        logger.warning("同花顺行业目录失败(%s)，降级东财", e)
    return _catalog_from_em()


def _fetch_members(industry: str) -> list[dict]:
    """行业成分股：东财行业板块成分（akshare 唯一成分接口）。

    ⚠️ 该接口不可达时抛异常 → 服务层 503（不静默返回空）。
    """
    df = ak.stock_board_industry_cons_em(symbol=industry)
    code_col = _pick(df, "代码", "股票代码")
    name_col = _pick(df, "名称", "股票名称")
    if not (code_col and name_col):
        logger.warning("行业成分列缺失（industry=%s）", industry)
        return []
    return [{"code": str(r[code_col]), "name": str(r[name_col])} for _, r in df.iterrows()]


def fetch(dataset_id: str, params: dict) -> list[dict]:
    """按数据集 id 分发到对应取数函数（供 providers/registry 调用）。"""
    if dataset_id == "industry.catalog":
        return _fetch_catalog()
    if dataset_id == "industry.members":
        industry = params.get("industry")
        if not industry:
            raise ValueError("industry.members 需要参数 industry")
        return _fetch_members(str(industry))
    raise ValueError(f"akshare_industry 不支持的数据集: {dataset_id}")
