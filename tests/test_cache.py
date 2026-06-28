"""Tests for the read-through cost cache, including on-disk persistence.

The disk layer is what lets a restarted box (or the next day's first query) serve
the prior fetch instead of re-hitting Cost Explorer. These tests simulate a
restart by clearing the in-memory store and asserting the value still resolves.
"""
import finops.cache as cache


def _fresh(tmp_path, monkeypatch):
    """Point the cache at a tmp data dir and reset module state for a clean run."""
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FINOPS_CACHE_DISABLED", raising=False)
    monkeypatch.delenv("FINOPS_CACHE_DISK_DISABLED", raising=False)
    cache._DISABLED = False
    cache._DISK_DISABLED = False
    cache._disk_ready = False
    cache._store.clear()


def test_memory_hit(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    cache.set("k", {"v": 1}, ttl=cache.COST_TTL)
    assert cache.get("k") == {"v": 1}


def test_long_ttl_survives_memory_clear(tmp_path, monkeypatch):
    """Cost-data TTL persists to disk: a restart (memory cleared) still serves it."""
    _fresh(tmp_path, monkeypatch)
    cache.set("cost", {"total": 42}, ttl=cache.COST_TTL)
    cache._store.clear()  # simulate a process restart: memory gone, disk remains
    assert cache.get("cost") == {"total": 42}
    # and the disk hit repopulated memory
    assert "cost" in cache._store


def test_short_ttl_not_persisted(tmp_path, monkeypatch):
    """Short-lived entries (k8s) are memory-only and do not survive a restart."""
    _fresh(tmp_path, monkeypatch)
    cache.set("k8s", "state", ttl=60)
    cache._store.clear()
    assert cache.get("k8s") is None


def test_expired_disk_entry_is_a_miss(tmp_path, monkeypatch):
    """A disk entry past its expiry returns None (and is pruned), not stale data."""
    _fresh(tmp_path, monkeypatch)
    cache._disk_set("old", expires_at=1.0, value="stale", now=0.0)
    cache._store.clear()
    assert cache.get("old") is None


def test_disk_disabled_stays_memory_only(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    cache._DISK_DISABLED = True
    cache.set("cost", {"total": 7}, ttl=cache.COST_TTL)
    cache._store.clear()
    assert cache.get("cost") is None


def test_clear_wipes_disk(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    cache.set("cost", {"total": 1}, ttl=cache.COST_TTL)
    cache.clear()
    cache._store.clear()
    assert cache.get("cost") is None
