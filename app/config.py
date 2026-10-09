"""datahub 配置（环境变量，覆盖默认值；DATAHUB_* 前缀）。

参照 collector/app/config.py 的风格：
- 纯函数式读取，import 期求值一次；
- 「双闸门」范式（总开关 × 白名单）默认全关 → 部署本代码不改变生产行为。
"""
import os


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    """逗号列表 env → 去空字符串列表"""
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


# ---------- 服务 ----------
PORT = _int("DATAHUB_PORT", 8100)
LOG_LEVEL = _str("DATAHUB_LOG_LEVEL", "info")
# Bearer token（属密钥）：空 = 未配置 → 鉴权层一律拒绝（不放行空口令）。
TOKEN = _str("DATAHUB_TOKEN", "")

# MCP Streamable HTTP 的 DNS-rebinding 允许 Host 列表（默认本机）。
# 跨机暴露时按部署拓扑加 Host（如 LAN IP/域名）；此即防 rebinding 的白名单。
MCP_ALLOWED_HOSTS = _list("DATAHUB_MCP_ALLOWED_HOSTS", "localhost,127.0.0.1")

# ---------- 请求层超时（沿用 collector 口径）----------
HTTP_CONNECT_TIMEOUT = _float("DATAHUB_HTTP_CONNECT_TIMEOUT", 5)
HTTP_READ_TIMEOUT = _float("DATAHUB_HTTP_READ_TIMEOUT", 15)
# with_timeout 兜底网超时（口径不变式：HTTP_READ_TIMEOUT ≤ REQUEST_TIMEOUT）
REQUEST_TIMEOUT = _int("DATAHUB_REQUEST_TIMEOUT", 15)
# 杀开关：置 0 硬旁路请求层补丁（当日回滚，无需重建）。默认开。
HTTP_TIMEOUT_PATCH = _bool("DATAHUB_HTTP_TIMEOUT_PATCH", True)

# ---------- 缓存 / 限流 ----------
CACHE_TTL_DEFAULT = _int("DATAHUB_CACHE_TTL_DEFAULT", 300)
# provider 最小请求间隔（秒）与并发上限
RATE_LIMIT = _float("DATAHUB_RATE_LIMIT", 0.2)
RATE_CONCURRENCY = _int("DATAHUB_RATE_CONCURRENCY", 4)
# 源被限（403/429/封禁）后的进程内冷却（秒）——冷却期内不再发请求
BLOCK_COOLDOWN = _int("DATAHUB_BLOCK_COOLDOWN", 1800)

# ---------- 外部源采集闸门（默认全关 → 部署零外部请求）----------
# 镜像 collector 的 baostock_enabled/tencent_enabled 双闸门：总开关 × 数据集白名单，
# 两者都不点名 → 一条外部请求都不发 → 部署本代码本身不改生产路径。
EXT_ENABLED = _bool("DATAHUB_EXT_ENABLED", False)
EXT_DATASETS = _list("DATAHUB_EXT_DATASETS", "")


def ext_enabled(dataset_id: str) -> bool:
    """dataset 的外部采集是否放闸（总开关 × 白名单）。

    未启用总开关或 dataset 不在白名单 → False → 该数据集返回 503，不发外部请求。
    """
    if not EXT_ENABLED:
        return False
    return dataset_id in EXT_DATASETS


def token_configured() -> bool:
    """是否已配置 token（未配置时鉴权层一律 401，且启动期告警）"""
    return bool(TOKEN)


# ---------- 热点数据集参数默认值 ----------
# 隔夜外盘指数代码（新浪格式）；A股指数固定取 上证/深成/创业板
HOTSPOT_INDICES = _str("DATAHUB_HOTSPOT_INDICES", ".DJI,.IXIC,.INX")
HOTSPOT_TOP_N = _int("DATAHUB_HOTSPOT_TOP_N", 10)


def hotspot_index_list() -> list[str]:
    return [c.strip() for c in HOTSPOT_INDICES.split(",") if c.strip()]

