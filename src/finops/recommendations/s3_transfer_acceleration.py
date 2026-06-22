"""
S3 Transfer Acceleration waste detector.

S3 Transfer Acceleration routes uploads and downloads through AWS edge
locations. It adds a surcharge of $0.04-$0.08/GB on top of standard
S3 data transfer costs. Most teams enable it speculatively and forget it.

This module identifies TA-enabled buckets that are unlikely to benefit:
- Low transfer volume (<1 GB/month).
- Bucket in us-east-1, where TA rarely adds speed.
- Bucket already behind CloudFront (CloudFront is faster and cheaper for reads).
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

_TA_SURCHARGE_PER_GB = 0.04          # USD, conservative average
_LOW_TRANSFER_THRESHOLD_GB = 1.0     # Below this, TA adds cost with negligible benefit
_LOOKBACK_DAYS = 30
_BYTES_PER_GB = 1024 ** 3

# Regions where TA rarely helps because traffic already enters AWS backbone nearby
_LOW_BENEFIT_REGIONS = {"us-east-1", "us-east-2"}


def _bytes_to_gb(bytes_val: float) -> float:
    return bytes_val / _BYTES_PER_GB


def _get_bucket_region(s3_client: Any, bucket_name: str) -> str:
    """Return the AWS region a bucket is located in."""
    try:
        resp = s3_client.get_bucket_location(Bucket=bucket_name)
        location = resp.get("LocationConstraint")
        # us-east-1 returns None from get_bucket_location
        return location if location else "us-east-1"
    except Exception as e:
        log.debug("Could not get region for bucket %s: %s", bucket_name, e)
        return "unknown"


def _is_ta_enabled(s3_client: Any, bucket_name: str) -> bool:
    """Return True if Transfer Acceleration is enabled on this bucket."""
    try:
        resp = s3_client.get_bucket_accelerate_configuration(Bucket=bucket_name)
        status = resp.get("Status", "")
        return status == "Enabled"
    except Exception as e:
        log.debug("Could not get TA config for %s: %s", bucket_name, e)
        return False


def _get_monthly_transfer_gb(
    cw_client: Any,
    bucket_name: str,
    start: datetime,
    end: datetime,
) -> float:
    """
    Estimate monthly data transfer in GB using CloudWatch S3 metrics.

    Sums BytesDownloaded and BytesUploaded. Returns 0.0 if metrics are
    not available (request metrics are not enabled by default).
    """
    total_bytes = 0.0
    for metric in ("BytesDownloaded", "BytesUploaded"):
        try:
            resp = cw_client.get_metric_statistics(
                Namespace="AWS/S3",
                MetricName=metric,
                Dimensions=[
                    {"Name": "BucketName", "Value": bucket_name},
                    {"Name": "FilterId", "Value": "EntireBucket"},
                ],
                StartTime=start,
                EndTime=end,
                Period=_LOOKBACK_DAYS * 86400,
                Statistics=["Sum"],
                Unit="Bytes",
            )
            for dp in resp.get("Datapoints", []):
                total_bytes += dp.get("Sum", 0.0)
        except Exception as e:
            log.debug("CloudWatch metric %s unavailable for %s: %s", metric, bucket_name, e)

    return _bytes_to_gb(total_bytes)


def _check_cloudfront_distribution(cf_client: Any, bucket_name: str) -> bool:
    """
    Return True if any CloudFront distribution uses this bucket as an origin.

    Uses a simple string match on the distribution origin domain names.
    """
    try:
        pag = cf_client.get_paginator("list_distributions")
        for page in pag.paginate():
            dist_list = page.get("DistributionList", {})
            for dist in dist_list.get("Items", []):
                origins = dist.get("Origins", {}).get("Items", [])
                for origin in origins:
                    domain = origin.get("DomainName", "")
                    if bucket_name in domain:
                        return True
    except Exception as e:
        log.debug("CloudFront check failed: %s", e)
    return False


def _build_waste_reasons(
    region: str,
    monthly_transfer_gb: float,
    behind_cloudfront: bool,
    has_cw_data: bool,
) -> list[str]:
    reasons: list[str] = []
    if monthly_transfer_gb < _LOW_TRANSFER_THRESHOLD_GB:
        if has_cw_data:
            reasons.append(
                f"low transfer volume ({monthly_transfer_gb:.3f} GB/month, threshold {_LOW_TRANSFER_THRESHOLD_GB} GB)"
            )
        else:
            reasons.append("transfer metrics unavailable, cannot confirm benefit")
    if region in _LOW_BENEFIT_REGIONS:
        reasons.append(f"bucket in {region} where TA rarely improves speed")
    if behind_cloudfront:
        reasons.append("bucket already behind CloudFront (prefer CloudFront for reads)")
    return reasons


def _make_disable_command(bucket_name: str) -> str:
    return (
        f"aws s3api put-bucket-accelerate-configuration "
        f"--bucket {bucket_name} "
        f"--accelerate-configuration Status=Suspended"
    )


async def audit_s3_transfer_acceleration(aws_client: Any) -> dict:
    """
    Find S3 buckets with Transfer Acceleration enabled that are unlikely to benefit.

    Args:
        aws_client: AWSConnector (used for credential context; boto3 imported internally).

    Returns:
        Dict with findings list and summary totals.
    """
    if boto3 is None:
        return {
            "error": "boto3 not installed",
            "findings": [],
            "total_monthly_ta_cost": 0.0,
            "potential_monthly_savings": 0.0,
            "finding": None,
        }

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=_LOOKBACK_DAYS)

    try:
        s3 = boto3.client("s3", region_name="us-east-1")
        cw = boto3.client("cloudwatch", region_name="us-east-1")
        cf = boto3.client("cloudfront", region_name="us-east-1")
    except Exception as e:
        log.error("Failed to create AWS clients: %s", e)
        return {
            "error": f"AWS client creation failed: {e}",
            "findings": [],
            "total_monthly_ta_cost": 0.0,
            "potential_monthly_savings": 0.0,
            "finding": None,
        }

    try:
        bucket_list = s3.list_buckets().get("Buckets", [])
    except Exception as e:
        log.error("list_buckets failed: %s", e)
        return {
            "error": f"list_buckets failed: {e}",
            "findings": [],
            "total_monthly_ta_cost": 0.0,
            "potential_monthly_savings": 0.0,
            "finding": None,
        }

    findings: list[dict] = []

    for bucket in bucket_list:
        bucket_name = bucket["Name"]

        if not _is_ta_enabled(s3, bucket_name):
            continue

        region = _get_bucket_region(s3, bucket_name)
        monthly_transfer_gb = _get_monthly_transfer_gb(cw, bucket_name, start, now)
        has_cw_data = monthly_transfer_gb > 0.0
        behind_cf = _check_cloudfront_distribution(cf, bucket_name)

        monthly_ta_cost = round(monthly_transfer_gb * _TA_SURCHARGE_PER_GB, 4)

        waste_reasons = _build_waste_reasons(
            region, monthly_transfer_gb, behind_cf, has_cw_data
        )
        likely_waste = len(waste_reasons) > 0

        findings.append({
            "bucket_name": bucket_name,
            "region": region,
            "ta_enabled": True,
            "monthly_transfer_gb": round(monthly_transfer_gb, 4),
            "monthly_ta_cost": monthly_ta_cost,
            "behind_cloudfront": behind_cf,
            "likely_waste": likely_waste,
            "reason": "; ".join(waste_reasons) if waste_reasons else "transfer volume justifies TA",
            "disable_command": _make_disable_command(bucket_name),
        })

    # Sort: likely_waste first, then by monthly TA cost descending
    findings.sort(key=lambda x: (not x["likely_waste"], -x["monthly_ta_cost"]))

    waste_findings = [f for f in findings if f["likely_waste"]]
    total_monthly_ta_cost = round(sum(f["monthly_ta_cost"] for f in findings), 2)
    potential_savings = round(sum(f["monthly_ta_cost"] for f in waste_findings), 2)

    # Trust envelope: this is an INVESTIGATION, not a recommendation.
    #
    # We can directly observe that Transfer Acceleration is ON (a bucket setting) and
    # that the bucket looks like a poor fit (us-east-1, behind CloudFront, or low
    # measured transfer). But the SAVINGS number is soft on two counts:
    #   - the surcharge is a flat assumed average ($0.04/GB) across a $0.04-$0.08 range, and
    #   - bucket request metrics are off by default, so monthly_transfer_gb is usually
    #     0.0 (no data) rather than a measured volume.
    # So we flag the misconfiguration and band the savings, we do not claim a figure.
    finding = None
    if waste_findings:
        top = waste_findings[0]
        finding = Finding(
            source="s3_transfer_acceleration",
            title="Let's check whether Transfer Acceleration is earning its surcharge",
            why=("Transfer Acceleration adds roughly $0.04 to $0.08 per GB on top of normal S3 "
                 "transfer, in exchange for edge-routed uploads. "
                 f"'{top['bucket_name']}' has it enabled but looks like a poor fit: "
                 f"{top['reason']}. Teams often turn this on speculatively and forget it."),
            evidence=INFERRED,
            confidence="low",
            why_unsure=("I can see TA is on and that the bucket fits a low-benefit pattern, but I "
                        "can't price the waste precisely. Bucket request metrics are usually off, "
                        "so I often have no real transfer volume, and the per-GB surcharge varies "
                        "with destination, so my dollar figure would be a guess."),
            assumptions=[
                "TA surcharge approximated at a flat $0.04/GB (actual is $0.04-$0.08/GB).",
                "When CloudWatch transfer metrics are missing, true accelerated volume is unknown.",
            ],
            rough_monthly=potential_savings if potential_savings > 0 else None,
            confirm_steps=[
                "Enable S3 request/transfer metrics on the bucket (or check the S3 Transfer "
                "Acceleration usage line in your bill) to see how much data actually goes through "
                "the accelerated endpoint.",
                "If clients upload from far from the bucket region and the volume is real, TA may "
                "be worth keeping. If the bucket is in us-east-1, served via CloudFront, or barely "
                "used, it is almost certainly waste.",
            ],
            pro_can_confirm=True,
            pro_unlock=("On Pro, nable reads your Cost and Usage Report and pulls the S3 "
                        "Transfer-Acceleration usage type for each bucket, so it confirms the exact "
                        "accelerated GB and surcharge instead of estimating from CloudWatch."),
            remediation=[
                "Confirm real accelerated volume first (see steps above). Suspending TA on a "
                "bucket that genuinely needs it will slow distant uploads.",
                f"If confirmed wasteful, suspend it: {_make_disable_command(top['bucket_name'])}",
            ],
            resource_id=top["bucket_name"],
            metadata={
                "likely_waste_buckets": len(waste_findings),
                "region": top["region"],
                "behind_cloudfront": top["behind_cloudfront"],
                "monthly_transfer_gb": top["monthly_transfer_gb"],
            },
        )

    return {
        "findings": findings,
        "total_ta_enabled_buckets": len(findings),
        "likely_waste_buckets": len(waste_findings),
        "total_monthly_ta_cost": total_monthly_ta_cost,
        "potential_monthly_savings": potential_savings,
        "finding": finding.to_dict() if finding else None,
    }
