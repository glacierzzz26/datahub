"""Phase 2/3 数据集定义（raw：读 datahub 自有库，`source="db"`）。

列名/参数**照 `steady/docs/phase2/design/数据接入层-datahub.md` §2.2 冻结**
（`trade_calendar`：params `start`,`end`,`is_open`；列 `cal_date,is_open,exchange`；
`stock_basic`：params `codes/market/industry/universe/scope/keyword/sort/order/limit/offset`；
列 `code,name,market,industry,list_date,status,universe,data_scope`）。

与 external 数据集的差别（服务层 `service.get_dataset` 按 `kind` 分派）：
- raw **不受** `DATAHUB_EXT_*` 外部采集闸门约束（读自有库，不发外部请求）；
- raw **不缓存**（`ttl_seconds=None`）——本地库权威且廉价，无 stale 兜底语义；
- raw **空结果是合法值**（库内确无 = 尚未采集），**不**转 503（对齐蓝图 §5 反例）。
"""
from app.datasets.spec import ColumnSpec, DatasetSpec, ParamSpec

TRADE_CALENDAR = DatasetSpec(
    id="trade_calendar",
    title="交易日历",
    kind="raw",
    params=(
        ParamSpec(name="start", type="date", desc="起始日（含），YYYY-MM-DD"),
        ParamSpec(name="end", type="date", desc="截止日（含），YYYY-MM-DD"),
        ParamSpec(name="is_open", type="bool", default=True,
                  desc="是否只返回交易日（默认 true；传 false 返回全部日历日）"),
    ),
    columns=(
        ColumnSpec("cal_date", "date", desc="日历日"),
        ColumnSpec("is_open", "bool", desc="是否交易日"),
        ColumnSpec("exchange", "str", desc="交易所（SSE/SZSE）"),
    ),
    ttl_seconds=None,          # 本地库读取：不缓存
    source="db",
    desc="读自有库 trade_calendar（采集落库结果）；本地权威，不缓存。",
)

# stock_basic：股票列表 + 采集域/选股域标记（Phase 3 逐集全迁移首个数据集）
STOCK_BASIC = DatasetSpec(
    id="stock_basic",
    title="股票基本信息",
    kind="raw",
    params=(
        ParamSpec(name="codes", type="str",
                  desc="代码白名单，逗号分隔（如 000001,600000）；空=不限"),
        ParamSpec(name="market", type="str",
                  desc="市场白名单，逗号分隔（SH/SZ/BJ）；空=不限"),
        ParamSpec(name="industry", type="str", desc="行业精确匹配"),
        ParamSpec(name="universe", type="str",
                  desc="策略选股域，逗号分隔（hs300/zz500）；空=不限"),
        ParamSpec(name="scope", type="str",
                  desc="采集域，逗号分隔（a_share）；空=不限"),
        ParamSpec(name="keyword", type="str", desc="代码/名称模糊匹配（不区分大小写）"),
        ParamSpec(name="sort", type="enum",
                  enum=("code", "name", "list_date", "market", "industry"),
                  default="code", desc="排序键（非法值→code）"),
        ParamSpec(name="order", type="enum", enum=("asc", "desc"),
                  default="asc", desc="排序方向"),
        ParamSpec(name="limit", type="int", desc="最多返回行数；空=不限"),
        ParamSpec(name="offset", type="int", desc="跳过行数（分页用）"),
    ),
    columns=(
        ColumnSpec("code", "str", desc="股票代码（6 位）"),
        ColumnSpec("name", "str", desc="名称"),
        ColumnSpec("market", "str", desc="市场：SH/SZ/BJ"),
        ColumnSpec("industry", "str", desc="行业（证监会/东财分类）"),
        ColumnSpec("list_date", "date", desc="上市日期"),
        ColumnSpec("status", "str", desc="状态：L=上市 / D=退市"),
        ColumnSpec("universe", "str", desc="策略选股域：hs300/zz500（空=全市场）"),
        ColumnSpec("data_scope", "str", desc="采集域：a_share / 空（与 universe 正交）"),
    ),
    ttl_seconds=None,          # 本地库读取：不缓存
    source="db",
    desc="读自有库 stock_basic（采集落库结果）；本地权威，不缓存。",
)

RAW_DATASETS: tuple[DatasetSpec, ...] = (
    TRADE_CALENDAR,
    STOCK_BASIC,
)
