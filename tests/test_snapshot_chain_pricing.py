"""Old EBS snapshots are priced as an incremental chain, not as full copies.

EC2 reports VolumeSize for a snapshot as the size of the volume it was taken
from: the same figure on every snapshot of that volume. Only the first
snapshot stores the blocks written; each later one stores the blocks changed
since the one before. analyzers/waste.py and cleanup/idle.py priced every
snapshot at the full volume size, so a 500 GB volume with thirty old daily
snapshots read as 15 TB of snapshot storage, $750/mo, and that went into the
headline savings as if it were measured.

Now the oldest snapshot of each volume carries the full-size figure, labelled
an upper bound, and the rest are unpriced and counted. A copied snapshot
(VolumeId vol-ffffffff) shares no chain and is priced on its own.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from finops.analyzers import waste
from finops.analyzers.optimizer import _monthly_savings
from finops.aws_prices import EBS_SNAPSHOT_PER_GB_MONTH

_NOW = datetime.now(timezone.utc)


def _snap(snap_id: str, volume_id: str, days_old: int, size: int = 500) -> dict:
    return {"SnapshotId": snap_id, "VolumeId": volume_id, "VolumeSize": size,
            "StartTime": _NOW - timedelta(days=days_old)}


# Thirty daily snapshots of one volume, two of another, and two copies.
_SNAPS = (
    [_snap(f"snap-a{d:02d}", "vol-a", 40 + d) for d in range(30)]
    + [_snap("snap-b1", "vol-b", 60, size=100), _snap("snap-b2", "vol-b", 50, size=100)]
    + [_snap("snap-copy1", "vol-ffffffff", 45, size=20),
       _snap("snap-copy2", "vol-ffffffff", 45, size=30)]
)


class _EC2:
    def __init__(self, snaps):
        self.snaps = snaps

    def get_paginator(self, name):
        snaps = self.snaps

        class _P:
            def paginate(self, **_kw):
                if name == "describe_images":
                    return iter([{"Images": []}])
                return iter([{"Snapshots": list(snaps)}])
        return _P()

    def describe_images(self, **_kw):
        return {"Images": []}


def test_full_size_goes_to_the_oldest_of_each_volume_and_every_copy():
    assert waste.full_size_snapshot_ids(_SNAPS) == {
        "snap-a29", "snap-b1", "snap-copy1", "snap-copy2"}


def test_waste_prices_one_full_size_per_volume():
    findings = {f["resource_id"]: f for f in waste.check_ebs_snapshots(_EC2(_SNAPS), "us-east-1")}
    assert len(findings) == 34

    oldest = findings["snap-a29"]
    assert oldest["estimated_monthly_savings"] == 500 * EBS_SNAPSHOT_PER_GB_MONTH == 25.0
    assert oldest["price_basis"] == "upper_bound_full_volume_size"
    assert "upper bound" in oldest["detail"]

    later = findings["snap-a00"]
    assert later["estimated_monthly_savings"] is None
    assert later["unpriced"] is True
    assert later["price_basis"] == "incremental_unpriced"
    assert later["volume_id"] == "vol-a"
    assert _monthly_savings(later) is None

    priced = [v for v in (_monthly_savings(f) for f in findings.values()) if v is not None]
    # 500 + 100 + 20 + 30 GB at $0.05, not 30 x 500 + 2 x 100 + 20 + 30.
    assert round(sum(priced), 2) == 32.5
    assert sum(1 for f in findings.values() if f["unpriced"]) == 30


def test_the_audit_total_counts_the_chain_once(monkeypatch):
    import boto3

    from finops.analyzers import optimizer

    class _Session:
        def client(self, service, region_name=None, **_kw):
            if service == "sts":
                class _STS:
                    def get_caller_identity(self):
                        return {"Account": "111122223333"}
                return _STS()
            return _EC2(_SNAPS)

    monkeypatch.setattr(boto3, "Session", lambda *a, **k: _Session())
    report = optimizer.run_deep_audit(regions=["us-east-1"], checks=["snapshots"])
    assert report["total_estimated_monthly_savings"] == 32.5
    assert report["unpriced_findings"] == 30


def test_idle_scan_prices_the_chain_once():
    from finops.cleanup.idle import _scan_old_snapshots, idle_resources_summary

    got = _scan_old_snapshots(_EC2(_SNAPS), "111122223333", "us-east-1", min_idle_days=7)
    by_id = {r.resource_id: r for r in got}
    assert by_id["snap-a29"].monthly_cost_usd == 25.0
    assert by_id["snap-a29"].metadata["price_basis"] == "upper_bound_full_volume_size"
    assert by_id["snap-a00"].monthly_cost_usd == 0.0
    assert by_id["snap-a00"].metadata["unpriced"] is True
    assert "not priced" in by_id["snap-a00"].reason

    summary = idle_resources_summary(got)
    assert summary["total_monthly_waste_usd"] == 32.5
    assert summary["unpriced_count"] == 30
