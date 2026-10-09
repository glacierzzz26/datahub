"""datahub 测试全局守卫与夹具。

- `_no_patch_leak`：请求层超时补丁**只能在进程入口安装**（app/server.py:main），
  用例间不得残留（镜像 collector/tests/conftest.py 的不变式）。
- `_clean_state`：每个用例前后清空 service 的缓存/冷却，避免跨用例串味。
"""
import pytest

from app import config, service


@pytest.fixture(autouse=True)
def _no_patch_leak():
    yield
    from app.providers.net import installed_timeouts

    assert installed_timeouts() is None, (
        "测试结束时请求层超时补丁仍处于安装态 —— 用例应自行 uninstall_http_timeouts()")


@pytest.fixture(autouse=True)
def _clean_state():
    service.reset_state()
    yield
    service.reset_state()


@pytest.fixture(autouse=True)
def _allow_test_host(monkeypatch):
    """TestClient 的 Host 头是 'testserver' —— MCP DNS-rebinding 白名单须放行。

    带 ':*' 通配，镜像出厂默认（真实客户端 Host 必带端口，见 app/config.py）。"""
    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS",
                        ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*",
                         "testserver", "testserver:*"])


@pytest.fixture
def auth_token(monkeypatch):
    """配置 token，返回明文供 Authorization 头使用。"""
    monkeypatch.setattr(config, "TOKEN", "secret123")
    return "secret123"


@pytest.fixture
def enable_all_ext(monkeypatch):
    """翻闸：启用全部 external 数据集（否则一律 503）。

    仅 `kind=="external"`——raw 数据集（Phase 2）不走 ext 闸门。
    """
    from app.datasets import registry

    ids = [s.id for s in registry.all_datasets() if s.kind == "external"]
    monkeypatch.setattr(config, "EXT_ENABLED", True)
    monkeypatch.setattr(config, "EXT_DATASETS", ids)
    return ids


@pytest.fixture
def bare_requests():
    """安装请求层超时补丁，用例结束后**必定**卸载（搬自 collector/tests/conftest.py）。

    `bare` 取「裸 requests」义：打补丁后裸 `requests.get(url)` 才受超时保护。
    """
    from app.providers.net import install_http_timeouts, uninstall_http_timeouts

    installed = install_http_timeouts()
    try:
        yield installed
    finally:
        uninstall_http_timeouts()


@pytest.fixture
def reset_leak_counter():
    """把 with_timeout 的泄漏计数归零（用例结束后恢复为 0）。

    计数是模块级全局，会被任一强制超时的用例推高。断言计数的用例必须用本 fixture，
    否则执行顺序一变就飘。
    """
    from app.collectors import base

    with base._leak_lock:
        base._leaked_workers = 0
    yield
    with base._leak_lock:
        base._leaked_workers = 0


@pytest.fixture
def no_retry(monkeypatch):
    """令 call_with_retries 直通（不重试、不 sleep）——测试里没有真实等待。"""
    from app.providers import base as prov_base

    def _passthrough(fn, *args, **kwargs):
        kwargs.pop("attempts", None)
        kwargs.pop("delay", None)
        kwargs.pop("timeout", None)
        kwargs.pop("name", None)
        return fn(*args, **kwargs)

    monkeypatch.setattr(prov_base, "call_with_retries", _passthrough)
