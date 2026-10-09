"""BaoStock 数据源适配层（阶段 3：daily/calendar/index/valuation/finance/stock_basic）

设计原则：
- 无 token / 无外部依赖，`get_session()` 恒可用（依赖缺失时返回 None，采集器跳过）；
- baostock 是自定义 TCP 协议，**进程内全局单 socket**（context.default_socket）：
  `login()` 建连一次，后续所有查询复用同一连接；登录 2~40s 不等。因此会话为
  **懒登录单例 + 失效重连**，`get_session()` 返回缓存实例，逐只循环只登录一次；
- 各查询返回与 AkShare **同形状**的数据（上层 build_rows / save / 清洗 /
  重试逻辑完全不动）；调用失败抛异常 → 采集器降级 AkShare；
- socket 全程套超时（`socket.setdefaulttimeout`），防止库内阻塞 connect/recv 挂死
  （曾卡死整批补救的同类问题；baostock 本身不设超时）。

复权语义（8/26 用 600519 茅台 / 689009 九号实测确认，勿再踩）：
- `adjustflag="1"` = **后复权**：茅台 8/26 close=9991.51（=原始 1302.8 × 累计因子
  7.67），与东财 hfq 同口径 → 派生 adj_factor 必须用它；
- `adjustflag="2"` = 前复权：最新日 = 原始价，用它派生 adj_factor 会在最新日得 1.0，
  历史全部错（九号实测差 6.4%）；
- `adjustflag="3"` = 不复权（入库 close 用这个）。

单位：BaoStock 成交量=股（→ ÷100 转手，对齐东财），成交额=元（无需换算）。
"""
import logging
import socket
import threading
import time
from collections.abc import Callable
from datetime import date, timedelta

import pandas as pd

try:
    import baostock as bs
except ImportError:  # baostock 未安装：本层退化，采集器跳过 BaoStock
    bs = None

try:
    from app.collect_config import (
        BAOSTOCK_BAN_COOLDOWN,
        BAOSTOCK_RETRIES,
        BAOSTOCK_RETRY_DELAY,
        BAOSTOCK_TIMEOUT,
    )
except ImportError:  # 旧部署无这些配置项：用内置默认值（本层保持独立可跑）
    BAOSTOCK_TIMEOUT = 60
    BAOSTOCK_RETRIES = 1
    BAOSTOCK_RETRY_DELAY = 2
    BAOSTOCK_BAN_COOLDOWN = 1800

logger = logging.getLogger(__name__)

# 成功码
_BSERR_SUCCESS = "0"

# 连接级错误码：命中即判定会话失效 → 重连重试（其余为参数/数据错误，直接抛）
_CONN_CODES = {
    "10001001",  # 未登录
    "10002001",  # 网络错误
    "10002002",  # 网络连接失败
    "10002003",  # 网络连接超时
    "10002004",  # 接收时连接断开
    "10002005",  # 网络发送失败
    "10002006",  # 网络发送超时
    "10002007",  # 网络接收错误
    "10002008",  # 网络接收超时
}

# 封禁码：命中 → 进程内黑名单冷却（08-28 事故根因，勿再逐股登录轰击）
_BAN_CODE = "10001011"


def is_source_blocked(exc: Exception) -> bool:
    """判定异常是否为「源被限」而非瞬时错误（自愈 stage1 据此分流，见迁移 005）。

    源被限（封禁/黑名单冷却）→ 任务置 source_blocked 不再重试，绝不反复轰被限源；
    瞬时错误（网络超时 10002003/10002006/10002008 等）→ 允许 attempts 重试。
    """
    msg = str(exc)
    return ("BaoStock 封禁冷却中" in msg
            or f"BaoStock 登录失败({_BAN_CODE}" in msg)

_session: "BaoStockSession | None" = None
_session_lock = threading.Lock()


class _SocketTimeout:
    """给 baostock 的阻塞 socket 套超时的上下文管理器（防止 connect/recv 挂死）。

    baostock 在 login 与 send_msg 内新建 socket，均不带 timeout。这里在调用前后
    设置/恢复进程级默认超时，让新建 socket 继承超时，超时后抛 socket.timeout，
    由会话按「连接失效」重连，而非无限阻塞。
    """

    def __init__(self, seconds: float):
        self.seconds = seconds

    def __enter__(self):
        self._prev = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.seconds)
        return self

    def __exit__(self, *exc):
        socket.setdefaulttimeout(self._prev)
        return False


class BaoStockSession:
    """BaoStock 会话：懒登录 + 连接失效重连 + socket 超时。

    baostock 全局单 socket 非线程安全，所有查询经 self._lock 串行化。
    """

    def __init__(self, timeout: float = BAOSTOCK_TIMEOUT,
                 retries: int = BAOSTOCK_RETRIES,
                 retry_delay: float = BAOSTOCK_RETRY_DELAY):
        self._connected = False
        self._lock = threading.Lock()
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self._ban_until = 0.0  # 黑名单冷却截止（unix 秒），命中 10001011 后置位

    # ---------- 连接管理 ----------

    def _login(self):
        """登录并置为已连接；失败抛异常（采集器降级下游源）。

        命中黑名单（10001011）→ 进程内冷却 BAOSTOCK_BAN_COOLDOWN，期间不再
        反复登录锤（08-28 事故：封禁期逐股登录把整链拖成数小时超时）。
        """
        now = time.time()
        if now < self._ban_until:
            raise RuntimeError(
                f"BaoStock 封禁冷却中（{int(self._ban_until - now)}s 后重试）")
        with _SocketTimeout(self.timeout):
            lg = bs.login()
        code = str(getattr(lg, "error_code", "?"))
        if code != _BSERR_SUCCESS:
            if code == _BAN_CODE:
                self._ban_until = now + BAOSTOCK_BAN_COOLDOWN
                logger.error("BaoStock 封禁(%s:%s)，%ss 内跳过该源",
                             code, getattr(lg, "error_msg", ""), BAOSTOCK_BAN_COOLDOWN)
            raise RuntimeError(f"BaoStock 登录失败({code}:{getattr(lg, 'error_msg', '')})")
        self._connected = True
        logger.info("BaoStock 登录成功")

    def _ensure(self):
        if bs is None:
            raise RuntimeError("baostock 未安装")
        if not self._connected:
            self._login()

    def _call(self, qfunc: Callable, *args, **kwargs):
        """调用查询函数并套 socket 超时"""
        with _SocketTimeout(self.timeout):
            return qfunc(*args, **kwargs)

    # ---------- 查询入口（重连重试） ----------

    def query(self, qfunc: Callable, *args, **kwargs):
        """执行一次查询；连接级失败自动重连重试，其余失败直接抛。

        返回 baostock ResultData（error_code == "0" 已确认）。
        """
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with self._lock:
                    self._ensure()
                    rs = self._call(qfunc, *args, **kwargs)
            except Exception as e:  # socket 超时 / 连接异常 / 登录失败
                self._connected = False
                last = e
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
                    continue
                raise RuntimeError(
                    f"BaoStock {getattr(qfunc, '__name__', qfunc)} 网络异常: {e}") from e
            if rs is None:
                self._connected = False
                last = RuntimeError("BaoStock 查询返回 None")
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
                    continue
                raise last
            code = str(rs.error_code)
            if code == _BSERR_SUCCESS:
                return rs
            name = getattr(qfunc, "__name__", str(qfunc))
            if code in _CONN_CODES and attempt < self.retries:
                self._connected = False
                time.sleep(self.retry_delay)
                continue
            raise RuntimeError(f"BaoStock {name} 失败({code}:{rs.error_msg})")

    def close(self):
        if bs is not None and self._connected:
            try:
                bs.logout()
            except Exception:
                pass
        self._connected = False


# ---------- 模块级会话单例（逐只循环共享一次登录） ----------

def get_session() -> BaoStockSession | None:
    """返回进程级 BaoStock 会话单例；依赖缺失 → None（采集器跳过）"""
    global _session
    if bs is None:
        return None
    with _session_lock:
        if _session is None:
            _session = BaoStockSession()
        return _session


def _reset_session():
    """测试用：重置单例（close 旧会话）"""
    global _session
    with _session_lock:
        if _session is not None:
            _session.close()
        _session = None


# ---------- 工具 ----------

def bs_code(code: str) -> str:
    """股票代码 → BaoStock 带市场前缀格式（600519→sh.600519 / 000001→sz.000001 /
    830001→bj.830001）"""
    code = str(code)
    if code.startswith("6"):
        return "sh." + code
    if code.startswith(("8", "4", "9")):
        return "bj." + code
    return "sz." + code


def _ymd(d) -> str:
    """date / str → BaoStock 需要的 YYYY-MM-DD"""
    if isinstance(d, date):
        return d.strftime("%Y-%m-%d")
    return str(d).replace("/", "-")


def _rs_to_df(rs) -> pd.DataFrame:
    """ResultData → DataFrame（翻页聚合；字段用 rs.fields 命名）"""
    if rs is None:
        return pd.DataFrame()
    rows = []
    while rs.error_code == _BSERR_SUCCESS and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=list(rs.fields))


# ---------- 按股票：日行情（raw + 后复权，对齐东财中文列） ----------

def daily_pairs(sess: BaoStockSession, code: str, start_date, end_date):
    """不复权 + 后复权 → (raw_df, hfq_df)，列为东财格式（中文）

    build_rows 期望：raw 含 日期/开盘/最高/最低/收盘/成交量/成交额；
    hfq 含 日期/收盘（后复权价）。单位：成交量 股→手（/100），成交额 元 原样。
    """
    b = bs_code(code)
    s, e = _ymd(start_date), _ymd(end_date)
    fields = "date,open,high,low,close,volume,amount"
    raw_rs = sess.query(bs.query_history_k_data_plus, b, fields,
                        start_date=s, end_date=e, frequency="d", adjustflag="3")
    raw = _rs_to_df(raw_rs)
    if raw.empty:
        return raw, pd.DataFrame()
    hfq_rs = sess.query(bs.query_history_k_data_plus, b, "date,close",
                        start_date=s, end_date=e, frequency="d", adjustflag="1")
    hfq = _rs_to_df(hfq_rs)
    if hfq.empty:
        raise RuntimeError(f"{code} BaoStock 后复权未返回数据（不复权有 {len(raw)} 行）")

    raw = raw.rename(columns={
        "date": "日期", "open": "开盘", "high": "最高", "low": "最低",
        "close": "收盘", "volume": "成交量", "amount": "成交额",
    })
    # BaoStock 偶发空串（如 000066 某批成交量）→ to_numeric 转 NaN，避免 astype 崩溃；
    # 无收盘的坏条直接丢弃（无法派生因子）；空成交量行由 cleaner 按停牌语义丢弃。
    for col in ("开盘", "最高", "最低", "收盘", "成交量", "成交额"):
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    raw = raw.dropna(subset=["收盘"])
    raw["成交量"] = raw["成交量"] / 100  # 股 → 手
    hfq = hfq.rename(columns={"date": "日期", "close": "收盘"})
    return raw[["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]], hfq


# ---------- 交易日历 ----------

def trade_cal_rows(sess: BaoStockSession, start_date=None, end_date=None) -> list[dict]:
    """交易日历（is_trading_day=1 才入库，对齐现有「只有交易日」语义）"""
    today = date.today()
    if start_date is None:
        start_date = today - timedelta(days=730)
    if end_date is None:
        end_date = today + timedelta(days=365)
    rs = sess.query(bs.query_trade_dates,
                    start_date=_ymd(start_date), end_date=_ymd(end_date))
    rows = []
    for _, r in _rs_to_df(rs).iterrows():
        if str(r["is_trading_day"]) != "1":
            continue
        rows.append({
            "cal_date": date.fromisoformat(str(r["calendar_date"])),
            "is_open": True,
            "exchange": "SSE",
        })
    return rows


# ---------- 阶段 2：股票列表 / 估值 / 指数 / 财务（同形状输出） ----------

def _num(v) -> float | None:
    """BaoStock 字符串值 → float；空串/NaN/异常 → None"""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        f = float(v)
        return f if not pd.isna(f) else None
    except (TypeError, ValueError):
        return None


def _market(code: str) -> str:
    """股票代码 → 交易所（SH/SZ/BJ），与 collectors/stock.infer_market 一致"""
    if code.startswith("6"):
        return "SH"
    if code.startswith(("8", "4", "9")):
        return "BJ"
    return "SZ"


def _plain_code(raw) -> str:
    """BaoStock 带前缀代码 → 6 位纯数字（sh.600519 → 600519）"""
    return str(raw).split(".")[-1].zfill(6)


def stock_basic_rows(sess: BaoStockSession) -> list[dict]:
    """全市场股票列表（type=1 股票）→ 入库行（无 industry，留 AkShare）

    与 stock.normalize_stock_rows 同形状：
    {code, name, market, status, list_date?}。query_stock_basic 的 code 带交易所
    前缀（sh.600519），入库须剥离成 6 位纯数字。
    """
    rs = sess.query(bs.query_stock_basic)
    df = _rs_to_df(rs)
    if df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        if str(r.get("type", "1")) != "1":  # 只收股票，过滤指数/ETF/转债
            continue
        code = _plain_code(r["code"])
        name = str(r["code_name"]).replace(" ", "").replace("　", "")
        st = str(r.get("status", "1"))
        row = {
            "code": code,
            "name": name,
            "market": _market(code),
            "status": "D" if st == "0" else "L",
        }
        ipo = r.get("ipoDate")
        if ipo is not None and str(ipo).strip():
            row["list_date"] = date.fromisoformat(str(ipo).strip()[:10])
        rows.append(row)
    return rows


def valuation_rows(sess: BaoStockSession, code: str, start_date, end_date) -> list[dict]:
    """日度估值（peTTM/pbMRQ 随日线一查给）→ DailyValuation 形状

    不含 total_mv/float_mv/pe_static（保留既有值：BaoStock 无直接源且无消费者；
    由采集器 save 分支跳过这三列，不覆盖既有 AkShare 值）。
    close 用不复权（adjustflag=3）。
    """
    b = bs_code(code)
    s, e = _ymd(start_date), _ymd(end_date)
    rs = sess.query(bs.query_history_k_data_plus, b, "date,close,peTTM,pbMRQ",
                    start_date=s, end_date=e, frequency="d", adjustflag="3")
    df = _rs_to_df(rs)
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "code": code,
            "trade_date": date.fromisoformat(str(r["date"])),
            "close": _num(r.get("close")),
            "pe_ttm": _num(r.get("peTTM")),
            "pb": _num(r.get("pbMRQ")),
        })
    return rows


def index_bs_code(symbol: str) -> str:
    """新浪指数码 → BaoStock 指数码（sh000001→sh.000001 / sz399106→sz.399106）

    不能复用 bs_code()：它把 000001 当股票映射成 sz.000001（上证综指是 sh.000001）。
    """
    symbol = str(symbol)
    if symbol.startswith("sh"):
        return "sh." + symbol[2:]
    if symbol.startswith("sz"):
        return "sz." + symbol[2:]
    return symbol


def index_rows(sess: BaoStockSession, symbol: str, start_date, end_date) -> list[dict]:
    """指数日行情 → 与 index.build_rows 同形状入库行（code 用原新浪码）"""
    if start_date is None:
        start_date = date.today() - timedelta(days=365)
    if end_date is None:
        end_date = date.today()
    b = index_bs_code(symbol)
    rs = sess.query(bs.query_history_k_data_plus, b,
                    "date,open,high,low,close,volume,amount",
                    start_date=_ymd(start_date), end_date=_ymd(end_date),
                    frequency="d", adjustflag="3")
    df = _rs_to_df(rs)
    rows = []
    for _, r in df.iterrows():
        vol = _num(r.get("volume"))
        rows.append({
            "code": symbol,
            "trade_date": date.fromisoformat(str(r["date"])),
            "open": _num(r.get("open")),
            "high": _num(r.get("high")),
            "low": _num(r.get("low")),
            "close": _num(r.get("close")),
            "volume": int(vol / 100) if vol is not None else None,  # 股 → 手，同 daily_pairs
            "amount": _num(r.get("amount")),                        # 元 原样
            "adj_factor": None,
        })
    return rows


# 报告期季度末（BaoStock 按 year+quarter 索引）
_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def financial_rows(sess: BaoStockSession, code: str, year: int, quarter: int) -> dict | None:
    """单报告期财务指标 → FinancialIndicator 形状（无 industry，留 AkShare）

    - profit_data: roeAvg→roe、gpMargin→gross_margin、MBRevenue→营收增速、pubDate→announce_date
    - growth_data: YOYNI→profit_growth
    - balance_data: liabilityToAsset→debt_ratio
    - revenue_growth: 两期 MBRevenue 同比（同季度上年）推导

    单位校准（2026-08-27 真实 API 实证，勿再用 ×10000）：BaoStock 四比例字段全是
    小数比例，库存为百分数 → 统一 ×100。实证：浦发 600000 liabilityToAsset=0.918356
    ×100=91.8356 = 库内 debt_ratio；茅台 gpMargin=0.895552×100=89.5552 = 库内。
    """
    b = bs_code(code)
    pr = _rs_to_df(sess.query(bs.query_profit_data, b, year, quarter))
    if pr.empty:
        return None
    p0 = pr.iloc[0]
    m, d = _QUARTER_END[quarter]
    row: dict = {
        "code": code,
        "report_date": date(year, m, d),
        "roe": _num(p0.get("roeAvg")),
        "profit_growth": None,
        "revenue_growth": None,
        "gross_margin": _num(p0.get("gpMargin")),
        "debt_ratio": None,
        "announce_date": None,
    }
    pub = p0.get("pubDate")
    if pub is not None and str(pub).strip():
        row["announce_date"] = date.fromisoformat(str(pub)[:10])
    gr = _rs_to_df(sess.query(bs.query_growth_data, b, year, quarter))
    if not gr.empty:
        row["profit_growth"] = _num(gr.iloc[0].get("YOYNI"))
    ba = _rs_to_df(sess.query(bs.query_balance_data, b, year, quarter))
    if not ba.empty:
        lta = _num(ba.iloc[0].get("liabilityToAsset"))
        if lta is not None:
            row["debt_ratio"] = round(lta * 100, 4)  # 小数比例 → 百分数
    mb = _num(p0.get("MBRevenue"))
    if mb is not None:
        prev = _rs_to_df(sess.query(bs.query_profit_data, b, year - 1, quarter))
        if not prev.empty:
            mb_prev = _num(prev.iloc[0].get("MBRevenue"))
            if mb_prev not in (None, 0):
                row["revenue_growth"] = round((mb / mb_prev - 1) * 100, 4)
    if row["roe"] is not None:
        row["roe"] = round(row["roe"] * 100, 4)
    if row["profit_growth"] is not None:
        row["profit_growth"] = round(row["profit_growth"] * 100, 4)
    if row["gross_margin"] is not None:
        row["gross_margin"] = round(row["gross_margin"] * 100, 4)
    return row
