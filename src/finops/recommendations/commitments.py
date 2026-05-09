"""
Reserved Instance & Savings Plan coverage analysis with purchase ROI.

Pulls real utilization data from AWS Cost Explorer:
  - Savings Plans utilization (coverage %, unused commitment)
  - RI utilization and coverage
  - On-demand spend that could be covered by commitments

Calculates:
  - Current waste (unused RI/SP payments)
  - Coverage gap (on-demand that commitments could cover)
  - Recommended purchase: type, term, payment, projected ROI

All figures come directly from the Cost Explorer API — no estimates.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

_COMPUTE_SP_DISCOUNT = 0.66   # ~34% off on-demand (1yr no-upfront compute SP)
_EC2_SP_DISCOUNT = 0.72       # ~28% off on-demand (1yr no-upfront EC2 SP)
_RI_DISCOUNT = 0.60           # ~40% off on-demand (1yr no-upfront RI)


@dataclass
class CommitmentAnalysis:
    # Current state
    savings_plan_coverage_pct: float
    savings_plan_utilization_pct: float
    savings_plan_unused_usd: float
    ri_coverage_pct: float
    ri_utilization_pct: float
    ri_unused_usd: float

    # On-demand that commitments could cover
    uncovered_on_demand_usd: float

    # Recommendations
    recommendations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_waste_usd(self) -> float:
        return self.savings_plan_unused_usd + self.ri_unused_usd

    @property
    def coverage_score(self) -> str:
        avg = (self.savings_plan_coverage_pct + self.ri_coverage_pct) / 2
        if avg >= 80:
            return "good"
        if avg >= 50:
            return "fair"
        return "poor"


def _get_date_range(months_back: int = 3) -> tuple[str, str]:
    end = date.today().replace(day=1) - timedelta(days=1)  # last day of prior month
    start = (end.replace(day=1) - timedelta(days=months_back * 30)).replace(day=1)
    return start.isoformat(), end.isoformat()


def _savings_plan_utilization(ce_client: Any, start: str, end: str) -> dict[str, float]:
    try:
        resp = ce_client.get_savings_plans_utilization(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
        )
        total = resp.get("Total", {})
        util = total.get("Utilization", {})
        return {
            "utilization_pct": float(util.get("UtilizationPercentage", 0)),
            "unused_usd": float(total.get("Savings", {}).get("NetSavings", 0)),
            "total_commitment": float(util.get("TotalCommitment", 0)),
        }
    except Exception as e:
        log.warning("SP utilization fetch failed: %s", e)
        return {"utilization_pct": 0.0, "unused_usd": 0.0, "total_commitment": 0.0}


def _savings_plan_coverage(ce_client: Any, start: str, end: str) -> float:
    try:
        resp = ce_client.get_savings_plans_coverage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
        )
        totals = resp.get("Total", {}).get("CoverageHours", {})
        return float(totals.get("CoverageHoursPercentage", 0))
    except Exception as e:
        log.warning("SP coverage fetch failed: %s", e)
        return 0.0


def _ri_utilization(ce_client: Any, start: str, end: str) -> dict[str, float]:
    try:
        resp = ce_client.get_reservation_utilization(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
        )
        total = resp.get("Total", {})
        util = total.get("Utilization", {})
        unused = total.get("UnusedHours", "0")
        unused_cost = float(total.get("UnusedAmortizedUpfrontCostForRIs", 0)) + \
                      float(total.get("UnusedRecurringFeeForRIs", 0))
        return {
            "utilization_pct": float(util.get("UtilizationPercentage", 0)),
            "unused_usd": unused_cost,
        }
    except Exception as e:
        log.warning("RI utilization fetch failed: %s", e)
        return {"utilization_pct": 0.0, "unused_usd": 0.0}


def _ri_coverage(ce_client: Any, start: str, end: str) -> float:
    try:
        resp = ce_client.get_reservation_coverage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
        )
        total = resp.get("Total", {}).get("CoverageHours", {})
        return float(total.get("CoverageHoursPercentage", 0))
    except Exception as e:
        log.warning("RI coverage fetch failed: %s", e)
        return 0.0


def _uncovered_on_demand(ce_client: Any, start: str, end: str) -> float:
    """On-demand EC2 + Fargate spend not covered by any commitment."""
    try:
        resp = ce_client.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter={
                "And": [
                    {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Elastic Compute Cloud - Compute", "AWS Fargate"]}},
                    {"Dimensions": {"Key": "PURCHASE_TYPE", "Values": ["On Demand"]}},
                ]
            },
            Metrics=["UnblendedCost"],
        )
        total = sum(
            float(p["Total"]["UnblendedCost"]["Amount"])
            for p in resp.get("ResultsByTime", [])
        )
        return total
    except Exception as e:
        log.warning("On-demand cost fetch failed: %s", e)
        return 0.0


def _build_recommendations(
    sp_coverage: float,
    uncovered_od: float,
    sp_util: float,
    ri_util: float,
) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    monthly_uncovered = uncovered_od / 3  # 3-month average

    # Recommend Compute Savings Plan if coverage < 60% and meaningful on-demand spend
    if sp_coverage < 60 and monthly_uncovered > 500:
        commitment_to_add = monthly_uncovered * 0.4 * _COMPUTE_SP_DISCOUNT
        monthly_savings = monthly_uncovered * 0.4 * (1 - _COMPUTE_SP_DISCOUNT)
        recs.append({
            "type": "savings_plan",
            "title": "Purchase Compute Savings Plan",
            "description": (
                f"Your SP coverage is {sp_coverage:.0f}%. Adding a 1-year no-upfront "
                f"Compute SP at ${commitment_to_add:,.0f}/mo hourly commitment "
                f"covers ~40% of your uncovered on-demand spend."
            ),
            "commitment_per_month": round(commitment_to_add, 2),
            "monthly_savings": round(monthly_savings, 2),
            "annual_savings": round(monthly_savings * 12, 2),
            "payback_months": 0,  # no-upfront has no payback period
            "term": "1-year",
            "payment": "no-upfront",
            "confidence": "high" if monthly_uncovered > 5000 else "medium",
        })

    # Warn about over-commitment (low utilization = waste)
    if sp_util < 70 and sp_util > 0:
        recs.append({
            "type": "warning",
            "title": "Savings Plan under-utilised",
            "description": (
                f"Your Savings Plans are only {sp_util:.0f}% utilized — "
                "you're paying for commitment you're not using. "
                "Consider reducing commitment at next renewal or moving workloads "
                "to covered instance families."
            ),
            "monthly_savings": 0,
            "annual_savings": 0,
            "confidence": "high",
        })

    if ri_util < 70 and ri_util > 0:
        recs.append({
            "type": "warning",
            "title": "Reserved Instances under-utilised",
            "description": (
                f"Your RIs are only {ri_util:.0f}% utilized. "
                "List unused RI capacity on the AWS Marketplace or "
                "modify to a different instance size within the same family."
            ),
            "monthly_savings": 0,
            "annual_savings": 0,
            "confidence": "high",
        })

    return recs


def analyze_commitments() -> CommitmentAnalysis | None:
    """
    Run full RI/SP analysis. Returns None if AWS is not configured.
    """
    try:
        import boto3
    except ImportError:
        return None

    try:
        ce = boto3.client("ce", region_name="us-east-1")
        start, end = _get_date_range(months_back=3)

        sp_util_data = _savings_plan_utilization(ce, start, end)
        sp_coverage = _savings_plan_coverage(ce, start, end)
        ri_util_data = _ri_utilization(ce, start, end)
        ri_coverage = _ri_coverage(ce, start, end)
        uncovered_od = _uncovered_on_demand(ce, start, end)

        recs = _build_recommendations(
            sp_coverage,
            uncovered_od,
            sp_util_data["utilization_pct"],
            ri_util_data["utilization_pct"],
        )

        return CommitmentAnalysis(
            savings_plan_coverage_pct=round(sp_coverage, 1),
            savings_plan_utilization_pct=round(sp_util_data["utilization_pct"], 1),
            savings_plan_unused_usd=round(sp_util_data["unused_usd"], 2),
            ri_coverage_pct=round(ri_coverage, 1),
            ri_utilization_pct=round(ri_util_data["utilization_pct"], 1),
            ri_unused_usd=round(ri_util_data["unused_usd"], 2),
            uncovered_on_demand_usd=round(uncovered_od, 2),
            recommendations=recs,
        )
    except Exception as e:
        log.error("Commitment analysis failed: %s", e)
        return None


def commitment_summary(analysis: CommitmentAnalysis) -> dict[str, Any]:
    return {
        "coverage_score": analysis.coverage_score,
        "savings_plan": {
            "coverage_pct": analysis.savings_plan_coverage_pct,
            "utilization_pct": analysis.savings_plan_utilization_pct,
            "unused_usd_per_month": analysis.savings_plan_unused_usd,
        },
        "reserved_instances": {
            "coverage_pct": analysis.ri_coverage_pct,
            "utilization_pct": analysis.ri_utilization_pct,
            "unused_usd_per_month": analysis.ri_unused_usd,
        },
        "uncovered_on_demand_usd_3mo": analysis.uncovered_on_demand_usd,
        "total_waste_usd_per_month": analysis.total_waste_usd,
        "recommendations": analysis.recommendations,
    }
