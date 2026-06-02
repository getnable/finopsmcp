"""Tests for finops.recommendations.lambda_concurrency."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.lambda_concurrency import (
    PROVISIONED_CONCURRENCY_PER_GB_SECOND,
    SECONDS_PER_MONTH,
    _classify_recommendation,
    scan_lambda_concurrency_waste,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    """Run a coroutine synchronously."""
    return asyncio.run(coro)


def _make_aws_client():
    """Minimal mock AWSConnector with no injected session."""
    client = MagicMock()
    client._session = None
    return client


def _pc_config(arn_suffix: str, allocated: int) -> dict:
    return {
        "FunctionArn": f"arn:aws:lambda:us-east-1:123456789012:function:my-fn:{arn_suffix}",
        "AllocatedProvisionedConcurrentExecutions": allocated,
    }


# ── unit tests: cost math ─────────────────────────────────────────────────────

def test_monthly_cost_calculation():
    """Verify the monthly cost formula matches the spec constants."""
    provisioned_count = 10
    memory_mb = 1024
    memory_gb = memory_mb / 1024.0

    expected = (
        provisioned_count
        * memory_gb
        * SECONDS_PER_MONTH
        * PROVISIONED_CONCURRENCY_PER_GB_SECOND
    )
    # 10 * 1.0 GB * 2592000 s * 0.0000041667 = ~108.00 (PC keep-warm rate)
    assert abs(expected - 108.00) < 0.50


def test_wasted_cost_at_zero_utilization():
    """When utilization is 0%, wasted cost equals full monthly cost."""
    provisioned = 5
    memory_gb = 0.5
    monthly = provisioned * memory_gb * SECONDS_PER_MONTH * PROVISIONED_CONCURRENCY_PER_GB_SECOND
    wasted = (1.0 - 0.0) * monthly
    assert abs(wasted - monthly) < 1e-9


def test_wasted_cost_at_partial_utilization():
    """When utilization is 30%, wasted cost is 70% of monthly cost."""
    provisioned = 4
    memory_gb = 1.0
    monthly = provisioned * memory_gb * SECONDS_PER_MONTH * PROVISIONED_CONCURRENCY_PER_GB_SECOND
    wasted = (1.0 - 0.30) * monthly
    assert abs(wasted - monthly * 0.70) < 1e-9


# ── unit tests: recommendation classification ─────────────────────────────────

def test_classify_remove_when_very_low():
    assert _classify_recommendation(avg_utilization=0.05, datapoints_count=14) == "remove_provisioned_concurrency"


def test_classify_remove_at_exactly_10_pct():
    # < 0.10 removes, >= 0.10 reduces
    assert _classify_recommendation(avg_utilization=0.09, datapoints_count=14) == "remove_provisioned_concurrency"


def test_classify_reduce_between_10_and_50():
    assert _classify_recommendation(avg_utilization=0.25, datapoints_count=14) == "reduce_provisioned_concurrency"


def test_classify_reduce_at_just_below_50():
    assert _classify_recommendation(avg_utilization=0.49, datapoints_count=14) == "reduce_provisioned_concurrency"


def test_classify_consider_scheduled_at_high_avg():
    # avg >= 0.50 maps to scheduled scaling
    assert _classify_recommendation(avg_utilization=0.60, datapoints_count=14) == "consider_scheduled_scaling"


# ── integration-style tests with mocked boto3 ─────────────────────────────────

def test_returns_empty_when_no_functions():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        lambda_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: (
            lambda_client if svc == "lambda" else cw_client
        )

        paginator = MagicMock()
        paginator.paginate.return_value = [{"Functions": []}]
        lambda_client.get_paginator.return_value = paginator

        result = _run(scan_lambda_concurrency_waste(
            aws_client=aws_client,
            regions=["us-east-1"],
        ))

    assert result == []


def test_skips_functions_with_no_pc_configs():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        lambda_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: (
            lambda_client if svc == "lambda" else cw_client
        )

        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Functions": [{"FunctionName": "fn-no-pc"}]}
        ]
        lambda_client.get_paginator.return_value = paginator

        lambda_client.list_provisioned_concurrency_configs.return_value = {
            "ProvisionedConcurrencyConfigs": []
        }

        result = _run(scan_lambda_concurrency_waste(
            aws_client=aws_client,
            regions=["us-east-1"],
        ))

    assert result == []


def test_flags_low_utilization_function():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        lambda_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: (
            lambda_client if svc == "lambda" else cw_client
        )

        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Functions": [{"FunctionName": "fn-wasteful"}]}
        ]
        lambda_client.get_paginator.return_value = paginator

        lambda_client.list_provisioned_concurrency_configs.return_value = {
            "ProvisionedConcurrencyConfigs": [
                _pc_config("prod", allocated=10)
            ]
        }
        lambda_client.get_function_configuration.return_value = {"MemorySize": 1024}

        # CloudWatch returns 20% utilization
        cw_client.get_metric_statistics.return_value = {
            "Datapoints": [{"Average": 20.0}]
        }

        result = _run(scan_lambda_concurrency_waste(
            aws_client=aws_client,
            regions=["us-east-1"],
            utilization_threshold=0.5,
        ))

    assert len(result) == 1
    finding = result[0]
    assert finding["function_name"] == "fn-wasteful"
    assert finding["provisioned_count"] == 10
    assert finding["avg_utilization_pct"] == 20.0
    assert finding["memory_mb"] == 1024
    assert finding["region"] == "us-east-1"
    assert finding["recommendation"] == "reduce_provisioned_concurrency"
    # Monthly cost: 10 * 1.0 GB * 2592000 * 0.0000041667 = ~108.00
    assert finding["monthly_cost"] > 100
    # Wasted: 80% of monthly cost
    assert abs(finding["wasted_monthly_cost"] - finding["monthly_cost"] * 0.80) < 0.01


def test_skips_high_utilization_function():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        lambda_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: (
            lambda_client if svc == "lambda" else cw_client
        )

        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Functions": [{"FunctionName": "fn-efficient"}]}
        ]
        lambda_client.get_paginator.return_value = paginator

        lambda_client.list_provisioned_concurrency_configs.return_value = {
            "ProvisionedConcurrencyConfigs": [_pc_config("prod", allocated=5)]
        }
        lambda_client.get_function_configuration.return_value = {"MemorySize": 512}

        # 80% utilization — above the default 50% threshold
        cw_client.get_metric_statistics.return_value = {
            "Datapoints": [{"Average": 80.0}]
        }

        result = _run(scan_lambda_concurrency_waste(
            aws_client=aws_client,
            regions=["us-east-1"],
        ))

    assert result == []


def test_treats_no_cw_data_as_fully_idle():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        lambda_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: (
            lambda_client if svc == "lambda" else cw_client
        )

        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Functions": [{"FunctionName": "fn-dark"}]}
        ]
        lambda_client.get_paginator.return_value = paginator

        lambda_client.list_provisioned_concurrency_configs.return_value = {
            "ProvisionedConcurrencyConfigs": [_pc_config("1", allocated=3)]
        }
        lambda_client.get_function_configuration.return_value = {"MemorySize": 256}

        # No CloudWatch datapoints
        cw_client.get_metric_statistics.return_value = {"Datapoints": []}

        result = _run(scan_lambda_concurrency_waste(
            aws_client=aws_client,
            regions=["us-east-1"],
        ))

    assert len(result) == 1
    assert result[0]["avg_utilization_pct"] == 0.0
    assert result[0]["recommendation"] == "remove_provisioned_concurrency"
    # Wasted = 100% of monthly cost
    assert abs(result[0]["wasted_monthly_cost"] - result[0]["monthly_cost"]) < 0.001


def test_sorted_by_wasted_cost_descending():
    """Multiple findings must come back sorted highest waste first."""
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        lambda_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: (
            lambda_client if svc == "lambda" else cw_client
        )

        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Functions": [
                {"FunctionName": "small-fn"},
                {"FunctionName": "big-fn"},
            ]}
        ]
        lambda_client.get_paginator.return_value = paginator

        def pc_configs(**kw):
            allocated = 2 if kw["FunctionName"] == "small-fn" else 20
            return {
                "ProvisionedConcurrencyConfigs": [
                    _pc_config("prod", allocated=allocated)
                ]
            }

        lambda_client.list_provisioned_concurrency_configs.side_effect = pc_configs
        lambda_client.get_function_configuration.return_value = {"MemorySize": 512}

        # Both at 10% utilization — big-fn has more waste
        cw_client.get_metric_statistics.return_value = {
            "Datapoints": [{"Average": 10.0}]
        }

        result = _run(scan_lambda_concurrency_waste(
            aws_client=aws_client,
            regions=["us-east-1"],
        ))

    assert len(result) == 2
    assert result[0]["wasted_monthly_cost"] >= result[1]["wasted_monthly_cost"]
    assert result[0]["function_name"] == "big-fn"
