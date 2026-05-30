"""Tests for finops.recommendations.efs_cross_az."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, call, patch

import pytest

from finops.recommendations.efs_cross_az import (
    BYTES_PER_GB,
    CROSS_AZ_COST_PER_GB,
    _find_instances_in_other_az,
    audit_efs_cross_az_mounts,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_aws_client():
    client = MagicMock()
    client._session = None
    return client


def _make_fs(fs_id: str, name: str | None = None) -> dict:
    tags = [{"Key": "Name", "Value": name}] if name else []
    return {"FileSystemId": fs_id, "Tags": tags}


def _make_mount_target(mt_id: str, fs_id: str, az: str) -> dict:
    return {"MountTargetId": mt_id, "FileSystemId": fs_id, "AvailabilityZoneName": az}


def _make_instance(instance_id: str, az: str) -> dict:
    return {"InstanceId": instance_id, "Placement": {"AvailabilityZone": az}}


# ── unit: _find_instances_in_other_az ────────────────────────────────────────

def test_find_instances_in_other_az_returns_cross_az_only():
    ec2_client = MagicMock()
    ec2_client.describe_instances.return_value = {
        "Reservations": [
            {"Instances": [_make_instance("i-same", "us-east-1a")]},
            {"Instances": [_make_instance("i-other", "us-east-1b")]},
        ]
    }
    result = _find_instances_in_other_az(ec2_client, ["sg-abc"], "us-east-1a")
    assert result == ["i-other"]


def test_find_instances_returns_empty_when_no_sgs():
    ec2_client = MagicMock()
    result = _find_instances_in_other_az(ec2_client, [], "us-east-1a")
    assert result == []
    ec2_client.describe_instances.assert_not_called()


def test_find_instances_returns_empty_when_all_same_az():
    ec2_client = MagicMock()
    ec2_client.describe_instances.return_value = {
        "Reservations": [
            {"Instances": [_make_instance("i-1", "us-east-1a")]},
            {"Instances": [_make_instance("i-2", "us-east-1a")]},
        ]
    }
    result = _find_instances_in_other_az(ec2_client, ["sg-xyz"], "us-east-1a")
    assert result == []


# ── unit: cost formula ────────────────────────────────────────────────────────

def test_cross_az_cost_formula():
    transfer_gb = 100.0
    expected = transfer_gb * CROSS_AZ_COST_PER_GB
    assert abs(expected - 2.0) < 0.001


def test_bytes_per_gb_constant():
    assert BYTES_PER_GB == 1024 ** 3


# ── integration: no file systems returns empty ────────────────────────────────

def test_returns_empty_when_no_file_systems():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        efs_client = MagicMock()
        ec2_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: {
            "efs": efs_client, "ec2": ec2_client, "cloudwatch": cw_client
        }.get(svc, MagicMock())
        efs_client.describe_file_systems.return_value = {"FileSystems": []}

        result = _run(audit_efs_cross_az_mounts(aws_client=aws_client, regions=["us-east-1"]))

    assert result == []


# ── integration: cross-AZ instance flagged ───────────────────────────────────

def test_cross_az_instance_flagged():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session

        efs_client = MagicMock()
        ec2_client = MagicMock()
        cw_client = MagicMock()

        session.client.side_effect = lambda svc, **kw: {
            "efs": efs_client, "ec2": ec2_client, "cloudwatch": cw_client
        }.get(svc, MagicMock())

        efs_client.describe_file_systems.return_value = {
            "FileSystems": [_make_fs("fs-abc123", name="shared-data")]
        }
        efs_client.describe_mount_targets.return_value = {
            "MountTargets": [_make_mount_target("fsmt-001", "fs-abc123", "us-east-1a")]
        }
        efs_client.describe_mount_target_security_groups.return_value = {
            "SecurityGroups": ["sg-efs"]
        }
        ec2_client.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [_make_instance("i-cross", "us-east-1b")]}
            ]
        }
        # 200 GB total I/O (100 read + 100 write)
        io_bytes = 100 * 1024 ** 3
        cw_client.get_metric_statistics.return_value = {
            "Datapoints": [{"Sum": io_bytes}]
        }

        result = _run(audit_efs_cross_az_mounts(aws_client=aws_client, regions=["us-east-1"]))

    assert len(result) == 1
    finding = result[0]
    assert finding["efs_id"] == "fs-abc123"
    assert finding["efs_name"] == "shared-data"
    assert finding["mount_target_az"] == "us-east-1a"
    assert "i-cross" in finding["connected_instances_other_az"]
    assert finding["estimated_monthly_cost"] > 0


# ── integration: same-AZ instance not flagged ────────────────────────────────

def test_same_az_instance_not_flagged():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session

        efs_client = MagicMock()
        ec2_client = MagicMock()
        cw_client = MagicMock()

        session.client.side_effect = lambda svc, **kw: {
            "efs": efs_client, "ec2": ec2_client, "cloudwatch": cw_client
        }.get(svc, MagicMock())

        efs_client.describe_file_systems.return_value = {
            "FileSystems": [_make_fs("fs-xyz")]
        }
        efs_client.describe_mount_targets.return_value = {
            "MountTargets": [_make_mount_target("fsmt-002", "fs-xyz", "us-east-1a")]
        }
        efs_client.describe_mount_target_security_groups.return_value = {
            "SecurityGroups": ["sg-efs"]
        }
        # Instance in the same AZ
        ec2_client.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [_make_instance("i-same", "us-east-1a")]}
            ]
        }
        cw_client.get_metric_statistics.return_value = {"Datapoints": []}

        result = _run(audit_efs_cross_az_mounts(aws_client=aws_client, regions=["us-east-1"]))

    assert result == []


# ── integration: sorted by cost descending ────────────────────────────────────

def test_sorted_by_cost_descending():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session

        efs_client = MagicMock()
        ec2_client = MagicMock()
        cw_client = MagicMock()

        session.client.side_effect = lambda svc, **kw: {
            "efs": efs_client, "ec2": ec2_client, "cloudwatch": cw_client
        }.get(svc, MagicMock())

        efs_client.describe_file_systems.return_value = {
            "FileSystems": [
                _make_fs("fs-cheap", "cheap"),
                _make_fs("fs-expensive", "expensive"),
            ]
        }

        def mount_targets(FileSystemId, **kw):
            return {
                "MountTargets": [
                    _make_mount_target(f"fsmt-{FileSystemId}", FileSystemId, "us-east-1a")
                ]
            }

        efs_client.describe_mount_targets.side_effect = mount_targets
        efs_client.describe_mount_target_security_groups.return_value = {
            "SecurityGroups": ["sg-efs"]
        }
        ec2_client.describe_instances.return_value = {
            "Reservations": [
                {"Instances": [_make_instance("i-other", "us-east-1b")]}
            ]
        }

        cheap_bytes = 1 * 1024 ** 3
        expensive_bytes = 1000 * 1024 ** 3

        call_count = {"n": 0}

        def cw_metrics(**kw):
            fs_dim = next(
                (d["Value"] for d in kw.get("Dimensions", []) if d["Name"] == "FileSystemId"),
                None,
            )
            if fs_dim == "fs-cheap":
                return {"Datapoints": [{"Sum": cheap_bytes}]}
            return {"Datapoints": [{"Sum": expensive_bytes}]}

        cw_client.get_metric_statistics.side_effect = cw_metrics

        result = _run(audit_efs_cross_az_mounts(aws_client=aws_client, regions=["us-east-1"]))

    assert len(result) == 2
    assert result[0]["estimated_monthly_cost"] >= result[1]["estimated_monthly_cost"]
    assert result[0]["efs_name"] == "expensive"


# ── integration: no mount targets returns nothing ────────────────────────────

def test_file_system_with_no_mount_targets_skipped():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session

        efs_client = MagicMock()
        ec2_client = MagicMock()
        cw_client = MagicMock()

        session.client.side_effect = lambda svc, **kw: {
            "efs": efs_client, "ec2": ec2_client, "cloudwatch": cw_client
        }.get(svc, MagicMock())

        efs_client.describe_file_systems.return_value = {
            "FileSystems": [_make_fs("fs-nomount")]
        }
        efs_client.describe_mount_targets.return_value = {"MountTargets": []}
        cw_client.get_metric_statistics.return_value = {"Datapoints": []}

        result = _run(audit_efs_cross_az_mounts(aws_client=aws_client, regions=["us-east-1"]))

    assert result == []
