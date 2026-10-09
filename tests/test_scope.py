"""采集范围闸门（Issue #13）测试：COLLECT_SCOPE 默认 pool 不改变现状行为。

关键不变量：默认分支必须与旧 `universe IN ('hs300','zz500')` **逐字等价** ——
这是「部署本批代码生产行为零变化」的根据，也是策略选股域不受扩池影响的证明。
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.collectors import scope as scope_mod
from app.models.tables import Base, StockBasic


def make_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def seed(db):
    # 池内（SH/SZ）
    db.add(StockBasic(code="600519", name="贵州茅台", market="SH",
                      universe="hs300", data_scope="a_share"))
    db.add(StockBasic(code="000001", name="平安银行", market="SZ",
                      universe="zz500", data_scope="a_share"))
    # 非池内但在采集范围（扩池后才采）
    db.add(StockBasic(code="002415", name="海康威视", market="SZ",
                      data_scope="a_share"))
    db.add(StockBasic(code="688111", name="金山办公", market="SH",
                      data_scope="a_share"))
    # 北交所：保留列表行但 data_scope=NULL（不采集）
    db.add(StockBasic(code="830001", name="北交所股", market="BJ",
                      data_scope=None))
    db.commit()


def test_default_scope_is_pool(monkeypatch):
    """默认 COLLECT_SCOPE=pool → 只有 800 池（这里 2 只）"""
    monkeypatch.setattr(scope_mod, "collect_scope", lambda: "pool")
    db = make_db()
    seed(db)
    assert scope_mod.collect_codes(db) == ["000001", "600519"]


def test_a_share_scope_returns_full_market(monkeypatch):
    """COLLECT_SCOPE=a_share → 按 data_scope='a_share'（排除 BJ/INDEX）"""
    monkeypatch.setattr(scope_mod, "collect_scope", lambda: "a_share")
    db = make_db()
    seed(db)
    codes = scope_mod.collect_codes(db)
    assert codes == ["000001", "002415", "600519", "688111"]
    assert "830001" not in codes  # 北交所被排除


def test_a_share_include_pool_unions_universe(monkeypatch):
    """include_pool=True：data_scope 漂移时股票池仍被采（OR 保底）"""
    monkeypatch.setattr(scope_mod, "collect_scope", lambda: "a_share")
    db = make_db()
    # 一只池内股 data_scope 被误标 NULL —— 仍须被采集
    db.add(StockBasic(code="000002", name="万科A", market="SZ",
                      universe="hs300", data_scope=None))
    db.add(StockBasic(code="600519", name="贵州茅台", market="SH",
                      universe="hs300", data_scope="a_share"))
    db.commit()
    codes = scope_mod.collect_codes(db, include_pool=True)
    assert "000002" in codes  # 池保底
    assert "600519" in codes


def test_unknown_scope_falls_back_to_pool(monkeypatch):
    """未知 COLLECT_SCOPE 值 → 防呆回退 pool（绝不误扩采集范围）"""
    from app import collect_config as cfg_mod

    monkeypatch.setattr(cfg_mod, "collect_scope", lambda: "a_share_x")
    # collect_codes 直接比对字面量，未知值应落默认分支
    monkeypatch.setattr(scope_mod, "collect_scope", lambda: "a_share_x")
    db = make_db()
    seed(db)
    assert scope_mod.collect_codes(db) == ["000001", "600519"]


def test_config_collect_scope_sanitizes(monkeypatch):
    """config.collect_scope()：非法值 → pool（防呆，绝不误扩采集范围）"""
    from app import collect_config as cfg_mod

    for bad in ("a_share_x", "ALL", ""):
        monkeypatch.setattr(cfg_mod, "COLLECT_SCOPE", bad)
        assert cfg_mod.collect_scope() == "pool"
    monkeypatch.setattr(cfg_mod, "COLLECT_SCOPE", "a_share")
    assert cfg_mod.collect_scope() == "a_share"
