"""请求层超时的**端到端**验证（Issue #14 / 验收 #2）。

用一个真实的本地「黑洞」服务（accept 后不回包，半开连接）复现 09-08 的卡死条件，
断言：配置秒数内抛 `ReadTimeout`，且 `fetch_pair` 在**东财腿真实超时**时降级新浪腿、
整体耗时受限——即"15s 内降级新浪继续"，全程不依赖外网。

为什么不用 mock：mock 掉 requests 就测不到「socket 层真的到点返回」这件事本身。
黑洞服务让 recv 真的永不返回，超时只能来自我们注入的 socket timeout。
"""
import socket
import threading
import time

import pytest

from app.collectors.daily import fetch_pair
from app.providers.net import install_http_timeouts, uninstall_http_timeouts


@pytest.fixture
def blackhole():
    """accept 连接但**永不回包**的本地 TCP 服务（半开连接 → recv 永久阻塞）"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held = []
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            held.append(conn)  # 攥着不放、不回任何数据

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.getsockname()[1]}"
    finally:
        stop.set()
        srv.close()
        for c in held:
            try:
                c.close()
            except OSError:
                pass


@pytest.fixture
def patched(monkeypatch):
    """装补丁并设短超时（测试要快，不真等 15s）"""
    monkeypatch.setenv("DATAHUB_HTTP_CONNECT_TIMEOUT", "2")
    monkeypatch.setenv("DATAHUB_HTTP_READ_TIMEOUT", "1")
    installed = install_http_timeouts()
    try:
        yield installed
    finally:
        uninstall_http_timeouts()


def test_bare_get_times_out_on_half_open(patched, blackhole):
    """裸 requests.get 打半开连接：必须在 read 超时内抛 ReadTimeout。

    这正是 09-08 卡死的那条腿（新浪 `stock_zh_a_daily` 是裸 requests.get、无 timeout）——
    没有补丁时它会挂到天荒地老（见本文件末尾的对照测试）。
    """
    import requests

    t0 = time.monotonic()
    with pytest.raises(requests.exceptions.Timeout):
        requests.get(blackhole, timeout=None)
    elapsed = time.monotonic() - t0
    assert elapsed < 5, f"超时应在配置秒数内发生，实际 {elapsed:.1f}s"


def test_connect_timeout_half_uses_connect_config(patched):
    """连接半边的超时也要生效：打 TEST-NET-1（192.0.2.0/24，保证不可达）"""
    import requests

    t0 = time.monotonic()
    with pytest.raises(requests.exceptions.Timeout):
        requests.get("http://192.0.2.1:9/", timeout=None)
    elapsed = time.monotonic() - t0
    assert elapsed < 8, f"connect 超时未生效，实际 {elapsed:.1f}s"


def test_without_patch_it_hangs(blackhole):
    """**对照**：不装补丁时同一个请求不会按时超时（证明上条的红不是白红的）。

    不实际等它挂死——只断言「1s 内没抛异常」即可说明补丁缺失时无保护。
    """
    import requests

    from app.providers.net import uninstall_http_timeouts  # noqa: F401  确保未安装
    assert __import__("app.providers.net", fromlist=["x"]).installed_timeouts() is None

    done = threading.Event()
    err = {}

    def call():
        try:
            requests.get(blackhole, timeout=None)
        except BaseException as e:  # noqa: BLE001
            err["e"] = e
        finally:
            done.set()

    threading.Thread(target=call, daemon=True).start()
    assert not done.wait(2.0), "未装补丁却按时返回了——黑洞服务没起作用？"
    assert "e" not in err


def test_fetch_pair_degrades_to_sina_within_budget(patched, blackhole, monkeypatch, caplog):
    """**端到端**：东财腿真实超时 → 降级新浪腿并返回新浪行，总耗时受限。

    这是 09-08 之后生产应有的行为：18:10 那次卡死若发生在今天，15s 内就会降级新浪继续，
    而不是永久挂起把当天行情拖没。
    """
    import pandas as pd

    from app.collectors import daily

    def fake_em(**kwargs):
        # 东财腿：真的去打黑洞（经补丁 → ReadTimeout），复现"接口卡住"
        import requests
        requests.get(blackhole)
        raise AssertionError("不应走到这里")

    sina_calls = []

    def fake_sina(symbol=None, start_date=None, end_date=None, adjust=None, **kw):
        sina_calls.append((symbol, adjust))
        return pd.DataFrame({
            "date": ["2026-09-16"], "open": [10.0], "high": [10.5], "low": [9.9],
            "close": [10.2], "volume": [123400], "amount": [1250000.0],
        })

    monkeypatch.setattr(daily.ak, "stock_zh_a_hist", fake_em)
    monkeypatch.setattr(daily.ak, "stock_zh_a_daily", fake_sina)

    t0 = time.monotonic()
    raw, hfq = fetch_pair("600519", "20260910", "20260916")
    elapsed = time.monotonic() - t0

    assert elapsed < 8, f"降级链路应在秒级完成，实际 {elapsed:.1f}s"
    assert not raw.empty, "降级新浪后应拿到数据"
    # fetch_pair 返回的是**新浪归一化后**的东财列名（normalize_sina 已把 close→收盘）
    assert raw.iloc[0]["收盘"] == 10.2
    assert hfq.iloc[0]["收盘"] == 10.2
    # 新浪腿被调了两次（raw + hfq），且带了市场前缀
    assert [c[0] for c in sina_calls] == ["sh600519", "sh600519"]
    assert [c[1] for c in sina_calls] == ["", "hfq"]
    # 降级原因记为「超时」（is_timeout 判定生效，而非退化成原始异常串）
    assert any("超时" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]


def test_fetch_pair_sina_leg_also_bounded(patched, blackhole, monkeypatch):
    """两腿都卡：新浪腿也必须在配置秒数内返回/失败，而非挂死（09-08 正是新浪腿挂住）。

    新浪腿失败时异常**原样上抛**（fetch_pair 不吞），由 base.run() 的重试框架接住——
    这正是不静默记 0 条成功的设计。故此处只断言「有界时间内抛异常」，不约束具体类型。
    """
    import pandas as pd

    from app.collectors import daily

    def fake_em(**kwargs):
        import requests
        requests.get(blackhole)

    def fake_sina(**kwargs):
        import requests
        requests.get(blackhole)
        return pd.DataFrame()

    monkeypatch.setattr(daily.ak, "stock_zh_a_hist", fake_em)
    monkeypatch.setattr(daily.ak, "stock_zh_a_daily", fake_sina)

    t0 = time.monotonic()
    with pytest.raises(Exception) as ei:
        fetch_pair("600519", "20260910", "20260916")
    elapsed = time.monotonic() - t0
    assert elapsed < 12, f"双源卡死时应快速失败触发重试，实际 {elapsed:.1f}s"
    # 必须是超时类异常（而非如 KeyError 之类的解析错），否则说明卡在别处
    from app.providers.net import is_timeout
    assert is_timeout(ei.value), f"应抛超时类异常，实际 {type(ei.value).__name__}: {ei.value}"
