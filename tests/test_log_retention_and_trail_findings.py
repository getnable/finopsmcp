"""What the log-retention and CloudTrail findings may claim.

check_cloudwatch_logs priced a never-expire log group's saving at its whole
stored volume x $0.03/GB-month. Setting a retention of N days deletes only
what is older than N days: a group created ten days ago, told to keep thirty,
loses nothing, and it used to read as saving all of its storage.

duplicate_cloudtrail_management_events carries a flat $20/mo ("rough estimate
per extra trail" in the code), and waste_evidence stamped it MEASURED at high
confidence. The extra copies bill $2 per 100,000 events, and the event count
is not read.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from finops.analyzers import waste, waste_evidence


class _Logs:
    def __init__(self, groups):
        self.groups = groups

    def get_paginator(self, _name):
        groups = self.groups

        class _P:
            def paginate(self, **_kw):
                return iter([{"logGroups": groups}])
        return _P()


def _group(name: str, gb: float, age_days: int) -> dict:
    created = datetime.now(UTC) - timedelta(days=age_days)
    return {"logGroupName": name, "storedBytes": int(gb * 1024 ** 3),
            "creationTime": int(created.timestamp() * 1000)}


def test_a_group_younger_than_the_retention_saves_nothing_yet():
    [f] = waste.check_cloudwatch_logs(_Logs([_group("/aws/ecs/api", 100, 10)]), "us-east-1")
    assert f["recommended_retention_days"] == 30
    assert f["estimated_monthly_savings"] == 0.0


def test_an_older_group_is_priced_as_an_upper_bound():
    [f] = waste.check_cloudwatch_logs(_Logs([_group("/aws/ecs/api", 100, 400)]), "us-east-1")
    assert f["estimated_monthly_savings"] == 3.0
    assert f["price_basis"] == "upper_bound_all_stored_bytes"
    assert "up to" in f["detail"]


def test_a_flat_trail_estimate_is_not_measured():
    spec = waste_evidence.spec_for("duplicate_cloudtrail_management_events")
    assert spec.evidence == waste_evidence.INFERRED
    assert spec.why_unsure
