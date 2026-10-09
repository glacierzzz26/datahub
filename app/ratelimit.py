"""限流与源被限冷却（Phase 1 蓝图 §5）。

- `RateLimiter`：每 provider 一个信号量（并发上限）+ 最小请求间隔（错峰上游）；
- `Blocklist`：命中 403/429/封禁后进程内冷却，冷却期内不再发请求；
- `is_source_blocked(exc)`：通用判定（搬 tencent.py:is_source_blocked / baostock.py 语义）。
"""
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

# 源被限的通用标记（小写子串匹配）
_BLOCK_MARKERS = (
    "403", "429", "forbidden", "too many requests",
    "被封", "封禁", "被限", "限频", "黑名单",
)


def is_source_blocked(exc: BaseException) -> bool:
    """判定异常是否为「源被限」而非瞬时错误（403/429/封禁 → 冷却，不反复轰击）。"""
    msg = str(exc).lower()
    return any(m in msg for m in _BLOCK_MARKERS)


class RateLimiter:
    """信号量并发上限 + 最小请求间隔。"""

    def __init__(self, min_interval: float = 0.2, concurrency: int = 4,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._sem = threading.BoundedSemaphore(max(1, concurrency))
        self._min = max(0.0, min_interval)
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    @contextmanager
    def slot(self):
        with self._sem:
            with self._lock:
                if self._last is not None:
                    wait = self._min - (self._clock() - self._last)
                    if wait > 0:
                        self._sleep(wait)
                self._last = self._clock()
            yield


class Blocklist:
    """进程内冷却表：key（provider 名）→ 冷却截止时刻。"""

    def __init__(self, cooldown: float = 1800,
                 clock: Callable[[], float] = time.monotonic):
        self._cooldown = cooldown
        self._clock = clock
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return self._until.get(key, 0.0) > self._clock()

    def remaining(self, key: str) -> float:
        with self._lock:
            return max(0.0, self._until.get(key, 0.0) - self._clock())

    def block(self, key: str, seconds: float | None = None) -> None:
        secs = self._cooldown if seconds is None else seconds
        with self._lock:
            self._until[key] = self._clock() + secs

    def clear(self) -> None:
        with self._lock:
            self._until.clear()
