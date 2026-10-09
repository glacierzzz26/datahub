"""缓存用例：TTL 过期重取 + 并发单飞只打一次上游。"""
import threading
import time

from app.cache import TTLCache


def test_ttl_expiry_refetches():
    t = [0.0]
    cache = TTLCache(clock=lambda: t[0])
    calls = []

    def producer():
        calls.append(1)
        return len(calls)

    assert cache.get_or_create("k", 10, producer) == 1
    assert cache.get_or_create("k", 10, producer) == 1   # 命中，不再调用
    t[0] = 11.0
    assert cache.get_or_create("k", 10, producer) == 2   # 过期重取


def test_peek_returns_stale_after_expiry():
    t = [0.0]
    cache = TTLCache(clock=lambda: t[0])
    cache.set("k", "v", ttl=10)
    assert cache.peek("k") == ("v", True)
    t[0] = 11.0
    assert cache.peek("k") == ("v", False)   # 过期值仍可取（降级兜底）


def test_producer_error_not_cached():
    t = [0.0]
    cache = TTLCache(clock=lambda: t[0])

    def boom():
        raise RuntimeError("上游挂")

    for _ in range(2):
        try:
            cache.get_or_create("k", 10, boom)
        except RuntimeError:
            pass
    assert cache.peek("k") is None


def test_single_flight_one_upstream_call():
    cache = TTLCache()
    calls = {"n": 0}
    barrier = threading.Barrier(5)

    def producer():
        calls["n"] += 1
        time.sleep(0.1)
        return "value"

    results = []

    def worker():
        barrier.wait()
        results.append(cache.get_or_create("k", 10, producer))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert calls["n"] == 1                 # 并发只放一个请求到上游
    assert results == ["value"] * 5
