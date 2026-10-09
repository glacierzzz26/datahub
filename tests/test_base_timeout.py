"""`with_timeout` 去死锁测试（Issue #14 / L2，**验收 #1**）。

**必须用子进程 + 硬超时**：原实现在超时后于 `with` 退出时 `shutdown(wait=True)` 卡死，
且 `concurrent.futures.thread._python_exit` 会在解释器退出时**二次** join 卡死 worker
→ 在**同一进程内**根本观测不到「卡死」这件事（测试自己会一起挂住，pytest 不报错只是不回）。
唯一能把它变成「失败的测试而非挂起的测试」的形态，是起子进程并在父进程加硬超时。

验收口径：mock 一个永不返回的 fn → 调用方按时抛 TimeoutError、**进程按时退出**。
"""
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from app.collectors.base import MAX_LEAKED_WORKERS, leaked_workers, with_timeout

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_child(body: str, hard_timeout: int = 20) -> subprocess.CompletedProcess:
    """在干净子进程里跑 body；父进程硬超时兜底（子进程卡死 → TimeoutExpired → 测试失败）"""
    prologue = f"import sys, threading, time\nsys.path.insert(0, {str(REPO_ROOT)!r})\n"
    code = prologue + textwrap.dedent(body).strip() + "\n"
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=hard_timeout, cwd=str(REPO_ROOT))


# ---------- 验收 #1：权威形态 ----------

def test_hung_fn_does_not_wedge_process():
    """核心验收：超时后调用方抛错，且**进程能正常退出**。

    现码（ThreadPoolExecutor + `with`）会卡在 shutdown(wait=True)/_python_exit，
    子进程永不返回 → 父进程 TimeoutExpired → 本测试红（这正是 09-08 生产症状）。
    修后 ~1s 内正常退出。
    """
    t0 = time.monotonic()
    r = _run_child("""
        from app.collectors.base import with_timeout
        never = threading.Event()
        try:
            with_timeout(never.wait, timeout=1.0)
            print("NO_TIMEOUT"); sys.exit(2)
        except TimeoutError:
            print("TIMEOUT_OK")
        sys.exit(0)
    """)
    elapsed = time.monotonic() - t0
    assert "TIMEOUT_OK" in r.stdout, f"未按时抛 TimeoutError:\n{r.stdout}\n{r.stderr}"
    assert r.returncode == 0, f"进程未能正常退出（returncode={r.returncode}）:\n{r.stderr}"
    assert elapsed < 10, f"进程退出耗时 {elapsed:.1f}s，疑似仍被卡死 worker 拖住"


def test_hung_fn_exits_cleanly_with_pending_worker():
    """超时后**进程仍能正常退出**——即没有任何 atexit 在 join 被遗弃的 worker。

    这是「用原生 daemon Thread 而非 ThreadPoolExecutor」的承重理由：后者把 worker
    登记进 `_threads_queues`，`_python_exit` 会 join 每一个 → 解释器退不出去。
    """
    r = _run_child("""
        import concurrent.futures.thread as t
        from app.collectors.base import with_timeout
        never = threading.Event()
        try:
            with_timeout(never.wait, timeout=0.5)
        except TimeoutError:
            pass
        # 若仍用 ThreadPoolExecutor，此断言会因 _threads_queues 被登记而失败
        assert len(t._threads_queues) == 0, f"worker 被登记进 _threads_queues: {t._threads_queues}"
        print("CLEAN")
        sys.exit(0)
    """)
    assert "CLEAN" in r.stdout, f"{r.stdout}\n{r.stderr}"
    assert r.returncode == 0


# ---------- 返回值 / 异常传递 ----------

def test_returns_value():
    assert with_timeout(lambda a, b: a + b, 2, 3, timeout=5) == 5


def test_passes_kwargs():
    assert with_timeout(lambda *, x: x * 2, x=21, timeout=5) == 42


def test_propagates_exception():
    def boom():
        raise ValueError("内部错误")
    with pytest.raises(ValueError, match="内部错误"):
        with_timeout(boom, timeout=5)


def test_propagates_base_exception():
    """BaseException 也照传（KeyboardInterrupt 不应被吞成超时）"""
    def interrupt():
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        with_timeout(interrupt, timeout=5)


def test_default_timeout_used(monkeypatch, caplog):
    """timeout=None 时用 REQUEST_TIMEOUT 缺省，且日志带上函数名标签"""
    import app.collectors.base as base
    monkeypatch.setattr(base, "REQUEST_TIMEOUT", 0.3)
    never = threading.Event()
    with pytest.raises(TimeoutError):
        with_timeout(never.wait)
    assert any("请求超时" in r.message for r in caplog.records)


def test_name_param_labels_log(caplog):
    never = threading.Event()
    with pytest.raises(TimeoutError):
        with_timeout(never.wait, timeout=0.2, name="自定义标签")
    assert any("自定义标签" in r.message for r in caplog.records)


# ---------- 泄漏计数（看门狗的探测器）----------

def test_leaked_workers_counted(reset_leak_counter, caplog):
    assert leaked_workers() == 0
    never = threading.Event()
    with pytest.raises(TimeoutError):
        with_timeout(never.wait, timeout=0.2)
    assert leaked_workers() == 1
    assert any("遗弃 worker 线程" in r.message for r in caplog.records)


def test_subsequent_call_returns_immediately(reset_leak_counter):
    """泄漏的 worker 不得拖慢后续调用。

    这是「per-call 线程」相对「共享长驻池」的承重性质：共享池里卡死 worker 永久占坑，
    下一个调用排在尸体后面 → 整体停摆。per-call 下每次都是新线程，只多一个线程而已。
    """
    never = threading.Event()
    with pytest.raises(TimeoutError):
        with_timeout(never.wait, timeout=5)  # 会泄漏 1 个
    t0 = time.monotonic()
    assert with_timeout(lambda: "ok", timeout=5) == "ok"
    assert time.monotonic() - t0 < 0.5, "后续调用被泄漏 worker 拖慢了"


def test_max_leaked_workers_is_sane():
    """看门狗以 MAX_LEAKED_WORKERS 为阈值；须为正且远小于线程耗尽规模"""
    assert 1 <= MAX_LEAKED_WORKERS <= 64


# ---------- 并发形态下的正确性 ----------

def test_no_cross_talk_between_calls(reset_leak_counter):
    """两个调用并行：各自拿到自己的返回值（box 是 per-call 的，不共享）"""
    barrier = threading.Barrier(2)

    def waiter(v):
        barrier.wait(timeout=5)
        return v

    results = {}

    def call(tag, v):
        results[tag] = with_timeout(waiter, v, timeout=5)

    ts = [threading.Thread(target=call, args=(i, i * 10)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert results == {0: 0, 1: 10}
