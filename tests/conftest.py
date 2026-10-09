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
    """TestClient 的 Host 头是 'testserver' —— MCP DNS-rebinding 白名单须放行。"""
    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS",
                        ["localhost", "127.0.0.1", "testserver"])


@pytest.fixture
def auth_token(monkeypatch):
    """配置 token，返回明文供 Authorization 头使用。"""
    monkeypatch.setattr(config, "TOKEN", "secret123")
    return "secret123"


@pytest.fixture
def enable_all_ext(monkeypatch):
    """翻闸：启用全部 external 数据集（否则一律 503）。"""
    from app.datasets import registry

    ids = [s.id for s in registry.all_datasets()]
    monkeypatch.setattr(config, "EXT_ENABLED", True)
    monkeypatch.setattr(config, "EXT_DATASETS", ids)
    return ids


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
