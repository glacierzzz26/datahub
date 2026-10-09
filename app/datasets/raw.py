"""Phase 2 数据集定义（raw：读 datahub 自有库，`source="db"`）。

列名/参数**照 `steady/docs/phase2/design/数据接入层-datahub.md` §2.2 冻结**
（`trade_calendar`：params `start`,`end`,`is_open`；列 `cal_date,is_open,exchange`）。

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

# Phase 2 数据集全集（顺序即对外列出的顺序）
RAW_DATASETS: tuple[DatasetSpec, ...] = (
    TRADE_CALENDAR,
)
