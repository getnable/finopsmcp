"""Tests for the realized-savings measurement tiers (recommendations/measure.py)
and the verified_basis plumbing through the tracker.

Covers: bill-measured before/after math, the too-fresh guard, the effective-rate
fallback, the list-price floor, the $0-verify regression (unknown instance type
must never bank $0 when the row carries an estimate), the RDS verifier, and the
basis surfacing in get_summary.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from finops.recommendations import measure
from finops.recommendations.measure import measure_realized_savings
from finops.recommendations.verifiers import verify_ec2_change, verify_rds_change


@pytest.fixture
def ledger(monkeypatch):
    td = tempfile.TemporaryDirectory()
    monkeypatch.setenv("FINOPS_DB_PATH", str(Path(td.name) / "t.db"))
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    yield db_mod
    db_mod._ENGINE = None
    td.cleanup()


def _row(days_since_acted=10, est=120.0, resource_id="i-abc123", resource_type="ec2"):
    return SimpleNamespace(
        resource_id=resource_id,
        resource_type=resource_type,
        region="us-east-1",
        estimated_monthly_savings_usd=est,
        acted_on_at=datetime.now(timezone.utc) - timedelta(days=days_since_acted),
    )


# ── Tier 1: bill measured ─────────────────────────────────────────────────────

def test_bill_measured_delta(monkeypatch):
    # before: $10/day, after: $6/day -> ~$121.60/mo saving, basis bill_measured
    monkeypatch.setattr(
        measure, "_resource_daily_cost",
        lambda rid, start, end: 10.0 if start < _row().acted_on_at.date() else 6.0,
    )
    usd, basis = measure_realized_savings(_row(days_since_acted=10), 90.0)
    assert basis == "bill_measured"
    assert usd == pytest.approx((10.0 - 6.0) * measure.DAYS_PER_MONTH, abs=0.01)


def test_bill_measured_negative_records_zero(monkeypatch):
    # Cost went UP after the change: record 0, never a negative saving.
    monkeypatch.setattr(
        measure, "_resource_daily_cost",
        lambda rid, start, end: 5.0 if start < _row().acted_on_at.date() else 9.0,
    )
    usd, basis = measure_realized_savings(_row(days_since_acted=10), 90.0)
    assert (usd, basis) == (0.0, "bill_measured")


def test_too_fresh_skips_bill_tier(monkeypatch):
    # Only 2 days since action: not enough settled data, must not use the bill.
    calls = []
    monkeypatch.setattr(measure, "_resource_daily_cost",
                        lambda *a: calls.append(a) or 10.0)
    monkeypatch.setattr(measure, "_effective_rate", lambda est, row: None)
    usd, basis = measure_realized_savings(_row(days_since_acted=2), 90.0)
    assert basis == "list_price"
    assert usd == 90.0
    assert not calls  # never queried CUR


def test_no_cur_falls_back(monkeypatch):
    monkeypatch.setattr(measure, "_resource_daily_cost", lambda *a: None)
    monkeypatch.setattr(measure, "_effective_rate", lambda est, row: None)
    usd, basis = measure_realized_savings(_row(), 90.0)
    assert (usd, basis) == (90.0, "list_price")


# ── Tier 2: effective rate ────────────────────────────────────────────────────

def test_effective_rate_tier(monkeypatch):
    monkeypatch.setattr(measure, "_resource_daily_cost", lambda *a: None)
    monkeypatch.setattr(measure, "_effective_rate",
                        lambda est, row: (round(est * 0.72, 2), "effective_rate"))
    usd, basis = measure_realized_savings(_row(), 100.0)
    assert (usd, basis) == (72.0, "effective_rate")


# ── $0-verify regression ──────────────────────────────────────────────────────

def test_zero_confirmed_estimate_uses_row_estimate(monkeypatch):
    # Verifier confirmed the change but priced it 0 (type not in the static
    # table). The row's own estimate must be used, never $0.
    monkeypatch.setattr(measure, "_resource_daily_cost", lambda *a: None)
    monkeypatch.setattr(measure, "_effective_rate", lambda est, row: None)
    usd, basis = measure_realized_savings(_row(est=140.0), 0.0)
    assert (usd, basis) == (140.0, "list_price")


def test_verify_ec2_unknown_type_returns_row_estimate():
    row = _row(est=88.0)
    fake_ec2 = SimpleNamespace(describe_instances=lambda **kw: {
        "Reservations": [{"Instances": [{"InstanceType": "z9x.mega"}]}]
    })
    with patch("boto3.client", return_value=fake_ec2):
        # z9x.mega is not in _EC2_HOURLY; old behavior banked $0.
        out = verify_ec2_change("i-abc", {"instance_type": "z9x.mega",
                                          "from_instance_type": "z9x.giga"}, row)
    assert out == 88.0


# ── RDS verifier ──────────────────────────────────────────────────────────────

def test_verify_rds_confirms_class_switch():
    row = _row(est=64.0, resource_id="prod-db", resource_type="rds")
    fake_rds = SimpleNamespace(describe_db_instances=lambda **kw: {
        "DBInstances": [{"DBInstanceClass": "db.r6g.large"}]
    })
    with patch("boto3.client", return_value=fake_rds):
        assert verify_rds_change("prod-db", {"instance_class": "db.r6g.large"}, row) == 64.0
        assert verify_rds_change("prod-db", {"instance_class": "db.r6g.xlarge"}, row) is None


def test_rds_verifier_registered():
    from finops.recommendations.verifiers import get_verifier
    assert get_verifier("rightsizing", "rds") is verify_rds_change


# ── basis plumbing ────────────────────────────────────────────────────────────

def test_mark_verified_persists_basis_and_summary_splits_measured(ledger):
    from finops.recommendations.savings_tracker import (
        get_summary, mark_verified, record_recommendation,
    )
    a = record_recommendation("rightsizing", "aws", "i-1", "ec2", "a", {}, {"t": 1},
                              "desc a", 100.0)
    b = record_recommendation("rightsizing", "aws", "i-2", "ec2", "b", {}, {"t": 2},
                              "desc b", 50.0)
    mark_verified(a, 95.0, basis="bill_measured")
    mark_verified(b, 40.0, basis="effective_rate")
    s = get_summary()
    assert s["verified_monthly_usd"] == 135.0
    assert s["verified_bill_measured_monthly_usd"] == 95.0
