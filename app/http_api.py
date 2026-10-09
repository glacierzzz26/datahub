"""原生 HTTP 出口（真实契约，Phase 1 蓝图 §7）。

- `GET /v1/healthz`：免鉴权（探针用）
- `GET /v1/datasets`：数据集元数据清单（id/title/params/columns/单位/ttl）
- `GET /v1/datasets/{id}`：query 传参；信封 {code,message,data,meta}

出口层**零业务逻辑**：一律委托 service.get_dataset。
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from app import __version__, service
from app.auth import require_token
from app.datasets import registry

router = APIRouter()


def envelope(data, message: str = "ok", code: int = 0, meta: dict | None = None) -> dict:
    body = {"code": code, "message": message, "data": data}
    if meta is not None:
        body["meta"] = meta
    return body


@router.get("/v1/healthz")
def healthz() -> dict:
    """探针：免鉴权、不触外部源。"""
    return envelope({"status": "ok", "version": __version__})


@router.get("/v1/datasets", dependencies=[Depends(require_token)])
def list_datasets() -> dict:
    return envelope([s.to_dict() for s in registry.all_datasets()])


@router.get("/v1/datasets/{dataset_id}", dependencies=[Depends(require_token)])
def get_dataset(dataset_id: str, request: Request) -> dict:
    params = dict(request.query_params)
    try:
        data, meta = service.get_dataset(dataset_id, params)
    except service.UnknownDataset:
        raise HTTPException(status_code=404, detail=f"未知数据集: {dataset_id}") from None
    except service.BadParams as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except service.DatasetUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from None
    return envelope(data, meta=meta)
