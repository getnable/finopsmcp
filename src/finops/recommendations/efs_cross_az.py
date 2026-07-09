"""
EFS cross-AZ mount detection.

EFS charges $0.02/GB for cross-AZ data transfer. EC2 instances mounting an
EFS file system from a different AZ pay this silently on every read and write.

This scanner cross-references EFS mount target AZs against the AZs of
EC2 instances that are likely connected to each file system, then estimates
monthly cross-AZ transfer costs using CloudWatch I/O byte metrics.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .envelope import INFERRED, Finding

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

    def _scan_region(region: str) -> list[dict]:
        out: list[dict] = []
        try:
            efs_client = session.client("efs", region_name=region)
            ec2_client = session.client("ec2", region_name=region)
            cw_client = session.client("cloudwatch", region_name=region)
        except Exception as exc:
            log.debug("Could not create clients for region %s: %s", region, exc)
            return out

        # List EFS file systems
        try:
            fs_resp = efs_client.describe_file_systems()
        except Exception as exc:
            log.debug("describe_file_systems failed in %s: %s", region, exc)
            return out

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

                # Classify by strength of evidence. Two heuristics stack here, so this
                # is an investigation, never a precise claim:
                #   1. We infer "this instance mounts this EFS" from shared security
                #      group membership, which is a proxy, not a confirmed NFS mount.
                #   2. We bill 100% of filesystem I/O as cross-AZ and split it evenly
                #      across mount targets, an upper bound on a number we cannot
                #      actually measure from CloudWatch alone.
                finding = None
                if max_monthly_cost > 5.0:
                    finding = Finding(
                        source="efs_cross_az",
                        title="Let's confirm whether this EFS file system is paying for cross-AZ traffic",
                        why=("EFS reads and writes that cross an AZ boundary cost $0.01/GB "
                             "per direction. This file system has running instances in a "
                             "different AZ from its mount target, which is the usual sign of "
                             "a cross-AZ mount silently adding transfer cost."),
                        evidence=INFERRED,
                        confidence="low",
                        why_unsure=("I matched instances to this file system by shared security "
                                    "group, not by an observed mount, so some of those instances "
                                    "may not mount it at all. And I billed all of its I/O as "
                                    "cross-AZ split evenly across mount targets, which overstates "
                                    "the real figure: most I/O usually stays within the AZ."),
                        assumptions=[
                            "Instances sharing the mount target's security group actually mount "
                            "this file system (a proxy, not a confirmed mount).",
                            "All filesystem I/O crosses an AZ boundary, split evenly across "
                            "mount targets (an upper bound).",
                        ],
                        rough_monthly=round(max_monthly_cost, 2),
                        confirm_steps=[
                            "On one of the listed instances, check the real mount: run "
                            "'mount | grep nfs' (or inspect /etc/fstab) and confirm it targets "
                            "this file system, then compare the instance AZ to the mount target AZ.",
                            "Look at the EFS file system's per-AZ data transfer in Cost Explorer "
                            "(EFS usage types) to see what cross-AZ transfer is actually billed.",
                        ],
                        pro_can_confirm=True,
                        pro_unlock=("On Pro, nable reads your CUR line items for EFS transfer usage "
                                    "types and confirms the real cross-AZ transfer cost per file "
                                    "system, instead of the upper bound shown here."),
                        remediation=[
                            "Once you have confirmed a genuine cross-AZ mount, add an EFS mount "
                            "target in each instance's AZ and point the instances at their local "
                            "AZ endpoint. This is additive and safe: it routes traffic locally "
                            "without changing the data.",
                        ],
                        resource_id=filesystem_id,
                        metadata={
                            "mount_target_id": mt_id,
                            "mount_target_az": mt_az,
                            "connected_instances_other_az": other_az_instances,
                            "cost_is_upper_bound": True,
                        },
                    )

                out.append({
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
                    "finding": finding.to_dict() if finding else None,
                })
        return out

    # Each region's scan is blocking boto3 I/O. Run them in threads concurrently:
    # the old serial loop both paid the sum of every region's latency AND blocked
    # the event loop for the whole scan.
    region_lists = await asyncio.gather(
        *[asyncio.to_thread(_scan_region, r) for r in target_regions]
    )
    for sub in region_lists:
        findings.extend(sub)

    findings.sort(key=lambda f: f["estimated_monthly_cost"], reverse=True)
    return findings
