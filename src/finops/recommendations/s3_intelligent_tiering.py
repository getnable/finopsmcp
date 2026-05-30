"""
S3 Intelligent-Tiering small object warning.

S3 Intelligent-Tiering charges $0.0025 per 1,000 monitored objects regardless
of access patterns. For objects smaller than 128KB the monitoring fee exceeds
any possible tiering savings, making IT more expensive than S3 Standard.

This scanner identifies IT-enabled buckets where the average object size is
below the break-even threshold and calculates the net monthly cost of IT.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

IT_MONITORING_COST_PER_1K_OBJECTS: float = 0.0025
IT_BREAKEVEN_SIZE_KB: float = 128.0
_LOOKBACK_DAYS = 30

# CloudWatch Storage Lens metrics namespace
_SL_NAMESPACE = "AWS/S3/Storage-Lens"
# Fallback: estimate savings assuming objects move to Infrequent Access tier
# IA pricing: $0.0125/GB vs Standard $0.023/GB = $0.0105/GB savings per GB in IA
_IA_SAVINGS_PER_GB: float = 0.0105

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


def _get_bucket_storage_stats(
    cw_client: Any,
    bucket_name: str,
    start: datetime,
    end: datetime,
) -> tuple[int | None, float | None]:
    """
    Fetch object count and total size for a bucket from CloudWatch bucket metrics.

    Returns (object_count, total_size_bytes). Both may be None if metrics are
    not available (bucket-level metrics must be explicitly enabled in S3).
    """
    period = _LOOKBACK_DAYS * 86400
    object_count: int | None = None
    total_size_bytes: float | None = None

    for metric, storage_type in [
        ("NumberOfObjects", "AllStorageTypes"),
        ("BucketSizeBytes", "StandardStorage"),
    ]:
        try:
            resp = cw_client.get_metric_statistics(
                Namespace="AWS/S3",
                MetricName=metric,
                Dimensions=[
                    {"Name": "BucketName", "Value": bucket_name},
                    {"Name": "StorageType", "Value": storage_type},
                ],
                StartTime=start,
                EndTime=end,
                Period=period,
                Statistics=["Average"],
            )
            datapoints = resp.get("Datapoints", [])
            if datapoints:
                value = datapoints[-1]["Average"]
                if metric == "NumberOfObjects":
                    object_count = int(value)
                else:
                    total_size_bytes = value
        except Exception as exc:
            log.debug("CW metric %s failed for %s: %s", metric, bucket_name, exc)

    return object_count, total_size_bytes


def _has_intelligent_tiering(s3_client: Any, bucket_name: str) -> bool:
    """Return True if the bucket has at least one Intelligent-Tiering configuration."""
    try:
        resp = s3_client.list_bucket_intelligent_tiering_configurations(Bucket=bucket_name)
        configs = resp.get("IntelligentTieringConfigurationList", [])
        return len(configs) > 0
    except Exception as exc:
        error_code = ""
        if hasattr(exc, "response"):
            error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("NoSuchBucket",):
            return False
        log.debug("list_bucket_intelligent_tiering_configurations failed for %s: %s", bucket_name, exc)
        return False


def _calculate_avg_object_size_kb(object_count: int | None, total_size_bytes: float | None) -> float | None:
    """Return average object size in KB, or None if data is unavailable."""
    if object_count is None or total_size_bytes is None:
        return None
    if object_count == 0:
        return 0.0
    return (total_size_bytes / object_count) / 1024.0


def _estimate_storage_savings(object_count: int | None, total_size_bytes: float | None) -> float:
    """
    Estimate monthly storage savings from tiering (upper bound).
    Assumes all objects eventually move to the Infrequent Access tier.
    """
    if object_count is None or total_size_bytes is None or object_count == 0:
        return 0.0
    total_gb = total_size_bytes / (1024 ** 3)
    return total_gb * _IA_SAVINGS_PER_GB


async def audit_s3_intelligent_tiering(
    aws_client: Any,
    regions: list[str] | None = None,
) -> list[dict]:
    """
    Audit S3 buckets using Intelligent-Tiering to find small-object waste.

    IT charges $0.0025 per 1,000 monitored objects. For buckets with average
    object size below 128KB the monitoring fee exceeds the tiering savings.

    Args:
        aws_client: AWSConnector instance (provides boto3 session).
        regions:    Unused (S3 is global but scanned from us-east-1). Kept for
                    API consistency with other audit tools.

    Returns:
        List of dicts with findings, sorted by net_monthly_cost descending.
    """
    session = _make_boto_session(aws_client)

    s3_client = session.client("s3", region_name="us-east-1")
    cw_client = session.client("cloudwatch", region_name="us-east-1")

    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(days=_LOOKBACK_DAYS)

    try:
        buckets_resp = s3_client.list_buckets()
    except Exception as exc:
        log.error("list_buckets failed: %s", exc)
        return []

    findings: list[dict] = []

    for bucket in buckets_resp.get("Buckets", []):
        bucket_name = bucket["Name"]

        if not _has_intelligent_tiering(s3_client, bucket_name):
            continue

        object_count, total_size_bytes = _get_bucket_storage_stats(
            cw_client, bucket_name, start_time, end_time
        )

        avg_size_kb = _calculate_avg_object_size_kb(object_count, total_size_bytes)

        monthly_monitoring_cost = (
            (object_count / 1000.0) * IT_MONITORING_COST_PER_1K_OBJECTS
            if object_count is not None
            else None
        )
        estimated_storage_savings = _estimate_storage_savings(object_count, total_size_bytes)

        net_monthly_cost = (
            (monthly_monitoring_cost - estimated_storage_savings)
            if monthly_monitoring_cost is not None
            else None
        )

        if avg_size_kb is not None and avg_size_kb < IT_BREAKEVEN_SIZE_KB:
            recommendation = "LIKELY_WASTE_switch_to_s3_standard_or_standard_ia"
        elif avg_size_kb is None:
            recommendation = "UNKNOWN_enable_bucket_metrics_for_analysis"
        else:
            recommendation = "IT_beneficial_objects_large_enough_to_justify_monitoring"

        findings.append({
            "bucket_name": bucket_name,
            "it_enabled": True,
            "avg_object_size_kb": round(avg_size_kb, 2) if avg_size_kb is not None else None,
            "object_count": object_count,
            "monthly_monitoring_cost": round(monthly_monitoring_cost, 4) if monthly_monitoring_cost is not None else None,
            "estimated_storage_savings": round(estimated_storage_savings, 4),
            "net_monthly_cost": round(net_monthly_cost, 4) if net_monthly_cost is not None else None,
            "recommendation": recommendation,
        })

    # Sort by net_monthly_cost descending (None values last)
    findings.sort(
        key=lambda f: f["net_monthly_cost"] if f["net_monthly_cost"] is not None else float("-inf"),
        reverse=True,
    )
    return findings
