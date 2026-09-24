"""Attributed cost storage: every dollar lands once, under the right key and date.

Attribution feeds chargeback, so a row that overwrites another is not a display
glitch, it is a team's bill silently shrinking. Real SQLite, and where Cost
Explorer is involved a fake boto3 module stands where the service sits.
"""
from __future__ import annotations

from datetime import date

import pytest

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


def _by_team_env(start: date, end: date) -> dict[tuple[str, str], float]:
    from finops.storage.snapshots import get_costs_by_team

    return {(r["team"], r["environment"]): round(r["total_usd"], 2)
            for r in get_costs_by_team(start, end)}


def test_same_team_two_environments_same_day_are_both_kept(fresh_db):
    """The upsert key left out environment, so teamA/dev replaced teamA/prod."""
    from finops.storage.snapshots import store_attributed_cost

    day = date(2026, 9, 1)
    store_attributed_cost("aws", "EC2", "111", "teamA", "prod", day, 100.0)
    store_attributed_cost("aws", "EC2", "111", "teamA", "dev", day, 40.0)

    assert _by_team_env(day, day) == {("teamA", "prod"): 100.0, ("teamA", "dev"): 40.0}


def test_rewriting_the_same_key_still_replaces_it(fresh_db):
    from finops.storage.snapshots import store_attributed_cost

    day = date(2026, 9, 1)
    store_attributed_cost("aws", "EC2", "111", "teamA", "prod", day, 100.0)
    store_attributed_cost("aws", "EC2", "111", "teamA", "prod", day, 120.0)

    assert _by_team_env(day, day) == {("teamA", "prod"): 120.0}
