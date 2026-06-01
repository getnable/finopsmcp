"""Tests for finops.recommendations.rds_snapshots."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.rds_snapshots import (
    _build_snapshot_record,
    _get_active_db_identifiers,
    _snapshot_monthly_cost,
    audit_rds_manual_snapshots,
)


# ── unit tests for helpers ─────────────────────────────────────────────────────

class TestSnapshotMonthlyCost:
    def test_small_snapshot(self):
        assert _snapshot_monthly_cost(10.0) == round(10.0 * 0.095, 4)

    def test_zero_size(self):
        assert _snapshot_monthly_cost(0.0) == 0.0

    def test_large_snapshot(self):
        result = _snapshot_monthly_cost(1000.0)
        assert result == round(1000.0 * 0.095, 4)


class TestGetActiveDbIdentifiers:
    def test_returns_instance_identifiers(self):
        rds = MagicMock()
        rds.get_paginator.return_value.paginate.return_value = [
            {"DBInstances": [
                {"DBInstanceIdentifier": "mydb-prod"},
                {"DBInstanceIdentifier": "mydb-dev"},
            ]}
        ]
        result = _get_active_db_identifiers(rds)
        assert "mydb-prod" in result
        assert "mydb-dev" in result

    def test_handles_paginator_exception_gracefully(self):
        rds = MagicMock()
        rds.get_paginator.side_effect = Exception("access denied")
        result = _get_active_db_identifiers(rds)
        assert isinstance(result, set)
        assert len(result) == 0

    def test_includes_cluster_identifiers(self):
        rds = MagicMock()
        # First paginator call (describe_db_instances) returns instances
        inst_pag = MagicMock()
        inst_pag.paginate.return_value = [
            {"DBInstances": [{"DBInstanceIdentifier": "instance-1"}]}
        ]
        # Second paginator call (describe_db_clusters) returns clusters
        cluster_pag = MagicMock()
        cluster_pag.paginate.return_value = [
            {"DBClusters": [{"DBClusterIdentifier": "aurora-cluster-1"}]}
        ]
        rds.get_paginator.side_effect = [inst_pag, cluster_pag]

        result = _get_active_db_identifiers(rds)
        assert "instance-1" in result
        assert "aurora-cluster-1" in result


class TestBuildSnapshotRecord:
    def _now(self):
        return datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)

    def _make_snap(self, snap_id: str, db_id: str, size_gb: int, days_old: int) -> dict:
        created = self._now() - timedelta(days=days_old)
        return {
            "DBSnapshotIdentifier": snap_id,
            "DBInstanceIdentifier": db_id,
            "AllocatedStorage": size_gb,
            "Status": "available",
            "SnapshotCreateTime": created,
        }

    def test_orphaned_when_db_missing(self):
        snap = self._make_snap("snap-1", "deleted-db", 100, 90)
        active = {"other-db"}
        record = _build_snapshot_record(snap, "us-east-1", self._now(), active, 30)
        assert record["is_orphaned"] is True
        assert record["is_old"] is True

    def test_not_orphaned_when_db_exists(self):
        snap = self._make_snap("snap-2", "live-db", 50, 45)
        active = {"live-db"}
        record = _build_snapshot_record(snap, "us-east-1", self._now(), active, 30)
        assert record["is_orphaned"] is False

    def test_old_flag_based_on_threshold(self):
        snap_old = self._make_snap("snap-old", "live-db", 50, 31)
        snap_new = self._make_snap("snap-new", "live-db", 50, 10)
        active = {"live-db"}
        now = self._now()

        record_old = _build_snapshot_record(snap_old, "us-east-1", now, active, 30)
        record_new = _build_snapshot_record(snap_new, "us-east-1", now, active, 30)

        assert record_old["is_old"] is True
        assert record_new["is_old"] is False

    def test_monthly_cost_calculated(self):
        snap = self._make_snap("snap-3", "db", 200, 10)
        active = {"db"}
        record = _build_snapshot_record(snap, "us-east-1", self._now(), active, 30)
        assert record["monthly_cost"] == round(200 * 0.095, 4)

    def test_handles_missing_snapshot_time(self):
        snap = {
            "DBSnapshotIdentifier": "snap-notimestamp",
            "DBInstanceIdentifier": "db",
            "AllocatedStorage": 10,
            "Status": "available",
        }
        active = {"db"}
        record = _build_snapshot_record(snap, "us-east-1", self._now(), active, 30)
        assert record["age_days"] == 0
        assert record["created_at"] == ""

    def test_region_stored_in_record(self):
        snap = self._make_snap("snap-4", "db", 10, 5)
        active = {"db"}
        record = _build_snapshot_record(snap, "eu-west-1", self._now(), active, 30)
        assert record["region"] == "eu-west-1"

    def test_all_required_fields_present(self):
        snap = self._make_snap("snap-5", "db", 50, 20)
        active = {"db"}
        record = _build_snapshot_record(snap, "us-east-1", self._now(), active, 30)
        required = {
            "snapshot_id", "db_identifier", "size_gb", "age_days",
            "monthly_cost", "status", "is_orphaned", "region", "created_at", "is_old",
        }
        assert required <= set(record.keys())


# ── integration-style tests for audit_rds_manual_snapshots ───────────────────

def _make_aws_client():
    return MagicMock()


def _run(coro):
    return asyncio.run(coro)


class TestAuditRdsManualSnapshots:
    def test_no_boto3_returns_error(self):
        with patch("finops.recommendations.rds_snapshots.boto3", None):
            result = _run(
                audit_rds_manual_snapshots(
                    aws_client=_make_aws_client(), regions=["us-east-1"]
                )
            )
        assert "error" in result

    def test_empty_regions_returns_empty(self):
        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_rds = MagicMock()
        mock_ec2.describe_regions.return_value = {"Regions": []}
        mock_rds.get_paginator.return_value.paginate.return_value = []
        mock_boto3.client.side_effect = lambda svc, **kw: mock_ec2 if svc == "ec2" else mock_rds

        with patch("finops.recommendations.rds_snapshots.boto3", mock_boto3):
            result = _run(
                audit_rds_manual_snapshots(aws_client=_make_aws_client(), regions=[])
            )

        assert result["total_snapshots"] == 0
        assert result["orphaned_snapshots"] == []
        assert result["old_snapshots"] == []
        assert result["total_monthly_cost"] == 0.0
        assert result["potential_monthly_savings"] == 0.0

    def _make_mock_boto3_with_snapshots(self, snapshots: list[dict], active_dbs: list[str]):
        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_rds = MagicMock()
        mock_ec2.describe_regions.return_value = {"Regions": [{"RegionName": "us-east-1"}]}

        # describe_db_instances paginator
        inst_pag = MagicMock()
        inst_pag.paginate.return_value = [
            {"DBInstances": [{"DBInstanceIdentifier": db} for db in active_dbs]}
        ]
        # describe_db_clusters paginator
        cluster_pag = MagicMock()
        cluster_pag.paginate.return_value = [{"DBClusters": []}]
        # describe_db_snapshots paginator
        snap_pag = MagicMock()
        snap_pag.paginate.return_value = [{"DBSnapshots": snapshots}]

        mock_rds.get_paginator.side_effect = [inst_pag, cluster_pag, snap_pag]

        mock_boto3.client.side_effect = lambda svc, **kw: mock_ec2 if svc == "ec2" else mock_rds
        return mock_boto3

    def test_orphaned_snapshot_identified(self):
        now = datetime.now(timezone.utc)
        snaps = [
            {
                "DBSnapshotIdentifier": "snap-orphan",
                "DBInstanceIdentifier": "deleted-db",
                "AllocatedStorage": 100,
                "Status": "available",
                "SnapshotCreateTime": now - timedelta(days=60),
            }
        ]
        mock_boto3 = self._make_mock_boto3_with_snapshots(snaps, active_dbs=[])

        with patch("finops.recommendations.rds_snapshots.boto3", mock_boto3):
            result = _run(
                audit_rds_manual_snapshots(
                    aws_client=_make_aws_client(), regions=["us-east-1"]
                )
            )

        assert result["total_snapshots"] == 1
        assert len(result["orphaned_snapshots"]) == 1
        assert result["orphaned_snapshots"][0]["snapshot_id"] == "snap-orphan"
        assert result["potential_monthly_savings"] > 0

    def test_old_snapshot_identified(self):
        now = datetime.now(timezone.utc)
        snaps = [
            {
                "DBSnapshotIdentifier": "snap-old",
                "DBInstanceIdentifier": "live-db",
                "AllocatedStorage": 50,
                "Status": "available",
                "SnapshotCreateTime": now - timedelta(days=45),
            }
        ]
        mock_boto3 = self._make_mock_boto3_with_snapshots(snaps, active_dbs=["live-db"])

        with patch("finops.recommendations.rds_snapshots.boto3", mock_boto3):
            result = _run(
                audit_rds_manual_snapshots(
                    aws_client=_make_aws_client(),
                    regions=["us-east-1"],
                    age_threshold_days=30,
                )
            )

        assert len(result["old_snapshots"]) == 1
        assert result["old_snapshots"][0]["snapshot_id"] == "snap-old"
        assert len(result["orphaned_snapshots"]) == 0

    def test_recent_snapshot_not_flagged(self):
        now = datetime.now(timezone.utc)
        snaps = [
            {
                "DBSnapshotIdentifier": "snap-recent",
                "DBInstanceIdentifier": "live-db",
                "AllocatedStorage": 50,
                "Status": "available",
                "SnapshotCreateTime": now - timedelta(days=5),
            }
        ]
        mock_boto3 = self._make_mock_boto3_with_snapshots(snaps, active_dbs=["live-db"])

        with patch("finops.recommendations.rds_snapshots.boto3", mock_boto3):
            result = _run(
                audit_rds_manual_snapshots(
                    aws_client=_make_aws_client(),
                    regions=["us-east-1"],
                    age_threshold_days=30,
                )
            )

        assert len(result["orphaned_snapshots"]) == 0
        assert len(result["old_snapshots"]) == 0
        assert result["potential_monthly_savings"] == 0.0

    def test_total_cost_aggregated_across_snapshots(self):
        now = datetime.now(timezone.utc)
        snaps = [
            {
                "DBSnapshotIdentifier": "snap-a",
                "DBInstanceIdentifier": "db-a",
                "AllocatedStorage": 100,
                "Status": "available",
                "SnapshotCreateTime": now - timedelta(days=5),
            },
            {
                "DBSnapshotIdentifier": "snap-b",
                "DBInstanceIdentifier": "db-b",
                "AllocatedStorage": 200,
                "Status": "available",
                "SnapshotCreateTime": now - timedelta(days=60),
            },
        ]
        mock_boto3 = self._make_mock_boto3_with_snapshots(snaps, active_dbs=["db-a", "db-b"])

        with patch("finops.recommendations.rds_snapshots.boto3", mock_boto3):
            result = _run(
                audit_rds_manual_snapshots(
                    aws_client=_make_aws_client(), regions=["us-east-1"]
                )
            )

        expected_total = round((100 + 200) * 0.095, 2)
        assert result["total_monthly_cost"] == expected_total
        assert result["total_snapshots"] == 2
        assert result["total_size_gb"] == 300.0

    def test_orphaned_sorted_by_cost_descending(self):
        now = datetime.now(timezone.utc)
        snaps = [
            {
                "DBSnapshotIdentifier": "snap-small",
                "DBInstanceIdentifier": "gone-db",
                "AllocatedStorage": 10,
                "Status": "available",
                "SnapshotCreateTime": now - timedelta(days=60),
            },
            {
                "DBSnapshotIdentifier": "snap-big",
                "DBInstanceIdentifier": "gone-db",
                "AllocatedStorage": 1000,
                "Status": "available",
                "SnapshotCreateTime": now - timedelta(days=60),
            },
        ]
        mock_boto3 = self._make_mock_boto3_with_snapshots(snaps, active_dbs=[])

        with patch("finops.recommendations.rds_snapshots.boto3", mock_boto3):
            result = _run(
                audit_rds_manual_snapshots(
                    aws_client=_make_aws_client(), regions=["us-east-1"]
                )
            )

        orphaned = result["orphaned_snapshots"]
        assert len(orphaned) == 2
        assert orphaned[0]["monthly_cost"] >= orphaned[1]["monthly_cost"]
        assert orphaned[0]["snapshot_id"] == "snap-big"

    def test_custom_age_threshold(self):
        now = datetime.now(timezone.utc)
        snaps = [
            {
                "DBSnapshotIdentifier": "snap-10d",
                "DBInstanceIdentifier": "db",
                "AllocatedStorage": 50,
                "Status": "available",
                "SnapshotCreateTime": now - timedelta(days=10),
            }
        ]
        mock_boto3 = self._make_mock_boto3_with_snapshots(snaps, active_dbs=["db"])

        with patch("finops.recommendations.rds_snapshots.boto3", mock_boto3):
            # With threshold=7, 10-day-old snapshot should be flagged
            result = _run(
                audit_rds_manual_snapshots(
                    aws_client=_make_aws_client(),
                    regions=["us-east-1"],
                    age_threshold_days=7,
                )
            )

        assert len(result["old_snapshots"]) == 1

    def test_region_scan_failure_does_not_crash(self):
        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_rds = MagicMock()
        mock_rds.get_paginator.side_effect = Exception("connection timeout")
        mock_boto3.client.side_effect = lambda svc, **kw: mock_ec2 if svc == "ec2" else mock_rds

        with patch("finops.recommendations.rds_snapshots.boto3", mock_boto3):
            result = _run(
                audit_rds_manual_snapshots(
                    aws_client=_make_aws_client(), regions=["us-east-1"]
                )
            )

        # Should return empty results, not raise
        assert result["total_snapshots"] == 0
        assert "error" not in result
