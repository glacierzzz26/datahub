"""采集子系统配置（环境变量，覆盖默认值）。

**搬自 steady `collector/app/config.py`（逐字，env 名原样保留 `COLLECTOR_*` /
`BAOSTOCK_*` / `TENCENT_*` / `COLLECT_SCOPE` / `DAILY_SOURCE_CHAIN` …）。**

为何独立成模块（而非并入 `app/config.py`）：datahub 的 `app/config.py` 是**服务**命名空间
（`DATAHUB_*`：端口/鉴权/ext 闸门/HTTP/MCP），其中已有 `HOTSPOT_TOP_N`（来自
`DATAHUB_HOTSPOT_TOP_N`，**服务参数默认**）；而本模块 `HOTSPOT_TOP_N`（来自
`COLLECTOR_HOTSPOT_TOP_N`，**采集旋钮**）语义不同——合并会静默遮蔽。独立模块还使
operator 的 steady `.env` 中 BAOSTOCK/TENCENT 值原样映射，watchdog 的
`COLLECTOR_JOB_LIMIT_*` 覆盖继续生效。

新增：**采集总闸门**（Phase 2）`DATAHUB_COLLECT_ENABLED × DATAHUB_COLLECT_DATASETS`，
默认全关 —— 部署本代码不发任何采集请求、不写任何行（与 `app/config.py` 的 `ext_enabled` 对称）。
"""
import os


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _list(name: str, default: str) -> list[str]:
    """逗号列表 env → 去空字符串列表"""
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# 请求限速（秒/只）：回填与每日同步共用，避免触发 AkShare 限速
RATE_LIMIT_SECONDS = _int("COLLECTOR_RATE_LIMIT", 3)

# 回填批次大小（只）
BACKFILL_BATCH_SIZE = _int("COLLECTOR_BATCH_SIZE", 50)

# 回填历史区间（默认近 10 年）
BACKFILL_START = _str("COLLECTOR_BACKFILL_START", "20160801")
BACKFILL_END = _str("COLLECTOR_BACKFILL_END", "")

# 财务回填报告期数（默认 20 个季度 ≈ 5 年）
BACKFILL_FINANCE_QUARTERS = _int("COLLECTOR_FINANCE_QUARTERS", 20)

# 每日增量财务同步的报告期数（默认最近 4 个季度，覆盖财报季尾部披露）
FINANCE_SYNC_QUARTERS = _int("COLLECTOR_FINANCE_QUARTERS_SYNC", 4)

# 每日增量同步的指数（上证指数 / 沪深300 / 中证500，作行情概览与收益基准；
# sz399106 深证综指 = 深市全市场成交额，G6 两市成交 = sh000001 + sz399106）
INDEX_CODES = _str("COLLECTOR_INDEX_CODES", "sh000001,sh000300,sh000905,sz399106")

# 每日增量同步：数据库无历史记录时的回退窗口（天）
DAILY_FALLBACK_DAYS = _int("COLLECTOR_DAILY_FALLBACK_DAYS", 30)

# 每日增量同步的股票间间隔（秒，回填用 RATE_LIMIT_SECONDS）
DAILY_SYNC_INTERVAL = _int("COLLECTOR_DAILY_INTERVAL", 1)

# 数据源请求超时（秒）：AkShare 底层 requests 无 timeout，遇到半开连接会永久挂起
# （曾卡死同步），这里统一兜底；超时抛异常走降级/重试，而非无限等待。
#
# ⚠️ 这是 with_timeout **兜底网** 的超时（Issue #14 起 with_timeout 已去死锁）；
# 真正的请求层根因修法是 providers/net.py 给 requests 注入 socket 超时。
# 不变式：HTTP_READ_TIMEOUT ≤ REQUEST_TIMEOUT —— L1 先触发，L2 才是最后一道。
REQUEST_TIMEOUT = _int("COLLECTOR_REQUEST_TIMEOUT", 15)

# ---------- 请求层超时补丁（Issue #14）----------
# AkShare 底层 requests 无有效 timeout：东财 stock_zh_a_hist 是 timeout=None 转发
# （等效无限），新浪腿 stock_zh_a_daily 更是**裸 requests.get 完全无 timeout**。
# ⚠️ socket.setdefaulttimeout 对它无效（urllib3 内部会显式 settimeout(None) 覆盖），
# 故只能在 requests.Session.request 层注入（见 providers/net.py）。
# connect/read 拆开：连接超时宜短（DNS/TCP 快失败），读超时给响应体留时间。
HTTP_CONNECT_TIMEOUT = _float("COLLECTOR_HTTP_CONNECT_TIMEOUT", 5)
HTTP_READ_TIMEOUT = _float("COLLECTOR_HTTP_READ_TIMEOUT", 15)
# 杀开关：置 0 硬旁路补丁（当日回滚，无需重建）。默认开。
HTTP_TIMEOUT_PATCH = _bool("COLLECTOR_HTTP_TIMEOUT_PATCH", True)
# 故障注入（验收用）：URL 命中任意子串 → 抛一次 ReadTimeout，验证降级链。
# 默认空 = 关；COLLECTOR_FAULT_INJECT_ONCE=1 时每进程只注入一次。
FAULT_INJECT_TIMEOUT_HOSTS = _list("COLLECTOR_FAULT_INJECT_TIMEOUT_HOSTS", "")
FAULT_INJECT_ONCE = _bool("COLLECTOR_FAULT_INJECT_ONCE", False)

# BaoStock 开关（阶段 3：prod 已全源翻 BaoStock，Tushare 依赖已移除）
BAOSTOCK_ENABLED = _bool("BAOSTOCK_ENABLED", False)
# BaoStock 参与的源链 scope（阶段 4 起语义从"主源"改为"源链中包含 BaoStock"，逗号列表）。
# - daily/valuation/index：链序 = AkShare 主源 → BaoStock 兜底（阶段 4 主源切换）
# - calendar/finance/stock_basic：链序 = BaoStock 主源 → AkShare 降级（阶段 3 维持）
# 默认 daily,calendar 供无 env 的本地/测试路径，代码上线本身不改变生产数据路径。
BAOSTOCK_SOURCES = _list("BAOSTOCK_SOURCES", "daily,calendar")
# BaoStock 单次 socket 超时（秒）：登录/查询共用，防库内阻塞 connect/recv 挂死
BAOSTOCK_TIMEOUT = _int("BAOSTOCK_TIMEOUT", 60)
# 连接级失败的重试次数与间隔（秒）
BAOSTOCK_RETRIES = _int("BAOSTOCK_RETRIES", 1)
BAOSTOCK_RETRY_DELAY = _int("BAOSTOCK_RETRY_DELAY", 2)
# 黑名单冷却（秒）：登录命中 10001011 后进程内跳过 BaoStock 该时长，防逐股反复登录
# 把单源故障拖成整链超时（08-28 事故教训）。默认 30 分钟，超时自愈再试一次。
BAOSTOCK_BAN_COOLDOWN = _int("BAOSTOCK_BAN_COOLDOWN", 1800)


def baostock_enabled(scope: str | None = None) -> bool:
    """scope 的源链中是否包含 BaoStock（env BAOSTOCK_ENABLED × BAOSTOCK_SOURCES 控制）

    主源顺序由各采集器代码决定：daily/valuation/index 主源 AkShare、BaoStock 兜底；
    calendar/stock_basic/finance 主源 BaoStock、AkShare 降级。
    未启用 BAOSTOCK_ENABLED 或 scope 不在 BAOSTOCK_SOURCES → False（链内无 BaoStock）。
    scope 取值 daily/calendar/valuation/finance/index/stock_basic；无参保留全局语义。
    """
    if not BAOSTOCK_ENABLED:
        return False
    if scope is None:
        return bool(BAOSTOCK_SOURCES)
    return scope in BAOSTOCK_SOURCES


# ---------- 腾讯行情源（Issue #15）----------
# 保险丝（镜像 baostock_enabled）：TENCENT_ENABLED × TENCENT_SOURCES 双闸门，
# 两者都不点名腾讯 → 一条腾讯请求都不发 → 部署代码本身不改生产路径。
TENCENT_ENABLED = _bool("TENCENT_ENABLED", False)
TENCENT_SOURCES = _list("TENCENT_SOURCES", "")
# 逐只源链顺序（profile 名，左→右为降级方向）；默认 akshare = 现状
DAILY_SOURCE_CHAIN = _list("DAILY_SOURCE_CHAIN", "akshare")
# 18:10 当日同步是否走腾讯批量快照（qt.gtimg.cn，全池 ~17s）
TENCENT_SNAPSHOT = _bool("TENCENT_SNAPSHOT", False)
TENCENT_RATE_LIMIT = _float("TENCENT_RATE_LIMIT", 0.2)
TENCENT_TIMEOUT = _int("TENCENT_TIMEOUT", 10)
TENCENT_BATCH_SIZE = _int("TENCENT_BATCH_SIZE", 50)
# 快照除权探测阈值：|快照昨收/库内最近收盘 − 1| 超此值 → 疑似除权，回退逐只链
TENCENT_DIV_TOL = _float("TENCENT_DIV_TOL", 0.005)


def tencent_enabled(scope: str | None = None) -> bool:
    """scope 的源链中是否包含腾讯（TENCENT_ENABLED × TENCENT_SOURCES 控制）

    无参保留全局语义（任一 scope 生效即 True）。未启用或 scope 不在
    TENCENT_SOURCES → False（链内无腾讯）。
    """
    if not TENCENT_ENABLED:
        return False
    if scope is None:
        return bool(TENCENT_SOURCES)
    return scope in TENCENT_SOURCES


def daily_source_chain() -> list[str]:
    """逐只日行情源链（profile 名，左→右为降级方向）；默认为 ['akshare']"""
    return DAILY_SOURCE_CHAIN or ["akshare"]


def tencent_snapshot_enabled() -> bool:
    """18:10 当日同步是否走腾讯批量快照（需保险丝与 scope 同时点名 daily）"""
    return TENCENT_SNAPSHOT and tencent_enabled("daily")


# ---------- 采集范围（Issue #13）----------
# 采集范围闸门：pool（默认，= 现状 800 只，按 universe 取）或 a_share
# （全量，按 data_scope='a_share'）。默认 pool → 部署本批代码生产行为零变化；
# 翻 a_share 才扩采集范围。**策略选股域（universe）不受此闸门影响** —— 拆列的要点。
COLLECT_SCOPE = _str("COLLECT_SCOPE", "pool").strip().lower()


def collect_scope() -> str:
    """当前采集范围：'pool'（默认）或 'a_share'（未知值回退 pool，防呆）"""
    return COLLECT_SCOPE if COLLECT_SCOPE in ("pool", "a_share") else "pool"


# 快照首日扩池时全部无历史 → 全部 deferred → 逐只补把收益归零。上限内的
# deferred 交夜间回填处理，超上限才告警（Issue #13 R5）。
TENCENT_DEFER_MAX = _int("TENCENT_DEFER_MAX", 300)

# 热点采集（早盘简报数据源，Issue #4）：每日早晨采集一次
HOTSPOT_TOP_N = _int("COLLECTOR_HOTSPOT_TOP_N", 10)          # 板块/人气榜取 TOP N
HOTSPOT_INDICES = _str("COLLECTOR_HOTSPOT_INDICES", ".DJI,.IXIC,.INX")  # 隔夜外盘代码


def index_code_list() -> list[str]:
    return [c.strip() for c in INDEX_CODES.split(",") if c.strip()]


def hotspot_index_list() -> list[str]:
    return [c.strip() for c in HOTSPOT_INDICES.split(",") if c.strip()]


# ---------- 采集总闸门（Phase 2）----------
# 镜像 app/config.py 的 ext 双闸门：总开关 × 数据集白名单，**默认全关** →
# 部署本代码本身**零采集行为变更**（不注册采集任务、不发采集请求、不写任何行）。
# 数据集名（与 tasks.py 的 _JOB_DATASET 一致）：
#   stock_basic / calendar / index / daily / valuation / finance
COLLECT_ENABLED = _bool("DATAHUB_COLLECT_ENABLED", False)
COLLECT_DATASETS = _list("DATAHUB_COLLECT_DATASETS", "")


def collect_enabled(dataset: str) -> bool:
    """数据集是否放闸采集（总开关 × 白名单）。

    未启用总开关或 dataset 不在白名单 → False → 对应采集任务不注册、不执行。
    """
    if not COLLECT_ENABLED:
        return False
    return dataset in COLLECT_DATASETS
