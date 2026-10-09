"""Provider 协议 + 超时兜底 + 重试（搬自 collector/app/collectors/base.py:with_timeout）。

`with_timeout` 用**一次性 daemon 线程**执行请求并施加超时；超时抛 TimeoutError，
遗弃 worker（Python 线程不可杀，但其 socket 带 HTTP_READ_TIMEOUT，到点自清）。
**勿改用 ThreadPoolExecutor** —— 09-08 采集卡死事故的根因即 shutdown(wait=True) 永久
阻塞在挂死 worker 上（见 collector Issue #14 / 项目记忆 collector-wedge-20260908-issue14）。
"""
import logging
import threading
import time
from typing import Protocol, runtime_checkable

from app import config

logger = logging.getLogger(__name__)

# 泄漏 worker 计数（看门狗/诊断用）
_leak_lock = threading.Lock()
_leaked_workers = 0
MAX_LEAKED_WORKERS = 8


def leaked_workers() -> int:
    return _leaked_workers


def with_timeout(fn, *args, timeout=None, name=None, **kwargs):
    """在一次性 daemon 线程内执行 fn 并施加超时；超时抛 TimeoutError。"""
    if timeout is None:
        timeout = config.REQUEST_TIMEOUT
    label = name or getattr(fn, "__name__", str(fn))
    box: dict = {}
    done = threading.Event()

    def _run():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001 —— 原样转抛给调用方
            box["error"] = e
        finally:
            done.set()

    threading.Thread(target=_run, name=f"with_timeout:{label}", daemon=True).start()
    if not done.wait(timeout):
        global _leaked_workers
        with _leak_lock:
            _leaked_workers += 1
            n = _leaked_workers
        logger.error("请求超时（>%ss，%s）——遗弃 worker 线程（累计 %s）", timeout, label, n)
        raise TimeoutError(f"请求超时（>{timeout}s）")
    if "error" in box:
        raise box["error"]
    return box["value"]


@runtime_checkable
class Provider(Protocol):
    """provider 协议：给定 dataset id + 归一化 params，返回行列表（list[dict]）。"""

    name: str

    def fetch(self, dataset_id: str, params: dict) -> list[dict]:
        ...


def call_with_retries(fn, *args, attempts: int = 3, delay: float = 5.0,
                      timeout=None, name=None, **kwargs):
    """带超时与重试地调用 fn：每次都施加 with_timeout，失败 sleep(delay) 重试。

    最后一次失败原样抛出（不静默吞）。实例化于 collector 的 `run` 3×5s 范式，
    但把超时下沉到每次尝试（collector 的 run 只兜 fetch 整体）。
    """
    label = name or getattr(fn, "__name__", str(fn))
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return with_timeout(fn, *args, timeout=timeout, name=label, **kwargs)
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("%s 第 %s/%s 次失败: %s", label, attempt, attempts, e)
            if attempt < attempts:
                time.sleep(delay)
    assert last is not None
    raise last
