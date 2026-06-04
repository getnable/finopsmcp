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
# Intelligent-Tiering is worth its monitoring fee when that fee is a small slice
# of the storage savings it unlocks. If monitoring costs <8% of the savings, keep
# it; if it eats more, it is marginal; if it exceeds the savings, it is waste.
IT_ROI_THRESHOLD_PCT: float = 8.0
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

    def _latest(resp) -> float | None:
        dps = resp.get("Datapoints", [])
        if not dps:
            return None
        # Sentinel must be a datetime, not int 0: boto Timestamps are tz-aware
        # datetimes and `datetime > 0` raises TypeError. Skip datapoints with no
        # usable Average rather than KeyError-ing the whole storage class.
        _floor = datetime.min.replace(tzinfo=timezone.utc)
        usable = [d for d in dps if d.get("Average") is not None]
        if not usable:
            return None
        return max(usable, key=lambda d: d.get("Timestamp") or _floor)["Average"]

    def _query(metric: str, storage_type: str):
        return cw_client.get_metric_statistics(
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

    # Object count: AllStorageTypes covers every class in one query.
    try:
        v = _latest(_query("NumberOfObjects", "AllStorageTypes"))
        if v is not None:
            object_count = int(v)
    except Exception as exc:
        log.debug("CW NumberOfObjects failed for %s: %s", bucket_name, exc)

    # Size: BucketSizeBytes has NO AllStorageTypes aggregate, so it must be summed
    # per storage class. On an Intelligent-Tiering bucket the bytes live under the
    # IntelligentTiering* classes, not StandardStorage. Querying StandardStorage
    # alone read ~0 and made avg-object-size tiny, which falsely flagged EVERY
    # IT bucket as waste. Sum the classes an IT bucket actually uses.
    _SIZE_STORAGE_TYPES = [
        "StandardStorage",
        "IntelligentTieringFAStorage",   # frequent access
        "IntelligentTieringIAStorage",   # infrequent access
        "IntelligentTieringAAStorage",   # archive instant access
        "IntelligentTieringAIAStorage",  # archive access
        "IntelligentTieringDAAStorage",  # deep archive access
    ]
    size_sum = 0.0
    found_size = False
    for st in _SIZE_STORAGE_TYPES:
        try:
            v = _latest(_query("BucketSizeBytes", st))
            if v is not None:
                size_sum += v
                found_size = True
        except Exception as exc:
            log.debug("CW BucketSizeBytes[%s] failed for %s: %s", st, bucket_name, exc)
    if found_size:
        total_size_bytes = size_sum

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

        # ROI framing: is the monitoring fee a small slice of the savings it
        # unlocks? monitoring_pct_of_savings = monitoring_cost / storage_savings.
        # < 8%  -> clearly worth it. 8-100% -> marginal (review). >= 100% (or no
        # savings) -> the fee meets/exceeds the benefit, IT is waste here.
        monitoring_pct_of_savings: float | None = None
        if monthly_monitoring_cost is None:
            recommendation = "UNKNOWN_enable_bucket_metrics_for_analysis"
            roi_summary = "Enable S3 bucket-level metrics to assess Intelligent-Tiering ROI."
        elif estimated_storage_savings <= 0:
            recommendation = "LIKELY_WASTE_no_tiering_savings_to_justify_monitoring"
            roi_summary = (
                f"Monitoring costs ${monthly_monitoring_cost:.2f}/mo but tiering yields no "
                f"estimated storage savings, so the fee is pure overhead. Consider S3 Standard / Standard-IA."
            )
        else:
            monitoring_pct_of_savings = round(
                monthly_monitoring_cost / estimated_storage_savings * 100, 1)
            if monitoring_pct_of_savings < IT_ROI_THRESHOLD_PCT:
                recommendation = "IT_beneficial_monitoring_under_8pct_of_savings"
                verdict = "worth it"
            elif monitoring_pct_of_savings < 100:
                recommendation = "MARGINAL_monitoring_is_a_large_share_of_savings"
                verdict = "marginal"
            else:
                recommendation = "LIKELY_WASTE_monitoring_exceeds_savings"
                verdict = "not worth it"
            roi_summary = (
                f"Monitoring ${monthly_monitoring_cost:.2f}/mo is {monitoring_pct_of_savings}% of the "
                f"~${estimated_storage_savings:.2f}/mo storage savings Intelligent-Tiering unlocks "
                f"(upper bound). Under {IT_ROI_THRESHOLD_PCT:.0f}% is worth it: this is {verdict}."
            )

        findings.append({
            "bucket_name": bucket_name,
            "it_enabled": True,
            "avg_object_size_kb": round(avg_size_kb, 2) if avg_size_kb is not None else None,
            "object_count": object_count,
            "monthly_monitoring_cost": round(monthly_monitoring_cost, 4) if monthly_monitoring_cost is not None else None,
            "estimated_storage_savings": round(estimated_storage_savings, 4),
            "estimated_storage_savings_is_upper_bound": True,
            "monitoring_pct_of_savings": monitoring_pct_of_savings,
            "roi_threshold_pct": IT_ROI_THRESHOLD_PCT,
            "net_monthly_cost": round(net_monthly_cost, 4) if net_monthly_cost is not None else None,
            "recommendation": recommendation,
            "roi_summary": roi_summary,
        })

    # Sort by net_monthly_cost descending (None values last)
    findings.sort(
        key=lambda f: f["net_monthly_cost"] if f["net_monthly_cost"] is not None else float("-inf"),
        reverse=True,
    )
    return findings
