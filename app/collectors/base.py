"""采集器基类：统一异常处理与重试逻辑"""
import logging
import threading
import time
from abc import ABC, abstractmethod
from datetime import date, datetime

from app.collect_config import REQUEST_TIMEOUT


def to_ak_date(value) -> str:
    """把 date / datetime / ISO 字符串统一成 AkShare 需要的 YYYYMMDD"""
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    return str(value).replace("-", "").replace("/", "")


# ---------- 请求超时兜底（Issue #14）----------
# 泄漏 worker 计数：每次强制超时遗弃一个 daemon 线程（Python 线程不可杀），
# 该线程自清（socket 带 HTTP_READ_TIMEOUT，到点抛异常即退），不持有 DB session
# （所有 with_timeout 调用点都是纯 fetch，无一写库）。稳态为 0，供看门狗探测。
_leak_lock = threading.Lock()
_leaked_workers = 0
MAX_LEAKED_WORKERS = 8


def leaked_workers() -> int:
    """累计被遗弃的 worker 线程数（看门狗探测用）"""
    return _leaked_workers


def with_timeout(fn, *args, timeout=None, name=None, **kwargs):
    """在**一次性 daemon 线程**内执行请求并施加超时；超时抛 TimeoutError。

    ⚠️ 历史坑（Issue #14）：原实现用
        with ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(fn, ...).result(timeout=timeout)
    超时后 `with` 退出执行 `shutdown(wait=True)` → **永久阻塞在还挂着的 worker 上**
    （09-08 采集卡死 24h+ 的根因）；且 `concurrent.futures.thread` 注册的
    `_python_exit` 会在解释器退出时再 join 每个 worker → 二次阻塞，连正常退出都做不到。
    故改为原生 daemon `threading.Thread`：它**不登记**在任何地方（只有 concurrent.futures
    碰 `_threads_queues`），超时即弃线程返回，调用方/进程都不会挂。

    请求层的真正修法在 providers/net.py（给 requests 注入 socket 超时）；本函数是
    兜底网，覆盖 requests 之外可能阻塞的环节（DNS/TLS/解析）。
    """
    if timeout is None:
        timeout = REQUEST_TIMEOUT
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
        logging.getLogger(__name__).error(
            "请求超时（>%ss，%s）——遗弃 worker 线程（累计 %s）", timeout, label, n)
        raise TimeoutError(f"请求超时（>{timeout}s）")
    if "error" in box:
        raise box["error"]
    return box["value"]


class BaseCollector(ABC):
    """所有采集器的基类。

    子类需实现 fetch()（拉取数据）与 save()（入库），
    run() 提供统一的重试与日志框架。
    """

    max_retries = 3
    retry_delay = 5  # seconds

    def __init__(self, db_session):
        self.db = db_session
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    def fetch(self, *args, **kwargs):
        """从数据源拉取数据，返回记录列表"""
        raise NotImplementedError

    @abstractmethod
    def save(self, data):
        """将数据保存到数据库"""
        raise NotImplementedError

    def run(self, *args, **kwargs):
        """执行采集（带重试）"""
        for attempt in range(1, self.max_retries + 1):
            try:
                data = self.fetch(*args, **kwargs)
                self.save(data)
                self.logger.info("采集成功: %s 条", len(data))
                return True
            except Exception as e:
                self.logger.warning("第 %s 次重试: %s", attempt, e)
                # 回滚失败事务，否则后续重试报 current transaction is aborted
                try:
                    self.db.rollback()
                except Exception:
                    pass
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay)
        self.logger.error("采集失败，已达最大重试次数")
        return False
