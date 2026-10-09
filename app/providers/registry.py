"""Provider 注册表：name → 模块（含 `fetch(dataset_id, params)`）。

- `akshare_hotspot` / `akshare_industry`：external 数据集（抓外部源）。
- `db`：raw 数据集（读 datahub 自有库，不发外部请求）。
"""
from app.providers import db
from app.providers.ext import akshare_hotspot, akshare_industry

PROVIDERS: dict[str, object] = {
    akshare_hotspot.name: akshare_hotspot,
    akshare_industry.name: akshare_industry,
    db.name: db,
}


def get_provider(name: str):
    return PROVIDERS.get(name)
