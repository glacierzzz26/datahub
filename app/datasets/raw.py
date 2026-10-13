"""Phase 2/3 数据集定义（raw：读 datahub 自有库，`source="db"`）。

列名/参数**照 `steady/docs/phase2/design/数据接入层-datahub.md` §2.2 冻结**
（`trade_calendar`：params `start`,`end`,`is_open`；列 `cal_date,is_open,exchange`；
`stock_basic`：params `codes/market/industry/universe/scope/keyword/sort/order/limit/offset`；
列 `code,name,market,industry,list_date,status,universe,data_scope`）。
Phase 3 增补：`daily_price`（**含指数行**，无独立 index 表）、`daily_valuation`、
`financial_indicator`，以及三个**小数据集** `latest_trade_date` / `market_ready` / `dup_keys`。
Phase 4 增补：`latest_trade_date` 加 `before` 参数（严格 `<`，取「低于该日的最近交易日」），
新增小数据集 `daily_price_days`（区间内有行情的交易日 DISTINCT 列表，供
qe `data_quality` 的 prev 推导 / `_check_missing_days` 脱离本地冻结 `daily_price`）。

**`last` 参数**（每 code 最近 N 行，服务端 `ROW_NUMBER`）把 backend 的窗口/聚合类读点
（LAG / 21 根窗 / top-2 / 每码最新一条）变成**有界批量**，一次往返、无 N+1。

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

# daily_price：日行情（**含指数行**——code 形如 sh000300，无独立 index 表）。
# 供 qe 因子/绩效/回测/数据质量 + backend 行情/指数条。
DAILY_PRICE = DatasetSpec(
    id="daily_price",
    title="日行情（含指数行）",
    kind="raw",
    params=(
        ParamSpec(name="codes", type="str",
                  desc="代码白名单，逗号 IN（含指数 sh000300）；空=不限"),
        ParamSpec(name="start", type="date", desc="起始日 trade_date ≥（含）"),
        ParamSpec(name="end", type="date", desc="截止日 trade_date ≤（含）"),
        ParamSpec(name="date", type="date",
                  desc="精确单日（给定时优先于 start/end）"),
        ParamSpec(name="asof", type="date", desc="trade_date ≤ asof（取数锚）"),
        ParamSpec(name="last", type="int",
                  desc="每 code 取最近 N 行（服务端窗口；空=不限）"),
        ParamSpec(name="fields", type="str",
                  desc="列投影白名单，逗号；空=全列"),
        ParamSpec(name="adj_not_null", type="bool",
                  desc="仅返回 adj_factor 非空（最新因子锚点用）"),
    ),
    columns=(
        ColumnSpec("code", "str", desc="代码"),
        ColumnSpec("trade_date", "date", desc="交易日"),
        ColumnSpec("open", "float"), ColumnSpec("high", "float"),
        ColumnSpec("low", "float"), ColumnSpec("close", "float"),
        ColumnSpec("volume", "int", desc="成交量（手）"),
        ColumnSpec("amount", "float", desc="成交额（元）"),
        ColumnSpec("adj_factor", "float", desc="复权因子"),
        ColumnSpec("turnover_rate", "float", desc="换手率（%）"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库 daily_price（含指数行）；本地权威，不缓存。",
)

# daily_valuation：日度估值（PE/PB/市值）。供 qe 因子/展示/信号 + backend 估值。
DAILY_VALUATION = DatasetSpec(
    id="daily_valuation",
    title="日度估值",
    kind="raw",
    params=(
        ParamSpec(name="codes", type="str", desc="代码白名单，逗号 IN；空=不限"),
        ParamSpec(name="start", type="date", desc="起始日 trade_date ≥（含）"),
        ParamSpec(name="end", type="date", desc="截止日 trade_date ≤（含）"),
        ParamSpec(name="asof", type="date", desc="trade_date ≤ asof（取数锚）"),
        ParamSpec(name="last", type="int", desc="每 code 取最近 N 行；空=不限"),
        ParamSpec(name="fields", type="str", desc="列投影白名单，逗号；空=全列"),
    ),
    columns=(
        ColumnSpec("code", "str"),
        ColumnSpec("trade_date", "date"),
        ColumnSpec("close", "float", desc="收盘价（元）"),
        ColumnSpec("total_mv", "float", desc="总市值（元）"),
        ColumnSpec("float_mv", "float", desc="流通市值（元）"),
        ColumnSpec("pe_ttm", "float", desc="市盈率 TTM"),
        ColumnSpec("pe_static", "float", desc="市盈率（静态）"),
        ColumnSpec("pb", "float", desc="市净率"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库 daily_valuation；本地权威，不缓存。",
)

# financial_indicator：财务指标（announce_date 防未来函数）。供 qe 因子/展示 + backend 财务。
FINANCIAL_INDICATOR = DatasetSpec(
    id="financial_indicator",
    title="财务指标",
    kind="raw",
    params=(
        ParamSpec(name="codes", type="str", desc="代码白名单，逗号 IN；空=不限"),
        ParamSpec(name="as_of", type="date",
                  desc="仅返回 announce_date ≤ as_of（防未来函数）"),
        ParamSpec(name="last", type="int",
                  desc="每 code 取最近 N 行（announce_date DESC, report_date DESC）"),
        ParamSpec(name="fields", type="str", desc="列投影白名单，逗号；空=全列"),
    ),
    columns=(
        ColumnSpec("code", "str"),
        ColumnSpec("report_date", "date", desc="报告期"),
        ColumnSpec("announce_date", "date", desc="公告日（财报披露日）"),
        ColumnSpec("pe", "float"), ColumnSpec("pb", "float"),
        ColumnSpec("roe", "float", desc="净资产收益率"),
        ColumnSpec("profit_growth", "float", desc="净利润增速"),
        ColumnSpec("revenue_growth", "float", desc="营收增速"),
        ColumnSpec("debt_ratio", "float", desc="资产负债率"),
        ColumnSpec("gross_margin", "float", desc="毛利率"),
        ColumnSpec("net_margin", "float",
                   desc="销售净利率（datahub 侧表 financial_indicator_ext；未采集时 null）"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库 financial_indicator（left-join 侧表 net_margin）；本地权威，不缓存。",
)

# latest_trade_date：最新交易日（编排/就绪度）。避免客户端拉全表取 MAX。
LATEST_TRADE_DATE = DatasetSpec(
    id="latest_trade_date",
    title="最新交易日",
    kind="raw",
    params=(
        ParamSpec(name="table", type="enum",
                  enum=("daily_price", "daily_valuation"), default="daily_price",
                  desc="取哪个表的最新交易日"),
        ParamSpec(name="exclude_index", type="bool", default=True,
                  desc="是否排除指数行（code NOT LIKE 'sh%'）；默认 true"),
        ParamSpec(name="before", type="date",
                  desc="trade_date < before（严格小于；取「低于该日的最近交易日」）"),
    ),
    columns=(ColumnSpec("trade_date", "date"),),
    ttl_seconds=None,
    source="db",
    desc="读自有库最新 trade_date（MAX）；本地权威，不缓存。",
)

# daily_price_days：区间内「有行情的交易日」去重列表（供 data_quality._check_missing_days）。
DAILY_PRICE_DAYS = DatasetSpec(
    id="daily_price_days",
    title="有行情的交易日",
    kind="raw",
    params=(
        ParamSpec(name="start", type="date", desc="起始日 trade_date ≥（含）"),
        ParamSpec(name="end", type="date", desc="截止日 trade_date ≤（含）"),
        ParamSpec(name="exclude_index", type="bool", default=True,
                  desc="是否排除指数行（code NOT LIKE 'sh%'）；默认 true"),
    ),
    columns=(ColumnSpec("trade_date", "date"),),
    ttl_seconds=None,
    source="db",
    desc="读自有库 daily_price 中区间内有行情的交易日（DISTINCT，升序）；本地权威，不缓存。",
)

# market_ready：采集就绪闸门（池覆盖率 ≥ 阈值）。避免客户端拉 800 行自算。
MARKET_READY = DatasetSpec(
    id="market_ready",
    title="采集就绪度",
    kind="raw",
    params=(
        ParamSpec(name="date", type="date", desc="判定日（该日池覆盖率）"),
        ParamSpec(name="threshold", type="str", default="0.9",
                  desc="就绪阈值（默认 0.9）"),
        ParamSpec(name="universe", type="str", default="hs300,zz500",
                  desc="池白名单，逗号；默认 hs300,zz500"),
    ),
    columns=(
        ColumnSpec("ready", "bool", desc="覆盖率 ≥ 阈值"),
        ColumnSpec("covered", "int", desc="当日有行情的池内 code 数"),
        ColumnSpec("total", "int", desc="池内 code 总数"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库池就绪度；本地权威，不缓存。",
)

# dup_keys：重复键检查（供 data_quality._check_duplicates）。datahub 有 UNIQUE 索引 → 恒空。
DUP_KEYS = DatasetSpec(
    id="dup_keys",
    title="重复键检查",
    kind="raw",
    params=(
        ParamSpec(name="table", type="enum",
                  enum=("daily_price", "daily_valuation", "financial_indicator"),
                  default="daily_price", desc="检查哪张表"),
    ),
    columns=(
        ColumnSpec("key", "str", desc="重复键（code|trade_date 或 code|report_date）"),
        ColumnSpec("n", "int", desc="重复条数（>1）"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库重复唯一键组（UNIQUE 索引下恒空）；本地权威，不缓存。",
)

RAW_DATASETS: tuple[DatasetSpec, ...] = (
    TRADE_CALENDAR,
    STOCK_BASIC,
    DAILY_PRICE,
    DAILY_VALUATION,
    FINANCIAL_INDICATOR,
    LATEST_TRADE_DATE,
    DAILY_PRICE_DAYS,
    MARKET_READY,
    DUP_KEYS,
)
