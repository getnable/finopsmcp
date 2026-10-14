"""The efficiency scorecard grades only what it read.

1. Tag hygiene was computed as total spend minus attributed spend whenever there
   was any spend, so an install that had never run attribution read as 100%
   untagged: on $1,000/day of snapshots, "$28,000/month (100% of spend) cannot
   be attributed to a team or service", graded 0 (F) and counted toward the
   overall grade. No attribution rows is "not measured", not "nothing tagged".

2. The waste dimension was fed every active inventory row whose cost is $0.00
   as an "idle resource". Zero-cost rows add $0 of waste and still made the
   dimension measured, so it scored 100 ("Minimal detected waste, good
   discipline") off rows that carry no waste signal at all.

3. With neither waste nor commitments measured the summary said "Estimated $0/
   month recoverable", which reads as a finding of nothing to recover.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from finops import server


@pytest.fixture
def db(tmp_path, monkeypatch):
    from finops.storage import db as db_mod

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(db_mod, "_ENGINE", None)
    # No cloud reads from a unit test: commitments and Kubernetes answer "nothing".
    import finops.recommendations.commitments as cm
    monkeypatch.setattr(cm, "analyze_commitments", lambda **kw: None)
    from finops.storage.snapshots import store_snapshots
    today = date.today()  # noqa: DTZ011 - the tool reads the local date
    store_snapshots([{"provider": "aws", "service": "Amazon EC2", "account_id": "1",
                      "region": "us-east-1", "snapshot_date": today - timedelta(days=i),
                      "amount_usd": 1000.0} for i in range(1, 29)])
    yield db_mod
    db_mod._ENGINE = None


def _dim(card, name):
    return next(d for d in card["dimensions"] if d["name"] == name)


def test_tags_are_not_graded_before_attribution_has_run(db):
    card = asyncio.run(server.get_efficiency_scorecard())
    tags = _dim(card, "tag_hygiene")
    assert tags["data_available"] is False, tags
    assert not any("100%" in f for f in tags["findings"]), tags


def test_tags_are_graded_once_attribution_has_run(db):
    from finops.storage.snapshots import store_attributed_cost
    today = date.today()  # noqa: DTZ011 - the tool reads the local date
    for i in range(1, 29):
        store_attributed_cost("aws", "Amazon EC2", "1", "platform", "prod",
                              today - timedelta(days=i), 250.0)
        store_attributed_cost("aws", "Amazon EC2", "1", "unattributed", "",
                              today - timedelta(days=i), 750.0)
    tags = _dim(asyncio.run(server.get_efficiency_scorecard()), "tag_hygiene")
    assert tags["data_available"] is True
    assert tags["metadata"]["untagged_pct"] == 75.0, tags


def test_zero_cost_inventory_rows_are_not_waste_evidence(db):
    from finops.storage.db import get_engine, resource_inventory
    with get_engine().begin() as conn:
        for i in range(3):
            conn.execute(resource_inventory.insert().values(
                provider="aws", account_id="1", region="us-east-1",
                resource_id=f"i-{i}", resource_type="ec2", resource_name="",
                tags="{}", monthly_cost_usd=0.0, first_seen="2026-01-01",
                last_seen="2026-10-01", is_active=True, metadata="{}"))
    waste = _dim(asyncio.run(server.get_efficiency_scorecard()), "waste_reduction")
    assert waste["data_available"] is False, waste
    assert waste["score"] != 100.0, waste


def test_recoverable_is_not_reported_as_zero_when_nothing_was_measured():
    from finops.scoring.scorecard import build_scorecard

    card = build_scorecard(scope="t-unmeasured", label="Test")
    assert "$0/month recoverable" not in card.summary, card.summary
    assert "not measured" in card.summary, card.summary
