"""archive_old_snapshots moves old rows inside the database, and all or nothing.

It used to fetchall() every row older than the cutoff into Python and insert
them back one parameter set at a time, so archiving a year of history held all
of it in memory at once. INSERT ... SELECT then DELETE, in one transaction, does
the same move without the rows ever leaving the database.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select

import finops.storage.db as db_mod


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


_COLS = ("provider", "service", "account_id", "region", "snapshot_date",
         "amount_usd", "granularity", "captured_at", "category")
_CAPTURED = datetime(2025, 1, 2, 3, 4, 5)


def _seed() -> None:
    today = date.today()
    rows = [
        # (days ago, service, category)
        (400, "EC2", "compute"),
        (380, "S3", None),
        (366, "RDS", "database"),
        (300, "EC2", "compute"),
        (1, "EC2", "compute"),
    ]
    with db_mod.get_engine().begin() as conn:
        conn.execute(db_mod.cost_snapshots.insert(), [
            {"provider": "aws", "service": svc, "account_id": "111",
             "region": "us-east-1", "snapshot_date": (today - timedelta(days=n)).isoformat(),
             "amount_usd": float(n), "granularity": "DAILY", "captured_at": _CAPTURED,
             "category": cat}
            for n, svc, cat in rows
        ])


def _rows(table) -> list[tuple]:
    with db_mod.get_engine().connect() as conn:
        return sorted(tuple(r) for r in conn.execute(
            select(*[table.c[c] for c in _COLS])).fetchall())


def test_old_rows_move_to_the_archive_with_every_column(fresh_db):
    _seed()
    before = _rows(db_mod.cost_snapshots)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")

    assert db_mod.archive_old_snapshots(days_to_keep=365) == 3

    archived = _rows(db_mod.cost_snapshots_archive)
    kept = _rows(db_mod.cost_snapshots)
    assert archived == [r for r in before if r[4] < cutoff]
    assert kept == [r for r in before if r[4] >= cutoff]
    assert {r[8] for r in archived} == {"compute", None, "database"}
    assert all(r[7] == _CAPTURED for r in archived)

    # Nothing left to move.
    assert db_mod.archive_old_snapshots(days_to_keep=365) == 0
    assert len(_rows(db_mod.cost_snapshots_archive)) == 3


def test_the_rows_never_come_back_into_python(fresh_db):
    _seed()
    statements: list[str] = []
    event.listen(db_mod.get_engine(), "before_cursor_execute",
                 lambda conn, cur, stmt, *a: statements.append(stmt.lstrip().upper()))

    db_mod.archive_old_snapshots(days_to_keep=365)

    assert statements, "archive ran no SQL"
    fetched = [s for s in statements if not s.startswith(("INSERT", "DELETE"))]
    assert not fetched, f"archive read rows back into Python: {fetched}"


def test_a_row_that_appears_mid_move_rolls_the_whole_move_back(fresh_db):
    """Another writer commits an old-dated row after the copy and before the
    delete. Deleting it would lose it, so the move is abandoned instead."""
    _seed()
    before_live = _rows(db_mod.cost_snapshots)
    engine = db_mod.get_engine()
    old_day = (date.today() - timedelta(days=500)).isoformat()

    def _sneak_in(conn, cursor, statement, *a):
        if statement.lstrip().upper().startswith("INSERT INTO COST_SNAPSHOTS_ARCHIVE"):
            other = conn.connection.dbapi_connection.cursor()
            other.execute(
                "INSERT INTO cost_snapshots (provider, service, account_id, region, "
                "snapshot_date, amount_usd, granularity, captured_at) "
                "VALUES ('aws', 'Late', '111', '', ?, 9.0, 'DAILY', '2025-01-01 00:00:00')",
                (old_day,))
            other.close()

    event.listen(engine, "after_cursor_execute", _sneak_in)
    try:
        with pytest.raises(RuntimeError, match="rolled back"):
            db_mod.archive_old_snapshots(days_to_keep=365)
    finally:
        event.remove(engine, "after_cursor_execute", _sneak_in)

    assert _rows(db_mod.cost_snapshots_archive) == []
    assert _rows(db_mod.cost_snapshots) == before_live
