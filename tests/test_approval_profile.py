"""approval_profile(): learn the dollar size and resource types a customer says
yes to, plus the dismiss-reason capture on dismiss_recommendation.

This is the "richer per-account signal" layer of the learning moat. Everything is
propose-only: it only produces a signal a ranker can read, never mutates anything.
Sparse-data safe: the dollar floor stays None until there are enough acted recs.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

import finops.server as server
from finops.recommendations.learning.signal import (
    approval_profile,
    customer_signal,
    APPROVAL_MIN_ACTED,
    _pctl,
)


@pytest.fixture
def ledger(monkeypatch):
    td = tempfile.TemporaryDirectory()
    monkeypatch.setenv("FINOPS_DB_PATH", str(Path(td.name) / "t.db"))
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    yield db_mod
    db_mod._ENGINE = None
    td.cleanup()


_seq = [0]


def _seed(source, status, est=100.0, resource_type="ec2", reason_category=None, n=1):
    from finops.storage.db import get_engine, savings_recommendations
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        for _ in range(n):
            _seq[0] += 1
            conn.execute(savings_recommendations.insert().values(
                source=source, provider="aws", status=status,
                resource_type=resource_type,
                estimated_monthly_savings_usd=est, verified_monthly_savings_usd=None,
                generated_at=now, dedup_key=f"ap{_seq[0]}", resource_id=f"ap{_seq[0]}",
                dismiss_reason_category=reason_category,
            ))


# ── _pctl helper ──────────────────────────────────────────────────────────────

def test_pctl_empty_and_single():
    assert _pctl([], 0.25) is None
    assert _pctl([42.0], 0.25) == 42.0


def test_pctl_interpolates():
    # 25th percentile of 0,100,200,300 = 75
    assert _pctl([0.0, 100.0, 200.0, 300.0], 0.25) == 75.0


# ── approval_profile ──────────────────────────────────────────────────────────

def test_cold_ledger_gives_no_floor(ledger):
    _seed("rightsizing", "open", n=3)  # nothing resolved
    p = approval_profile()
    assert p["coverage"] == "COLD"
    assert p["approval_floor_usd"] is None
    assert p["acted_count"] == 0
    assert p["by_resource_type"] == []


def test_floor_needs_enough_acted(ledger):
    # Fewer than APPROVAL_MIN_ACTED acted -> no trusted floor yet.
    _seed("rightsizing", "acted_on", est=100.0, n=APPROVAL_MIN_ACTED - 1)
    p = approval_profile()
    assert p["acted_count"] == APPROVAL_MIN_ACTED - 1
    assert p["approval_floor_usd"] is None


def test_floor_is_p25_of_acted(ledger):
    # Acted amounts 100,200,300,400 -> p25 = 175. Dismissed small ones don't move it.
    for amt in (100.0, 200.0, 300.0, 400.0):
        _seed("rightsizing", "acted_on", est=amt)
    _seed("idle", "dismissed", est=5.0, n=3)
    p = approval_profile()
    assert p["approval_floor_usd"] == 175.0
    assert p["acted_median_usd"] == 250.0
    assert p["dismissed_median_usd"] == 5.0


def test_per_resource_type_act_rate(ledger):
    # ec2: acted 3, dismissed 1 -> 0.75; rds: acted 0, dismissed 2 -> 0.0
    _seed("rightsizing", "acted_on", resource_type="ec2", n=3)
    _seed("rightsizing", "dismissed", resource_type="ec2", n=1)
    _seed("rightsizing", "dismissed", resource_type="rds", n=2)
    p = approval_profile()
    by = {t["resource_type"]: t for t in p["by_resource_type"]}
    assert by["ec2"]["act_rate"] == 0.75
    assert by["rds"]["act_rate"] == 0.0
    # highest act-rate ranks first
    assert p["by_resource_type"][0]["resource_type"] == "ec2"


def test_business_dismissals_excluded(ledger):
    # A business-reason dismissal is a choice, not a no: it must not count as a
    # dismissed rec in the profile, and must not pull the type's act-rate down.
    _seed("rightsizing", "acted_on", resource_type="ec2", n=2)
    _seed("rightsizing", "dismissed", resource_type="ec2",
          reason_category="reserved_for_peak", n=5)
    p = approval_profile()
    by = {t["resource_type"]: t for t in p["by_resource_type"]}
    # only the 2 acted count; the 5 business dismissals are excluded
    assert by["ec2"]["resolved"] == 2
    assert by["ec2"]["act_rate"] == 1.0
    assert p["dismissed_median_usd"] is None


def test_profile_surfaces_in_customer_signal(ledger):
    for amt in (100.0, 200.0, 300.0, 400.0):
        _seed("rightsizing", "acted_on", est=amt)
    sig = customer_signal()
    assert "approval_profile" in sig
    assert sig["approval_profile"]["approval_floor_usd"] == 175.0


# ── dismiss_recommendation capture ────────────────────────────────────────────

def _open_rec(ledger, reason_seed_source="rightsizing"):
    from finops.storage.db import get_engine, savings_recommendations
    now = datetime.now(timezone.utc)
    _seq[0] += 1
    with get_engine().begin() as conn:
        res = conn.execute(savings_recommendations.insert().values(
            source=reason_seed_source, provider="aws", status="open",
            resource_type="ec2", estimated_monthly_savings_usd=50.0,
            generated_at=now, dedup_key=f"dr{_seq[0]}", resource_id=f"dr{_seq[0]}",
        ))
        return res.inserted_primary_key[0]


def test_dismiss_echoes_business_category(ledger):
    rid = _open_rec(ledger)
    out = asyncio.run(server.dismiss_recommendation(rid, "we reserve this for peak traffic"))
    assert out["status"] == "dismissed"
    assert out["reason_category"] == "reserved_for_peak"
    assert "not count against" in out["learning_note"]


def test_dismiss_without_reason_nudges(ledger):
    rid = _open_rec(ledger)
    out = asyncio.run(server.dismiss_recommendation(rid, ""))
    assert out["status"] == "dismissed"
    assert out["reason_category"] == "other"
    assert "helps nable learn" in out["learning_note"]
