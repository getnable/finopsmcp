"""Hot read paths have an index to use, on new databases and on existing ones.

latest_captured_at asks for the newest cost snapshot, and with no index on
captured_at that was a sort of the whole cost_snapshots table on every budget
gate. The Kubernetes trend query filters by date range and cluster with no index
at all. create_all builds indexes only for tables it creates, so an existing
database only gets them from the migration.
"""
from __future__ import annotations

import logging
from datetime import date

import pytest
from sqlalchemy import func, inspect, select, text

import finops.storage.db as db_mod

_NEW = {"cost_snapshots": "ix_cs_captured_at", "kubernetes_costs": "ix_k8s_date_cluster"}


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A brand new SQLite database, with the engine singleton restored after."""
    prev_engine, prev_dir = db_mod._ENGINE, db_mod._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    db_mod._ENGINE = None
    db_mod._DATA_DIR = None
    yield db_mod
    if db_mod._ENGINE is not None and db_mod._ENGINE is not prev_engine:
        try:
            db_mod._ENGINE.dispose()
        except Exception:
            pass
    db_mod._ENGINE = prev_engine
    db_mod._DATA_DIR = prev_dir


def _index_names(engine, table: str) -> set[str]:
    return {ix["name"] for ix in inspect(engine).get_indexes(table)}


def _plan(engine, query) -> str:
    compiled = query.compile(engine, compile_kwargs={"literal_binds": True})
    with engine.connect() as conn:
        rows = conn.execute(text(f"EXPLAIN QUERY PLAN {compiled}")).fetchall()
    return " | ".join(str(r[-1]) for r in rows)


def test_a_new_database_has_the_indexes(fresh_db):
    engine = db_mod.get_engine()
    for table, name in _NEW.items():
        assert name in _index_names(engine, table)


def test_an_existing_database_gets_them_from_the_migration(fresh_db, caplog):
    engine = db_mod.get_engine()
    with engine.begin() as conn:
        for name in _NEW.values():
            conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
    engine.dispose()
    db_mod._ENGINE = None

    engine = db_mod.get_engine()
    for table, name in _NEW.items():
        assert name in _index_names(engine, table)

    # A second run finds them and leaves them alone.
    with caplog.at_level(logging.WARNING, logger="finops.storage.db"):
        db_mod._run_sqlite_migrations(engine)
    assert not [r for r in caplog.records if "index" in r.getMessage()]


def test_latest_captured_at_reads_the_index_not_the_table(fresh_db):
    from finops.storage.snapshots import latest_captured_at, store_snapshot

    store_snapshot("aws", "EC2", "111", "us-east-1", date(2026, 9, 1), 5.0)
    assert latest_captured_at() is not None

    t = db_mod.cost_snapshots
    plan = _plan(db_mod.get_engine(),
                 select(t.c.captured_at).order_by(t.c.captured_at.desc()).limit(1))
    assert "ix_cs_captured_at" in plan
    assert "TEMP B-TREE" not in plan, plan


def test_the_kubernetes_trend_query_uses_an_index(fresh_db):
    k = db_mod.kubernetes_costs
    query = (
        select(k.c.snapshot_date, k.c.cluster, k.c.namespace,
               func.sum(k.c.monthly_cost_usd))
        .where(k.c.snapshot_date >= "2026-08-01")
        .where(k.c.cluster == "prod")
        .group_by(k.c.snapshot_date, k.c.cluster, k.c.namespace)
        .order_by(k.c.snapshot_date)
    )
    plan = _plan(db_mod.get_engine(), query)
    assert "ix_k8s_date_cluster" in plan, plan
