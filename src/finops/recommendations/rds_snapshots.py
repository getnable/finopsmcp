"""
RDS manual snapshot audit.

Manual RDS snapshots never auto-expire. They accumulate silently at
$0.095/GB-month. This module identifies:
  - Orphaned snapshots: the source DB no longer exists.
  - Old snapshots: older than a configurable threshold, source DB still exists.

Storage cost formula: allocated_storage_gb * 0.095 per month.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .envelope import MEASURED, Finding

log = logging.getLogger(__name__)

try:
    import boto3 as boto3
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]

_SNAPSHOT_COST_PER_GB = 0.095   # USD per GB per month


def _snapshot_monthly_cost(size_gb: float) -> float:
    return round(size_gb * _SNAPSHOT_COST_PER_GB, 4)


def _get_active_db_identifiers(rds_client: Any) -> set[str]:
    """Return a set of all DB instance identifiers currently active in the account."""
    identifiers: set[str] = set()
    try:
        pag = rds_client.get_paginator("describe_db_instances")
        for page in pag.paginate():
            for db in page.get("DBInstances", []):
                identifiers.add(db["DBInstanceIdentifier"])
    except Exception as e:
        log.warning("Could not list DB instances: %s", e)
    # Also check DB clusters (Aurora)
    try:
        pag = rds_client.get_paginator("describe_db_clusters")
        for page in pag.paginate():
            for cluster in page.get("DBClusters", []):
                identifiers.add(cluster["DBClusterIdentifier"])
    except Exception as e:
        log.debug("Could not list DB clusters (may not be supported): %s", e)
    return identifiers


def _build_snapshot_record(
    snap: dict,
    region: str,
    now: datetime,
    active_dbs: set[str],
    age_threshold_days: int,
) -> dict:
    """Convert a raw describe_db_snapshots entry into a normalised record dict."""
    snap_id = snap.get("DBSnapshotIdentifier", "")
    db_id = snap.get("DBInstanceIdentifier", "")
    size_gb = float(snap.get("AllocatedStorage", 0))
    status = snap.get("Status", "")
    created_at_raw = snap.get("SnapshotCreateTime")

    if created_at_raw is None:
        age_days = 0
        created_at_str = ""
    else:
        if created_at_raw.tzinfo is None:
            created_at_raw = created_at_raw.replace(tzinfo=timezone.utc)
        age_days = (now - created_at_raw).days
        created_at_str = created_at_raw.isoformat()

    is_orphaned = db_id not in active_dbs
    monthly_cost = _snapshot_monthly_cost(size_gb)

    return {
        "snapshot_id": snap_id,
        "db_identifier": db_id,
        "size_gb": size_gb,
        "age_days": age_days,
        "monthly_cost": monthly_cost,
        "status": status,
        "is_orphaned": is_orphaned,
        "region": region,
        "created_at": created_at_str,
        "is_old": age_days > age_threshold_days,
    }


async def audit_rds_manual_snapshots(
    aws_client: Any,
    regions: list[str] | None = None,
    age_threshold_days: int = 30,
) -> dict:
    """
    Audit manual RDS snapshots across regions for cost waste.

    Args:
        aws_client:         AWSConnector (used only for credential checking; boto3
                            is imported internally for testability).
        regions:            AWS regions to scan. Defaults to all opted-in regions.
        age_threshold_days: Snapshots older than this (and not orphaned) appear in
                            old_snapshots. Default: 30 days.

    Returns:
        Dict with orphaned_snapshots, old_snapshots, cost totals, and counts.
    """
    if boto3 is None:
        return {
            "error": "boto3 not installed",
            "orphaned_snapshots": [],
            "old_snapshots": [],
            "total_monthly_cost": 0.0,
            "potential_monthly_savings": 0.0,
            "total_snapshots": 0,
            "total_size_gb": 0.0,
        }

    if regions is None:
        try:
            ec2g = boto3.client("ec2", region_name="us-east-1")
            resp = ec2g.describe_regions(
                Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
            )
            regions = [r["RegionName"] for r in resp.get("Regions", [])]
        except Exception:
            regions = ["us-east-1", "us-west-2", "eu-west-1"]

    now = datetime.now(timezone.utc)

    def _scan_region(region: str) -> list[dict]:
        recs: list[dict] = []
        try:
            rds = boto3.client("rds", region_name=region)
            active_dbs = _get_active_db_identifiers(rds)
            pag = rds.get_paginator("describe_db_snapshots")
            for page in pag.paginate(SnapshotType="manual"):
                for snap in page.get("DBSnapshots", []):
                    recs.append(_build_snapshot_record(
                        snap, region, now, active_dbs, age_threshold_days
                    ))
        except Exception as e:
            log.warning("RDS snapshot audit failed for region %s: %s", region, e)
        return recs

    # Each region's describe_db_snapshots is an independent blocking round-trip;
    # scan them concurrently so the audit costs the slowest region, not the sum.
    import asyncio
    region_lists = await asyncio.gather(*[asyncio.to_thread(_scan_region, r) for r in regions])
    all_records: list[dict] = [rec for sub in region_lists for rec in sub]
    # orphaned takes precedence over old (was an if/elif in the serial loop).
    orphaned: list[dict] = [r for r in all_records if r["is_orphaned"]]
    old: list[dict] = [r for r in all_records if r["is_old"] and not r["is_orphaned"]]

    # Sort by cost descending so the most expensive appear first
    orphaned.sort(key=lambda x: x["monthly_cost"], reverse=True)
    old.sort(key=lambda x: x["monthly_cost"], reverse=True)

    total_monthly_cost = round(sum(r["monthly_cost"] for r in all_records), 2)
    saveable = [r for r in all_records if r["is_orphaned"] or r["is_old"]]
    potential_savings = round(sum(r["monthly_cost"] for r in saveable), 2)
    total_size_gb = round(sum(r["size_gb"] for r in all_records), 1)

    # Classify by strength of evidence. Each snapshot is one we read from
    # describe_db_snapshots: it exists, with a known AllocatedStorage and AWS's
    # published $0.095/GB-month rate. Orphaned status is directly verified (the
    # source DB identifier is not in the live instance/cluster set we listed).
    # That is measured, so this is a recommendation. We lead with orphaned
    # snapshots because deleting them is unambiguously safe; old snapshots of a
    # live DB may still be a deliberate retention copy, so the action there is
    # "review then delete", not a blind delete.
    finding = None
    n_orphaned = len(orphaned)
    n_old = len(old)
    if potential_savings > 1.0 and (n_orphaned or n_old):
        parts = []
        if n_orphaned:
            parts.append(f"{n_orphaned} orphaned snapshot(s) whose source DB is gone")
        if n_old:
            parts.append(f"{n_old} snapshot(s) older than {age_threshold_days} days "
                         f"on a DB that still exists")
        what = " and ".join(parts)

        remediation = []
        if n_orphaned:
            remediation.append(
                "Delete the orphaned snapshots. Their source database no longer exists, "
                "so they are dead storage: aws rds delete-db-snapshot "
                "--db-snapshot-identifier <id>. Snapshot deletion is irreversible, so "
                "confirm you have no compliance or restore requirement first.")
        if n_old:
            remediation.append(
                "For old snapshots of a live DB, confirm they are not a required "
                "retention or compliance copy before deleting. If they are routine "
                "backups, move retention to automated snapshots with a lifecycle policy "
                "so they expire on their own instead of accumulating manually.")

        single_id = ""
        if potential_savings > 1.0 and (n_orphaned + n_old) == 1:
            only = (orphaned or old)[0]
            single_id = only.get("snapshot_id", "")

        finding = Finding(
            source="rds_snapshots",
            title="Manual RDS snapshots are accumulating storage cost",
            why=("Manual RDS snapshots never auto-expire and bill at $0.095/GB-month "
                 f"until you delete them. You have {what}, which together cost about "
                 f"${potential_savings:,.2f}/mo to keep."),
            evidence=MEASURED,
            confidence="high" if n_orphaned and not n_old else "medium",
            est_monthly_savings=potential_savings,
            remediation=remediation,
            resource_id=single_id,
            metadata={
                "orphaned_count": n_orphaned,
                "old_count": n_old,
                "age_threshold_days": age_threshold_days,
                "saveable_size_gb": round(sum(r["size_gb"] for r in saveable), 1),
            },
        )

    return {
        "orphaned_snapshots": orphaned,
        "old_snapshots": old,
        "total_monthly_cost": total_monthly_cost,
        "potential_monthly_savings": potential_savings,
        "total_snapshots": len(all_records),
        "total_size_gb": total_size_gb,
        "finding": finding.to_dict() if finding else None,
    }
