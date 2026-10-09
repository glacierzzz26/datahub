"""进程内 TTL 缓存 + 单飞（single-flight）。

- `key = "{dataset_id}:{canonical(params)}"`，TTL 取 `DatasetSpec.ttl_seconds`；
- **单飞**：每 key 一把锁，并发只放一个请求到上游，其余等结果——
  防并发消费打爆上游（Phase 1 蓝图 §5）；
- 过期值**不立即清除**：保留作降级兜底（上游全失败时返 stale + `meta.stale=true`）。
"""
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Entry:
    expire_at: float
    payload: object


class TTLCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._data: dict[str, _Entry] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            lk = self._locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._locks[key] = lk
            return lk

    def peek(self, key: str) -> tuple[object, bool] | None:
        """返回 (payload, fresh) 或 None；过期值仍可取（fresh=False）。"""
        e = self._data.get(key)
        if e is None:
            return None
        return e.payload, e.expire_at > self._clock()

    def get_or_create(self, key: str, ttl: float, producer: Callable[[], object]):
        """命中新鲜值直接返回；否则单飞执行 producer 并写入。

        producer 抛异常则原样转抛，**不写入缓存**（旧值留待降级兜底）。
        """
        e = self._data.get(key)
        if e is not None and e.expire_at > self._clock():
            return e.payload
        with self._lock_for(key):
            e = self._data.get(key)
            if e is not None and e.expire_at > self._clock():
                return e.payload
            payload = producer()
            self._data[key] = _Entry(self._clock() + ttl, payload)
            return payload

    def set(self, key: str, payload: object, ttl: float) -> None:
        self._data[key] = _Entry(self._clock() + ttl, payload)

    def clear(self) -> None:
        with self._guard:
            self._data.clear()
            self._locks.clear()
