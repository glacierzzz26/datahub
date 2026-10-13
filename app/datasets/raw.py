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
Phase 4 修复：`dup_keys` 加 `start`/`end`（键日期窗口）——全表 `GROUP BY` 在 10M 行上需
~10–55s，超消费方读超时致质检 job 假失败；qe 只断言近窗零重复。

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
# `start`/`end` 收窄「键日期」窗口：全表 GROUP BY（daily_price 10.26M / daily_valuation 9.28M 行）
# 需 ~10–55s，会超消费方（qe）10s 读超时 → 质检 job 假失败；qe 只断言「近期零重复」，传近窗即可。
DUP_KEYS = DatasetSpec(
    id="dup_keys",
    title="重复键检查",
    kind="raw",
    params=(
        ParamSpec(name="table", type="enum",
                  enum=("daily_price", "daily_valuation", "financial_indicator"),
                  default="daily_price", desc="检查哪张表"),
        ParamSpec(name="start", type="date",
                  desc="键日期下界（含）：trade_date/report_date ≥；空=不限"),
        ParamSpec(name="end", type="date",
                  desc="键日期上界（含）：trade_date/report_date ≤；空=不限"),
    ),
    columns=(
        ColumnSpec("key", "str", desc="重复键（code|trade_date 或 code|report_date）"),
        ColumnSpec("n", "int", desc="重复条数（>1）"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库重复唯一键组（UNIQUE 索引下恒空；可用 start/end 收窄窗口）；本地权威，不缓存。",
)

# ---------- P14-1 研报数值域（datahub 自有表）----------
# research/src/providers/* 现直调 akshare 的 8 个数据域；列照其现用输出冻结。
# 通用参数风格与上文一致：codes 逗号 IN、start/end 区间、date 精确、last 每码最近 N、fields 投影。

# 公共参数片段（复用，避免 8 处逐字重复）
_FIELDS = ParamSpec(name="fields", type="str", desc="列投影白名单，逗号；空=全列")
_CODES = ParamSpec(name="codes", type="str", desc="代码白名单，逗号 IN；空=不限")

# capital_flow：龙虎榜记录（每 code 每日一行）。
CAPITAL_FLOW = DatasetSpec(
    id="capital_flow",
    title="龙虎榜记录",
    kind="raw",
    params=(
        _CODES,
        ParamSpec(name="start", type="date", desc="起始日 trade_date ≥（含）"),
        ParamSpec(name="end", type="date", desc="截止日 trade_date ≤（含）"),
        ParamSpec(name="last", type="int", desc="每 code 取最近 N 行；空=不限"),
        _FIELDS,
    ),
    columns=(
        ColumnSpec("code", "str"),
        ColumnSpec("trade_date", "date"),
        ColumnSpec("name", "str", desc="名称"),
        ColumnSpec("close", "float", desc="收盘价（元）"),
        ColumnSpec("pct_change", "float", desc="涨跌幅（%），东财口径"),
        ColumnSpec("turnover_rate", "float", desc="换手率（%）"),
        ColumnSpec("reasons", "str", desc="上榜原因，多因以「；」连接"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库 capital_flow（龙虎榜记录）；本地权威，不缓存。",
)

# capital_flow_detail：龙虎榜明细（营业部级）。
CAPITAL_FLOW_DETAIL = DatasetSpec(
    id="capital_flow_detail",
    title="龙虎榜明细",
    kind="raw",
    params=(
        _CODES,
        ParamSpec(name="start", type="date", desc="起始日 trade_date ≥（含）"),
        ParamSpec(name="end", type="date", desc="截止日 trade_date ≤（含）"),
        ParamSpec(name="date", type="date", desc="精确单日（给定时优先于 start/end）"),
        _FIELDS,
    ),
    columns=(
        ColumnSpec("code", "str"),
        ColumnSpec("trade_date", "date"),
        ColumnSpec("dealer_name", "str", desc="交易营业部名称"),
        ColumnSpec("buy_amount", "float", desc="买入金额（元）"),
        ColumnSpec("buy_pct", "float", desc="买入金额-占总成交比例"),
        ColumnSpec("sell_amount", "float", desc="卖出金额（元）"),
        ColumnSpec("sell_pct", "float", desc="卖出金额-占总成交比例"),
        ColumnSpec("net_amount", "float", desc="净额（元）"),
        ColumnSpec("reason", "str", desc="类型（买入/卖出…）"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库 capital_flow_detail（龙虎榜营业部明细）；本地权威，不缓存。",
)

# industry_index：申万行业指数日线（不存量价——SW 量纲与 A 股不兼容）。
INDUSTRY_INDEX = DatasetSpec(
    id="industry_index",
    title="申万行业指数日线",
    kind="raw",
    params=(
        _CODES,
        ParamSpec(name="start", type="date", desc="起始日 trade_date ≥（含）"),
        ParamSpec(name="end", type="date", desc="截止日 trade_date ≤（含）"),
        ParamSpec(name="asof", type="date", desc="trade_date ≤ asof（取数锚）"),
        ParamSpec(name="last", type="int", desc="每 code 取最近 N 行；空=不限"),
        _FIELDS,
    ),
    columns=(
        ColumnSpec("code", "str", desc="申万行业指数代码（如 801010）"),
        ColumnSpec("trade_date", "date"),
        ColumnSpec("open", "float"),
        ColumnSpec("high", "float"),
        ColumnSpec("low", "float"),
        ColumnSpec("close", "float"),
        ColumnSpec("pct_change", "float", desc="涨跌幅（%）"),
        ColumnSpec("amplitude", "float", desc="振幅（%）"),
        ColumnSpec("chg_amount", "float", desc="涨跌额"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库 industry_index（申万行业指数日线）；本地权威，不缓存。",
)

# industry_sw_info：申万行业分类信息（一/二/三级）。
INDUSTRY_SW_INFO = DatasetSpec(
    id="industry_sw_info",
    title="申万行业分类信息",
    kind="raw",
    params=(
        ParamSpec(name="levels", type="str", desc="级别白名单，逗号（1/2/3）；空=不限"),
        ParamSpec(name="codes", type="str", desc="行业代码白名单，逗号 IN；空=不限"),
        ParamSpec(name="parent", type="str", desc="上级行业名称精确匹配"),
        _FIELDS,
    ),
    columns=(
        ColumnSpec("code", "str", desc="行业代码（如 801010）"),
        ColumnSpec("level", "int", desc="级别：1/2/3"),
        ColumnSpec("name", "str", desc="行业名称"),
        ColumnSpec("level_label", "str", desc="一级/二级/三级"),
        ColumnSpec("parent_name", "str", desc="上级行业"),
        ColumnSpec("member_count", "int", desc="成份个数"),
        ColumnSpec("pe_static", "float", desc="静态市盈率"),
        ColumnSpec("pe_ttm", "float", desc="TTM(滚动)市盈率"),
        ColumnSpec("pb", "float", desc="市净率"),
        ColumnSpec("dividend_yield", "float", desc="静态股息率"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库 industry_sw_info（申万行业分类信息）；本地权威，不缓存。",
)

# industry_sw_cons：申万三级成分股。
INDUSTRY_SW_CONS = DatasetSpec(
    id="industry_sw_cons",
    title="申万三级成分股",
    kind="raw",
    params=(
        ParamSpec(name="index_codes", type="str",
                  desc="申万三级行业代码白名单，逗号 IN；空=不限"),
        ParamSpec(name="codes", type="str", desc="成分股代码白名单，逗号 IN；空=不限"),
        _FIELDS,
    ),
    columns=(
        ColumnSpec("index_code", "str", desc="申万三级行业代码"),
        ColumnSpec("stock_code", "str", desc="成分股代码"),
        ColumnSpec("stock_name", "str", desc="成分股名称"),
        ColumnSpec("market_cap", "float", desc="市值（亿元）"),
        ColumnSpec("pe_ttm", "float", desc="市盈率 TTM"),
        ColumnSpec("pb", "float", desc="市净率"),
        ColumnSpec("roe", "float", desc="ROE（%）"),
        ColumnSpec("dividend_yield", "float", desc="股息率（%）"),
        ColumnSpec("net_profit_growth", "float", desc="净利润增速（%）"),
        ColumnSpec("revenue_growth", "float", desc="营收增速（%）"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库 industry_sw_cons（申万三级成分股）；本地权威，不缓存。",
)

# macro：宏观指标序列（CPI/PPI/M2/GDP）。
MACRO = DatasetSpec(
    id="macro",
    title="宏观指标序列",
    kind="raw",
    params=(
        ParamSpec(name="indicator", type="str",
                  desc="指标白名单，逗号（cn_cpi/cn_ppi/cn_m2/cn_gdp）；空=不限"),
        ParamSpec(name="start", type="date", desc="起始期 period_end ≥（含）"),
        ParamSpec(name="end", type="date", desc="截止期 period_end ≤（含）"),
        ParamSpec(name="last", type="int", desc="每 indicator 取最近 N 行；空=不限"),
        _FIELDS,
    ),
    columns=(
        ColumnSpec("indicator", "str", desc="指标键：cn_cpi/cn_ppi/cn_m2/cn_gdp"),
        ColumnSpec("period_label", "str", desc="统计期原文（如 2026年08月份）"),
        ColumnSpec("period_end", "date", desc="统计期末"),
        ColumnSpec("cadence", "str", desc="频率：month/quarter"),
        ColumnSpec("level", "float", desc="指标值（指数/绝对量；口径见 research）"),
        ColumnSpec("yoy", "float", desc="同比（%）"),
        ColumnSpec("mom", "float", desc="环比（%）；源无则 null"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库 macro（宏观指标序列）；本地权威，不缓存。",
)

# news_by_stock：个股新闻（content_hash 去重键不外发）。
NEWS_BY_STOCK = DatasetSpec(
    id="news_by_stock",
    title="个股新闻",
    kind="raw",
    params=(
        _CODES,
        ParamSpec(name="start", type="date", desc="起始 published_at ≥（含）"),
        ParamSpec(name="end", type="date", desc="截止 published_at ≤（含当日）"),
        ParamSpec(name="last", type="int", desc="每 code 取最近 N 行；空=不限"),
        _FIELDS,
    ),
    columns=(
        ColumnSpec("code", "str"),
        ColumnSpec("title", "str", desc="新闻标题"),
        ColumnSpec("content", "str", desc="新闻内容"),
        ColumnSpec("published_at", "str", desc="发布时间（ISO 字符串）"),
        ColumnSpec("source", "str", desc="文章来源"),
        ColumnSpec("url", "str", desc="新闻链接"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库 news_by_stock（个股新闻）；本地权威，不缓存。",
)

# announcement_by_stock：个股公告（标题级）。
ANNOUNCEMENT_BY_STOCK = DatasetSpec(
    id="announcement_by_stock",
    title="个股公告",
    kind="raw",
    params=(
        _CODES,
        ParamSpec(name="start", type="date", desc="起始 publish_date ≥（含）"),
        ParamSpec(name="end", type="date", desc="截止 publish_date ≤（含）"),
        ParamSpec(name="last", type="int", desc="每 code 取最近 N 行；空=不限"),
        _FIELDS,
    ),
    columns=(
        ColumnSpec("code", "str"),
        ColumnSpec("title", "str", desc="公告标题"),
        ColumnSpec("category", "str", desc="公告类型"),
        ColumnSpec("publish_date", "date", desc="公告日期"),
        ColumnSpec("url", "str", desc="网址"),
    ),
    ttl_seconds=None,
    source="db",
    desc="读自有库 announcement_by_stock（个股公告）；本地权威，不缓存。",
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
    CAPITAL_FLOW,
    CAPITAL_FLOW_DETAIL,
    INDUSTRY_INDEX,
    INDUSTRY_SW_INFO,
    INDUSTRY_SW_CONS,
    MACRO,
    NEWS_BY_STOCK,
    ANNOUNCEMENT_BY_STOCK,
)
