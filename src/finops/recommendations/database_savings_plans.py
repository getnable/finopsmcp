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

from .envelope import INFERRED, Finding

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


def _get_rds_spend(ce_client: Any, start: str, end: str) -> float | None:
    """
    RDS + Aurora COMPUTE (instance-hours) spend over the window. Returns None when
    the Cost Explorer query fails, so the caller can distinguish a real $0 from a
    failed fetch instead of emitting a confident "no Savings Plan needed".

    Database Savings Plans only discount instance running hours. They do NOT
    cover storage, provisioned IOPS, backups, snapshots, or data transfer.
    Summing total RDS spend oversizes the recommended commitment, so we group
    by usage type and keep only the instance-usage line items.
    """
    try:
        resp = ce_client.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": _RDS_SERVICES}},
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
        total = 0.0
        for period in resp.get("ResultsByTime", []):
            for grp in period.get("Groups", []):
                ut = (grp.get("Keys", [""])[0] or "").lower()
                # Instance hours: "...-InstanceUsage:db.r5.large", "...-Multi-AZUsage:db..."
                if "instanceusage" in ut or "multi-azusage" in ut:
                    total += float(grp["Metrics"]["UnblendedCost"]["Amount"])
        return total
    except Exception as e:
        log.warning("RDS compute spend fetch failed: %s", e)
        return None


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
        from .._logutil import note_sp_error
        note_sp_error(log, "Database SP coverage", e)
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
        if monthly_rds_spend is None:
            # CE query failed. Do not emit a confident $0 "no SP needed" result
            # that is indistinguishable from a genuine zero.
            return {
                "data_incomplete": True,
                "error": "Could not read RDS compute spend from Cost Explorer "
                         "(throttle, permissions, or outage). No recommendation made.",
                "recommendation_type": "database_savings_plan_1yr_no_upfront",
                "finding": None,
            }
        sp_coverage_pct = _get_database_sp_coverage(ce, start, end)

        uncovered_fraction = max(0.0, 1.0 - sp_coverage_pct / 100)
        uncovered_monthly = monthly_rds_spend * uncovered_fraction

        # Size the SP at the DISCOUNTED hourly rate you commit to, not on-demand
        # dollars. uncovered_monthly is on-demand spend; committing on-demand/730
        # per hour over-buys by 1/(1-discount). The commitment is the post-SP rate.
        hours_per_month = 730.0
        recommended_hourly = (
            uncovered_monthly * (1.0 - DATABASE_SP_DISCOUNT_1YR_NO_UPFRONT) / hours_per_month
            if uncovered_monthly > 0 else 0.0
        )

        estimated_monthly_savings = uncovered_monthly * DATABASE_SP_DISCOUNT_1YR_NO_UPFRONT
        estimated_annual_savings = estimated_monthly_savings * 12

        # Classify the finding. The uncovered baseline and current coverage are measured
        # from Cost Explorer, but a Savings Plan is a 1-year commitment, and the saving
        # only lands if that RDS/Aurora baseline keeps running for the term. We have only
        # looked at the last 30 days, so we cannot confirm the spend is stable enough to
        # commit to. That makes this an investigation: a band plus the steps to confirm
        # the baseline holds, not a precise promise. The discount rate is also a
        # conservative published estimate, not your account's actual SP rate card.
        finding = None
        if uncovered_monthly > 50:
            finding = Finding(
                source="database_savings_plans",
                title="Uncovered RDS/Aurora spend may be worth a Database Savings Plan",
                why=("Database Savings Plans discount RDS and Aurora instance hours. About "
                     f"${round(uncovered_monthly, 0):,.0f}/mo of your database compute is "
                     f"running on demand with only {round(sp_coverage_pct, 0):.0f}% covered. "
                     "Committing to a Savings Plan on the steady part of that would cut the "
                     "rate."),
                evidence=INFERRED,
                confidence="medium" if sp_coverage_pct < 50 else "low",
                why_unsure=("We measured the last 30 days of uncovered instance-hour spend, "
                            "but a Savings Plan locks you in for a year. We have not confirmed "
                            "this baseline is stable: if you are about to downsize, migrate "
                            "off RDS, or move to Aurora Serverless, committing to it would "
                            "strand the commitment. The discount is also a conservative "
                            "published estimate, not your account's exact SP rate."),
                assumptions=[
                    "The uncovered RDS/Aurora baseline keeps running at this level for the "
                    "1-year term.",
                    "Saving uses a conservative 30% no-upfront discount; your actual SP rate "
                    "may differ.",
                    "Instance-hour line items were identified correctly (storage, IOPS, "
                    "backups, and transfer are excluded, since Database SPs do not cover them).",
                ],
                rough_monthly=estimated_monthly_savings,
                confirm_steps=[
                    "Pull 3 to 6 months of RDS/Aurora instance-hour spend and confirm the "
                    "uncovered baseline is flat or growing, not trending down.",
                    "Check for planned migrations, downsizing, or a move to Aurora Serverless "
                    "that would drop the baseline inside the next year.",
                    "Size the commitment to the floor of that history, not the recent peak, so "
                    "you do not over-commit.",
                ],
                pro_can_confirm=True,
                pro_unlock=("On Pro, nable reads several months of your CUR to confirm the "
                            "database baseline is stable, models the right commitment to the "
                            "stable floor, and uses your account's actual Savings Plan rates "
                            "instead of a conservative published estimate."),
                remediation=[
                    "Confirm first: verify the baseline holds over a longer window and rule out "
                    "planned changes to the fleet.",
                    "Then buy a 1-year no-upfront Database Savings Plan sized to the stable "
                    f"floor, around ${round(recommended_hourly, 2):,.2f}/hr to start.",
                    "Risk: an over-sized commitment on spend that later drops is wasted. "
                    "Commit to the floor, not the peak, and consider laddering.",
                ],
                est_monthly_savings=None,
                metadata={
                    "current_monthly_rds_spend": round(monthly_rds_spend, 2),
                    "current_sp_coverage_pct": round(sp_coverage_pct, 1),
                    "uncovered_monthly_spend": round(uncovered_monthly, 2),
                    "recommended_sp_hourly_commitment": round(recommended_hourly, 4),
                    "lookback_days": 30,
                },
            )

        return {
            "current_monthly_rds_spend": round(monthly_rds_spend, 2),
            "current_sp_coverage_pct": round(sp_coverage_pct, 1),
            "uncovered_monthly_spend": round(uncovered_monthly, 2),
            "recommended_sp_hourly_commitment": round(recommended_hourly, 4),
            "estimated_monthly_savings": round(estimated_monthly_savings, 2),
            "estimated_annual_savings": round(estimated_annual_savings, 2),
            "payback_days": 0,  # no-upfront has no upfront cost
            "recommendation_type": "database_savings_plan_1yr_no_upfront",
            "finding": finding.to_dict() if finding else None,
        }

    except Exception as e:
        log.error("Database SP recommendation failed: %s", e)
        return None
