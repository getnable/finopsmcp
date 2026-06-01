"""Tests for finops.recommendations.s3_transfer_acceleration."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, call, patch

import pytest

from finops.recommendations.s3_transfer_acceleration import (
    _LOW_TRANSFER_THRESHOLD_GB,
    _LOW_BENEFIT_REGIONS,
    _TA_SURCHARGE_PER_GB,
    _build_waste_reasons,
    _bytes_to_gb,
    _check_cloudfront_distribution,
    _is_ta_enabled,
    _make_disable_command,
    audit_s3_transfer_acceleration,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def _make_aws_client():
    return MagicMock()


# ── unit: _bytes_to_gb ────────────────────────────────────────────────────────

class TestBytesToGb:
    def test_1_gib(self):
        result = _bytes_to_gb(1024 ** 3)
        assert abs(result - 1.0) < 0.0001

    def test_zero(self):
        assert _bytes_to_gb(0) == 0.0


# ── unit: _is_ta_enabled ──────────────────────────────────────────────────────

class TestIsTaEnabled:
    def test_enabled(self):
        s3 = MagicMock()
        s3.get_bucket_accelerate_configuration.return_value = {"Status": "Enabled"}
        assert _is_ta_enabled(s3, "my-bucket") is True

    def test_suspended(self):
        s3 = MagicMock()
        s3.get_bucket_accelerate_configuration.return_value = {"Status": "Suspended"}
        assert _is_ta_enabled(s3, "my-bucket") is False

    def test_no_status_key(self):
        s3 = MagicMock()
        s3.get_bucket_accelerate_configuration.return_value = {}
        assert _is_ta_enabled(s3, "my-bucket") is False

    def test_exception_returns_false(self):
        s3 = MagicMock()
        s3.get_bucket_accelerate_configuration.side_effect = Exception("access denied")
        assert _is_ta_enabled(s3, "my-bucket") is False


# ── unit: _build_waste_reasons ────────────────────────────────────────────────

class TestBuildWasteReasons:
    def test_low_volume_flagged(self):
        reasons = _build_waste_reasons(
            region="ap-southeast-1",
            monthly_transfer_gb=0.1,
            behind_cloudfront=False,
            has_cw_data=True,
        )
        assert any("low transfer" in r for r in reasons)

    def test_us_east_1_flagged(self):
        reasons = _build_waste_reasons(
            region="us-east-1",
            monthly_transfer_gb=100.0,
            behind_cloudfront=False,
            has_cw_data=True,
        )
        assert any("us-east-1" in r for r in reasons)

    def test_behind_cloudfront_flagged(self):
        reasons = _build_waste_reasons(
            region="ap-southeast-1",
            monthly_transfer_gb=100.0,
            behind_cloudfront=True,
            has_cw_data=True,
        )
        assert any("CloudFront" in r for r in reasons)

    def test_high_volume_non_us_east_no_cf_not_flagged(self):
        reasons = _build_waste_reasons(
            region="ap-southeast-1",
            monthly_transfer_gb=50.0,
            behind_cloudfront=False,
            has_cw_data=True,
        )
        assert reasons == []

    def test_no_cw_data_adds_reason(self):
        reasons = _build_waste_reasons(
            region="ap-southeast-1",
            monthly_transfer_gb=0.0,
            behind_cloudfront=False,
            has_cw_data=False,
        )
        assert any("metrics unavailable" in r for r in reasons)


# ── unit: _make_disable_command ───────────────────────────────────────────────

class TestMakeDisableCommand:
    def test_contains_bucket_name(self):
        cmd = _make_disable_command("my-test-bucket")
        assert "my-test-bucket" in cmd

    def test_contains_suspended(self):
        cmd = _make_disable_command("my-test-bucket")
        assert "Suspended" in cmd

    def test_uses_correct_api_call(self):
        cmd = _make_disable_command("my-test-bucket")
        assert "put-bucket-accelerate-configuration" in cmd


# ── unit: _check_cloudfront_distribution ─────────────────────────────────────

class TestCheckCloudfrontDistribution:
    def test_detects_distribution_with_bucket_origin(self):
        cf = MagicMock()
        cf.get_paginator.return_value.paginate.return_value = [
            {
                "DistributionList": {
                    "Items": [
                        {
                            "Origins": {
                                "Items": [
                                    {"DomainName": "my-bucket.s3.amazonaws.com"}
                                ]
                            }
                        }
                    ]
                }
            }
        ]
        assert _check_cloudfront_distribution(cf, "my-bucket") is True

    def test_returns_false_when_no_match(self):
        cf = MagicMock()
        cf.get_paginator.return_value.paginate.return_value = [
            {
                "DistributionList": {
                    "Items": [
                        {
                            "Origins": {
                                "Items": [
                                    {"DomainName": "other-bucket.s3.amazonaws.com"}
                                ]
                            }
                        }
                    ]
                }
            }
        ]
        assert _check_cloudfront_distribution(cf, "my-bucket") is False

    def test_returns_false_on_exception(self):
        cf = MagicMock()
        cf.get_paginator.side_effect = Exception("network error")
        assert _check_cloudfront_distribution(cf, "my-bucket") is False


# ── integration: audit_s3_transfer_acceleration ───────────────────────────────

class TestAuditS3TransferAcceleration:
    def _make_clients(
        self,
        buckets: list[dict],
        ta_status: str = "Enabled",
        location: str | None = None,
        transfer_bytes: float = 0.0,
        cf_distributions: list | None = None,
    ):
        s3 = MagicMock()
        cw = MagicMock()
        cf = MagicMock()

        s3.list_buckets.return_value = {"Buckets": buckets}
        s3.get_bucket_accelerate_configuration.return_value = {"Status": ta_status}
        s3.get_bucket_location.return_value = {"LocationConstraint": location}

        dp = [{"Sum": transfer_bytes}] if transfer_bytes > 0 else []
        cw.get_metric_statistics.return_value = {"Datapoints": dp}

        if cf_distributions is None:
            cf_distributions = []
        cf.get_paginator.return_value.paginate.return_value = [
            {"DistributionList": {"Items": cf_distributions}}
        ]

        mock_boto3 = MagicMock()

        def _client(svc, **kw):
            if svc == "s3":
                return s3
            if svc == "cloudwatch":
                return cw
            return cf

        mock_boto3.client.side_effect = _client
        return mock_boto3

    def test_returns_error_when_boto3_missing(self):
        with patch("finops.recommendations.s3_transfer_acceleration.boto3", None):
            result = _run(audit_s3_transfer_acceleration(aws_client=_make_aws_client()))
        assert "error" in result

    def test_empty_bucket_list_returns_empty(self):
        mock_boto3 = self._make_clients(buckets=[])

        with patch("finops.recommendations.s3_transfer_acceleration.boto3", mock_boto3):
            result = _run(audit_s3_transfer_acceleration(aws_client=_make_aws_client()))

        assert result["findings"] == []
        assert result["total_monthly_ta_cost"] == 0.0

    def test_ta_disabled_bucket_skipped(self):
        mock_boto3 = self._make_clients(
            buckets=[{"Name": "no-ta"}],
            ta_status="Suspended",
        )

        with patch("finops.recommendations.s3_transfer_acceleration.boto3", mock_boto3):
            result = _run(audit_s3_transfer_acceleration(aws_client=_make_aws_client()))

        assert result["findings"] == []

    def test_ta_enabled_low_volume_flagged_as_waste(self):
        mock_boto3 = self._make_clients(
            buckets=[{"Name": "low-volume-bucket"}],
            ta_status="Enabled",
            location="ap-southeast-1",
            transfer_bytes=500 * 1024,  # 500 KB, well below 1 GB threshold
        )

        with patch("finops.recommendations.s3_transfer_acceleration.boto3", mock_boto3):
            result = _run(audit_s3_transfer_acceleration(aws_client=_make_aws_client()))

        assert len(result["findings"]) == 1
        finding = result["findings"][0]
        assert finding["ta_enabled"] is True
        assert finding["likely_waste"] is True

    def test_us_east_1_bucket_flagged(self):
        mock_boto3 = self._make_clients(
            buckets=[{"Name": "us-east-bucket"}],
            ta_status="Enabled",
            location=None,  # None means us-east-1
            transfer_bytes=100 * 1024 ** 3,  # 100 GB, high volume
        )

        with patch("finops.recommendations.s3_transfer_acceleration.boto3", mock_boto3):
            result = _run(audit_s3_transfer_acceleration(aws_client=_make_aws_client()))

        assert len(result["findings"]) == 1
        assert result["findings"][0]["likely_waste"] is True
        assert result["findings"][0]["region"] == "us-east-1"

    def test_monthly_ta_cost_calculated(self):
        # _make_clients returns transfer_bytes for EACH of BytesDownloaded and
        # BytesUploaded, so total = 2 * transfer_bytes. Use 50 GB each to get
        # a 100 GB total and $4.00 expected TA surcharge.
        per_metric_gb = 50.0
        total_transfer_gb = per_metric_gb * 2  # two CW metric calls
        per_metric_bytes = per_metric_gb * 1024 ** 3

        mock_boto3 = self._make_clients(
            buckets=[{"Name": "big-transfer"}],
            ta_status="Enabled",
            location="ap-southeast-1",
            transfer_bytes=per_metric_bytes,
        )

        with patch("finops.recommendations.s3_transfer_acceleration.boto3", mock_boto3):
            result = _run(audit_s3_transfer_acceleration(aws_client=_make_aws_client()))

        finding = result["findings"][0]
        expected_cost = round(total_transfer_gb * _TA_SURCHARGE_PER_GB, 4)
        assert abs(finding["monthly_ta_cost"] - expected_cost) < 0.01

    def test_disable_command_included(self):
        mock_boto3 = self._make_clients(
            buckets=[{"Name": "needs-disabling"}],
            ta_status="Enabled",
            location="ap-southeast-1",
            transfer_bytes=0.0,
        )

        with patch("finops.recommendations.s3_transfer_acceleration.boto3", mock_boto3):
            result = _run(audit_s3_transfer_acceleration(aws_client=_make_aws_client()))

        assert "needs-disabling" in result["findings"][0]["disable_command"]
        assert "Suspended" in result["findings"][0]["disable_command"]

    def test_likely_waste_buckets_sorted_first(self):
        def _ta_enabled(bucket_name):
            return True

        s3 = MagicMock()
        s3.list_buckets.return_value = {
            "Buckets": [{"Name": "good-bucket"}, {"Name": "waste-bucket"}]
        }
        s3.get_bucket_accelerate_configuration.return_value = {"Status": "Enabled"}

        def _location(Bucket):
            return {"LocationConstraint": "ap-southeast-1" if Bucket == "good-bucket" else None}

        s3.get_bucket_location.side_effect = lambda **kw: _location(kw["Bucket"])

        cw = MagicMock()

        def _cw_metrics(**kw):
            bucket = next(
                d["Value"] for d in kw["Dimensions"] if d["Name"] == "BucketName"
            )
            if bucket == "good-bucket":
                # 50 GB, above threshold, non-us-east
                return {"Datapoints": [{"Sum": 50.0 * 1024 ** 3}]}
            return {"Datapoints": []}  # waste-bucket: no data

        cw.get_metric_statistics.side_effect = _cw_metrics

        cf = MagicMock()
        cf.get_paginator.return_value.paginate.return_value = [
            {"DistributionList": {"Items": []}}
        ]

        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: (
            s3 if svc == "s3" else (cw if svc == "cloudwatch" else cf)
        )

        with patch("finops.recommendations.s3_transfer_acceleration.boto3", mock_boto3):
            result = _run(audit_s3_transfer_acceleration(aws_client=_make_aws_client()))

        findings = result["findings"]
        assert len(findings) == 2
        # waste bucket (likely_waste=True) must come before good bucket
        assert findings[0]["likely_waste"] is True

    def test_all_required_keys_in_finding(self):
        mock_boto3 = self._make_clients(
            buckets=[{"Name": "test-bucket"}],
            ta_status="Enabled",
            location="eu-west-1",
            transfer_bytes=0.0,
        )

        with patch("finops.recommendations.s3_transfer_acceleration.boto3", mock_boto3):
            result = _run(audit_s3_transfer_acceleration(aws_client=_make_aws_client()))

        finding = result["findings"][0]
        required = {
            "bucket_name", "region", "ta_enabled", "monthly_transfer_gb",
            "monthly_ta_cost", "behind_cloudfront", "likely_waste",
            "reason", "disable_command",
        }
        assert required <= set(finding.keys())
