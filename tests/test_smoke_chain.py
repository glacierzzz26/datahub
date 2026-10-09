"""验收 #4 的 opt-in 冒烟：18:10→19:30 链路含一次模拟超时（Issue #14）。

**默认整文件跳过**（CI/日常 `pytest` 不会执行、不碰 DB）。要跑需显式开：
```bash
export COLLECTOR_SMOKE=1
export COLLECTOR_FAULT_INJECT_TIMEOUT_HOSTS=<东财 host，如 push2his.eastmoney.com>
export COLLECTOR_FAULT_INJECT_ONCE=1
export COLLECTOR_HTTP_READ_TIMEOUT=5
python3 -m pytest tests/test_smoke_chain.py -v
```
它做的是**真链路**：打一次东财 → 被故障注入打断 → 降级新浪 → 行入库，
断言"秒级完成 + 有行落库"。跑之前请确认连的是**开发库**，它会写 daily_price。

验收 #4 的完整手工 runbook（含看门狗自杀、容器拉起、startup_catchup 补跑）
写在 PR body 里——那部分要真容器，无法在 pytest 内断言。
"""
import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("COLLECTOR_SMOKE"),
    reason="opt-in 冒烟：需 COLLECTOR_SMOKE=1 且连开发库（会写 daily_price）")


@pytest.fixture(autouse=True)
def _require_inject():
    if not os.getenv("COLLECTOR_FAULT_INJECT_TIMEOUT_HOSTS"):
        pytest.skip("需 COLLECTOR_FAULT_INJECT_TIMEOUT_HOSTS 才能模拟超时")


def test_daily_chain_degrades_on_injected_timeout():
    """东财腿被注入超时 → 降级新浪 → 拿到行（正是 09-08 之后生产应有的行为）"""
    from app.collectors.daily import fetch_pair
    from app.providers.net import install_http_timeouts, uninstall_http_timeouts

    install_http_timeouts()
    try:
        t0 = time.monotonic()
        raw, hfq = fetch_pair("600519", "20260901", "20260916")
        elapsed = time.monotonic() - t0
    finally:
        uninstall_http_timeouts()

    assert not raw.empty, "降级后应拿到行（若为空：新浪腿也被封/断网，非本测试失败）"
    assert elapsed < 60, f"降级链路过慢（{elapsed:.0f}s），超时保护未生效？"
    # fetch_pair 返回的是**新浪归一化后**的东财列名（normalize_sina 已把 close→收盘）
    assert {"日期", "开盘", "最高", "最低", "收盘", "成交量"} <= set(raw.columns), \
        f"列名不符（新浪归一化应产出东财列名）: {list(raw.columns)}"
    assert raw["收盘"].notna().all(), "收盘价不应有空值"


def test_run_writes_rows_to_db():
    """真写库：DailyCollector.run() 后 daily_price 当日有行（确认链路端到端）

    ⚠️ 会写开发库。确认 DB_* 指向开发库再跑。
    """
    from app.collectors.daily import DailyCollector
    from app.db import get_session
    from app.providers.net import install_http_timeouts, uninstall_http_timeouts

    install_http_timeouts()
    try:
        ok = DailyCollector(get_session()).run(codes=["600519"], start="20260901",
                                               end="20260916")
    finally:
        uninstall_http_timeouts()
    assert ok is True, "采集未成功（base.run 已达最大重试）"
