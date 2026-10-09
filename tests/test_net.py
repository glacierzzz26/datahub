"""请求层超时补丁测试（Issue #14 / L1）。

**这里测的是「补丁真的把超时送到底层」，不是「补丁函数被调用」**——用
`HTTPAdapter.send` 作为观测点：requests 的 `Session.request` 最终会经它出网，
它拿到的 `timeout` 就是套接字层真正用的值。若只看补丁函数的返回值，
补丁写错（比如 setdefault 在显式 `timeout=None` 时不生效）也能「通过」。

三种调用形态必须全覆盖（实测三者都会走到 Session.request，故一处补丁即可）：
  1. 裸 `requests.get(url)`          —— 无 timeout 键
  2. `requests.get(url, timeout=None)` —— 键在值为 None（**setdefault 式的补丁在此失效**）
  3. `Session().get(url)`             —— 复用会话

另含 **akshare 传输层审计守卫**：补丁只覆盖走 `requests` 的 akshare 函数；
若某个函数被上游改成 `curl_cffi`，补丁静默失效（超时不再受控）——该测试把这条
隐形依赖变成会红的告警线。
"""
import inspect
import subprocess
import sys
import types

import pytest
import requests
from requests.adapters import HTTPAdapter

from app.providers.net import (
    install_http_timeouts,
    installed_timeouts,
    is_timeout,
    uninstall_http_timeouts,
)


@pytest.fixture
def spy_adapter(monkeypatch):
    """拦在 HTTPAdapter.send：记录底层实际收到的 timeout（不出网）"""
    seen: dict = {}
    real_send = HTTPAdapter.send

    def fake_send(self, request, stream=False, timeout=None, verify=True,
                  cert=None, proxies=None):
        seen["timeout"] = timeout
        raise requests.exceptions.ConnectionError("spy：不真正出网")

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    yield seen
    HTTPAdapter.send = real_send


def _attempt(fn, *a, **kw):
    """触发一次请求并吞掉 spy 抛出的连接错误（只为看 timeout 落到哪）"""
    with pytest.raises(requests.exceptions.ConnectionError):
        fn(*a, **kw)


# ---------- 补丁生效性 ----------

def test_idempotent(bare_requests):
    """二次安装返回同一元组，不重复打补丁"""
    first = install_http_timeouts()
    second = install_http_timeouts()
    assert first == second == (5.0, 15.0)
    assert installed_timeouts() == (5.0, 15.0)


@pytest.mark.parametrize("shape", ["bare", "explicit_none", "session"])
def test_all_call_shapes_get_real_timeout(bare_requests, spy_adapter, shape):
    """三种形态都必须落到同一个真实 (connect, read)"""
    if shape == "bare":
        _attempt(requests.get, "http://example.invalid/")
    elif shape == "explicit_none":
        _attempt(requests.get, "http://example.invalid/", timeout=None)
    else:
        _attempt(requests.Session().get, "http://example.invalid/")
    assert spy_adapter["timeout"] == (5.0, 15.0), f"{shape} 形态未拿到真实超时"


def test_explicit_float_preserved(bare_requests, spy_adapter):
    """显式数字 timeout 原样保留（改成元组只会改变异常语义，无收益）"""
    _attempt(requests.get, "http://example.invalid/", timeout=3)
    assert spy_adapter["timeout"] == 3


def test_complete_tuple_preserved(bare_requests, spy_adapter):
    """完整元组不动"""
    _attempt(requests.get, "http://example.invalid/", timeout=(1, 2))
    assert spy_adapter["timeout"] == (1, 2)


@pytest.mark.parametrize("given,want", [
    ((None, 7), (5.0, 7)),
    ((3, None), (3, 15.0)),
])
def test_partial_tuple_filled(bare_requests, spy_adapter, given, want):
    """半空元组补空位（requests 允许 (connect, read) 任一为 None）"""
    _attempt(requests.get, "http://example.invalid/", timeout=given)
    assert spy_adapter["timeout"] == want


def test_env_override_without_reload(monkeypatch, spy_adapter):
    """env 在 install 时读（不依赖 import config），便于运维当日调参不重建"""
    monkeypatch.setenv("DATAHUB_HTTP_CONNECT_TIMEOUT", "2.5")
    monkeypatch.setenv("DATAHUB_HTTP_READ_TIMEOUT", "9")
    install_http_timeouts()
    try:
        assert installed_timeouts() == (2.5, 9.0)
        _attempt(requests.get, "http://example.invalid/")
        assert spy_adapter["timeout"] == (2.5, 9.0)
    finally:
        uninstall_http_timeouts()


def test_explicit_args_beat_env(monkeypatch):
    monkeypatch.setenv("DATAHUB_HTTP_CONNECT_TIMEOUT", "2.5")
    install_http_timeouts(connect=1.0, read=4.0)
    try:
        assert installed_timeouts() == (1.0, 4.0)
    finally:
        uninstall_http_timeouts()


def test_bad_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DATAHUB_HTTP_READ_TIMEOUT", "abc")
    install_http_timeouts()
    try:
        assert installed_timeouts() == (5.0, 15.0)
    finally:
        uninstall_http_timeouts()


# ---------- 开关 / 旁路 ----------

def test_disabled_returns_none(spy_adapter):
    """enabled=False 硬旁路：请求回到「无限等」（当日回滚用）"""
    assert install_http_timeouts(enabled=False) is None
    assert installed_timeouts() is None
    _attempt(requests.get, "http://example.invalid/")
    assert spy_adapter["timeout"] is None


def test_env_disables_patch(monkeypatch, spy_adapter):
    monkeypatch.setenv("DATAHUB_HTTP_TIMEOUT_PATCH", "0")
    assert install_http_timeouts() is None
    _attempt(requests.get, "http://example.invalid/")
    assert spy_adapter["timeout"] is None


@pytest.mark.parametrize("value", ["1", "true", "YES", "On"])
def test_env_enables_patch(monkeypatch, value):
    monkeypatch.setenv("DATAHUB_HTTP_TIMEOUT_PATCH", value)
    install_http_timeouts()
    try:
        assert installed_timeouts() is not None
    finally:
        uninstall_http_timeouts()


# ---------- 身份还原 ----------

def test_uninstall_restores_identity():
    original = requests.Session.request
    install_http_timeouts()
    assert requests.Session.request is not original
    uninstall_http_timeouts()
    assert requests.Session.request is original, "卸载后必须还原为**同一个**函数对象"


def test_install_fails_loudly_when_patch_point_missing(monkeypatch):
    """打不上补丁就抛，别让进程静默裸奔（fail loudly）"""
    original = requests.Session.request
    monkeypatch.delattr(requests.Session, "request", raising=False)
    try:
        with pytest.raises(RuntimeError, match="无法安装"):
            install_http_timeouts()
    finally:
        requests.Session.request = original


# ---------- import 期不安装（防污染测试集）----------

@pytest.mark.parametrize("mod", ["app.tasks", "app.collectors.daily", "app.cli"])
def test_not_installed_on_import(mod):
    """在**干净子进程**里 import 目标模块 → 补丁必须仍未安装。

    本进程内测不了（其它用例可能装过补丁），故起子进程。这条防的是「有人在
    import 期装补丁」——那会静默改变整个测试集里所有 requests 调用的行为。
    """
    code = (
        f"import {mod}\n"
        "from app.providers.net import installed_timeouts\n"
        "import sys\n"
        "sys.exit(1 if installed_timeouts() is not None else 0)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    assert r.returncode == 0, f"import {mod} 后补丁被安装了（应只在 __main__ 装）:\n{r.stderr}"


# ---------- 故障注入（让验收 #4 可复现）----------

def test_fault_injection_once(monkeypatch):
    monkeypatch.setenv("DATAHUB_FAULT_INJECT_TIMEOUT_HOSTS", "example.invalid")
    monkeypatch.setenv("DATAHUB_FAULT_INJECT_ONCE", "1")
    monkeypatch.setattr("app.providers.net._injected", False)
    install_http_timeouts()
    try:
        from app.providers.net import _should_inject
        assert _should_inject("http://example.invalid/x") is True
        assert _should_inject("http://example.invalid/x") is False, "ONCE 应只注入一次"
        assert _should_inject("http://other.host/x") is False, "未命中主机不注入"
    finally:
        uninstall_http_timeouts()


def test_fault_injection_off_by_default(bare_requests):
    from app.providers.net import _should_inject
    assert _should_inject("http://example.invalid/x") is False


def test_fault_injection_raises_readtimeout(monkeypatch):
    monkeypatch.setenv("DATAHUB_FAULT_INJECT_TIMEOUT_HOSTS", "example.invalid")
    install_http_timeouts()
    try:
        with pytest.raises(requests.exceptions.ReadTimeout):
            requests.get("http://example.invalid/")
    finally:
        uninstall_http_timeouts()


# ---------- 统一超时判定 ----------

def test_is_timeout_recognizes_both_families():
    """requests 的 ReadTimeout 是 OSError **不是** 内置 TimeoutError —— 两族都要认，
    否则降级日志里「超时」会退化成原始异常串（09-08 事故取证就吃过这个亏）"""
    assert is_timeout(requests.exceptions.ReadTimeout("x")) is True
    assert is_timeout(requests.exceptions.ConnectTimeout("x")) is True
    assert is_timeout(TimeoutError("x")) is True
    assert is_timeout(ValueError("x")) is False
    assert is_timeout(OSError("x")) is False


def test_readtimeout_is_not_builtin_timeouterror():
    """把上条判定的**理由**钉住：哪天 requests 改继承，这条会红，提示可简化 is_timeout"""
    assert not isinstance(requests.exceptions.ReadTimeout("x"), TimeoutError)
    assert isinstance(requests.exceptions.ReadTimeout("x"), OSError)


# ---------- akshare 传输层审计守卫 ----------

# 本库实际调用的 akshare 函数（app/ 下 `ak.<name>` 的全部取值）。
# 补丁只覆盖走 requests 的那些；上游改用 curl_cffi 则补丁静默失效。
AK_TARGETS = [
    "stock_zh_a_daily", "stock_zh_a_hist", "stock_zh_index_daily",
    "stock_zh_index_spot_sina", "stock_zh_index_spot_em",
    "stock_info_sh_name_code", "stock_info_sz_name_code", "stock_info_bj_name_code",
    "stock_info_a_code_name", "stock_board_industry_name_em",
    "stock_board_industry_summary_ths", "stock_fund_flow_industry",
    "stock_zt_pool_em", "stock_hot_rank_em", "stock_zcfz_em", "stock_yjbb_em",
    "stock_value_em", "tool_trade_date_hist_sina",
    "index_us_stock_sina", "index_stock_cons_csindex",
]


def _transitive_source(fn) -> str:
    """函数自身 + 其 globals 里同属 akshare 的辅助函数（如 utils.func 的请求助手）的源码。
    只看函数体本身会漏掉「函数薄封装 + 助手出网」的函数（如 stock_board_industry_name_em）。

    起点先 `inspect.unwrap`：部分 akshare 函数带 `@lru_cache`，此时 `__globals__` 是
    functools 的（看不到任何 akshare 助手），必须剥掉包装再走。
    """
    seen, chunks = set(), []

    def walk(o, depth):
        if depth < 0 or id(o) in seen:
            return
        seen.add(id(o))
        try:
            chunks.append(inspect.getsource(o))
        except (OSError, TypeError):
            pass
        for v in getattr(o, "__globals__", {}).values():
            if isinstance(v, types.FunctionType):
                try:
                    v = inspect.unwrap(v)
                except ValueError:
                    continue
                # functools 包装器剥后可能已不是 akshare 函数（如内建），跳过
                if getattr(v, "__module__", "").startswith("akshare"):
                    walk(v, depth - 1)

    walk(inspect.unwrap(fn), 4)
    return "\n".join(chunks)


def test_akshare_targets_go_through_requests():
    """审计守卫：本库调用的 akshare 函数必须经 requests 出网（补丁才覆盖得到）。

    红了的含义：akshare 上游把某函数迁到 curl_cffi（该类请求**绕过**本补丁），
    需要为它单独加超时（或在 with_timeout 之外再包一层），否则超时重新失控。
    """
    ak = pytest.importorskip("akshare")
    offenders = []
    for name in AK_TARGETS:
        fn = getattr(ak, name, None)
        if fn is None:
            offenders.append(f"{name}（函数已不存在，需更新审计名单）")
            continue
        src = _transitive_source(fn)
        if "curl_cffi" in src:
            offenders.append(f"{name}（已迁到 curl_cffi，补丁失效）")
        elif "requests" not in src:
            offenders.append(f"{name}（未见 requests，传输层需人工确认）")
    assert not offenders, "akshare 传输层漂移：\n  " + "\n  ".join(offenders)
