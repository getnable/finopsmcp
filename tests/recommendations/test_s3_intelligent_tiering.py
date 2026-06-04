"""Tests for finops.recommendations.s3_intelligent_tiering."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.s3_intelligent_tiering import (
    IT_BREAKEVEN_SIZE_KB,
    IT_MONITORING_COST_PER_1K_OBJECTS,
    _calculate_avg_object_size_kb,
    _estimate_storage_savings,
    _has_intelligent_tiering,
    audit_s3_intelligent_tiering,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def _make_aws_client():
    client = MagicMock()
    client._session = None
    return client


def _make_bucket(name: str) -> dict:
    return {"Name": name}


# ── unit: _calculate_avg_object_size_kb ──────────────────────────────────────

def test_avg_size_with_data():
    # 1000 objects, 100 MB total = 102.4 KB avg
    avg = _calculate_avg_object_size_kb(1000, 100 * 1024 * 1024)
    assert abs(avg - 102.4) < 0.1


def test_avg_size_returns_none_when_count_is_none():
    assert _calculate_avg_object_size_kb(None, 1000) is None


def test_avg_size_returns_none_when_size_is_none():
    assert _calculate_avg_object_size_kb(1000, None) is None


def test_avg_size_returns_zero_when_no_objects():
    assert _calculate_avg_object_size_kb(0, 0) == 0.0


# ── unit: monitoring cost formula ────────────────────────────────────────────

def test_monitoring_cost_formula():
    object_count = 1_000_000
    expected = (object_count / 1000.0) * IT_MONITORING_COST_PER_1K_OBJECTS
    # 1M objects * $0.0025/1k = $2.50
    assert abs(expected - 2.50) < 0.001


def test_breakeven_size_is_128kb():
    assert IT_BREAKEVEN_SIZE_KB == 128.0


# ── unit: _has_intelligent_tiering ────────────────────────────────────────────

def test_has_it_returns_true_when_configs_present():
    s3_client = MagicMock()
    s3_client.list_bucket_intelligent_tiering_configurations.return_value = {
        "IntelligentTieringConfigurationList": [{"Id": "default"}]
    }
    assert _has_intelligent_tiering(s3_client, "my-bucket") is True


def test_has_it_returns_false_when_no_configs():
    s3_client = MagicMock()
    s3_client.list_bucket_intelligent_tiering_configurations.return_value = {
        "IntelligentTieringConfigurationList": []
    }
    assert _has_intelligent_tiering(s3_client, "my-bucket") is False


def test_has_it_returns_false_on_exception():
    s3_client = MagicMock()
    s3_client.list_bucket_intelligent_tiering_configurations.side_effect = Exception("API error")
    assert _has_intelligent_tiering(s3_client, "my-bucket") is False


# ── unit: _estimate_storage_savings ──────────────────────────────────────────

def test_storage_savings_returns_zero_for_no_data():
    assert _estimate_storage_savings(None, None) == 0.0
    assert _estimate_storage_savings(0, 0) == 0.0


def test_storage_savings_positive_for_data():
    # 1 GB of objects should have some savings
    savings = _estimate_storage_savings(1000, 1 * 1024 ** 3)
    assert savings > 0


# ── integration: no IT-enabled buckets returns empty ────────────────────────

def test_returns_empty_when_no_it_buckets():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: s3_client if svc == "s3" else cw_client

        s3_client.list_buckets.return_value = {"Buckets": [_make_bucket("my-bucket")]}
        s3_client.list_bucket_intelligent_tiering_configurations.return_value = {
            "IntelligentTieringConfigurationList": []
        }

        result = _run(audit_s3_intelligent_tiering(aws_client=aws_client))

    assert result == []


# ── integration: small-object bucket flagged as waste ─────────────────────────

def test_small_object_bucket_flagged_as_likely_waste():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: s3_client if svc == "s3" else cw_client

        s3_client.list_buckets.return_value = {"Buckets": [_make_bucket("tiny-objects-bucket")]}
        s3_client.list_bucket_intelligent_tiering_configurations.return_value = {
            "IntelligentTieringConfigurationList": [{"Id": "default"}]
        }
        # 1M objects, 10 KB each = 10 GB total — avg 10 KB (well below 128 KB)
        cw_client.get_metric_statistics.side_effect = [
            # NumberOfObjects
            {"Datapoints": [{"Average": 1_000_000}]},
            # BucketSizeBytes: 1M * 10 KB = 10 GB
            {"Datapoints": [{"Average": 10 * 1024 ** 3}]},
        ]

        result = _run(audit_s3_intelligent_tiering(aws_client=aws_client))

    assert len(result) == 1
    finding = result[0]
    assert finding["bucket_name"] == "tiny-objects-bucket"
    assert finding["it_enabled"] is True
    assert finding["avg_object_size_kb"] < IT_BREAKEVEN_SIZE_KB
    # Monitoring ($2.50/mo on 1M objects) dwarfs the ~$0.11/mo storage savings,
    # so the ROI verdict is waste.
    assert finding["recommendation"] == "LIKELY_WASTE_monitoring_exceeds_savings"
    assert finding["monitoring_pct_of_savings"] >= 100
    assert finding["monthly_monitoring_cost"] is not None
    assert finding["monthly_monitoring_cost"] > 0
    # Contract guard: the three consolidated reports in server.py
    # (run_full_cost_audit, export_cost_report_csv, publish_cost_report_to_notion)
    # filter S3 IT waste with recommendation.startswith("LIKELY_WASTE"). If the
    # waste prefix ever changes, those aggregators silently drop S3 IT savings,
    # so pin the prefix here.
    assert finding["recommendation"].startswith("LIKELY_WASTE")


# ── integration: large-object bucket not flagged ──────────────────────────────

def test_large_object_bucket_not_flagged_as_waste():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: s3_client if svc == "s3" else cw_client

        s3_client.list_buckets.return_value = {"Buckets": [_make_bucket("big-objects-bucket")]}
        s3_client.list_bucket_intelligent_tiering_configurations.return_value = {
            "IntelligentTieringConfigurationList": [{"Id": "default"}]
        }
        # 1000 objects, 10 MB each = 10 GB total — avg 10 MB (well above 128 KB)
        cw_client.get_metric_statistics.side_effect = [
            {"Datapoints": [{"Average": 1000}]},
            {"Datapoints": [{"Average": 10 * 1024 ** 3}]},
        ]

        result = _run(audit_s3_intelligent_tiering(aws_client=aws_client))

    assert len(result) == 1
    # Tiny monitoring fee vs storage savings -> under the 8% threshold -> worth it.
    assert result[0]["recommendation"] == "IT_beneficial_monitoring_under_8pct_of_savings"
    assert result[0]["monitoring_pct_of_savings"] < result[0]["roi_threshold_pct"]


# ── integration: no CloudWatch data returns unknown recommendation ─────────────

def test_no_cw_data_returns_unknown_recommendation():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: s3_client if svc == "s3" else cw_client

        s3_client.list_buckets.return_value = {"Buckets": [_make_bucket("dark-bucket")]}
        s3_client.list_bucket_intelligent_tiering_configurations.return_value = {
            "IntelligentTieringConfigurationList": [{"Id": "default"}]
        }
        cw_client.get_metric_statistics.return_value = {"Datapoints": []}

        result = _run(audit_s3_intelligent_tiering(aws_client=aws_client))

    assert len(result) == 1
    assert result[0]["avg_object_size_kb"] is None
    assert result[0]["object_count"] is None
    assert result[0]["recommendation"] == "UNKNOWN_enable_bucket_metrics_for_analysis"


# ── integration: net_monthly_cost is negative for beneficial IT ───────────────

def test_net_cost_negative_means_it_is_saving_money():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: s3_client if svc == "s3" else cw_client

        s3_client.list_buckets.return_value = {"Buckets": [_make_bucket("archive-bucket")]}
        s3_client.list_bucket_intelligent_tiering_configurations.return_value = {
            "IntelligentTieringConfigurationList": [{"Id": "default"}]
        }
        # 100 objects, 10 GB each = 1 TB total — tiny monitoring cost, big savings
        one_tb_bytes = 1024 ** 4
        cw_client.get_metric_statistics.side_effect = [
            {"Datapoints": [{"Average": 100}]},
            {"Datapoints": [{"Average": one_tb_bytes}]},
        ]

        result = _run(audit_s3_intelligent_tiering(aws_client=aws_client))

    assert len(result) == 1
    finding = result[0]
    # net = monitoring_cost - savings; should be negative (IT is beneficial)
    assert finding["net_monthly_cost"] is not None
    assert finding["net_monthly_cost"] < 0


# ── regression: IT bucket bytes live under IntelligentTiering* storage types ──

def test_it_bucket_size_read_from_intelligent_tiering_storage_not_falsely_flagged():
    """On a real IT bucket, BucketSizeBytes has ~0 under StandardStorage; the bytes
    live under IntelligentTieringFAStorage. The old code queried StandardStorage
    only, read ~0, and falsely flagged EVERY IT bucket as tiny-object waste.
    Now the size is summed across IT classes, so a large-object bucket is not flagged."""
    aws_client = _make_aws_client()

    def _cw(**kwargs):
        metric = kwargs.get("MetricName")
        st = next((d["Value"] for d in kwargs.get("Dimensions", [])
                   if d["Name"] == "StorageType"), None)
        if metric == "NumberOfObjects":
            return {"Datapoints": [{"Average": 1000}]}          # 1000 objects
        if metric == "BucketSizeBytes" and st == "IntelligentTieringFAStorage":
            return {"Datapoints": [{"Average": 10 * 1024 ** 3}]}  # 10 GB -> 10 MB/obj
        return {"Datapoints": []}  # StandardStorage etc: empty, like a real IT bucket

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: s3_client if svc == "s3" else cw_client
        s3_client.list_buckets.return_value = {"Buckets": [_make_bucket("it-fa-bucket")]}
        s3_client.list_bucket_intelligent_tiering_configurations.return_value = {
            "IntelligentTieringConfigurationList": [{"Id": "default"}]
        }
        cw_client.get_metric_statistics.side_effect = _cw

        result = _run(audit_s3_intelligent_tiering(aws_client=aws_client))

    assert len(result) == 1
    finding = result[0]
    # ~10 MB avg object: large, so NOT flagged as waste (the FP we fixed)
    assert finding["avg_object_size_kb"] is not None
    assert finding["recommendation"] == "IT_beneficial_monitoring_under_8pct_of_savings"


# ── regression: ROI framing classifies the marginal band ──────────────────────

def test_roi_marginal_band_when_monitoring_is_large_share_of_savings():
    """When the monitoring fee is between 8% and 100% of the storage savings,
    Intelligent-Tiering is neither clearly worth it nor clearly waste: it should
    be surfaced as MARGINAL with the ROI math attached so the user can decide."""
    aws_client = _make_aws_client()

    # Tune object count + size so monitoring_pct lands in the 8-100% band.
    # 4,000,000 objects -> monitoring = 4000/1000... = $10.00/mo.
    # Total 2 GB -> savings = 2 * 0.0105 = $0.021/mo would be tiny; we want the
    # fee to be ~20-40% of savings, so pick a larger volume.
    # 200,000 objects -> monitoring = $0.50/mo. 200 GB -> savings = $2.10/mo.
    # 0.50 / 2.10 = 23.8% -> MARGINAL.
    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        s3_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: s3_client if svc == "s3" else cw_client
        s3_client.list_buckets.return_value = {"Buckets": [_make_bucket("marginal-bucket")]}
        s3_client.list_bucket_intelligent_tiering_configurations.return_value = {
            "IntelligentTieringConfigurationList": [{"Id": "default"}]
        }
        cw_client.get_metric_statistics.side_effect = [
            {"Datapoints": [{"Average": 200_000}]},          # objects
            {"Datapoints": [{"Average": 200 * 1024 ** 3}]},   # 200 GB
        ]

        result = _run(audit_s3_intelligent_tiering(aws_client=aws_client))

    finding = result[0]
    assert finding["recommendation"] == "MARGINAL_monitoring_is_a_large_share_of_savings"
    assert finding["roi_threshold_pct"] <= finding["monitoring_pct_of_savings"] < 100
    assert "marginal" in finding["roi_summary"].lower()
