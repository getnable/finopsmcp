"""Tests for finops.recommendations.database_savings_plans."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.database_savings_plans import (
    DATABASE_SP_DISCOUNT_1YR_ALL_UPFRONT,
    DATABASE_SP_DISCOUNT_1YR_NO_UPFRONT,
    DATABASE_SP_DISCOUNT_3YR_ALL_UPFRONT,
    _get_database_sp_coverage,
    _get_rds_spend,
    recommend_database_savings_plans,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _ce_cost_response(monthly_amount: float) -> dict:
    return {
        "ResultsByTime": [
            {"Total": {"UnblendedCost": {"Amount": str(monthly_amount)}}}
        ]
    }


def _ce_coverage_response(pct: float) -> dict:
    return {
        "Total": {
            "CoverageHours": {"CoverageHoursPercentage": str(pct)}
        }
    }


def _make_ce_client(spend: float, coverage_pct: float) -> MagicMock:
    ce = MagicMock()
    ce.get_cost_and_usage.return_value = _ce_cost_response(spend)
    ce.get_savings_plans_coverage.return_value = _ce_coverage_response(coverage_pct)
    return ce


# ── unit: discount constants are in expected order ────────────────────────────

class TestDiscountConstants:
    def test_1yr_no_upfront_below_all_upfront(self):
        assert DATABASE_SP_DISCOUNT_1YR_NO_UPFRONT < DATABASE_SP_DISCOUNT_1YR_ALL_UPFRONT

    def test_3yr_all_upfront_highest(self):
        assert DATABASE_SP_DISCOUNT_3YR_ALL_UPFRONT > DATABASE_SP_DISCOUNT_1YR_ALL_UPFRONT

    def test_no_upfront_is_30_pct(self):
        assert DATABASE_SP_DISCOUNT_1YR_NO_UPFRONT == 0.30


# ── unit: _get_rds_spend ──────────────────────────────────────────────────────

class TestGetRdsSpend:
    def test_sums_monthly_amounts(self):
        ce = MagicMock()
        ce.get_cost_and_usage.return_value = {
            "ResultsByTime": [
                {"Total": {"UnblendedCost": {"Amount": "1200.50"}}},
                {"Total": {"UnblendedCost": {"Amount": "800.00"}}},
            ]
        }
        result = _get_rds_spend(ce, "2026-04-01", "2026-05-01")
        assert abs(result - 2000.50) < 0.01

    def test_returns_zero_on_exception(self):
        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = Exception("access denied")
        result = _get_rds_spend(ce, "2026-04-01", "2026-05-01")
        assert result == 0.0

    def test_returns_zero_for_empty_response(self):
        ce = MagicMock()
        ce.get_cost_and_usage.return_value = {"ResultsByTime": []}
        result = _get_rds_spend(ce, "2026-04-01", "2026-05-01")
        assert result == 0.0


# ── unit: _get_database_sp_coverage ──────────────────────────────────────────

class TestGetDatabaseSpCoverage:
    def test_returns_coverage_pct(self):
        ce = MagicMock()
        ce.get_savings_plans_coverage.return_value = _ce_coverage_response(65.0)
        result = _get_database_sp_coverage(ce, "2026-04-01", "2026-05-01")
        assert abs(result - 65.0) < 0.01

    def test_returns_zero_on_exception(self):
        ce = MagicMock()
        ce.get_savings_plans_coverage.side_effect = Exception("not supported")
        result = _get_database_sp_coverage(ce, "2026-04-01", "2026-05-01")
        assert result == 0.0

    def test_returns_zero_when_no_coverage_data(self):
        ce = MagicMock()
        ce.get_savings_plans_coverage.return_value = {"Total": {}}
        result = _get_database_sp_coverage(ce, "2026-04-01", "2026-05-01")
        assert result == 0.0


# ── integration: recommend_database_savings_plans ─────────────────────────────

class TestRecommendDatabaseSavingsPlans:
    def _patch_boto3(self, ce_client):
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = ce_client
        return mock_boto3

    def test_returns_none_when_boto3_missing(self):
        with patch("finops.recommendations.database_savings_plans.boto3", None):
            result = recommend_database_savings_plans()
        assert result is None

    def test_returns_expected_keys(self):
        ce = _make_ce_client(spend=5000.0, coverage_pct=0.0)
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = ce

        with patch("finops.recommendations.database_savings_plans.boto3", mock_boto3):
            result = recommend_database_savings_plans()

        assert result is not None
        required_keys = {
            "current_monthly_rds_spend",
            "current_sp_coverage_pct",
            "uncovered_monthly_spend",
            "recommended_sp_hourly_commitment",
            "estimated_monthly_savings",
            "estimated_annual_savings",
            "payback_days",
            "recommendation_type",
        }
        assert required_keys <= set(result.keys())

    def test_zero_coverage_means_full_spend_uncovered(self):
        monthly_spend = 10_000.0
        ce = _make_ce_client(spend=monthly_spend, coverage_pct=0.0)
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = ce

        with patch("finops.recommendations.database_savings_plans.boto3", mock_boto3):
            result = recommend_database_savings_plans()

        assert result is not None
        assert result["uncovered_monthly_spend"] == monthly_spend

    def test_100_pct_coverage_means_zero_uncovered(self):
        ce = _make_ce_client(spend=10_000.0, coverage_pct=100.0)
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = ce

        with patch("finops.recommendations.database_savings_plans.boto3", mock_boto3):
            result = recommend_database_savings_plans()

        assert result is not None
        assert result["uncovered_monthly_spend"] == 0.0
        assert result["recommended_sp_hourly_commitment"] == 0.0
        assert result["estimated_monthly_savings"] == 0.0

    def test_savings_is_30_pct_of_uncovered(self):
        monthly_spend = 8_000.0
        ce = _make_ce_client(spend=monthly_spend, coverage_pct=0.0)
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = ce

        with patch("finops.recommendations.database_savings_plans.boto3", mock_boto3):
            result = recommend_database_savings_plans()

        assert result is not None
        expected_savings = round(monthly_spend * DATABASE_SP_DISCOUNT_1YR_NO_UPFRONT, 2)
        assert abs(result["estimated_monthly_savings"] - expected_savings) < 0.01

    def test_annual_savings_is_12x_monthly(self):
        ce = _make_ce_client(spend=6_000.0, coverage_pct=50.0)
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = ce

        with patch("finops.recommendations.database_savings_plans.boto3", mock_boto3):
            result = recommend_database_savings_plans()

        assert result is not None
        assert abs(result["estimated_annual_savings"] - result["estimated_monthly_savings"] * 12) < 0.01

    def test_payback_days_is_zero_for_no_upfront(self):
        ce = _make_ce_client(spend=5_000.0, coverage_pct=0.0)
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = ce

        with patch("finops.recommendations.database_savings_plans.boto3", mock_boto3):
            result = recommend_database_savings_plans()

        assert result is not None
        assert result["payback_days"] == 0

    def test_hourly_commitment_calculation(self):
        # Uncovered spend = 7300 / month, hourly = 7300 / 730
        ce = _make_ce_client(spend=7_300.0, coverage_pct=0.0)
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = ce

        with patch("finops.recommendations.database_savings_plans.boto3", mock_boto3):
            result = recommend_database_savings_plans()

        assert result is not None
        assert abs(result["recommended_sp_hourly_commitment"] - 10.0) < 0.01

    def test_returns_none_on_exception(self):
        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = Exception("auth failure")

        with patch("finops.recommendations.database_savings_plans.boto3", mock_boto3):
            result = recommend_database_savings_plans()

        assert result is None
