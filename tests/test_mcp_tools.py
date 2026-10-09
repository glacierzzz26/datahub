"""MCP 门面用例：tool 清单、inputSchema 由 params 生成、返回形状、e2e initialize。"""
import asyncio

from fastapi.testclient import TestClient

from app.datasets import registry
from app.mcp_facade import _make_tool, build_mcp, tool_name
from app.server import build_app


def _tools():
    return {t.name: t for t in asyncio.run(build_mcp().list_tools())}


def test_tool_list_matches_datasets():
    names = set(_tools())
    expected = {"list_datasets"} | {tool_name(s.id) for s in registry.all_datasets()}
    assert names == expected


def test_input_schema_generated_from_params():
    props = _tools()["hotspot_hot_stocks"].inputSchema.get("properties", {})
    assert "top" in props
    assert props["top"].get("default") == 10


def test_input_schema_marks_required_param():
    schema = _tools()["industry_members"].inputSchema
    assert "industry" in schema.get("properties", {})
    assert "industry" in schema.get("required", [])


def test_tool_dispatch_returns_shape(monkeypatch, enable_all_ext, no_retry):
    import types

    from app.providers import registry as prov_reg

    monkeypatch.setattr(
        prov_reg, "get_provider",
        lambda name: types.SimpleNamespace(
            fetch=lambda dataset_id, params: [{"name": "X", "code": "1",
                                               "close": 1.0, "change_pct": 1.0}]))
    tool = _make_tool(registry.get_spec("hotspot.indices"))
    out = tool()
    assert out["dataset"] == "hotspot.indices"
    assert out["rows"][0]["name"] == "X"
    assert out["meta"]["stale"] is False


def test_mcp_initialize_over_http():
    """端到端：MCP Streamable HTTP 挂在 /mcp，能完成 initialize 握手。"""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}},
    }
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    with TestClient(build_app()) as client:
        r = client.post("/mcp", json=payload, headers=headers)
        assert r.status_code == 200
        body = r.text
        assert "datahub" in body            # serverInfo 名


def test_mcp_initialize_allows_host_with_port():
    """回归：真实客户端 Host 头**带端口**（http://127.0.0.1:8100/mcp）——
    白名单须以 'host:*' 通配匹配，否则一律 421（DNS-rebinding 误杀全客户端）。
    曾因默认白名单无 ':*'，本地翻闸冒烟时 MCP 客户端全线 421。"""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}},
    }
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    # base_url 带端口 → TestClient 发出 Host: testserver:8100
    with TestClient(build_app(), base_url="http://testserver:8100") as client:
        r = client.post("/mcp", json=payload, headers=headers)
        assert r.status_code == 200, f"带端口 Host 应放行，实得 {r.status_code}"


def test_default_allowed_hosts_covers_ports():
    """出厂默认必须含 ':*' 通配——否则部署后所有带端口的真实 MCP 客户端 421。"""
    from app.config import _DEFAULT_MCP_ALLOWED_HOSTS

    parts = [p.strip() for p in _DEFAULT_MCP_ALLOWED_HOSTS.split(",")]
    assert "127.0.0.1:*" in parts
    assert "localhost:*" in parts
