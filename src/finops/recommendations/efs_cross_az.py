"""
EFS cross-AZ mount detection.

EFS charges $0.02/GB for cross-AZ data transfer. EC2 instances mounting an
EFS file system from a different AZ pay this silently on every read and write.

This scanner cross-references EFS mount target AZs against the AZs of
EC2 instances that are likely connected to each file system, then estimates
monthly cross-AZ transfer costs using CloudWatch I/O byte metrics.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# EC2 inter-AZ data transfer is $0.01/GB per direction.
CROSS_AZ_COST_PER_GB: float = 0.01
BYTES_PER_GB: int = 1024 ** 3
_LOOKBACK_DAYS = 30

_DEFAULT_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-central-1",
    "ap-southeast-1", "ap-southeast-2", "ap-northeast-1",
]


def _make_boto_session(aws_client: Any):
    """Return a boto3 session from the AWSConnector, or a fresh default session."""
    import boto3

    if hasattr(aws_client, "_session") and aws_client._session is not None:
        return aws_client._session
    return boto3.Session()


def _get_efs_io_bytes(
    cw_client: Any,
    filesystem_id: str,
    start: datetime,
    end: datetime,
) -> float:
    """
    Return total data transferred (read + write) for an EFS file system in bytes.
    Falls back to 0.0 if no CloudWatch data is available.
    """
    total_bytes = 0.0
    period = _LOOKBACK_DAYS * 86400

    for metric in ("DataReadIOBytes", "DataWriteIOBytes"):
        try:
            resp = cw_client.get_metric_statistics(
                Namespace="AWS/EFS",
                MetricName=metric,
                Dimensions=[{"Name": "FileSystemId", "Value": filesystem_id}],
                StartTime=start,
                EndTime=end,
                Period=period,
                Statistics=["Sum"],
            )
            for dp in resp.get("Datapoints", []):
                total_bytes += dp.get("Sum", 0.0)
        except Exception as exc:
            log.debug("CW metric %s failed for %s: %s", metric, filesystem_id, exc)

    return total_bytes


def _get_mount_target_security_groups(efs_client: Any, mount_target_id: str) -> list[str]:
    """Return security group IDs attached to a mount target."""
    try:
        resp = efs_client.describe_mount_target_security_groups(
            MountTargetId=mount_target_id
        )
        return resp.get("SecurityGroups", [])
    except Exception as exc:
        log.debug("describe_mount_target_security_groups failed for %s: %s", mount_target_id, exc)
        return []


def _find_instances_in_other_az(
    ec2_client: Any,
    mount_target_security_groups: list[str],
    mount_target_az: str,
) -> list[str]:
    """
    Find running EC2 instances that share security groups with the mount target
    but are in a different AZ. This is a reasonable proxy for cross-AZ mounts.

    Returns a list of instance IDs in other AZs.
    """
    if not mount_target_security_groups:
        return []

    try:
        resp = ec2_client.describe_instances(
            Filters=[
                {"Name": "instance-state-name", "Values": ["running"]},
                {"Name": "network-interface.groups.group-id", "Values": mount_target_security_groups},
            ]
        )
    except Exception as exc:
        log.debug("describe_instances failed: %s", exc)
        return []

    other_az_instances = []
    for reservation in resp.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            instance_az = instance.get("Placement", {}).get("AvailabilityZone", "")
            if instance_az and instance_az != mount_target_az:
                other_az_instances.append(instance["InstanceId"])

    return other_az_instances


async def audit_efs_cross_az_mounts(
    aws_client: Any,
    regions: list[str] | None = None,
) -> list[dict]:
    """
    Detect EFS file systems with EC2 instances mounting from a different AZ.

    For each EFS mount target, checks for connected instances in other AZs
    using shared security group membership as a proxy. Estimates monthly
    cross-AZ transfer cost from CloudWatch I/O metrics.

    Args:
        aws_client: AWSConnector instance (provides boto3 session).
        regions:    AWS regions to scan. Defaults to common regions.

    Returns:
        List of dicts with cross-AZ findings, sorted by estimated_monthly_cost descending.
    """
    target_regions = regions or _DEFAULT_REGIONS
    session = _make_boto_session(aws_client)

    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(days=_LOOKBACK_DAYS)

    findings: list[dict] = []

    for region in target_regions:
        try:
            efs_client = session.client("efs", region_name=region)
            ec2_client = session.client("ec2", region_name=region)
            cw_client = session.client("cloudwatch", region_name=region)
        except Exception as exc:
            log.debug("Could not create clients for region %s: %s", region, exc)
            continue

        # List EFS file systems
        try:
            fs_resp = efs_client.describe_file_systems()
        except Exception as exc:
            log.debug("describe_file_systems failed in %s: %s", region, exc)
            continue

        for fs in fs_resp.get("FileSystems", []):
            filesystem_id = fs["FileSystemId"]
            fs_name = next(
                (t["Value"] for t in fs.get("Tags", []) if t["Key"] == "Name"),
                filesystem_id,
            )

            # List mount targets for this file system
            try:
                mt_resp = efs_client.describe_mount_targets(FileSystemId=filesystem_id)
            except Exception as exc:
                log.debug("describe_mount_targets failed for %s: %s", filesystem_id, exc)
                continue

            mount_targets = mt_resp.get("MountTargets", [])
            if not mount_targets:
                continue

            # Get total I/O for cost estimation (shared across all mount targets)
            total_io_bytes = _get_efs_io_bytes(cw_client, filesystem_id, start_time, end_time)
            total_io_gb = total_io_bytes / BYTES_PER_GB

            for mt in mount_targets:
                mt_id = mt["MountTargetId"]
                mt_az = mt.get("AvailabilityZoneName", "")
                if not mt_az:
                    continue

                sgs = _get_mount_target_security_groups(efs_client, mt_id)
                other_az_instances = _find_instances_in_other_az(ec2_client, sgs, mt_az)

                if not other_az_instances:
                    continue

                # Apportion I/O evenly across mount targets for per-MT cost estimate.
                # This is an UPPER BOUND: it assumes all filesystem I/O crosses an AZ
                # boundary, which is rarely true. Real cost depends on the cross-AZ
                # fraction, which CloudWatch I/O metrics alone cannot determine.
                mt_io_gb = total_io_gb / max(len(mount_targets), 1)
                max_monthly_cost = mt_io_gb * CROSS_AZ_COST_PER_GB

                findings.append({
                    "efs_id": filesystem_id,
                    "efs_name": fs_name,
                    "region": region,
                    "mount_target_id": mt_id,
                    "mount_target_az": mt_az,
                    "connected_instances_other_az": other_az_instances,
                    "estimated_monthly_transfer_gb": round(mt_io_gb, 2),
                    "estimated_monthly_cost": round(max_monthly_cost, 4),
                    "cost_basis": "upper bound: assumes 100% of I/O crosses AZ; verify mount AZ vs instance AZ",
                    "recommendation": (
                        "Create an EFS mount target in each instance AZ "
                        "and update mount configurations to use the local AZ endpoint."
                    ),
                })

    findings.sort(key=lambda f: f["estimated_monthly_cost"], reverse=True)
    return findings
