"""请求层超时补丁（搬自 collector/app/sources/net.py，env 前缀改 DATAHUB_）。

给 `requests.Session.request` 打一次补丁，把 `None`/缺省的 timeout 换成真实
`(connect, read)` 元组（AkShare 底层 requests 无有效超时，半开连接会永久挂起）。

**只在进程入口安装**（app/server.py 的启动处），**不在 import 期装**——测试会
import 各模块，import 期安装会静默污染整个测试集（见 collector Issue #14）。
"""
import logging
import os
import threading

import requests

logger = logging.getLogger(__name__)

_DEFAULTS = {"connect": 5.0, "read": 15.0}
_ENV = {"connect": "DATAHUB_HTTP_CONNECT_TIMEOUT",
        "read": "DATAHUB_HTTP_READ_TIMEOUT"}

_lock = threading.Lock()
_installed: tuple[float, float] | None = None
_original_request = None
_injected = False


def _resolve(connect, read) -> tuple[float, float]:
    def pick(given, key):
        if given is not None:
            return float(given)
        raw = os.getenv(_ENV[key])
        if raw is None or raw.strip() == "":
            return _DEFAULTS[key]
        try:
            return float(raw)
        except ValueError:
            logger.warning("环境变量 %s=%r 非法，回退默认 %s", _ENV[key], raw, _DEFAULTS[key])
            return _DEFAULTS[key]

    return pick(connect, "connect"), pick(read, "read")


def install_http_timeouts(connect: float | None = None,
                          read: float | None = None,
                          enabled: bool | None = None) -> tuple[float, float] | None:
    """安装请求层超时补丁（幂等）。返回生效的 (connect, read)；被旁路返回 None。

    只替换 `None`（显式 timeout 原样保留）。`enabled=False` 或 env
    `DATAHUB_HTTP_TIMEOUT_PATCH=0` → 硬旁路（当日回滚用）。
    """
    global _installed, _original_request

    if enabled is None:
        enabled = os.getenv("DATAHUB_HTTP_TIMEOUT_PATCH",
                            "1").strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        logger.info("请求层超时补丁已旁路（DATAHUB_HTTP_TIMEOUT_PATCH=0）")
        return None

    with _lock:
        if _installed is not None:
            return _installed

        connect_s, read_s = _resolve(connect, read)

        if not hasattr(requests.Session, "request"):
            raise RuntimeError("requests.Session.request 不存在，请求层超时补丁无法安装")

        _original_request = requests.Session.request

        def _patched_request(self, method, url, **kwargs):
            t = kwargs.get("timeout")
            if t is None:
                kwargs["timeout"] = (connect_s, read_s)
            elif isinstance(t, tuple) and len(t) == 2 and (t[0] is None or t[1] is None):
                kwargs["timeout"] = (t[0] if t[0] is not None else connect_s,
                                     t[1] if t[1] is not None else read_s)
            if _should_inject(url):
                raise requests.exceptions.ReadTimeout(f"故障注入（命中 {url}）")
            return _original_request(self, method, url, **kwargs)

        requests.Session.request = _patched_request
        _installed = (connect_s, read_s)
        logger.info("请求层超时补丁已安装：connect=%ss read=%ss", connect_s, read_s)
        return _installed


def _should_inject(url: str) -> bool:
    """故障注入（验收用）：URL 命中子串 → 抛一次 ReadTimeout，验证降级链。"""
    global _injected
    hosts = [h for h in os.getenv("DATAHUB_FAULT_INJECT_TIMEOUT_HOSTS", "").split(",") if h.strip()]
    if not hosts:
        return False
    once = os.getenv("DATAHUB_FAULT_INJECT_ONCE", "").strip().lower() in ("1", "true", "yes", "on")
    if once and _injected:
        return False
    if any(h.strip() in str(url) for h in hosts):
        if once:
            _injected = True
        logger.warning("故障注入：主动对 %s 抛 ReadTimeout", url)
        return True
    return False


def uninstall_http_timeouts() -> None:
    """还原原始 requests.Session.request（**仅测试用**）。"""
    global _installed, _original_request
    with _lock:
        if _original_request is not None:
            requests.Session.request = _original_request
        _installed = None
        _original_request = None


def installed_timeouts() -> tuple[float, float] | None:
    return _installed


def is_timeout(exc: BaseException) -> bool:
    """统一的超时判定：requests 的 Timeout 是 OSError 不是内置 TimeoutError。"""
    return isinstance(exc, (TimeoutError, requests.exceptions.Timeout))
