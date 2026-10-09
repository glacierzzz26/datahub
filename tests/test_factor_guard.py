"""复权因子除权一致性守卫测试（阶段 3：adj_factor 切 BaoStock 派生因子）"""
from datetime import date

from app.cleaners.factor_guard import factor_change_pairs, guard_factor


def _rows(dates, closes, factors):
    return [{"code": "600519", "trade_date": d, "close": c, "adj_factor": f}
            for d, c, f in zip(dates, closes, factors, strict=False)]


def test_normal_dividend_pass():
    """正常分红：因子阶跃 3% 且价格对应跳空 3% → 接受"""
    rows = _rows(
        [date(2026, 8, 3), date(2026, 8, 4)],
        [10.0, 10.0 / 1.03],   # 除权日收盘降 3%
        [2.0, 2.06],           # 因子 +3%
    )
    _, ok = guard_factor(rows)
    assert ok


def test_small_dividend_with_market_noise_pass():
    """微分红 1% + 市场噪声 1% → 后复权比落在涨跌停内放行"""
    rows = _rows(
        [date(2026, 8, 3), date(2026, 8, 4)],
        [10.0, 10.0 / 1.02],   # gap ≈ +2%
        [2.0, 2.02],           # step +1%
    )
    _, ok = guard_factor(rows)
    assert ok


def test_large_dividend_with_modest_market_move_pass():
    """大分红 10% + 市场涨 4% → 后复权比 ≈ 1.04 在板块限幅内放行"""
    rows = _rows(
        [date(2026, 8, 3), date(2026, 8, 4)],
        [10.0, 10.0 / 1.14],
        [2.0, 2.20],
    )
    _, ok = guard_factor(rows)
    assert ok


def test_pingan_type_fake_adjust_reject():
    """平安型虚假调整：因子阶跃 16.7% 但 close 反涨（无跳空）→ 拒收"""
    rows = _rows(
        [date(2020, 12, 30), date(2020, 12, 31)],
        [10.0, 10.5],          # close 反涨 +5%
        [2.0, 2.334],          # 因子 +16.7% → 后复权比 1.2254
    )
    _, ok = guard_factor(rows)
    assert not ok


def test_tianqi_type_late_adjust_reject():
    """天齐型滞后调整：因子阶跃 16% 但价格无跳空 → 拒收"""
    rows = _rows(
        [date(2020, 1, 1), date(2020, 1, 2)],
        [10.0, 10.0],          # 无跳空
        [2.0, 2.32],           # 因子 +16% → 后复权比 1.16
    )
    _, ok = guard_factor(rows)
    assert not ok


def test_window_start_ignores_context_bad_pairs():
    """window_start 只判窗口内变化对：窗口前（守卫上下文）的坏数据不误拒"""
    rows = _rows(
        [date(2020, 12, 30), date(2020, 12, 31),
         date(2026, 8, 3), date(2026, 8, 4)],
        [10.0, 10.5, 10.5, 10.5 / 1.03],   # 12-31→08-03 因子与收盘均连续
        [2.0, 2.334, 2.334, 2.405],         # 坏对在窗口前；08-04 正常分红 +3%
    )
    ws = date(2026, 8, 3)
    # 无 window_start：12-30→12-31 的坏对（后复权比 1.225）被判违规
    _, ok = guard_factor(rows)
    assert not ok
    # 有 window_start：上下文坏对忽略，窗口内正常分红放行
    _, ok = guard_factor(rows, window_start=ws)
    assert ok


def test_factor_change_pairs_break_on_missing():
    """因子/收盘缺失断开连续性上下文：None 两侧都不成对"""
    rows = [
        {"trade_date": date(2026, 8, 1), "close": 10.0, "adj_factor": 2.0},
        {"trade_date": date(2026, 8, 2), "close": 10.0, "adj_factor": None},
        {"trade_date": date(2026, 8, 3), "close": 10.0, "adj_factor": 2.5},
    ]
    assert list(factor_change_pairs(rows)) == []


def test_empty_rows_pass():
    _, ok = guard_factor([])
    assert ok


# ---------- issue #22：涨跌停日误拒 ----------

def test_limit_move_with_factor_noise_pass_main_board():
    """主板跌停 −9.1% + 因子舍入噪声（step≈−2e-5）→ 放行。

    原实现 tol=0.07 < 板块 10%，把「因子未变 + 跌停」误判为违规；全史回填时
    每只股票都必然遇到涨跌停日 → 100% 拒收（issue #22 实测 30/30）。
    """
    rows = _rows(
        [date(2016, 9, 5), date(2016, 9, 6)],
        [10.0, 10.0 / (1 - 0.091)],   # gap ≈ −9.1%（主板跌停附近）
        [2.0, 2.0 * (1 - 2e-5)],      # step ≈ −2e-5（round(...,4) 噪声）
    )
    _, ok = guard_factor(rows)
    assert ok


def test_limit_move_with_factor_noise_pass_chinext():
    """创业板跌停 −19% + 因子噪声 → 放行（容差须覆盖 20% 板）。"""
    rows = [{"code": "300750", "trade_date": d, "close": c, "adj_factor": f}
            for d, c, f in zip(
                [date(2021, 3, 8), date(2021, 3, 9)],
                [100.0, 100.0 / (1 - 0.19)],
                [1.0, 1.0 * (1 - 3e-5)], strict=False)]
    _, ok = guard_factor(rows)
    assert ok


def test_small_dividend_below_noise_floor_pass():
    """可忽略微分红（step 5e-4 < 噪声下限）+ 大涨 8% → 放行（无真调整可验）。"""
    rows = _rows(
        [date(2026, 8, 3), date(2026, 8, 4)],
        [10.0, 10.0 / 1.08],
        [2.0, 2.0 * 1.0005],
    )
    _, ok = guard_factor(rows)
    assert ok


def test_large_real_dividend_with_limit_move_pass():
    """真除权 9% + 大涨 → ratio 落在板块限幅内放行（边界容差覆盖 20% 板）。"""
    rows = [{"code": "300750", "trade_date": d, "close": c, "adj_factor": f}
            for d, c, f in zip(
                [date(2026, 8, 3), date(2026, 8, 4)],
                [100.0, 100.0 / 1.09],
                [2.0, 2.18], strict=False)]
    _, ok = guard_factor(rows)
    assert ok


def test_pingan_type_still_rejected_after_fix():
    """修复后真·虚假调整仍被拒（严格加严）：平安型 16.7% 无跳空。"""
    rows = _rows(
        [date(2020, 12, 30), date(2020, 12, 31)],
        [10.0, 10.5],          # close 反涨 +5%
        [2.0, 2.334],          # 因子 +16.7%
    )
    _, ok = guard_factor(rows)
    assert not ok
