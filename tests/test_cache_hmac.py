"""The on-disk cache authenticates its pickle blobs with an HMAC so a tampered
cache file cannot inject a malicious pickle (code execution on read). These tests
pin the round-trip, the tamper-rejection, and the legacy/unsigned-entry rejection.
"""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

from finops import cache


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FINOPS_CACHE_DISABLED", raising=False)
    monkeypatch.delenv("FINOPS_CACHE_DISK_DISABLED", raising=False)
    # reset module-level key cache between tests
    cache._hmac_key = None
    cache._hmac_key_loaded = False
    cache._disk_ready = False
    return tmp_path


def test_disk_roundtrip_authenticated(data_dir):
    exp = time.time() + 3600
    cache._disk_set("k1", exp, {"cost": 42}, time.time())
    got = cache._disk_get("k1", time.time())
    assert got is not None
    assert got[1] == {"cost": 42}


def test_key_file_is_0600(data_dir):
    cache._disk_set("k", time.time() + 3600, {"x": 1}, time.time())
    keypath = data_dir / "cache.key"
    assert keypath.exists()
    assert (os.stat(keypath).st_mode & 0o777) == 0o600


def test_tampered_blob_is_rejected(data_dir):
    exp = time.time() + 3600
    cache._disk_set("k2", exp, {"secret": "v"}, time.time())
    # Corrupt the stored payload (flip bytes after the 32-byte tag).
    db = data_dir / "cache.db"
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT value FROM kv WHERE key='k2'").fetchone()
    blob = bytes(row[0])
    tampered = blob[:32] + b"\x00" + blob[33:]  # mutate one payload byte
    conn.execute("UPDATE kv SET value=? WHERE key='k2'", (sqlite3.Binary(tampered),))
    conn.commit()
    conn.close()
    # A tampered entry must read as a miss, never unpickle.
    assert cache._disk_get("k2", time.time()) is None


def test_legacy_unsigned_entry_is_rejected(data_dir):
    # Simulate a pre-HMAC row: raw pickle with no tag prefix.
    import pickle
    cache._disk_conn().close()  # ensure table exists
    conn = sqlite3.connect(data_dir / "cache.db")
    raw = pickle.dumps({"old": True})
    conn.execute(
        "INSERT OR REPLACE INTO kv (key, expires_at, value) VALUES (?,?,?)",
        ("legacy", time.time() + 3600, sqlite3.Binary(raw)),
    )
    conn.commit()
    conn.close()
    assert cache._disk_get("legacy", time.time()) is None
