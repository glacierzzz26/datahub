"""Phase 1 数据集定义（external：抓外部源 + 缓存）。

列名/单位**照 collector/app/collectors/hotspot.py 的实际输出逐字冻结**
（见 `_fetch_indices` / `_sectors_gain_from` / `_sectors_flow_from` /
`_fetch_hot_rank` / `_fetch_zt_pool`），不凭记忆。

Phase 1 TTL 统一 300s（热点）/ 86400s（行业目录/成分，日内稳定）。
"""
from app.datasets.spec import ColumnSpec, DatasetSpec, ParamSpec

_TOP = ParamSpec(name="top", type="int", required=False, default=10,
                 desc="取前 N 条")

# hotspots ------------------------------------------------------------------

HOTSPOT_INDICES = DatasetSpec(
    id="hotspot.indices",
    title="指数快照（隔夜外盘 + A股重要指数）",
    kind="external",
    params=(),
    columns=(
        ColumnSpec("name", "str", desc="指数名（道琼斯/上证指数…）"),
        ColumnSpec("code", "str", desc="源代码（.DJI / sh000001…）"),
        ColumnSpec("close", "float", unit="点", desc="最新价"),
        ColumnSpec("change_pct", "float", unit="%", desc="涨跌幅"),
    ),
    ttl_seconds=300,
    source="akshare_hotspot",
    desc="隔夜外盘末收盘 + A股指数当日实时（东财主源/新浪兜底）。",
)

HOTSPOT_SECTORS_GAIN = DatasetSpec(
    id="hotspot.sectors_gain",
    title="行业板块涨幅榜",
    kind="external",
    params=(_TOP,),
    columns=(
        ColumnSpec("name", "str", desc="板块名"),
        ColumnSpec("change_pct", "float", unit="%", desc="涨跌幅"),
        ColumnSpec("leader", "str", desc="领涨股"),
    ),
    ttl_seconds=300,
    source="akshare_hotspot",
    desc="同花顺板块概览主源，东财行业板块兜底。",
)

HOTSPOT_SECTORS_FLOW = DatasetSpec(
    id="hotspot.sectors_flow",
    title="行业板块资金净流入榜",
    kind="external",
    params=(_TOP,),
    columns=(
        ColumnSpec("name", "str", desc="板块名"),
        ColumnSpec("net_inflow", "str", unit="亿元" ,
                   desc="净流入（格式化字符串，如 '3.21亿'）"),
    ),
    ttl_seconds=300,
    source="akshare_hotspot",
    desc="同花顺板块概览主源，东财主力净流入/同花顺行业资金流兜底。",
)

HOTSPOT_HOT_STOCKS = DatasetSpec(
    id="hotspot.hot_stocks",
    title="活跃个股（人气榜 / 涨停池）",
    kind="external",
    params=(_TOP,),
    columns=(
        ColumnSpec("rank", "int", desc="排名"),
        ColumnSpec("code", "str", desc="股票代码"),
        ColumnSpec("name", "str", desc="股票简称"),
        ColumnSpec("change_pct", "float", unit="%", desc="涨跌幅"),
        ColumnSpec("board_days", "int", desc="连板数（涨停池兜底时有值）"),
        ColumnSpec("industry", "str", desc="所属行业（涨停池兜底时有值）"),
    ),
    ttl_seconds=300,
    source="akshare_hotspot",
    desc="人气榜主源，涨停池兜底（早盘语义=昨日涨停·今日关注）。",
)

# industry ------------------------------------------------------------------

INDUSTRY_CATALOG = DatasetSpec(
    id="industry.catalog",
    title="行业目录",
    kind="external",
    params=(),
    columns=(
        ColumnSpec("name", "str", desc="行业板块名"),
        ColumnSpec("code", "str", desc="板块代码"),
        ColumnSpec("change_pct", "float", unit="%", desc="涨跌幅"),
    ),
    ttl_seconds=86400,
    source="akshare_industry",
    desc="东财行业板块目录。",
)

INDUSTRY_MEMBERS = DatasetSpec(
    id="industry.members",
    title="行业成分股",
    kind="external",
    params=(ParamSpec(name="industry", type="str", required=True,
                      desc="行业板块名（取自 industry.catalog）"),),
    columns=(
        ColumnSpec("code", "str", desc="股票代码"),
        ColumnSpec("name", "str", desc="股票简称"),
    ),
    ttl_seconds=86400,
    source="akshare_industry",
    desc="东财行业板块成分股（首源，需实测可用性——见蓝图 §13.2）。",
)

# Phase 1 数据集全集（顺序即对外列出的顺序）
EXTERNAL_DATASETS: tuple[DatasetSpec, ...] = (
    HOTSPOT_INDICES,
    HOTSPOT_SECTORS_GAIN,
    HOTSPOT_SECTORS_FLOW,
    HOTSPOT_HOT_STOCKS,
    INDUSTRY_CATALOG,
    INDUSTRY_MEMBERS,
)
