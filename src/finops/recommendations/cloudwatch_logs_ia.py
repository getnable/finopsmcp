"""
CloudWatch Logs Infrequent Access (IA) class migration audit.

CloudWatch Logs IA costs 50% less for ingestion:
  Standard:           $0.075/GB ingested
  Infrequent Access:  $0.0375/GB ingested

Log groups that are STANDARD class, older than 30 days, and ingesting >1 GB/month
are candidates for migration to IA class.

Limitations of IA class:
  - No metric filter support
  - No subscription filter support
  - No live tail support
  - CloudWatch Logs Insights queries still work

This module surfaces the savings opportunity without migrating automatically.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# us-east-1 CloudWatch Logs ingestion: $0.50/GB Standard, $0.25/GB Infrequent Access.
STANDARD_INGESTION_COST_PER_GB = 0.50
IA_INGESTION_COST_PER_GB = 0.25
SAVINGS_PER_GB = STANDARD_INGESTION_COST_PER_GB - IA_INGESTION_COST_PER_GB  # 0.25

_BYTES_PER_GB = 1024 ** 3
_LOOKBACK_DAYS = 30
_MIN_INGESTION_GB = 1.0   # only flag groups above this threshold
_MIN_AGE_DAYS = 30        # only flag groups at least this old


def _make_logs(session_or_none: Any, region: str) -> Any:
    import boto3

    if session_or_none is not None:
        return session_or_none.client("logs", region_name=region)
    return boto3.client("logs", region_name=region)


def _make_cw(session_or_none: Any, region: str) -> Any:
    import boto3

    if session_or_none is not None:
        return session_or_none.client("cloudwatch", region_name=region)
    return boto3.client("cloudwatch", region_name=region)


def _get_opted_in_regions(session_or_none: Any) -> list[str]:
    import boto3

    ec2 = (
        boto3.client("ec2", region_name="us-east-1")
        if session_or_none is None
        else session_or_none.client("ec2", region_name="us-east-1")
    )
    resp = ec2.describe_regions(
        Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
    )
    return [r["RegionName"] for r in resp.get("Regions", [])]


def _list_log_groups(logs: Any) -> list[dict]:
    """Return all log groups in the region."""
    groups: list[dict] = []
    paginator = logs.get_paginator("describe_log_groups")
    try:
        for page in paginator.paginate():
            groups.extend(page.get("logGroups", []))
    except Exception as exc:
        log.debug("describe_log_groups failed: %s", exc)
    return groups


def _get_incoming_bytes_gb(
    cw: Any,
    log_group_name: str,
    lookback_days: int = _LOOKBACK_DAYS,
) -> float:
    """
    Fetch total IncomingBytes for the log group over the last lookback_days.
    Returns the value in GB.
    """
    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(days=lookback_days)
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/Logs",
            MetricName="IncomingBytes",
            Dimensions=[{"Name": "LogGroupName", "Value": log_group_name}],
            StartTime=start,
            EndTime=now,
            Period=lookback_days * 86400,  # one big bucket
            Statistics=["Sum"],
        )
        datapoints = resp.get("Datapoints", [])
        if not datapoints:
            return 0.0
        total_bytes = sum(dp.get("Sum", 0) for dp in datapoints)
        return total_bytes / _BYTES_PER_GB
    except Exception as exc:
        log.debug("get_metric_statistics (IncomingBytes) failed for %s: %s", log_group_name, exc)
        return 0.0


def _group_age_days(log_group: dict, now: datetime) -> int:
    """Return the age of the log group in days based on creationTime."""
    creation_time_ms = log_group.get("creationTime", 0)
    if not creation_time_ms:
        return 0
    created = datetime.fromtimestamp(creation_time_ms / 1000, tz=timezone.utc)
    return (now - created).days


def _build_recommendation(log_group_name: str, monthly_savings: float) -> str:
    return (
        f"Move '{log_group_name}' to the Infrequent Access log class to save "
        f"${monthly_savings:.2f}/month. Confirm it has no metric filters or "
        f"subscription filters first. The log class is set at creation and cannot "
        f"be changed in place: create a replacement and repoint producers, e.g. "
        f"aws logs create-log-group --log-group-name '{log_group_name}-ia' "
        f"--log-group-class INFREQUENT_ACCESS, then delete the old group."
    )


def _audit_region(session_or_none: Any, region: str) -> dict:
    """Audit one region for IA migration candidates."""
    now = datetime.now(tz=timezone.utc)
    logs = _make_logs(session_or_none, region)
    cw = _make_cw(session_or_none, region)

    groups = _list_log_groups(logs)
    candidates: list[dict] = []

    for group in groups:
        storage_class = group.get("logGroupClass", "STANDARD")

        # Already on IA: skip
        if storage_class == "INFREQUENT_ACCESS":
            continue

        age_days = _group_age_days(group, now)
        if age_days < _MIN_AGE_DAYS:
            continue

        log_group_name = group.get("logGroupName", "")
        stored_bytes = group.get("storedBytes", 0)
        stored_bytes_gb = stored_bytes / _BYTES_PER_GB
        retention_days = group.get("retentionInDays")  # None means infinite

        monthly_ingestion_gb = _get_incoming_bytes_gb(cw, log_group_name, _LOOKBACK_DAYS)

        if monthly_ingestion_gb < _MIN_INGESTION_GB:
            continue

        monthly_cost_standard = round(monthly_ingestion_gb * STANDARD_INGESTION_COST_PER_GB, 4)
        monthly_cost_ia = round(monthly_ingestion_gb * IA_INGESTION_COST_PER_GB, 4)
        monthly_savings = round(monthly_ingestion_gb * SAVINGS_PER_GB, 4)

        candidates.append({
            "log_group_name": log_group_name,
            "storage_class": storage_class,
            "stored_bytes_gb": round(stored_bytes_gb, 4),
            "monthly_ingestion_gb": round(monthly_ingestion_gb, 4),
            "monthly_cost_standard": monthly_cost_standard,
            "monthly_cost_ia": monthly_cost_ia,
            "monthly_savings": monthly_savings,
            "retention_days": retention_days,
            "age_days": age_days,
            "region": region,
            "recommendation": _build_recommendation(log_group_name, monthly_savings),
        })

    candidates.sort(key=lambda c: c["monthly_savings"], reverse=True)
    return {
        "region": region,
        "total_groups_scanned": len(groups),
        "candidates": candidates,
    }


async def audit_cloudwatch_logs_ia_opportunities(
    aws_client: Any,
    regions: list[str] | None = None,
) -> dict:
    """
    Identify CloudWatch Log groups that can be migrated to Infrequent Access class.

    IA class cuts ingestion cost by 50% ($0.075 -> $0.0375/GB). Flags STANDARD
    class groups older than 30 days with >1 GB/month ingestion.

    Limitations of IA: no metric filters, no subscription filters, no live tail.
    Confirm before migrating.

    Args:
        aws_client: AWSConnector instance (provides boto3 session).
        regions:    AWS regions to scan. Defaults to all opted-in regions.

    Returns:
        {
          total_groups_scanned: int,
          total_candidates: int,
          total_monthly_savings: float,
          candidates: list[{
            log_group_name, storage_class, stored_bytes_gb,
            monthly_ingestion_gb, monthly_cost_standard, monthly_cost_ia,
            monthly_savings, retention_days, age_days, region, recommendation
          }],
          by_region: {region: {groups_scanned, candidates_count, monthly_savings}},
        }
    """
    import asyncio

    loop = asyncio.get_event_loop()
    session = getattr(aws_client, "_session", None)

    if not regions:
        try:
            regions = await loop.run_in_executor(None, _get_opted_in_regions, session)
        except Exception as exc:
            log.warning("Could not list regions, falling back to us-east-1: %s", exc)
            regions = ["us-east-1"]

    tasks = [
        loop.run_in_executor(None, _audit_region, session, region)
        for region in regions
    ]
    region_results = await asyncio.gather(*tasks, return_exceptions=True)

    all_candidates: list[dict] = []
    by_region: dict[str, dict] = {}
    grand_total_groups = 0

    for result in region_results:
        if isinstance(result, Exception):
            log.warning("Region logs-IA scan failed: %s", result)
            continue
        region_name = result["region"]
        candidates = result["candidates"]
        grand_total_groups += result["total_groups_scanned"]
        region_savings = sum(c["monthly_savings"] for c in candidates)
        by_region[region_name] = {
            "groups_scanned": result["total_groups_scanned"],
            "candidates_count": len(candidates),
            "monthly_savings": round(region_savings, 4),
        }
        all_candidates.extend(candidates)

    all_candidates.sort(key=lambda c: c["monthly_savings"], reverse=True)
    total_savings = round(sum(c["monthly_savings"] for c in all_candidates), 4)

    return {
        "total_groups_scanned": grand_total_groups,
        "total_candidates": len(all_candidates),
        "total_monthly_savings": total_savings,
        "candidates": all_candidates,
        "by_region": by_region,
    }
