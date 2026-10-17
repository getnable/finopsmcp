"""Verified banked savings is a hero output (Deliverable 3).

Banked = money that actually left the bill (verified rows, measured amount only),
kept clearly distinct from predicted/found opportunity. get_nable_roi leads with
it, verify_savings surfaces the cumulative figure, quality_signal exposes an
explicit banked alias.

"Left the bill" means measured on it: verified_basis == "bill_measured" (CUR
before and after). A verified change priced at the effective rate or list price
is verified, not banked, so the fixtures that mean banked say so.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import finops.server as server


@pytest.fixture(autouse=True)
def _pro_gate(monkeypatch):
    monkeypatch.setattr("finops.server.require_pro", lambda f: None)


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


def _seed(source, status, est=100.0, ver=None, n=1, basis=None):
    from finops.storage.db import get_engine, savings_recommendations
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        for _ in range(n):
            _seq[0] += 1
            conn.execute(savings_recommendations.insert().values(
                source=source, provider="aws", status=status,
                estimated_monthly_savings_usd=est, verified_monthly_savings_usd=ver,
                verified_basis=basis,
                generated_at=now, dedup_key=f"vb{_seq[0]}", resource_id=f"vb{_seq[0]}",
            ))


def _free_plan(monkeypatch):
    monkeypatch.setattr(server, "get_status", lambda: SimpleNamespace(mode="free", plan="solo"))


# ── get_nable_roi leads with banked ───────────────────────────────────────────

def test_roi_leads_with_verified_banked(ledger, monkeypatch):
    _free_plan(monkeypatch)
    _seed("rightsizing", "verified", est=120.0, ver=100.0, n=1, basis="bill_measured")
    out = asyncio.run(server.get_nable_roi())
    # hero line appears before the pipeline breakdown
    summary = out["summary"]
    assert "Verified banked savings" in summary
    assert summary.index("Verified banked savings") < summary.index("Savings pipeline")
    assert out["verified_banked_monthly_usd"] == 100.0
    assert out["verified_banked_annual_usd"] == 1200.0
    assert out["verified_count"] == 1


def test_roi_banked_uses_measured_not_estimate(ledger, monkeypatch):
    """A verified row banks its MEASURED amount, never the (larger) predicted estimate."""
    _free_plan(monkeypatch)
    _seed("idle", "verified", est=500.0, ver=300.0, n=1,   # predicted 500, banked 300
          basis="bill_measured")
    out = asyncio.run(server.get_nable_roi())
    assert out["verified_banked_monthly_usd"] == 300.0     # measured, not 500
    assert out["found_monthly_usd"] >= 500.0               # predicted still shown separately


def test_roi_verified_row_missing_measure_banks_zero(ledger, monkeypatch):
    """A 'verified' row with no measured value banks $0, never falls back to the estimate."""
    _free_plan(monkeypatch)
    _seed("idle", "verified", est=500.0, ver=None, n=1)
    out = asyncio.run(server.get_nable_roi())
    assert out["verified_banked_monthly_usd"] == 0.0


def test_roi_zero_banked_message(ledger, monkeypatch):
    _free_plan(monkeypatch)
    _seed("rightsizing", "acted_on", est=100.0, n=1)  # acted, not verified
    out = asyncio.run(server.get_nable_roi())
    assert "Verified banked savings: $0/mo" in out["summary"]
    assert out["verified_banked_monthly_usd"] == 0.0


# ── verify_savings surfaces cumulative banked ─────────────────────────────────

def test_verify_savings_reports_banked(ledger, monkeypatch):
    from finops.recommendations import verifiers

    # one acted idle EBS volume that is now gone -> verifies to its estimate
    _seed("idle", "acted_on", est=42.5, n=1)
    from finops.storage.db import get_engine, savings_recommendations
    with get_engine().begin() as conn:
        conn.execute(savings_recommendations.update()
                     .values(resource_type="ebs_volume"))

    class _FakeEC2:
        def describe_volumes(self, **_):
            return {"Volumes": []}  # gone

    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeEC2())

    out = asyncio.run(server.verify_savings())
    assert out["verified_count"] == 1
    # The deletion is confirmed, but with no CUR there is no bill before and
    # after: the $42.50 is the volume priced at list rate. Verified, not banked.
    # This used to report it as banked and "confirmed off your bill".
    assert out["newly_verified"][0]["verified_basis"] != "bill_measured"
    assert out["cumulative_verified_monthly_usd"] == 42.5
    assert out["verified_estimated_monthly_usd"] == 42.5
    assert out["verified_banked_monthly_usd"] == 0.0
    assert "banked" in out["message"].lower()
    assert "confirmed off your bill" not in out["message"]


# ── quality_signal exposes an explicit banked alias ───────────────────────────

def test_quality_signal_has_banked_alias(ledger):
    from finops.recommendations.savings_tracker import quality_signal
    _seed("rightsizing", "verified", est=100.0, ver=80.0, n=1, basis="bill_measured")
    q = quality_signal()
    assert q["verified_banked_monthly_usd"] == 80.0
    assert q["verified_banked_annual_usd"] == 960.0
    # banked alias matches the existing verified figure exactly when all of it
    # was measured on the bill
    assert q["verified_banked_monthly_usd"] == q["verified_monthly_usd"]


def test_quality_signal_banks_only_what_the_bill_measured(ledger):
    from finops.recommendations.savings_tracker import quality_signal
    _seed("rightsizing", "verified", est=100.0, ver=80.0, n=1, basis="bill_measured")
    _seed("graviton", "verified", est=30.0, ver=30.0, n=1, basis="list_price")
    q = quality_signal()
    assert q["verified_monthly_usd"] == 110.0
    assert q["verified_banked_monthly_usd"] == 80.0
    assert q["verified_estimated_monthly_usd"] == 30.0
