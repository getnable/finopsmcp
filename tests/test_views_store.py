"""Tests for the pinned-views store (finops.slice.views): saved moldable cards."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def views(monkeypatch):
    """A fresh temp SQLite DB with the dashboard_views table, plus the store module."""
    td = tempfile.TemporaryDirectory()
    monkeypatch.setenv("FINOPS_DB_PATH", str(Path(td.name) / "test.db"))
    import finops.storage.db as db_mod
    db_mod._ENGINE = None  # force a new engine against the temp DB (creates tables)
    from finops.slice import views as v
    yield v
    db_mod._ENGINE = None
    td.cleanup()


def _card(title, dims, metric="EffectiveCost", template="bar", days=30):
    return {
        "title": title, "template": template, "metric": metric, "dimensions": dims,
        "slice": {"dimensions": dims, "metric": metric, "filters": [], "exclusions": []},
        "days": days,
    }


def test_pin_and_list(views):
    a = views.pin_view(_card("EC2 by region", ["RegionId"]))
    b = views.pin_view(_card("Spend by service", ["ServiceName"]))
    assert a and b and a != b
    out = views.list_pinned_views(owner="instance")
    assert [v["title"] for v in out] == ["EC2 by region", "Spend by service"]  # position order
    # the slice round-trips so the card can be re-run
    assert out[0]["slice"]["dimensions"] == ["RegionId"]
    assert out[0]["card"]["days"] == 30


def test_get_pinned_view(views):
    vid = views.pin_view(_card("Daily spend", ["date"], template="line"))
    v = views.get_pinned_view(vid, owner="instance")
    assert v is not None
    assert v["title"] == "Daily spend" and v["template"] == "line"
    assert views.get_pinned_view(99999, owner="instance") is None


def test_unpin_view(views):
    a = views.pin_view(_card("A", ["RegionId"]))
    views.pin_view(_card("B", ["ServiceName"]))
    assert views.unpin_view(a, owner="instance") is True
    titles = [v["title"] for v in views.list_pinned_views(owner="instance")]
    assert titles == ["B"]
    # unpinning a gone id is a no-op
    assert views.unpin_view(a, owner="instance") is False


def test_reorder_views(views):
    a = views.pin_view(_card("A", ["RegionId"]))
    b = views.pin_view(_card("B", ["ServiceName"]))
    c = views.pin_view(_card("C", ["SubAccountId"]))
    views.reorder_views([c, a, b], owner="instance")
    assert [v["title"] for v in views.list_pinned_views(owner="instance")] == ["C", "A", "B"]


def test_me_scope_isolation_plus_shared_instance(views):
    """A 'me' pin is private to its owner; an 'instance' pin is visible to everyone."""
    mine = views.pin_view(_card("Mine", ["RegionId"]), owner="alice", scope="me")
    shared = views.pin_view(_card("Shared", ["ServiceName"]), owner="instance", scope="instance")
    # alice sees her own + the shared instance pin
    alice = {v["title"] for v in views.list_pinned_views(owner="alice")}
    assert alice == {"Mine", "Shared"}
    # bob sees only the shared one, never alice's private pin
    bob = {v["title"] for v in views.list_pinned_views(owner="bob")}
    assert bob == {"Shared"}
    # bob cannot fetch alice's private pin by id
    assert views.get_pinned_view(mine, owner="bob") is None
    assert views.get_pinned_view(shared, owner="bob") is not None
