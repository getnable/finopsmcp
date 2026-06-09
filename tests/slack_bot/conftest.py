"""Shared fixtures for Slack bot tests: isolated SQLite database per test."""
from __future__ import annotations

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Point the storage layer at a throwaway SQLite DB and reset engine caches."""
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)

    from finops.storage import db as db_mod

    old_engine, old_dir = db_mod._ENGINE, db_mod._DATA_DIR
    db_mod._ENGINE = None
    db_mod._DATA_DIR = None
    yield
    db_mod._ENGINE = None
    db_mod._DATA_DIR = None
    db_mod._ENGINE, db_mod._DATA_DIR = old_engine, old_dir
