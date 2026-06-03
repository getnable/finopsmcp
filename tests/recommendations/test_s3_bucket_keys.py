"""Tests for finops.recommendations.s3_bucket_keys."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.s3_bucket_keys import (
    BUCKET_KEY_REDUCTION_FACTOR,
    KMS_COST_PER_10K_REQUESTS,
    _build_fix_command,
    _estimate_kms_calls,
    scan_s3_bucket_key_opportunities,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    """Run a coroutine synchronously."""
    return asyncio.run(coro)


def _make_aws_client():
    client = MagicMock()
    client._session = None
    return client


def _kms_enc_rule(kms_key_id: str = "arn:aws:kms:us-east-1:123:key/abc", bucket_key: bool = False):
    return {
        "ApplyServerSideEncryptionByDefault": {
            "SSEAlgorithm": "aws:kms",
            "KMSMasterKeyID": kms_key_id,
        },
        "BucketKeyEnabled": bucket_key,
    }


def _aes_enc_rule():
    return {
        "ApplyServerSideEncryptionByDefault": {
            "SSEAlgorithm": "AES256",
        },
        "BucketKeyEnabled": False,
    }


# ── unit tests: cost math ─────────────────────────────────────────────────────

def test_cost_math_at_100k_calls():
    """Verify cost math at a known call volume (100,000 requests)."""
    calls = 100_000
    expected_cost = (calls / 10_000) * KMS_COST_PER_10K_REQUESTS  # $0.30
    expected_savings = expected_cost * BUCKET_KEY_REDUCTION_FACTOR
    assert abs(expected_cost - 0.30) < 0.001
    assert abs(expected_savings - 0.297) < 0.001


def test_cost_at_high_call_volume():
    """1 million KMS calls/month should cost $3.00 and save $2.97."""
    calls = 1_000_000
    cost = (calls / 10_000) * KMS_COST_PER_10K_REQUESTS
    savings = cost * BUCKET_KEY_REDUCTION_FACTOR
    assert abs(cost - 3.00) < 0.001
    assert abs(savings - 2.97) < 0.001


def test_estimate_kms_calls_with_none_returns_none():
    # No request metrics must NOT fabricate a count (that invented savings on
    # every KMS bucket). It returns None and the caller reports zero savings.
    assert _estimate_kms_calls(None) is None


def test_estimate_kms_calls_with_data():
    assert _estimate_kms_calls(500_000) == 500_000


# ── unit tests: fix_command structure ─────────────────────────────────────────

def test_fix_command_contains_bucket_name():
    cmd = _build_fix_command("my-bucket", "arn:aws:kms:us-east-1:123:key/abc")
    assert "my-bucket" in cmd
    assert "put-bucket-encryption" in cmd
    assert "BucketKeyEnabled" in cmd


def test_fix_command_has_bucket_key_true():
    cmd = _build_fix_command("my-bucket", "arn:aws:kms:us-east-1:123:key/abc")
    # Parse out the JSON config from the command
    json_start = cmd.index("'{") + 1
    json_end = cmd.rindex("}'") + 1
    config = json.loads(cmd[json_start:json_end])
    rule = config["Rules"][0]
    assert rule["BucketKeyEnabled"] is True
    assert rule["ApplyServerSideEncryptionByDefault"]["KMSMasterKeyID"] == "arn:aws:kms:us-east-1:123:key/abc"


# ── integration-style tests with mocked boto3 ─────────────────────────────────

def test_returns_empty_when_no_buckets():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: (
            s3_client if svc == "s3" else cw_client
        )
        s3_client.list_buckets.return_value = {"Buckets": []}

        result = _run(scan_s3_bucket_key_opportunities(aws_client=aws_client))

    assert result == []


def test_skips_bucket_with_bucket_key_already_enabled():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: (
            s3_client if svc == "s3" else cw_client
        )

        s3_client.list_buckets.return_value = {"Buckets": [{"Name": "already-good"}]}
        s3_client.get_bucket_encryption.return_value = {
            "ServerSideEncryptionConfiguration": {
                "Rules": [_kms_enc_rule(bucket_key=True)]
            }
        }

        result = _run(scan_s3_bucket_key_opportunities(aws_client=aws_client))

    assert result == []


def test_skips_aes256_bucket():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: (
            s3_client if svc == "s3" else cw_client
        )

        s3_client.list_buckets.return_value = {"Buckets": [{"Name": "aes-bucket"}]}
        s3_client.get_bucket_encryption.return_value = {
            "ServerSideEncryptionConfiguration": {
                "Rules": [_aes_enc_rule()]
            }
        }

        result = _run(scan_s3_bucket_key_opportunities(aws_client=aws_client))

    assert result == []


def test_flags_kms_bucket_without_bucket_key():
    aws_client = _make_aws_client()
    key_id = "arn:aws:kms:us-east-1:123456789012:key/test-key"

    with patch("boto3.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: (
            s3_client if svc == "s3" else cw_client
        )

        s3_client.list_buckets.return_value = {"Buckets": [{"Name": "needs-bucket-key"}]}
        s3_client.get_bucket_encryption.return_value = {
            "ServerSideEncryptionConfiguration": {
                "Rules": [_kms_enc_rule(kms_key_id=key_id, bucket_key=False)]
            }
        }

        # No CloudWatch data available
        cw_client.get_metric_statistics.return_value = {"Datapoints": []}

        result = _run(scan_s3_bucket_key_opportunities(aws_client=aws_client))

    assert len(result) == 1
    finding = result[0]
    assert finding["bucket_name"] == "needs-bucket-key"
    assert finding["kms_key_id"] == key_id
    assert finding["bucket_key_enabled"] is False
    # No request metrics in this mock: surface the bucket (bucket keys are a
    # low-risk best practice) but with no fabricated savings.
    assert finding["estimated_monthly_kms_calls"] is None
    assert finding["estimated_savings"] == 0.0
    assert finding["note"] is not None
    assert "needs-bucket-key" in finding["fix_command"]
    assert "BucketKeyEnabled" in finding["fix_command"]


def test_uses_cloudwatch_data_when_available():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: (
            s3_client if svc == "s3" else cw_client
        )

        s3_client.list_buckets.return_value = {"Buckets": [{"Name": "big-bucket"}]}
        s3_client.get_bucket_encryption.return_value = {
            "ServerSideEncryptionConfiguration": {
                "Rules": [_kms_enc_rule(bucket_key=False)]
            }
        }

        # CloudWatch reports 2,000,000 requests over the period
        cw_client.get_metric_statistics.return_value = {
            "Datapoints": [{"Sum": 2_000_000.0}]
        }

        result = _run(scan_s3_bucket_key_opportunities(aws_client=aws_client))

    assert len(result) == 1
    finding = result[0]
    assert finding["estimated_monthly_kms_calls"] == 2_000_000
    # Cost: 2,000,000 / 10,000 * 0.03 = $6.00; savings = $5.94
    assert abs(finding["estimated_monthly_kms_cost"] - 6.00) < 0.01
    assert abs(finding["estimated_savings"] - 5.94) < 0.01


def test_sorted_by_estimated_savings_descending():
    """Buckets with higher call volume (more savings) should appear first."""
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: (
            s3_client if svc == "s3" else cw_client
        )

        s3_client.list_buckets.return_value = {
            "Buckets": [{"Name": "small"}, {"Name": "large"}]
        }
        s3_client.get_bucket_encryption.return_value = {
            "ServerSideEncryptionConfiguration": {
                "Rules": [_kms_enc_rule(bucket_key=False)]
            }
        }

        call_counts = {"small": 50_000.0, "large": 5_000_000.0}

        def cw_side_effect(**kwargs):
            dims = {d["Name"]: d["Value"] for d in kwargs.get("Dimensions", [])}
            bucket = dims.get("BucketName", "small")
            return {"Datapoints": [{"Sum": call_counts.get(bucket, 0.0)}]}

        cw_client.get_metric_statistics.side_effect = cw_side_effect

        result = _run(scan_s3_bucket_key_opportunities(aws_client=aws_client))

    assert len(result) == 2
    assert result[0]["estimated_savings"] >= result[1]["estimated_savings"]
    assert result[0]["bucket_name"] == "large"


def test_skips_bucket_with_no_encryption():
    """Buckets with no encryption config should be silently skipped."""
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: (
            s3_client if svc == "s3" else cw_client
        )

        s3_client.list_buckets.return_value = {"Buckets": [{"Name": "unencrypted"}]}

        # Simulate the NoSuchEncryptionConfig error via ClientError
        from botocore.exceptions import ClientError
        error = ClientError(
            {"Error": {"Code": "ServerSideEncryptionConfigurationNotFoundError", "Message": ""}},
            "GetBucketEncryption",
        )
        s3_client.get_bucket_encryption.side_effect = error
        # Patch the exceptions attribute so the isinstance check in the scanner works
        s3_client.exceptions.ClientError = ClientError

        result = _run(scan_s3_bucket_key_opportunities(aws_client=aws_client))

    assert result == []
