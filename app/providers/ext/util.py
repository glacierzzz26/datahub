"""provider 取数公共助手（列名容错 + 安全数值转换）。"""
import pandas as pd


def pick(df: pd.DataFrame, *candidates: str):
    """按候选名取列（忽略大小写）：先精确匹配，再子串双向包含。找不到返回 None。"""
    lower = {str(c).lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    for cand in candidates:
        low = cand.lower()
        for k, orig in lower.items():
            if low in k or k in low:
                return orig
    return None


def f(x) -> float | None:
    """安全转 float；NaN/空返回 None"""
    try:
        v = float(x)
        return v if pd.notna(v) else None
    except (TypeError, ValueError):
        return None
