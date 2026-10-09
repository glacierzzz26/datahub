"""数据集契约的代码形态（Phase 1 §3）。

`DatasetSpec` 由 `datasets/registry.py` 机械推导出：
- HTTP 的 query 参数与响应列；
- MCP tool 的 inputSchema 与返回字段；
- 契约测试断言。

**出口层零业务逻辑** —— 这是「MCP 薄门面」的落地保证。
"""
from dataclasses import dataclass
from typing import Literal

ParamType = Literal["str", "int", "date", "code", "enum", "bool"]
# kind: external = 抓外部源 + 缓存（Phase 1 全部）；raw = 读自有库（Phase 2）
DatasetKind = Literal["external", "raw"]


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: ParamType
    required: bool = False
    default: object = None
    enum: tuple = ()
    desc: str = ""


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str          # str / int / float / bool / date
    unit: str = ""
    desc: str = ""


@dataclass(frozen=True)
class DatasetSpec:
    id: str
    title: str
    kind: DatasetKind
    params: tuple[ParamSpec, ...]
    columns: tuple[ColumnSpec, ...]
    ttl_seconds: int | None          # None = 不缓存
    paginated: bool = False
    source: str = ""                 # 源链键（默认 provider 名）
    desc: str = ""

    @property
    def param_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.params)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def to_dict(self) -> dict:
        """元数据形态（HTTP /v1/datasets 与 MCP list_datasets 共用）。"""
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "ttl_seconds": self.ttl_seconds,
            "paginated": self.paginated,
            "desc": self.desc,
            "params": [
                {"name": p.name, "type": p.type, "required": p.required,
                 "default": p.default, "enum": list(p.enum), "desc": p.desc}
                for p in self.params
            ],
            "columns": [
                {"name": c.name, "type": c.type, "unit": c.unit, "desc": c.desc}
                for c in self.columns
            ],
        }
