"""
EBS snapshot cross-region replication audit.

EBS snapshots replicated across regions incur:
- $0.05/GB-month storage cost in EACH region.
- Inter-region data transfer charges when the copy was made.

Many teams replicate snapshots for DR but accumulate stale copies.
This module finds:
- Orphaned copies: source volume no longer exists in any region.
- Excessive copies: same volume snapshot appears in more than 3 regions.
- Old copies: >90 days old where a newer copy of the same volume exists.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .envelope import INFERRED, Finding

log = logging.getLogger(__name__)

try:
    import boto3 as boto3
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]

_SNAPSHOT_STORAGE_COST_PER_GB = 0.05   # USD per GB per month, per region
_OLD_SNAPSHOT_DAYS = 90
_MAX_COPY_REGIONS = 3
_DEFAULT_REGIONS = ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"]


def _snapshot_monthly_cost(size_gb: float, num_regions: int) -> float:
    """Upper-bound monthly cost across all regions the snapshot lives in.

    Uses the full provisioned volume size. Snapshots are incremental, so real
    storage is typically a fraction of this; treat the result as a ceiling.
    """
    return round(size_gb * _SNAPSHOT_STORAGE_COST_PER_GB * num_regions, 4)


def _get_all_regions(ec2_client: Any) -> list[str]:
    """Return all opted-in AWS regions."""
    try:
        resp = ec2_client.describe_regions(
            Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
        )
        return [r["RegionName"] for r in resp.get("Regions", [])]
    except Exception as e:
        log.warning("Could not list regions: %s", e)
        return _DEFAULT_REGIONS


def _list_snapshots_in_region(ec2_client: Any) -> list[dict]:
    """List all account-owned EBS snapshots in one region."""
    snapshots: list[dict] = []
    try:
        pag = ec2_client.get_paginator("describe_snapshots")
        for page in pag.paginate(OwnerIds=["self"]):
            snapshots.extend(page.get("Snapshots", []))
    except Exception as e:
        log.warning("Snapshot list failed: %s", e)
    return snapshots


def _live_volume_ids(ec2_client: Any) -> set[str]:
    """Return IDs of volumes that currently exist in this region."""
    ids: set[str] = set()
    try:
        pag = ec2_client.get_paginator("describe_volumes")
        for page in pag.paginate():
            for vol in page.get("Volumes", []):
                ids.add(vol["VolumeId"])
    except Exception as e:
        log.warning("Volume list failed: %s", e)
    return ids


def _parse_snapshot_time(snap: dict) -> datetime | None:
    t = snap.get("StartTime")
    if t is None:
        return None
    if isinstance(t, str):
        try:
            return datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            return None
    if hasattr(t, "tzinfo"):
        if t.tzinfo is None:
            return t.replace(tzinfo=timezone.utc)
        return t
    return None


def _build_cross_region_findings(
    snapshots_by_region: dict[str, list[dict]],
    live_volumes_by_region: dict[str, set[str]],
    now: datetime,
) -> list[dict]:
    """
    Group snapshots by volume ID and find those that appear in multiple regions.

    A cross-region set is identified when the same volume_id has snapshots in
    more than one region, OR when a snapshot's description contains "Copied from"
    (the standard EBS copy description format).

    Returns one record per cross-region snapshot set (grouped by volume_id).
    """
    # Group by volume_id across all regions
    by_volume: dict[str, dict[str, list[dict]]] = {}
    for region, snaps in snapshots_by_region.items():
        for snap in snaps:
            vol_id = snap.get("VolumeId", "")
            if not vol_id:
                continue
            if vol_id not in by_volume:
                by_volume[vol_id] = {}
            if region not in by_volume[vol_id]:
                by_volume[vol_id][region] = []
            by_volume[vol_id][region].append(snap)

    findings: list[dict] = []

    for vol_id, region_map in by_volume.items():
        if len(region_map) < 2:
            continue  # only in one region, not a cross-region replication

        copy_regions = list(region_map.keys())
        all_snaps = [(region, s) for region, snaps in region_map.items() for s in snaps]

        # Source region heuristic: region whose snapshot was created first
        def _snap_time(item: tuple) -> datetime:
            t = _parse_snapshot_time(item[1])
            return t if t is not None else datetime(2000, 1, 1, tzinfo=timezone.utc)

        all_snaps_sorted = sorted(all_snaps, key=_snap_time)
        source_region = all_snaps_sorted[0][0]

        total_size_gb = max(
            float(s.get("VolumeSize", 0))
            for _, s in all_snaps
        )
        num_regions = len(copy_regions)
        total_monthly_cost = _snapshot_monthly_cost(total_size_gb, num_regions)

        # Orphaned: volume not live in any region
        orphaned = not any(
            vol_id in live_volumes_by_region.get(r, set())
            for r in copy_regions
        )

        # Excess copies: more regions than the threshold
        excess_copies = num_regions > _MAX_COPY_REGIONS

        # Old copies: any snapshot older than threshold AND a newer one exists
        times = sorted(
            [t for _, t in [(r, _parse_snapshot_time(s)) for r, s in all_snaps] if t is not None]
        )
        has_old_copies = (
            len(times) > 1
            and times[0] < now - timedelta(days=_OLD_SNAPSHOT_DAYS)
        )

        # Pick a representative snapshot_id from the source region
        source_snaps = region_map.get(source_region, all_snaps_sorted[:1])
        snap_id = (source_snaps[0] if isinstance(source_snaps[0], str)
                   else source_snaps[0].get("SnapshotId", ""))

        # Recommendation text
        if orphaned:
            recommendation = (
                f"Source volume {vol_id} no longer exists. "
                f"Delete all {num_regions} copies to save ${total_monthly_cost:.2f}/mo."
            )
        elif excess_copies:
            excess = num_regions - _MAX_COPY_REGIONS
            cost_per_region = _snapshot_monthly_cost(total_size_gb, 1)
            recommendation = (
                f"Snapshot exists in {num_regions} regions. "
                f"Remove {excess} excess copy/copies to save ~${cost_per_region * excess:.2f}/mo."
            )
        elif has_old_copies:
            recommendation = (
                f"Old copies (>{_OLD_SNAPSHOT_DAYS}d) exist alongside newer ones. "
                f"Delete oldest copies to reduce storage cost."
            )
        else:
            recommendation = "Cross-region replication within acceptable parameters."

        findings.append({
            "snapshot_id": snap_id,
            "volume_id": vol_id,
            "source_region": source_region,
            "copy_regions": copy_regions,
            "total_size_gb": total_size_gb,
            "total_monthly_cost": total_monthly_cost,
            # Cost uses the full provisioned VolumeSize. EBS snapshots are
            # incremental (you pay for changed blocks, often a fraction of the
            # volume), so this is an UPPER BOUND, not the exact bill. It is the
            # ceiling on what deleting the extra copies could save.
            "cost_is_upper_bound": True,
            "excess_copies": excess_copies,
            "orphaned": orphaned,
            "has_old_copies": has_old_copies,
            "recommendation": recommendation,
        })

    # Sort by total monthly cost, most expensive first
    findings.sort(key=lambda x: x["total_monthly_cost"], reverse=True)
    return findings


async def audit_ebs_snapshot_replication(
    aws_client: Any,
    regions: list[str] | None = None,
) -> dict:
    """
    Audit EBS snapshots replicated across regions for cost waste.

    Args:
        aws_client: AWSConnector (used for credential context; boto3 imported internally).
        regions:    AWS regions to scan. Defaults to all opted-in regions.

    Returns:
        Dict with cross_region_findings, summary totals, and potential savings.
    """
    if boto3 is None:
        return {
            "error": "boto3 not installed",
            "cross_region_findings": [],
            "total_cross_region_cost": 0.0,
            "potential_monthly_savings": 0.0,
            "total_volume_sets": 0,
        }

    try:
        if regions is None:
            ec2g = boto3.client("ec2", region_name="us-east-1")
            regions = _get_all_regions(ec2g)
    except Exception as e:
        log.warning("Region discovery failed, using defaults: %s", e)
        regions = _DEFAULT_REGIONS

    now = datetime.now(timezone.utc)
    snapshots_by_region: dict[str, list[dict]] = {}
    live_volumes_by_region: dict[str, set[str]] = {}

    for region in regions:
        try:
            ec2 = boto3.client("ec2", region_name=region)
            snapshots_by_region[region] = _list_snapshots_in_region(ec2)
            live_volumes_by_region[region] = _live_volume_ids(ec2)
        except Exception as e:
            log.warning("EBS snapshot audit failed for region %s: %s", region, e)

    findings = _build_cross_region_findings(
        snapshots_by_region, live_volumes_by_region, now
    )

    total_cost = round(sum(f["total_monthly_cost"] for f in findings), 2)

    # Potential savings: orphaned + excess copy cost
    saveable_cost = 0.0
    for f in findings:
        if f["orphaned"]:
            saveable_cost += f["total_monthly_cost"]
        elif f["excess_copies"]:
            num_excess = len(f["copy_regions"]) - _MAX_COPY_REGIONS
            per_region = _snapshot_monthly_cost(f["total_size_gb"], 1)
            saveable_cost += per_region * num_excess
        elif f["has_old_copies"]:
            # Estimate one region's worth of savings for old copies
            per_region = _snapshot_monthly_cost(f["total_size_gb"], 1)
            saveable_cost += per_region

    saveable_cost = round(saveable_cost, 2)

    # Classify by strength of evidence. We genuinely see snapshots of one volume
    # living in multiple regions, and orphaned status is checked against live
    # volumes. But the dollar figure is an UPPER BOUND on purpose: it bills the
    # full provisioned VolumeSize, while EBS snapshots are incremental (you pay
    # only for changed blocks, often a small fraction of the volume). The real
    # saving depends on actual snapshot storage, which describe_snapshots does not
    # report. Because the number rests on that ceiling assumption, this is an
    # investigation with a magnitude band, not a precise claim.
    finding = None
    saveable_findings = [
        f for f in findings
        if f["orphaned"] or f["excess_copies"] or f["has_old_copies"]
    ]
    if saveable_findings and saveable_cost > 5.0:
        top = saveable_findings[0]
        n_orphaned = sum(1 for f in saveable_findings if f["orphaned"])
        n_excess = sum(1 for f in saveable_findings if f["excess_copies"] and not f["orphaned"])
        reasons = []
        if n_orphaned:
            reasons.append(f"{n_orphaned} set(s) whose source volume no longer exists")
        if n_excess:
            reasons.append(f"{n_excess} set(s) copied into more than {_MAX_COPY_REGIONS} regions")
        what = " and ".join(reasons) if reasons else "stale cross-region copies"

        finding = Finding(
            source="ebs_snapshot_replication",
            title="Let's confirm how much your cross-region EBS snapshot copies actually cost",
            why=("Some EBS snapshots are replicated across several regions and you pay "
                 f"$0.05/GB-month for storage in each one. I see {what}, which look like "
                 "DR copies that were never cleaned up."),
            evidence=INFERRED,
            confidence="medium",
            why_unsure=("My cost figure bills the full provisioned volume size, but EBS "
                        "snapshots are incremental: you are charged only for changed "
                        "blocks, which is usually far less. So the real saving is somewhere "
                        "between a fraction of this number and the full amount, and I can't "
                        "size it exactly from the snapshot metadata alone."),
            assumptions=[
                "Snapshot storage is billed at the full provisioned VolumeSize (an upper "
                "bound; real incremental storage is typically less).",
                "Each extra region holds a full additional copy.",
            ],
            rough_monthly=saveable_cost,
            confirm_steps=[
                "Check the actual snapshot storage you are billed for per region in Cost "
                "Explorer (filter to EBS snapshot usage type, group by region), then "
                "compare against these volume sets.",
                "For each orphaned set, confirm the source volume is truly gone in every "
                "region before deleting the copies.",
            ],
            pro_can_confirm=True,
            pro_unlock=("On Pro, nable reads your CUR line items and resolves the exact "
                        "per-snapshot storage you are billed for in each region, so it can "
                        "put a precise number on these copies instead of an upper bound."),
            remediation=[
                "Once you have confirmed the real cost, delete orphaned copies first "
                "(their source volume is gone): aws ec2 delete-snapshot --snapshot-id <id> "
                "in each region. Deletion is irreversible, so verify you have a current "
                "copy of anything you still need before removing the extras.",
            ],
            resource_id=top.get("volume_id", ""),
            metadata={
                "top_volume_id": top.get("volume_id", ""),
                "top_copy_regions": top.get("copy_regions", []),
                "orphaned_sets": n_orphaned,
                "excess_copy_sets": n_excess,
                "cost_is_upper_bound": True,
            },
        )

    return {
        "cross_region_findings": findings,
        "total_cross_region_cost": total_cost,
        "potential_monthly_savings": saveable_cost,
        "total_volume_sets": len(findings),
        "regions_scanned": regions,
        "finding": finding.to_dict() if finding else None,
    }
