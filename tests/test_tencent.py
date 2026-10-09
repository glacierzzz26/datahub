"""腾讯源适配层测试（mock HTTP，不打真实端点）"""
from datetime import date

import pytest

from app.sources import tencent

# 腾讯日 K 归一化后的列序（多处断言复用，避免重复超长行）
_COLUMNS = ["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额", "换手率"]

# ---------- 代码 / 单位 ----------

def test_tx_code():
    assert tencent.tx_code("600519") == "sh600519"
    assert tencent.tx_code("000001") == "sz000001"
    assert tencent.tx_code("300750") == "sz300750"
    assert tencent.tx_code("430047") == "bj430047"
    assert tencent.tx_code("830001") == "bj830001"
    assert tencent.tx_code("920001") == "bj920001"


def test_volume_divisor_by_board():
    # 科创板 688/689 = 股 → ÷100；其余 = 手 → ÷1
    assert tencent.volume_divisor("688111") == 100
    assert tencent.volume_divisor("689009") == 100
    assert tencent.volume_divisor("600519") == 1
    assert tencent.volume_divisor("300750") == 1
    assert tencent.volume_divisor("430047") == 1  # 北交所暂按手（未实证）


def test_to_lots_and_yuan():
    assert tencent._to_lots("688111", "4482345") == 44823  # 股 → 手
    assert tencent._to_lots("600519", "26235") == 26235    # 手原样
    assert tencent._to_lots("600519", "") is None
    assert tencent._to_yuan("330793") == 3.30793e9          # 万元 → 元


def test_to_pct_is_percent_verbatim():
    """换手率源侧已是 %，**不做换算**（勿混入新浪小数口径，否则 100×）"""
    assert tencent._to_pct("0.21") == 0.21
    assert tencent._to_pct("1.85") == 1.85
    assert tencent._to_pct("") is None
    assert tencent._to_pct(None) is None
    assert tencent._to_pct("bad") is None


# ---------- 日K ----------

def _kline_row(d, o, c, h, lo, v, amt):
    return [d, o, c, h, lo, v, {}, "0.5", amt]


def test_daily_raw_parses_close_at_index_2(monkeypatch):
    """行结构 [date,open,CLOSE,high,low,volume,...,amount] —— close 在下标 2"""
    captured = {}

    def fake_page(symbol, end, count):
        captured["symbol"] = symbol
        return [
            _kline_row("2026-09-10", "1285.13", "1275.16", "1290.00", "1270.00", "26235", "330793"),
            _kline_row("2026-09-11", "1276.00", "1280.00", "1285.00", "1272.00", "28000", "350000"),
        ]

    monkeypatch.setattr(tencent, "_kline_page", fake_page)
    df = tencent.daily_raw("600519", "20260901", "20260916")
    assert captured["symbol"] == "sh600519"
    assert list(df.columns) == _COLUMNS
    r = df.iloc[0]
    assert r["收盘"] == 1275.16   # 下标 2 是 close，不是 high
    assert r["最高"] == 1290.00   # 下标 3
    assert r["最低"] == 1270.00   # 下标 4
    assert r["成交额"] == 3.30793e9
    assert r["换手率"] == 0.5      # [7] 换手率（%），源侧已是百分数


def test_daily_raw_star_board_volume_divided(monkeypatch):
    monkeypatch.setattr(tencent, "_kline_page",
                        lambda *a: [_kline_row("2026-09-10", "220", "222", "225",
                                               "219", "4482345", "99626")])
    df = tencent.daily_raw("688111", "20260901", "20260916")
    assert df.iloc[0]["成交量"] == 44823  # 股 → 手


def test_daily_raw_drops_malformed_short_rows(monkeypatch):
    """行长度 <9 视为畸形，丢弃 + warning，不崩"""
    monkeypatch.setattr(tencent, "_kline_page", lambda *a: [
        _kline_row("2026-09-10", "10", "11", "12", "9", "100", "1"),
        ["2026-09-11", "10", "11"],  # 畸形
    ])
    df = tencent.daily_raw("600519", "20260901", "20260916")
    assert len(df) == 1
    assert df.iloc[0]["日期"] == "2026-09-10"


def test_daily_raw_empty_when_no_data(monkeypatch):
    monkeypatch.setattr(tencent, "_kline_page", lambda *a: [])
    df = tencent.daily_raw("600519", "20260901", "20260916")
    assert df.empty
    assert list(df.columns) == _COLUMNS


def test_daily_raw_paginates_backward(monkeypatch):
    """count 上限 2000：页满且未覆盖 start → end 前移继续翻页"""
    calls = []

    def fake_page(symbol, end, count):
        calls.append(end)
        if len(calls) == 1:
            # 满页（2000 行），最早 2020-01-01 > start → 触发翻页
            return [_kline_row("2020-01-01", "1", "1", "1", "1", "1", "1")] * 2000
        return [_kline_row("2019-12-31", "1", "1", "1", "1", "1", "1")]

    monkeypatch.setattr(tencent, "_kline_page", fake_page)
    tencent.daily_raw("600519", "20190101", "20260916")
    assert calls[0] == "2026-09-16"
    assert calls[1] == "2019-12-31"  # 最早行前一日


def test_daily_raw_filters_out_of_window(monkeypatch):
    """产出只保留 [start, end] 内行（翻页可能带回窗口外）"""
    monkeypatch.setattr(tencent, "_kline_page", lambda *a: [
        _kline_row("2026-08-25", "1", "1", "1", "1", "1", "1"),  # 窗口外
        _kline_row("2026-09-10", "1", "1", "1", "1", "1", "1"),
    ])
    df = tencent.daily_raw("600519", "20260901", "20260916")
    assert [r["日期"] for _, r in df.iterrows()] == ["2026-09-10"]


# ---------- tripwire ----------

def test_daily_pairs_is_tripwire():
    """腾讯 hfq 非等比 → 派生因子入口必须显式抛异常（防后人误用）"""
    with pytest.raises(RuntimeError, match="hfq"):
        tencent.daily_pairs("600519", "20260901", "20260916")


# ---------- 批量快照 ----------

_QUOTE_600519 = ('1~贵州茅台~600519~1258.00~1272.75~1273.93~26235~12117~14119~'
                 "1258.00~17~1257.99~4~1257.88~6~1257.80~3~1257.79~1~1258.02~2~"
                 "1258.04~4~1258.08~1~1258.18~1~1258.23~1~~20260916161500~-14.75~"
                 "-1.16~1274.98~1254.10~1258.00/26235/3307926407~26235~330793~"
                 "0.21~19.31~~1274.98~1254.10~1.64~15726.03~15726.03~6.26~1400.03~"
                 "1145.48~1.13~22~1260.87~17.66~19.10~~~0.09~330792.6407~100.6400~")


def test_quote_batch_parses_gbk_js_lines(monkeypatch):
    payload = f'v_sh600519="{_QUOTE_600519}";'
    monkeypatch.setattr(tencent, "_get_text", lambda url: payload)
    out = tencent.quote_batch(["sh600519"])
    assert "sh600519" in out
    assert out["sh600519"][3] == "1258.00"  # 现价
    assert out["sh600519"][4] == "1272.75"  # 昨收


def test_quote_batch_tolerates_missing_codes(monkeypatch):
    """实测 58 请求 / 48 返回：缺码须容忍，不可当整体成功"""
    payload = f'v_sh600519="{_QUOTE_600519}";'
    monkeypatch.setattr(tencent, "_get_text", lambda url: payload)
    out = tencent.quote_batch(["sh600519", "sz000001"])
    assert set(out) == {"sh600519"}  # 000001 缺失，不崩


def test_snapshot_rows_shape_and_units(monkeypatch):
    monkeypatch.setattr(tencent, "quote_batch",
                        lambda syms: {"sh600519": _QUOTE_600519.split("~")})
    rows = tencent.snapshot_rows(["600519"])
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "600519"
    assert r["close"] == 1258.00
    assert r["open"] == 1273.93
    assert r["high"] == 1274.98
    assert r["low"] == 1254.10
    assert r["prev_close"] == 1272.75
    assert r["volume"] == 26235           # 主板=手
    assert r["amount"] == 3.30793e9       # 万元 → 元
    assert r["adj_factor"] is None        # 快照不含因子
    assert r["trade_date"] == date(2026, 9, 16)  # 取快照时间戳日期


def test_snapshot_rows_star_board_volume(monkeypatch):
    fields = _QUOTE_600519.split("~")
    fields[6] = "4482345"  # 科创板成交量（股）
    monkeypatch.setattr(tencent, "quote_batch", lambda syms: {"sh688111": fields})
    rows = tencent.snapshot_rows(["688111"])
    assert rows[0]["volume"] == 44823


def test_snapshot_rows_skips_suspended(monkeypatch):
    """停牌（现价 0）→ 跳过，交 cleaner 语义处理"""
    fields = _QUOTE_600519.split("~")
    fields[3] = "0.00"
    monkeypatch.setattr(tencent, "quote_batch", lambda syms: {"sh600519": fields})
    assert tencent.snapshot_rows(["600519"]) == []


def test_snapshot_rows_empty_codes():
    assert tencent.snapshot_rows([]) == []


# ---------- 源被限 ----------

def test_is_source_blocked():
    assert tencent.is_source_blocked(RuntimeError("腾讯源被限(HTTP 429)"))
    assert not tencent.is_source_blocked(RuntimeError("腾讯请求失败: timeout"))


def test_get_text_403_raises_blocked(monkeypatch):
    class Resp:
        status_code = 403

    monkeypatch.setattr(tencent, "_get_session", lambda: type(
        "S", (), {"get": lambda self, url, timeout: Resp()})())
    with pytest.raises(RuntimeError, match="源被限"):
        tencent._get_text("http://x")


def test_decode_gbk_fallback():
    # 快照行是 GBK：UTF-8 解不开 → 回退 GBK
    assert "贵州茅台" in tencent._decode("贵州茅台".encode("gbk"))
    # 日K JSON 是 UTF-8
    assert tencent._decode(b'{"a":1}') == '{"a":1}'
