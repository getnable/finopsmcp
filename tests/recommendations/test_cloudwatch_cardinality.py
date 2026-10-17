"""Tests for finops.recommendations.cloudwatch_cardinality."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from finops.recommendations.cloudwatch_cardinality import (
    FREE_METRICS,
    METRIC_COST_PER_MONTH,
    METRIC_PRICE_TIERS,
    _HIGH_CARDINALITY_THRESHOLD,
    _estimate_cost,
    _identify_high_cardinality_dimensions,
    audit_cloudwatch_metric_cardinality,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def _make_aws_client():
    client = SimpleNamespace(_session=None)
    return client


def _make_cw_mock(pages_by_namespace: dict[str | None, list[dict]]) -> MagicMock:
    """
    Return a mock CloudWatch client whose list_metrics paginator returns the
    given pages.

    pages_by_namespace: {namespace_or_None: [metric_dicts]}
      - None key is used when no Namespace kwarg is passed (full listing).
      - A namespace key is used when Namespace= is specified.
    """
    cw = MagicMock()

    def _make_paginator(operation_name):
        paginator = MagicMock()

        def _paginate(**kwargs):
            ns = kwargs.get("Namespace")
            key = ns if ns in pages_by_namespace else None
            metrics = pages_by_namespace.get(key, [])
            return iter([{"Metrics": metrics}])

        paginator.paginate.side_effect = _paginate
        return paginator

    cw.get_paginator.side_effect = _make_paginator
    return cw


# ── unit tests: pricing constants ─────────────────────────────────────────────

#
# AWS Price List, AmazonCloudWatch offer, us-east-1, version 20261007115540:
#   CW:MetricMonitorUsage  0-10,000 $0.30, 10,000-250,000 $0.10,
#                          250,000-1,000,000 $0.05, over 1,000,000 $0.02
#   Global-CW:MetricMonitorUsage 0-10 $0.00 (the free tier, account-wide)
# These tests used to pin a 10,000-metric free tier that does not exist:
# 5,000 custom metrics read $0 against a list price of $1,497/mo, and 50,000
# read $12,000 against $6,997 because every metric was priced at the first
# tier's $0.30.

def test_free_metrics_threshold():
    assert FREE_METRICS == 10


def test_metric_cost_constant():
    assert METRIC_COST_PER_MONTH == 0.30
    assert METRIC_PRICE_TIERS == (
        (10_000, 0.30), (250_000, 0.10), (1_000_000, 0.05), (float("inf"), 0.02))


def test_estimate_cost_bills_the_first_ten_thousand():
    """5,000 custom metrics are not free: only 10 are."""
    assert _estimate_cost(5_000) == round(5_000 * 0.30 - 10 * 0.30, 2) == 1497.0
    assert _estimate_cost(10) == 0.0


def test_estimate_cost_above_first_tier_is_tiered():
    """Past 10,000 metrics the rate drops to $0.10, not $0.30."""
    assert _estimate_cost(10_100) == round(10_000 * 0.30 + 100 * 0.10 - 3.0, 2) == 3007.0


def test_estimate_cost_large_count():
    assert _estimate_cost(50_000) == 6997.0
    expected = 10_000 * 0.30 + 240_000 * 0.10 + 750_000 * 0.05 + 500_000 * 0.02 - 3.0
    assert _estimate_cost(1_500_000) == round(expected, 2) == 74497.0


def test_estimate_cost_tiers_each_region_separately():
    """Each region's usage type is tiered on its own; the 10 free metrics are
    account-wide and come off once."""
    assert _estimate_cost({"us-east-1": 5_000, "eu-west-1": 5_000}) == 2997.0


def test_finding_is_not_a_measured_saving():
    """The custom-metric bill is not what a cardinality cut saves, so the
    finding is an investigation without a precise savings figure."""
    ns = "MyApp/Service"
    metrics = [
        {"Namespace": ns, "MetricName": f"m{i}",
         "Dimensions": [{"Name": "pod_id", "Value": str(i)}]}
        for i in range(150)
    ]
    cw_mock = _make_cw_mock({None: metrics, ns: metrics})
    with patch("finops.recommendations.cloudwatch_cardinality._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_metric_cardinality(_make_aws_client(), regions=["us-east-1"]))

    assert result["estimated_monthly_cost"] == round(150 * 0.30 - 3.0, 2)
    finding = result["finding"]
    assert finding is not None
    assert finding["kind"] == "investigation"
    assert finding["est_monthly_savings"] is None
    assert "two weeks" in finding["why_unsure"]


# ── unit tests: dimension detection ───────────────────────────────────────────

def test_identify_known_bad_dimensions():
    """pod_id and request_id are known high-cardinality dimensions."""
    metrics = [
        {"Dimensions": [{"Name": "pod_id", "Value": "abc"}, {"Name": "env", "Value": "prod"}]},
        {"Dimensions": [{"Name": "request_id", "Value": "xyz"}]},
    ]
    flagged = _identify_high_cardinality_dimensions(metrics)
    assert "pod_id" in flagged
    assert "request_id" in flagged


def test_safe_dimensions_not_flagged():
    """Stable dimensions like 'service' and 'env' should not be flagged."""
    metrics = [
        {"Dimensions": [{"Name": "service", "Value": "payments"}, {"Name": "env", "Value": "prod"}]},
    ]
    flagged = _identify_high_cardinality_dimensions(metrics)
    assert flagged == []


def test_empty_metrics_no_flags():
    assert _identify_high_cardinality_dimensions([]) == []


# ── integration tests: audit function ─────────────────────────────────────────

def test_returns_required_structure():
    """audit_cloudwatch_metric_cardinality must return all required keys."""
    cw_mock = _make_cw_mock({None: []})
    with patch("finops.recommendations.cloudwatch_cardinality._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_metric_cardinality(_make_aws_client(), regions=["us-east-1"]))

    assert "total_custom_metrics" in result
    assert "estimated_monthly_cost" in result
    assert "high_cardinality_namespaces" in result
    assert "by_region" in result


def test_aws_namespaces_excluded():
    """AWS/* namespaces must not appear in findings (they are free)."""
    # The full listing returns only an AWS namespace
    all_metrics = [
        {"Namespace": "AWS/EC2", "MetricName": "CPUUtilization", "Dimensions": []},
        {"Namespace": "AWS/Lambda", "MetricName": "Invocations", "Dimensions": []},
    ]
    cw_mock = _make_cw_mock({None: all_metrics})
    with patch("finops.recommendations.cloudwatch_cardinality._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_metric_cardinality(_make_aws_client(), regions=["us-east-1"]))

    assert result["total_custom_metrics"] == 0
    assert result["estimated_monthly_cost"] == 0.0
    assert result["high_cardinality_namespaces"] == []


def test_high_cardinality_namespace_flagged():
    """A namespace with >100 metrics should appear in high_cardinality_namespaces."""
    # Build 101 custom metrics for one namespace
    ns = "MyApp/Service"
    all_metrics = [{"Namespace": ns, "MetricName": f"m{i}", "Dimensions": []} for i in range(101)]
    ns_metrics = [{"Namespace": ns, "MetricName": f"m{i}", "Dimensions": []} for i in range(101)]

    cw_mock = _make_cw_mock({None: all_metrics, ns: ns_metrics})
    with patch("finops.recommendations.cloudwatch_cardinality._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_metric_cardinality(_make_aws_client(), regions=["us-east-1"]))

    assert len(result["high_cardinality_namespaces"]) == 1
    finding = result["high_cardinality_namespaces"][0]
    assert finding["namespace"] == ns
    assert finding["metric_count"] == 101
    assert finding["estimated_monthly_cost"] == round(101 * METRIC_COST_PER_MONTH, 2)


def test_low_count_namespace_not_flagged():
    """A namespace with <=100 metrics should not appear in findings."""
    ns = "MyApp/Small"
    all_metrics = [{"Namespace": ns, "MetricName": f"m{i}", "Dimensions": []} for i in range(50)]
    ns_metrics = [{"Namespace": ns, "MetricName": f"m{i}", "Dimensions": []} for i in range(50)]

    cw_mock = _make_cw_mock({None: all_metrics, ns: ns_metrics})
    with patch("finops.recommendations.cloudwatch_cardinality._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_metric_cardinality(_make_aws_client(), regions=["us-east-1"]))

    assert result["high_cardinality_namespaces"] == []


def test_bad_dimensions_surfaced_in_finding():
    """pod_id dimension in sampled metrics should appear in finding."""
    ns = "MyApp/Service"
    bad_metrics = [
        {"Namespace": ns, "MetricName": f"m{i}", "Dimensions": [{"Name": "pod_id", "Value": f"pod-{i}"}]}
        for i in range(150)
    ]
    cw_mock = _make_cw_mock({None: bad_metrics, ns: bad_metrics})
    with patch("finops.recommendations.cloudwatch_cardinality._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_metric_cardinality(_make_aws_client(), regions=["us-east-1"]))

    assert len(result["high_cardinality_namespaces"]) == 1
    finding = result["high_cardinality_namespaces"][0]
    assert "pod_id" in finding["high_cardinality_dimensions"]
    assert "pod_id" in finding["recommendation"]


def test_by_region_populated():
    """by_region should have an entry for each scanned region."""
    cw_mock = _make_cw_mock({None: []})
    with patch("finops.recommendations.cloudwatch_cardinality._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_metric_cardinality(_make_aws_client(), regions=["us-east-1", "eu-west-1"]))

    assert "us-east-1" in result["by_region"]
    assert "eu-west-1" in result["by_region"]
