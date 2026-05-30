"""Tests for finops.recommendations.ebs_snapshot_replication."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.ebs_snapshot_replication import (
    _MAX_COPY_REGIONS,
    _SNAPSHOT_STORAGE_COST_PER_GB,
    _build_cross_region_findings,
    _list_snapshots_in_region,
    _live_volume_ids,
    _snapshot_monthly_cost,
    audit_ebs_snapshot_replication,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_aws_client():
    return MagicMock()


def _now():
    return datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)


def _make_snap(
    snap_id: str,
    vol_id: str,
    size_gb: int,
    days_old: int,
    now: datetime | None = None,
) -> dict:
    if now is None:
        now = _now()
    return {
        "SnapshotId": snap_id,
        "VolumeId": vol_id,
        "VolumeSize": size_gb,
        "StartTime": now - timedelta(days=days_old),
        "State": "completed",
        "Description": "",
    }


# ── unit: _snapshot_monthly_cost ─────────────────────────────────────────────

class TestSnapshotMonthlyCost:
    def test_single_region(self):
        result = _snapshot_monthly_cost(100.0, 1)
        assert result == round(100.0 * _SNAPSHOT_STORAGE_COST_PER_GB, 4)

    def test_three_regions_triples_cost(self):
        one = _snapshot_monthly_cost(50.0, 1)
        three = _snapshot_monthly_cost(50.0, 3)
        assert abs(three - one * 3) < 0.001

    def test_zero_size(self):
        assert _snapshot_monthly_cost(0.0, 5) == 0.0


# ── unit: _list_snapshots_in_region ──────────────────────────────────────────

class TestListSnapshotsInRegion:
    def test_returns_snapshots(self):
        ec2 = MagicMock()
        ec2.get_paginator.return_value.paginate.return_value = [
            {"Snapshots": [{"SnapshotId": "snap-abc"}, {"SnapshotId": "snap-def"}]}
        ]
        result = _list_snapshots_in_region(ec2)
        assert len(result) == 2
        assert result[0]["SnapshotId"] == "snap-abc"

    def test_returns_empty_on_exception(self):
        ec2 = MagicMock()
        ec2.get_paginator.side_effect = Exception("not authorized")
        result = _list_snapshots_in_region(ec2)
        assert result == []


# ── unit: _live_volume_ids ────────────────────────────────────────────────────

class TestLiveVolumeIds:
    def test_returns_volume_ids(self):
        ec2 = MagicMock()
        ec2.get_paginator.return_value.paginate.return_value = [
            {"Volumes": [{"VolumeId": "vol-111"}, {"VolumeId": "vol-222"}]}
        ]
        result = _live_volume_ids(ec2)
        assert "vol-111" in result
        assert "vol-222" in result

    def test_returns_empty_on_exception(self):
        ec2 = MagicMock()
        ec2.get_paginator.side_effect = Exception("access denied")
        result = _live_volume_ids(ec2)
        assert isinstance(result, set)
        assert len(result) == 0


# ── unit: _build_cross_region_findings ───────────────────────────────────────

class TestBuildCrossRegionFindings:
    def test_single_region_not_flagged(self):
        snaps = {"us-east-1": [_make_snap("snap-1", "vol-abc", 100, 10)]}
        live = {"us-east-1": {"vol-abc"}}
        result = _build_cross_region_findings(snaps, live, _now())
        assert result == []

    def test_two_region_volume_detected(self):
        snaps = {
            "us-east-1": [_make_snap("snap-1", "vol-xyz", 200, 30)],
            "eu-west-1": [_make_snap("snap-2", "vol-xyz", 200, 10)],
        }
        live = {"us-east-1": {"vol-xyz"}, "eu-west-1": {"vol-xyz"}}
        result = _build_cross_region_findings(snaps, live, _now())
        assert len(result) == 1
        assert result[0]["volume_id"] == "vol-xyz"
        assert len(result[0]["copy_regions"]) == 2

    def test_orphaned_when_volume_gone(self):
        snaps = {
            "us-east-1": [_make_snap("snap-1", "vol-dead", 100, 60)],
            "eu-west-1": [_make_snap("snap-2", "vol-dead", 100, 50)],
        }
        live: dict = {"us-east-1": set(), "eu-west-1": set()}
        result = _build_cross_region_findings(snaps, live, _now())
        assert len(result) == 1
        assert result[0]["orphaned"] is True

    def test_not_orphaned_when_volume_exists_in_any_region(self):
        snaps = {
            "us-east-1": [_make_snap("snap-1", "vol-live", 100, 60)],
            "eu-west-1": [_make_snap("snap-2", "vol-live", 100, 50)],
        }
        live = {"us-east-1": {"vol-live"}, "eu-west-1": set()}
        result = _build_cross_region_findings(snaps, live, _now())
        assert result[0]["orphaned"] is False

    def test_excess_copies_flag(self):
        snaps = {
            f"region-{i}": [_make_snap(f"snap-{i}", "vol-many", 50, 10)]
            for i in range(_MAX_COPY_REGIONS + 2)
        }
        live = {r: {"vol-many"} for r in snaps}
        result = _build_cross_region_findings(snaps, live, _now())
        assert result[0]["excess_copies"] is True

    def test_within_limit_no_excess_flag(self):
        snaps = {
            f"region-{i}": [_make_snap(f"snap-{i}", "vol-ok", 50, 10)]
            for i in range(_MAX_COPY_REGIONS)
        }
        live = {r: {"vol-ok"} for r in snaps}
        result = _build_cross_region_findings(snaps, live, _now())
        assert result[0]["excess_copies"] is False

    def test_old_copies_detected(self):
        now = _now()
        snaps = {
            "us-east-1": [_make_snap("snap-old", "vol-mixed", 100, 100, now)],
            "eu-west-1": [_make_snap("snap-new", "vol-mixed", 100, 5, now)],
        }
        live = {"us-east-1": {"vol-mixed"}, "eu-west-1": {"vol-mixed"}}
        result = _build_cross_region_findings(snaps, live, now)
        assert result[0]["has_old_copies"] is True

    def test_total_cost_accounts_for_all_regions(self):
        size_gb = 200
        num_regions = 2
        snaps = {
            "us-east-1": [_make_snap("snap-1", "vol-cost", size_gb, 10)],
            "us-west-2": [_make_snap("snap-2", "vol-cost", size_gb, 5)],
        }
        live = {"us-east-1": {"vol-cost"}, "us-west-2": {"vol-cost"}}
        result = _build_cross_region_findings(snaps, live, _now())
        expected = round(size_gb * _SNAPSHOT_STORAGE_COST_PER_GB * num_regions, 4)
        assert result[0]["total_monthly_cost"] == expected

    def test_sorted_by_cost_descending(self):
        snaps = {
            "us-east-1": [
                _make_snap("snap-cheap-1", "vol-cheap", 10, 5),
                _make_snap("snap-exp-1", "vol-expensive", 1000, 5),
            ],
            "eu-west-1": [
                _make_snap("snap-cheap-2", "vol-cheap", 10, 3),
                _make_snap("snap-exp-2", "vol-expensive", 1000, 3),
            ],
        }
        live = {r: {"vol-cheap", "vol-expensive"} for r in snaps}
        result = _build_cross_region_findings(snaps, live, _now())
        assert len(result) == 2
        assert result[0]["total_monthly_cost"] >= result[1]["total_monthly_cost"]


# ── integration: audit_ebs_snapshot_replication ───────────────────────────────

class TestAuditEbsSnapshotReplication:
    def test_returns_error_when_boto3_missing(self):
        with patch("finops.recommendations.ebs_snapshot_replication.boto3", None):
            result = _run(
                audit_ebs_snapshot_replication(aws_client=_make_aws_client(), regions=["us-east-1"])
            )
        assert "error" in result

    def test_no_snapshots_returns_empty(self):
        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_ec2.get_paginator.return_value.paginate.return_value = []
        mock_boto3.client.return_value = mock_ec2

        with patch("finops.recommendations.ebs_snapshot_replication.boto3", mock_boto3):
            result = _run(
                audit_ebs_snapshot_replication(
                    aws_client=_make_aws_client(), regions=["us-east-1"]
                )
            )

        assert result["total_volume_sets"] == 0
        assert result["cross_region_findings"] == []
        assert result["total_cross_region_cost"] == 0.0

    def test_cross_region_snapshot_identified(self):
        now = datetime.now(timezone.utc)
        snap_us = {
            "SnapshotId": "snap-us",
            "VolumeId": "vol-abc",
            "VolumeSize": 100,
            "StartTime": now - timedelta(days=30),
            "State": "completed",
            "Description": "",
        }
        snap_eu = {
            "SnapshotId": "snap-eu",
            "VolumeId": "vol-abc",
            "VolumeSize": 100,
            "StartTime": now - timedelta(days=10),
            "State": "completed",
            "Description": "Copied from us-east-1",
        }

        def _make_ec2_client(region):
            ec2 = MagicMock()
            # snapshot paginator
            snap_data = {"us-east-1": [snap_us], "eu-west-1": [snap_eu]}
            snap_pag = MagicMock()
            snap_pag.paginate.return_value = [{"Snapshots": snap_data.get(region, [])}]
            # volume paginator
            vol_pag = MagicMock()
            vol_pag.paginate.return_value = [
                {"Volumes": [{"VolumeId": "vol-abc"}]}
            ]
            ec2.get_paginator.side_effect = lambda name: (
                snap_pag if name == "describe_snapshots" else vol_pag
            )
            return ec2

        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = lambda svc, region_name: _make_ec2_client(region_name)

        with patch("finops.recommendations.ebs_snapshot_replication.boto3", mock_boto3):
            result = _run(
                audit_ebs_snapshot_replication(
                    aws_client=_make_aws_client(),
                    regions=["us-east-1", "eu-west-1"],
                )
            )

        assert result["total_volume_sets"] == 1
        finding = result["cross_region_findings"][0]
        assert finding["volume_id"] == "vol-abc"
        assert len(finding["copy_regions"]) == 2
        assert result["total_cross_region_cost"] > 0

    def test_region_failure_does_not_crash(self):
        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = Exception("connection error")

        with patch("finops.recommendations.ebs_snapshot_replication.boto3", mock_boto3):
            result = _run(
                audit_ebs_snapshot_replication(
                    aws_client=_make_aws_client(), regions=["us-east-1"]
                )
            )

        assert "error" not in result
        assert result["total_volume_sets"] == 0
