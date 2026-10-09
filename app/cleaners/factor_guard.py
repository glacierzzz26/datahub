"""复权因子除权一致性守卫（阶段 3：adj_factor 切 BaoStock 派生因子的拒收层）

背景（docs/phase2/design/数据源评估-BaoStock.md §2.2/§6）：BaoStock 派生因子
全池 51% 有分段阶跃，其中平安银行（2020-12-31 虚假调整 16.7%，close 反涨）与
天齐锂业（2020-01-02 滞后调整 16%，无对应价格跳空）为实证缺陷。本守卫在入库前
校验「因子阶跃必须对应价格跳空」，不符 → 整段拒收、由采集器降级 AkShare
（东财/新浪 hfq 派生因子），保该股单源一致。

判据（**后复权连续性**，issue #22 起改直判）：
    后复权价 hfq = close × factor，除权日 hfq 连续，故相邻日
        ratio = (close[d]·f[d]) / (close[d-1]·f[d-1]) = 1 + 当日市场涨跌
    市场涨跌幅恒在板块涨跌停内 ⇒ ratio 必须落在 [1−限, 1+限]。
虚假/滞后调整 = 因子阶跃但价格未对应 ⇒ ratio 明显超出涨跌停（可高可低）。
平安：ratio = (10.5·2.334)/(10·2.0) = 1.2254（+22.5%，无此涨幅）→ 拒；
天齐：ratio = (10.0·2.32)/(10·2.0) = 1.16（+16%，无跳空）→ 拒。

容差下限 = 板块涨跌停（主板 10%、创业/科创 20%、北交 30%）× `_TOL_CUSHION`：
- 涨跌停是**硬边界**——真·假调必须超出它才会被判违规，故取满；
- 余量吸收两处非理想：① `adj_factor` 为 `round(...,4)`、价格 2 位小数 →
  低股价股在涨跌停边缘 ratio 可达 1.10xx（实测 1.1037）；② 边界日（ST 5% 除外，
  实测样本未见超 1.1037）。`_TOL_CUSHION=1.05` 实测 101 只样本 0 拒收，
  而平安（1.225）天齐（1.160）仍远在容差（0.105/0.21）之外被拒。

> **issue #22 根因**：原判据 `|step − gap| > max(0.07, 0.5·|step|)`（step=f[d]/f[d-1]−1,
> gap=close[d-1]/close[d]−1）把涨跌停日当违规——容差下限 0.07 比板块限幅还小，
> 单日涨跌幅 >7% 即触发。全史批量回填时每只股票 10 年必遇涨跌停 → **100% 拒收**
> （实测 30/30），扩池新码回填彻底死锁。改直判 ratio（市场涨跌自然封顶于板块限幅）
> 后根治，且**严格加严**检出力（真异常被拒更明确）。
"""
import logging

logger = logging.getLogger(__name__)

# 容差 = 板块涨跌停 × 本系数。>1 的余量吸收 adj_factor round(...,4) 与价格
# 2 位小数在涨跌停边缘的舍入溢出（低股价股实测 ratio 至 1.1037）。不可取过大：
# 真异常下界约 1.16（天齐），本金保留足够判别间隔。
_TOL_CUSHION = 1.05
# 因子噪声下限：|step| 低于此值视为「因子未变」——round(...,4) 舍入噪声（实测 ≤4e-4）
# 或可忽略微分红，无真调整可验，市场涨跌即 ratio，跳过不予比对。
_MIN_STEP = 1e-3


def board_limit(code: str) -> float:
    """板块日涨跌幅上限（涨跌停）：市场自身波动可达此幅度，容差须覆盖之。

    创业/科创 ±20%（300/301/688/689）、北交所 ±30%（8/4 开头）、其余主板 ±10%。
    与 `quant-engine/app/data_quality.py::_board_limit` 同口径（两服务各持一份，
    避免跨服务 import）。
    """
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("8", "4")):
        return 0.30
    return 0.10


def factor_change_pairs(rows):
    """相邻交易日复权因子变化对 → 迭代 (d1, f1, c1, d2, f2, c2)

    因子或收盘缺失（None）断开连续性上下文：前一行不再作为下一对的 d1。
    """
    prev = None
    for r in rows:
        f, c = r.get("adj_factor"), r.get("close")
        if f is None or c is None:
            prev = None
            continue
        if prev is not None and abs(f - prev[1]) > 1e-9:
            yield (prev[0], prev[1], prev[2], r["trade_date"], f, c)
        prev = (r["trade_date"], f, c)


def guard_factor(rows: list[dict], window_start=None) -> tuple[list[dict], bool]:
    """除权一致性校验 → (rows, ok)。

    :param window_start: 仅对 trade_date >= window_start 的因子变化对判违规；
        更早的行只作上下文（采集窗口 start−7 拉取的边界行不因自身旧数据被拒）。
        None 则全部判定（历史重算脚本 rewrite_adj_factor 用）。
    :return: (rows, ok)；ok=False 时调用方整段降级，保该股单源一致。
    """
    if not rows:
        return rows, True
    code = rows[0].get("code")
    tol = board_limit(code or "") * _TOL_CUSHION
    for d1, f1, c1, d2, f2, c2 in factor_change_pairs(rows):
        if window_start is not None and d2 < window_start:
            continue
        if f1 <= 0 or f2 <= 0 or c1 <= 0 or c2 <= 0:
            continue
        step = f2 / f1 - 1
        if abs(step) < _MIN_STEP:
            # 因子未变（舍入噪声/可忽略分红）：无调整可验，市场涨跌即 ratio，跳过。
            continue
        ratio = (c2 * f2) / (c1 * f1)  # 后复权价之比 = 1 + 当日市场涨跌
        if abs(ratio - 1) > tol:
            logger.warning(
                "复权因子拒收 %s %s→%s（close %s→%s, factor %s→%s）："
                "后复权比 %.4f 偏离 1 超容差 %.4f（板块限幅 %.0f%%，step=%.4f）",
                code, d1, d2, c1, c2, f1, f2, ratio, tol, board_limit(code or "") * 100,
                step)
            return rows, False
    return rows, True
