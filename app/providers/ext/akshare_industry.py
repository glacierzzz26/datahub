"""行业目录 / 成分取数（东财行业板块）。

⚠️ 首源未定（Phase 1 蓝图 §13.2）：此处选东财 `stock_board_industry_name_em`
（目录）与 `stock_board_industry_cons_em`（成分）作首源，**需实测可用性**；
翻闸前须在目标环境验证，不可用再换同花顺 board 接口。

列名同样用 `_pick` 容错（AkShare 版本演进列名可能变化）。
"""
import logging

import akshare as ak

from app.providers.ext.util import f as _f
from app.providers.ext.util import pick as _pick

logger = logging.getLogger(__name__)

name = "akshare_industry"


def _fetch_catalog() -> list[dict]:
    """行业目录：东财行业板块名 / 代码 / 涨跌幅"""
    df = ak.stock_board_industry_name_em()
    name_col = _pick(df, "板块名称", "名称")
    code_col = _pick(df, "板块代码", "代码")
    pct_col = _pick(df, "涨跌幅")
    if not name_col:
        logger.warning("行业目录列缺失")
        return []
    out = []
    for _, r in df.iterrows():
        out.append({
            "name": str(r[name_col]),
            "code": str(r[code_col]) if code_col else None,
            "change_pct": _f(r[pct_col]) if pct_col else None,
        })
    return out


def _fetch_members(industry: str) -> list[dict]:
    """行业成分股：东财行业板块成分（代码 / 名称）"""
    df = ak.stock_board_industry_cons_em(symbol=industry)
    code_col = _pick(df, "代码", "股票代码")
    name_col = _pick(df, "名称", "股票名称")
    if not (code_col and name_col):
        logger.warning("行业成分列缺失（industry=%s）", industry)
        return []
    out = []
    for _, r in df.iterrows():
        out.append({"code": str(r[code_col]), "name": str(r[name_col])})
    return out


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
