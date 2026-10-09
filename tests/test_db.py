"""db 连接管理单测：get_session 复用进程级单例引擎（连接池整进程复用）。

**搬自 steady `collector/tests/test_db.py`**（逐字，仅 docstring 标注来源）。

回归 2026-08-30 生产事故：get_session 每次新建 engine（新连接池），旧池 idle 连接
依赖 GC 才关闭 → 缓慢堆积打满 Postgres max_connections。修复后引擎只创建一次。
本测试 monkeypatch 计数 create_db_engine（create_engine 本身不建连，无需真实 DB）。
"""
from app import db


def test_get_session_reuses_single_engine(monkeypatch):
    # 重置模块级单例，验证惰性初始化路径
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)

    created = []
    real = db.create_db_engine

    def counting(*a, **kw):
        e = real(*a, **kw)
        created.append(e)
        return e

    monkeypatch.setattr(db, "create_db_engine", counting)

    s1 = db.get_session()
    s2 = db.get_session()

    assert len(created) == 1, "get_session 应复用同一引擎，只创建一次"
    assert s1.get_bind() is s2.get_bind()
    s1.close()
    s2.close()


def test_get_engine_singleton(monkeypatch):
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)

    e1 = db.get_engine()
    e2 = db.get_engine()
    assert e1 is e2, "get_engine 应返回同一实例"


def test_get_dsn_defaults_to_datahub_db(monkeypatch):
    """DSN 默认库名是 datahub（Phase 2 复用生产实例另建库）——防误连 quant_system。"""
    for k in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"):
        monkeypatch.delenv(k, raising=False)
    assert db.get_dsn() == "postgresql+psycopg2://quant:@localhost:5432/datahub"


def test_get_dsn_reads_env(monkeypatch):
    monkeypatch.setenv("DB_HOST", "host.docker.internal")
    monkeypatch.setenv("DB_NAME", "datahub")
    monkeypatch.setenv("DB_USER", "quant")
    monkeypatch.setenv("DB_PASSWORD", "pw")
    assert db.get_dsn() == (
        "postgresql+psycopg2://quant:pw@host.docker.internal:5432/datahub"
    )


def test_pool_sized_small(monkeypatch):
    """连接池须**调小**（默认 2+3）：与 steady 共享生产 PG max_connections。"""
    monkeypatch.delenv("DB_POOL_SIZE", raising=False)
    monkeypatch.delenv("DB_MAX_OVERFLOW", raising=False)
    e = db.create_db_engine()
    assert e.pool.size() == 2
    assert e.pool._max_overflow == 3
    e.dispose()
