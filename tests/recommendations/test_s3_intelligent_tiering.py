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
    return asyncio.get_event_loop().run_until_complete(coro)


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
    assert finding["recommendation"] == "LIKELY_WASTE_switch_to_s3_standard_or_standard_ia"
    assert finding["monthly_monitoring_cost"] is not None
    assert finding["monthly_monitoring_cost"] > 0


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
    assert result[0]["recommendation"] == "IT_beneficial_objects_large_enough_to_justify_monitoring"


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
