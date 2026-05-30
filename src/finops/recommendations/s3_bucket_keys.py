"""
S3 Bucket Key opportunity scanner.

S3 Bucket Keys reduce KMS API calls by up to 99% by caching the data key
at the bucket level instead of calling KMS for every object PUT/GET.
KMS API calls cost $0.03 per 10,000 requests.

This scanner finds buckets using aws:kms encryption without Bucket Keys enabled
and estimates monthly savings.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# $0.03 per 10,000 KMS API requests
KMS_COST_PER_10K_REQUESTS: float = 0.03
BUCKET_KEY_REDUCTION_FACTOR: float = 0.99  # ~99% reduction in KMS calls

_LOOKBACK_DAYS = 30
# Estimated KMS calls per S3 PUT/GET when bucket key is disabled
# Each PUT and each GET triggers a GenerateDataKey / Decrypt call respectively
_ASSUMED_PUT_GET_RATIO = 0.5  # rough split — we count both as KMS calls
_FALLBACK_MONTHLY_KMS_CALLS = 100_000  # conservative fallback when no CW data


def _make_boto_session(aws_client: Any):
    """Return a boto3 session from the AWSConnector, or a fresh default session."""
    import boto3

    if hasattr(aws_client, "_session") and aws_client._session is not None:
        return aws_client._session
    return boto3.Session()


def _get_bucket_request_count(
    cw_client: Any,
    bucket_name: str,
    start: datetime,
    end: datetime,
) -> int | None:
    """
    Fetch total AllRequests metric for a bucket over the window.
    Returns None if bucket request metrics are not enabled.
    """
    try:
        resp = cw_client.get_metric_statistics(
            Namespace="AWS/S3",
            MetricName="AllRequests",
            Dimensions=[
                {"Name": "BucketName",  "Value": bucket_name},
                {"Name": "FilterId",    "Value": "EntireBucket"},
            ],
            StartTime=start,
            EndTime=end,
            Period=_LOOKBACK_DAYS * 86400,
            Statistics=["Sum"],
        )
        datapoints = resp.get("Datapoints", [])
        if not datapoints:
            return None
        total = sum(d["Sum"] for d in datapoints)
        return int(total)
    except Exception as exc:
        log.debug("AllRequests metric fetch failed for %s: %s", bucket_name, exc)
        return None


def _estimate_kms_calls(total_requests: int | None) -> int:
    """
    Estimate monthly KMS API call count from S3 request volume.
    Each S3 request with SSE-KMS triggers one KMS call when bucket key is off.
    """
    if total_requests is None:
        return _FALLBACK_MONTHLY_KMS_CALLS
    return total_requests


def _build_fix_command(bucket_name: str, kms_key_id: str) -> str:
    config = {
        "Rules": [
            {
                "ApplyServerSideEncryptionByDefault": {
                    "SSEAlgorithm": "aws:kms",
                    "KMSMasterKeyID": kms_key_id,
                },
                "BucketKeyEnabled": True,
            }
        ]
    }
    config_json = json.dumps(config)
    return (
        f"aws s3api put-bucket-encryption "
        f"--bucket {bucket_name} "
        f"--server-side-encryption-configuration '{config_json}'"
    )


async def scan_s3_bucket_key_opportunities(aws_client: Any) -> list[dict]:
    """
    Scan S3 buckets using aws:kms encryption without Bucket Keys enabled.

    For each affected bucket, estimates monthly KMS API call volume from
    CloudWatch request metrics (falls back to a conservative estimate when
    bucket-level metrics are not enabled), then calculates potential savings.

    Args:
        aws_client: AWSConnector instance (provides boto3 session).

    Returns:
        List of dicts with affected buckets, sorted by estimated_savings descending.
    """
    session = _make_boto_session(aws_client)

    s3_client = session.client("s3", region_name="us-east-1")
    cw_client = session.client("cloudwatch", region_name="us-east-1")

    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(days=_LOOKBACK_DAYS)

    # List all buckets
    try:
        buckets_resp = s3_client.list_buckets()
    except Exception as exc:
        log.error("list_buckets failed: %s", exc)
        return []

    buckets = buckets_resp.get("Buckets", [])
    findings: list[dict] = []

    for bucket in buckets:
        bucket_name = bucket["Name"]

        # Get encryption config
        try:
            enc_resp = s3_client.get_bucket_encryption(Bucket=bucket_name)
        except s3_client.exceptions.ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("ServerSideEncryptionConfigurationNotFound", "NoSuchBucketPolicy"):
                # No encryption configured — not relevant
                continue
            log.debug("get_bucket_encryption failed for %s: %s", bucket_name, exc)
            continue
        except Exception as exc:
            log.debug("get_bucket_encryption failed for %s: %s", bucket_name, exc)
            continue

        rules = (
            enc_resp
            .get("ServerSideEncryptionConfiguration", {})
            .get("Rules", [])
        )

        for rule in rules:
            default_enc = rule.get("ApplyServerSideEncryptionByDefault", {})
            algorithm = default_enc.get("SSEAlgorithm", "")
            bucket_key_enabled = rule.get("BucketKeyEnabled", False)

            if algorithm != "aws:kms":
                continue
            if bucket_key_enabled:
                continue

            kms_key_id = default_enc.get("KMSMasterKeyID", "aws/s3")

            # Try to get request volume from CloudWatch
            total_requests = _get_bucket_request_count(
                cw_client, bucket_name, start_time, end_time
            )
            estimated_monthly_kms_calls = _estimate_kms_calls(total_requests)

            # Cost: $0.03 per 10,000 requests
            estimated_monthly_kms_cost = (
                estimated_monthly_kms_calls / 10_000
            ) * KMS_COST_PER_10K_REQUESTS

            estimated_savings = estimated_monthly_kms_cost * BUCKET_KEY_REDUCTION_FACTOR

            findings.append({
                "bucket_name": bucket_name,
                "kms_key_id": kms_key_id,
                "bucket_key_enabled": False,
                "estimated_monthly_kms_calls": estimated_monthly_kms_calls,
                "estimated_monthly_kms_cost": round(estimated_monthly_kms_cost, 4),
                "estimated_savings": round(estimated_savings, 4),
                "fix_command": _build_fix_command(bucket_name, kms_key_id),
            })
            break  # one finding per bucket is enough

    findings.sort(key=lambda f: f["estimated_savings"], reverse=True)
    return findings
