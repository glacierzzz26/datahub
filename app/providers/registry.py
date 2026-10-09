"""Provider 注册表：name → 模块（含 `fetch(dataset_id, params)`）。"""
from app.providers.ext import akshare_hotspot, akshare_industry

PROVIDERS: dict[str, object] = {
    akshare_hotspot.name: akshare_hotspot,
    akshare_industry.name: akshare_industry,
}


def get_provider(name: str):
    return PROVIDERS.get(name)
