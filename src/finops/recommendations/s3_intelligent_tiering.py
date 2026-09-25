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

from ..analyzers.cloudwatch import (
    MetricQuery,
    fetch_metric_values_by_region,
    s3_bucket_region,
)
from .envelope import INFERRED, Finding

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


def _bucket_storage_queries(bucket_name: str) -> list[MetricQuery]:
    """The CloudWatch series behind a bucket's object count and total size, as
    one period spanning the lookback window. Object count: AllStorageTypes
    covers every class in one query."""
    period = _LOOKBACK_DAYS * 86400
    return [
        MetricQuery((bucket_name, metric, storage_type), "AWS/S3", metric,
                    (("BucketName", bucket_name), ("StorageType", storage_type)),
                    "Average", period)
        for metric, storage_type in (
            [("NumberOfObjects", "AllStorageTypes")]
            + [("BucketSizeBytes", st) for st in _SIZE_STORAGE_TYPES]
        )
    ]


def _bucket_storage_stats(
    series: dict,
    bucket_name: str,
) -> tuple[int | None, float | None]:
    """
    Object count and total size for a bucket from its batched CloudWatch series.

    Returns (object_count, total_size_bytes). Both may be None if metrics are
    not available (bucket-level metrics must be explicitly enabled in S3), and
    a series that could not be read counts as not available.

    If ANY of the bucket's series could not be read, both come back None. The
    size is a sum over six storage classes, and summing the ones that read
    while one failed returned a partial size as if it were the whole bucket:
    small enough, next to a full object count, to call a bucket's
    Intelligent-Tiering waste on the strength of a throttled read.
    """
    if _bucket_read_failed(series, bucket_name):
        return None, None

    def _latest(key) -> float | None:
        values = series.get(key)
        return values[-1] if values else None   # oldest first

    object_count: int | None = None
    v = _latest((bucket_name, "NumberOfObjects", "AllStorageTypes"))
    if v is not None:
        object_count = int(v)

    total_size_bytes: float | None = None
    size_sum = 0.0
    found_size = False
    for st in _SIZE_STORAGE_TYPES:
        v = _latest((bucket_name, "BucketSizeBytes", st))
        if v is not None:
            size_sum += v
            found_size = True
    if found_size:
        total_size_bytes = size_sum

    return object_count, total_size_bytes


def _bucket_read_failed(series: dict, bucket_name: str) -> bool:
    """Whether any of the bucket's size or count series failed to read. A
    series that read empty ([]) is a real answer: the bucket holds nothing in
    that class. None, or a series never asked for, is not."""
    return any(series.get(q.key) is None for q in _bucket_storage_queries(bucket_name))


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
        regions:    Unused. Buckets are listed once from us-east-1 and each one's
                    storage metrics are read in its own region. Kept for API
                    consistency with other audit tools.

    Returns:
        List of dicts with findings, sorted by net_monthly_cost descending.
    """
    session = _make_boto_session(aws_client)

    s3_client = session.client("s3", region_name="us-east-1")

    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(days=_LOOKBACK_DAYS)

    try:
        buckets_resp = s3_client.list_buckets()
    except Exception as exc:
        log.error("list_buckets failed: %s", exc)
        return []

    findings: list[dict] = []

    it_buckets = [
        bucket for bucket in buckets_resp.get("Buckets", [])
        if _has_intelligent_tiering(s3_client, bucket["Name"])
    ]

    # S3 publishes storage metrics in the bucket's own region. Reading them all
    # from us-east-1 answered every other bucket with no data, which surfaced as
    # "enable bucket metrics" on buckets that have them. One batched read per
    # region, seven series per bucket.
    queries_by_region: dict[str, list[MetricQuery]] = {}
    for bucket in it_buckets:
        region = s3_bucket_region(s3_client, bucket, "us-east-1")
        queries_by_region.setdefault(region, []).extend(
            _bucket_storage_queries(bucket["Name"]))
    series = fetch_metric_values_by_region(
        lambda r: session.client("cloudwatch", region_name=r),
        queries_by_region, start_time, end_time)

    for bucket in it_buckets:
        bucket_name = bucket["Name"]

        object_count, total_size_bytes = _bucket_storage_stats(series, bucket_name)

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
        if monthly_monitoring_cost is None and _bucket_read_failed(series, bucket_name):
            recommendation = "UNKNOWN_metrics_could_not_be_read"
            roi_summary = ("The bucket's CloudWatch storage metrics could not all be read "
                           "(throttled or denied), so its Intelligent-Tiering ROI is not assessed.")
        elif monthly_monitoring_cost is None:
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
            "finding": None,
        })

    # Sort by net_monthly_cost descending (None values last)
    findings.sort(
        key=lambda f: f["net_monthly_cost"] if f["net_monthly_cost"] is not None else float("-inf"),
        reverse=True,
    )

    # Attach a trust-envelope Finding to the worst bucket, if any clears the bar.
    #
    # This is an INVESTIGATION, not a recommendation. The object count and
    # monitoring fee are measured, but the verdict ("waste" vs "worth it") turns on
    # estimated_storage_savings, which assumes EVERY object eventually moves to the
    # Infrequent Access tier (_IA_SAVINGS_PER_GB). That is an upper bound, not an
    # observed tier distribution, so we cannot put a precise saved-dollar figure on
    # flipping the bucket off Intelligent-Tiering. We size it as a band and tell the
    # user how to confirm.
    worst = next(
        (
            f for f in findings
            if f["monthly_monitoring_cost"] is not None
            and f["recommendation"].startswith("LIKELY_WASTE")
        ),
        None,
    )
    if worst is not None and worst["monthly_monitoring_cost"] >= 1.0:
        finding = Finding(
            source="s3_intelligent_tiering",
            title="Let's check whether Intelligent-Tiering is paying off on this bucket",
            why=("Intelligent-Tiering bills $0.0025 per 1,000 objects every month just to "
                 "monitor them. On a bucket full of small objects that monitoring fee can "
                 f"outweigh the tiering savings. '{worst['bucket_name']}' pays "
                 f"${worst['monthly_monitoring_cost']:.2f}/mo in monitoring and the tiering "
                 "savings look thin."),
            evidence=INFERRED,
            confidence="low",
            why_unsure=("The monitoring fee is real, but the savings side is an estimate: I "
                        "assumed every object eventually lands in the Infrequent Access tier "
                        "($0.0105/GB cheaper). I haven't seen the actual tier distribution or "
                        "access pattern, so I can't say the exact dollars you'd save by "
                        "switching off Intelligent-Tiering."),
            assumptions=[
                "All bytes settle in the Infrequent Access tier (an upper bound on savings).",
                "Bucket-level CloudWatch metrics reflect the steady-state object count and size.",
            ],
            rough_monthly=worst["monthly_monitoring_cost"],
            confirm_steps=[
                "Open S3 Storage Lens (or the bucket's Intelligent-Tiering metrics) and read "
                "the real split across the Frequent, Infrequent, and Archive tiers.",
                "If most bytes never leave Frequent Access, the monitoring fee is buying you "
                "nothing and S3 Standard (or Standard-IA with a lifecycle rule) is cheaper.",
            ],
            pro_can_confirm=True,
            pro_unlock=("On Pro, nable reads your Cost and Usage Report line items for this "
                        "bucket, sees the actual per-tier storage and the monitoring charge "
                        "side by side, and confirms whether Intelligent-Tiering is net "
                        "positive, no manual Storage Lens digging."),
            remediation=[
                "Confirm the tier split first (see steps above). Do not assume waste from the "
                "monitoring fee alone.",
                "If confirmed unhelpful, remove the Intelligent-Tiering configuration and set a "
                "plain lifecycle policy. Existing objects keep their data, only the tiering "
                "behavior changes.",
            ],
            resource_id=worst["bucket_name"],
            metadata={
                "monitoring_pct_of_savings": worst["monitoring_pct_of_savings"],
                "avg_object_size_kb": worst["avg_object_size_kb"],
                "object_count": worst["object_count"],
            },
        )
        worst["finding"] = finding.to_dict()

    return findings
