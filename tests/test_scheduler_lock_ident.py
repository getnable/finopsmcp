"""The scheduler single-owner lock must key on the RESOLVED database location.
The old fallback keyed on an env var that is usually unset, so every SQLite
instance on a host shared one "default" lock and all but one silently lost
their digest/anomaly jobs, found live in a cold-run dogfood where a fresh
HOME's instance collided with the dev machine's."""
from __future__ import annotations

import fcntl
import os

from finops.scheduler import jobs


def _reset_data_dir(monkeypatch, path):
    """data_dir() memoizes in a module global; point it fresh at path."""
    import finops.storage.db as db
    monkeypatch.setenv("FINOPS_DATA_DIR", str(path))
    monkeypatch.setattr(db, "_DATA_DIR", None)


def test_different_data_dirs_get_different_locks(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_DB_PATH", raising=False)

    # Instance A in one data dir takes its lock.
    _reset_data_dir(monkeypatch, tmp_path / "a")
    jobs._scheduler_lock_handle = None
    assert jobs._acquire_scheduler_lock() is True
    lock_a = jobs._scheduler_lock_handle
    assert lock_a is not None

    # Instance B in a DIFFERENT data dir must get a DIFFERENT lock file and
    # therefore also succeed (the old "default" ident made this fail).
    _reset_data_dir(monkeypatch, tmp_path / "b")
    jobs._scheduler_lock_handle = None
    assert jobs._acquire_scheduler_lock() is True
    lock_b = jobs._scheduler_lock_handle
    assert lock_b is not None
    assert os.path.realpath(lock_a.name) != os.path.realpath(lock_b.name)

    # cleanup
    for fh in (lock_a, lock_b):
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN); fh.close()
        except Exception:
            pass
    jobs._scheduler_lock_handle = None


def test_same_data_dir_still_excludes(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_DB_PATH", raising=False)
    _reset_data_dir(monkeypatch, tmp_path / "same")

    jobs._scheduler_lock_handle = None
    assert jobs._acquire_scheduler_lock() is True
    held = jobs._scheduler_lock_handle

    # A second acquisition against the same resolved path must FAIL (that is
    # the whole point of the lock).
    jobs._scheduler_lock_handle = None
    assert jobs._acquire_scheduler_lock() is False

    try:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN); held.close()
    except Exception:
        pass
    jobs._scheduler_lock_handle = None
