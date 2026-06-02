"""Tests for finops.recommendations.cloudwatch_logs_ia."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from finops.recommendations.cloudwatch_logs_ia import (
    IA_INGESTION_COST_PER_GB,
    SAVINGS_PER_GB,
    STANDARD_INGESTION_COST_PER_GB,
    _BYTES_PER_GB,
    _MIN_AGE_DAYS,
    _MIN_INGESTION_GB,
    _build_recommendation,
    _group_age_days,
    audit_cloudwatch_logs_ia_opportunities,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def _make_aws_client():
    return SimpleNamespace(_session=None)


def _now():
    return datetime.now(tz=timezone.utc)


def _creation_time_ms(days_ago: int) -> int:
    dt = _now() - timedelta(days=days_ago)
    return int(dt.timestamp() * 1000)


def _make_log_group(
    name: str,
    storage_class: str = "STANDARD",
    stored_bytes: int = 0,
    retention_days: int | None = None,
    days_old: int = 60,
) -> dict:
    group: dict = {
        "logGroupName": name,
        "logGroupClass": storage_class,
        "storedBytes": stored_bytes,
        "creationTime": _creation_time_ms(days_old),
    }
    if retention_days is not None:
        group["retentionInDays"] = retention_days
    return group


def _make_logs_mock(groups: list[dict]) -> MagicMock:
    logs = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = iter([{"logGroups": groups}])
    logs.get_paginator.return_value = paginator
    return logs


def _make_cw_mock(incoming_bytes_sum: float = 0.0) -> MagicMock:
    """Return a CloudWatch mock that reports the given IncomingBytes sum."""
    cw = MagicMock()
    datapoints = [{"Sum": incoming_bytes_sum}] if incoming_bytes_sum > 0 else []
    cw.get_metric_statistics.return_value = {"Datapoints": datapoints}
    return cw


# ── unit tests: pricing constants ─────────────────────────────────────────────

def test_standard_ingestion_cost():
    assert STANDARD_INGESTION_COST_PER_GB == 0.50


def test_ia_ingestion_cost():
    assert IA_INGESTION_COST_PER_GB == 0.25


def test_savings_per_gb():
    assert abs(SAVINGS_PER_GB - 0.25) < 1e-9


# ── unit tests: group_age_days ─────────────────────────────────────────────────

def test_group_age_recent():
    group = _make_log_group("test", days_old=5)
    assert _group_age_days(group, _now()) == 5


def test_group_age_old():
    group = _make_log_group("test", days_old=90)
    assert _group_age_days(group, _now()) == 90


def test_group_age_missing_creation_time():
    group = {}
    assert _group_age_days(group, _now()) == 0


# ── unit tests: build_recommendation ──────────────────────────────────────────

def test_build_recommendation_contains_group_name():
    rec = _build_recommendation("/aws/lambda/my-fn", 1.50)
    assert "/aws/lambda/my-fn" in rec


def test_build_recommendation_contains_savings():
    rec = _build_recommendation("/aws/lambda/my-fn", 1.50)
    assert "1.50" in rec


# ── integration tests ──────────────────────────────────────────────────────────

def test_returns_required_structure():
    logs_mock = _make_logs_mock([])
    cw_mock = _make_cw_mock()
    with patch("finops.recommendations.cloudwatch_logs_ia._make_logs", return_value=logs_mock), \
         patch("finops.recommendations.cloudwatch_logs_ia._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_logs_ia_opportunities(_make_aws_client(), regions=["us-east-1"]))

    assert "total_groups_scanned" in result
    assert "total_candidates" in result
    assert "total_monthly_savings" in result
    assert "candidates" in result
    assert "by_region" in result


def test_ia_class_group_skipped():
    """Groups already on INFREQUENT_ACCESS storage class should be skipped."""
    group = _make_log_group("/aws/lambda/already-ia", storage_class="INFREQUENT_ACCESS", days_old=60)
    logs_mock = _make_logs_mock([group])
    cw_mock = _make_cw_mock(incoming_bytes_sum=10 * _BYTES_PER_GB)

    with patch("finops.recommendations.cloudwatch_logs_ia._make_logs", return_value=logs_mock), \
         patch("finops.recommendations.cloudwatch_logs_ia._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_logs_ia_opportunities(_make_aws_client(), regions=["us-east-1"]))

    assert result["total_candidates"] == 0


def test_young_group_skipped():
    """Groups younger than 30 days should not be flagged."""
    group = _make_log_group("/aws/lambda/new-fn", storage_class="STANDARD", days_old=10)
    logs_mock = _make_logs_mock([group])
    cw_mock = _make_cw_mock(incoming_bytes_sum=5 * _BYTES_PER_GB)

    with patch("finops.recommendations.cloudwatch_logs_ia._make_logs", return_value=logs_mock), \
         patch("finops.recommendations.cloudwatch_logs_ia._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_logs_ia_opportunities(_make_aws_client(), regions=["us-east-1"]))

    assert result["total_candidates"] == 0


def test_low_ingestion_skipped():
    """Groups ingesting less than 1 GB/month should not be flagged."""
    group = _make_log_group("/aws/lambda/quiet-fn", storage_class="STANDARD", days_old=60)
    # 0.5 GB
    logs_mock = _make_logs_mock([group])
    cw_mock = _make_cw_mock(incoming_bytes_sum=0.5 * _BYTES_PER_GB)

    with patch("finops.recommendations.cloudwatch_logs_ia._make_logs", return_value=logs_mock), \
         patch("finops.recommendations.cloudwatch_logs_ia._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_logs_ia_opportunities(_make_aws_client(), regions=["us-east-1"]))

    assert result["total_candidates"] == 0


def test_candidate_flagged_correctly():
    """A STANDARD group, 60 days old, ingesting 2 GB/month should be flagged."""
    group = _make_log_group("/aws/lambda/active-fn", storage_class="STANDARD", days_old=60, stored_bytes=int(50 * _BYTES_PER_GB))
    logs_mock = _make_logs_mock([group])
    cw_mock = _make_cw_mock(incoming_bytes_sum=2 * _BYTES_PER_GB)

    with patch("finops.recommendations.cloudwatch_logs_ia._make_logs", return_value=logs_mock), \
         patch("finops.recommendations.cloudwatch_logs_ia._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_logs_ia_opportunities(_make_aws_client(), regions=["us-east-1"]))

    assert result["total_candidates"] == 1
    c = result["candidates"][0]
    assert c["log_group_name"] == "/aws/lambda/active-fn"
    assert c["storage_class"] == "STANDARD"
    assert abs(c["monthly_ingestion_gb"] - 2.0) < 0.01
    expected_savings = round(2.0 * SAVINGS_PER_GB, 4)
    assert abs(c["monthly_savings"] - expected_savings) < 0.001
    assert abs(result["total_monthly_savings"] - expected_savings) < 0.001


def test_savings_math():
    """Verify cost and savings arithmetic: standard - ia = savings_per_gb * gb."""
    gb = 10.0
    expected_std = round(gb * STANDARD_INGESTION_COST_PER_GB, 4)
    expected_ia = round(gb * IA_INGESTION_COST_PER_GB, 4)
    expected_savings = round(gb * SAVINGS_PER_GB, 4)
    assert abs(expected_std - 5.0) < 0.001
    assert abs(expected_ia - 2.5) < 0.001
    assert abs(expected_savings - 2.5) < 0.001


def test_by_region_populated():
    logs_mock = _make_logs_mock([])
    cw_mock = _make_cw_mock()
    with patch("finops.recommendations.cloudwatch_logs_ia._make_logs", return_value=logs_mock), \
         patch("finops.recommendations.cloudwatch_logs_ia._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_logs_ia_opportunities(_make_aws_client(), regions=["us-east-1", "us-west-2"]))

    assert "us-east-1" in result["by_region"]
    assert "us-west-2" in result["by_region"]
    for data in result["by_region"].values():
        assert "groups_scanned" in data
        assert "candidates_count" in data
        assert "monthly_savings" in data


def test_retention_period_preserved():
    """retention_days should be in candidate output when set."""
    group = _make_log_group("/my/app", storage_class="STANDARD", days_old=60, retention_days=90)
    logs_mock = _make_logs_mock([group])
    cw_mock = _make_cw_mock(incoming_bytes_sum=3 * _BYTES_PER_GB)

    with patch("finops.recommendations.cloudwatch_logs_ia._make_logs", return_value=logs_mock), \
         patch("finops.recommendations.cloudwatch_logs_ia._make_cw", return_value=cw_mock):
        result = _run(audit_cloudwatch_logs_ia_opportunities(_make_aws_client(), regions=["us-east-1"]))

    assert result["total_candidates"] == 1
    assert result["candidates"][0]["retention_days"] == 90
