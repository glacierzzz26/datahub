"""MCP 门面（FastMCP，Streamable HTTP，Phase 1 蓝图 §7）。

每个 `DatasetSpec` → 一个 tool + `list_datasets`。tool 的 inputSchema 由 params
机械生成、返回字段即数据行列 —— **出口层零业务逻辑**，一律委托 service.get_dataset。

MCP SDK 锁 `mcp<2`（2.x 已把 FastMCP 改名为 MCPServer）。streamable_http_path="/"
是为了让本 app 挂到 FastAPI 的 `/mcp` 后，对外地址恰为 `/mcp`（内层 route 为 "/"）。
"""
import inspect
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app import config, service
from app.datasets import registry
from app.datasets.spec import DatasetSpec

logger = logging.getLogger(__name__)

# ParamSpec.type → Python 注解（决定 MCP inputSchema 的类型）
_PY_TYPE = {
    "str": str, "int": int, "float": float, "bool": bool,
    "date": str, "code": str, "enum": str,
}


def tool_name(dataset_id: str) -> str:
    """dataset id → tool 名（点号换下划线，如 hotspot.indices → hotspot_indices）"""
    return dataset_id.replace(".", "_")


def _make_tool(spec: DatasetSpec):
    """由 DatasetSpec 造一个 MCP tool：签名来自 params，返回 {dataset, rows, meta}。"""

    def tool(**kwargs):
        rows, meta = service.get_dataset(spec.id, kwargs)
        return {"dataset": spec.id, "rows": rows, "meta": meta}

    tool.__name__ = tool_name(spec.id)
    tool.__doc__ = spec.desc or spec.title

    params = []
    for p in spec.params:
        default = inspect.Parameter.empty if p.required else p.default
        params.append(inspect.Parameter(
            p.name, inspect.Parameter.KEYWORD_ONLY,
            default=default, annotation=_PY_TYPE.get(p.type, str)))
    tool.__signature__ = inspect.Signature(params)
    return tool


def _list_datasets_tool() -> list[dict]:
    """列出所有数据集（id/title/params/columns/单位/ttl）。"""
    return [s.to_dict() for s in registry.all_datasets()]


def build_mcp() -> FastMCP:
    """装配 FastMCP：list_datasets + 每数据集一个 tool。"""
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(config.MCP_ALLOWED_HOSTS),
    )
    mcp = FastMCP("datahub", stateless_http=True, streamable_http_path="/",
                  transport_security=security)
    mcp.add_tool(_list_datasets_tool, name="list_datasets",
                 description="列出所有数据集（id/title/params/columns/单位/ttl）")
    for spec in registry.all_datasets():
        try:
            mcp.add_tool(_make_tool(spec), name=tool_name(spec.id), description=spec.title)
        except Exception as e:  # noqa: BLE001
            logger.warning("注册 MCP tool %s 失败: %s", spec.id, e)
    return mcp
