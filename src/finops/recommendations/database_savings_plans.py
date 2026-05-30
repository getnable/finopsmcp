"""
Database Savings Plans recommender.

AWS launched Database Savings Plans at re:Invent 2025. They cover RDS and
Aurora spend with up to 45% savings, separate from Compute Savings Plans.

This module:
1. Pulls current RDS/Aurora spend from Cost Explorer (last 30 days).
2. Checks existing Savings Plans coverage filtered to database services.
3. Calculates uncovered RDS/Aurora baseline spend.
4. Recommends a 1-year no-upfront Database SP sized to the uncovered baseline.
5. Estimates savings using conservative discount rates.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

try:
    import boto3 as boto3
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]

# Discount rates relative to on-demand list price
DATABASE_SP_DISCOUNT_1YR_NO_UPFRONT = 0.30
DATABASE_SP_DISCOUNT_1YR_ALL_UPFRONT = 0.35
DATABASE_SP_DISCOUNT_3YR_ALL_UPFRONT = 0.45

_RDS_SERVICES = [
    "Amazon Relational Database Service",
    "Amazon Aurora",
]

_DAYS_IN_MONTH = 730 / 12  # average hours / 24


def _date_range_30d() -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=30)
    return start.isoformat(), end.isoformat()


def _get_rds_spend(ce_client: Any, start: str, end: str) -> float:
    """Total RDS + Aurora spend over the window."""
    try:
        resp = ce_client.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": _RDS_SERVICES}},
            Metrics=["UnblendedCost"],
        )
        return sum(
            float(p["Total"]["UnblendedCost"]["Amount"])
            for p in resp.get("ResultsByTime", [])
        )
    except Exception as e:
        log.warning("RDS spend fetch failed: %s", e)
        return 0.0


def _get_database_sp_coverage(ce_client: Any, start: str, end: str) -> float:
    """
    Savings Plans coverage % for RDS/Aurora services.

    AWS Cost Explorer supports filtering SP coverage by service dimension
    for Database Savings Plans.
    """
    try:
        resp = ce_client.get_savings_plans_coverage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": _RDS_SERVICES}},
        )
        totals = resp.get("Total", {}).get("CoverageHours", {})
        return float(totals.get("CoverageHoursPercentage", 0))
    except Exception as e:
        log.warning("Database SP coverage fetch failed: %s", e)
        return 0.0


def recommend_database_savings_plans() -> dict[str, Any] | None:
    """
    Analyse current RDS/Aurora spend and recommend Database Savings Plans.

    Returns None if AWS is not configured (boto3 unavailable or no credentials).

    Return dict keys:
        current_monthly_rds_spend     float  USD
        current_sp_coverage_pct       float  0-100
        uncovered_monthly_spend       float  USD
        recommended_sp_hourly_commitment float  USD/hr
        estimated_monthly_savings     float  USD
        estimated_annual_savings      float  USD
        payback_days                  int    0 for no-upfront
        recommendation_type           str
    """
    if boto3 is None:
        return None

    try:
        ce = boto3.client("ce", region_name="us-east-1")
        start, end = _date_range_30d()

        monthly_rds_spend = _get_rds_spend(ce, start, end)
        sp_coverage_pct = _get_database_sp_coverage(ce, start, end)

        uncovered_fraction = max(0.0, 1.0 - sp_coverage_pct / 100)
        uncovered_monthly = monthly_rds_spend * uncovered_fraction

        # Size the SP to cover the uncovered baseline at on-demand rates.
        # Hourly commitment = monthly uncovered / (hours in a month).
        hours_per_month = 730.0
        recommended_hourly = uncovered_monthly / hours_per_month if uncovered_monthly > 0 else 0.0

        estimated_monthly_savings = uncovered_monthly * DATABASE_SP_DISCOUNT_1YR_NO_UPFRONT
        estimated_annual_savings = estimated_monthly_savings * 12

        return {
            "current_monthly_rds_spend": round(monthly_rds_spend, 2),
            "current_sp_coverage_pct": round(sp_coverage_pct, 1),
            "uncovered_monthly_spend": round(uncovered_monthly, 2),
            "recommended_sp_hourly_commitment": round(recommended_hourly, 4),
            "estimated_monthly_savings": round(estimated_monthly_savings, 2),
            "estimated_annual_savings": round(estimated_annual_savings, 2),
            "payback_days": 0,  # no-upfront has no upfront cost
            "recommendation_type": "database_savings_plan_1yr_no_upfront",
        }

    except Exception as e:
        log.error("Database SP recommendation failed: %s", e)
        return None
