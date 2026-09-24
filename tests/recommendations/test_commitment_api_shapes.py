"""Commitment maths driven by Cost Explorer responses in their real shapes.

Every fake here follows botocore's ce/2017-10-25 service model, not the shape
the code under test happens to read. The earlier fakes were written to match
the code, which is how three field-name bugs survived with green tests:

  - GetSavingsPlansCoverage has no Total. It returns SavingsPlansCoverages[]
    (one row per period) plus NextToken, so reading Total.CoverageHours gave 0%
    coverage for every Savings Plans customer, and a Compute SP recommendation.
  - Savings.NetSavings is money SAVED by the plan, not unused commitment.
  - ReservationAggregates has no UnusedAmortizedUpfrontCostForRIs or
    UnusedRecurringFeeForRIs, and UtilizationPercentage sits on Total itself,
    not under Total.Utilization. The unused cost is RICostForUnusedHours.
"""
from __future__ import annotations

from datetime import date

import pytest

from finops.recommendations import commitments as c


class _FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 24)


@pytest.fixture
def frozen_today(monkeypatch):
    monkeypatch.setattr(c, "date", _FrozenDate)


class _RealShapeCE:
    """Answers in the documented response shapes, paginating SP coverage."""

    def __init__(self):
        self.sp_coverage_calls: list[dict] = []
        self.windows: list[dict] = []

    def get_savings_plans_coverage(self, **kw):
        self.sp_coverage_calls.append(kw)
        self.windows.append(kw["TimePeriod"])
        if kw.get("NextToken") is None:
            return {
                "SavingsPlansCoverages": [{
                    "Attributes": {},
                    "Coverage": {"SpendCoveredBySavingsPlans": "6000",
                                 "OnDemandCost": "4000", "TotalCost": "10000",
                                 "CoveragePercentage": "60"},
                    "TimePeriod": {"Start": "2026-06-01", "End": "2026-07-01"},
                }],
                "NextToken": "page-2",
            }
        assert kw["NextToken"] == "page-2"
        return {
            "SavingsPlansCoverages": [{
                "Attributes": {},
                "Coverage": {"SpendCoveredBySavingsPlans": "2000",
                             "OnDemandCost": "28000", "TotalCost": "30000",
                             "CoveragePercentage": "6.67"},
                "TimePeriod": {"Start": "2026-07-01", "End": "2026-08-01"},
            }],
        }

    def get_savings_plans_utilization(self, **kw):
        self.windows.append(kw["TimePeriod"])
        return {
            "SavingsPlansUtilizationsByTime": [],
            "Total": {
                "Utilization": {"TotalCommitment": "3000", "UsedCommitment": "2400",
                                "UnusedCommitment": "600",
                                "UtilizationPercentage": "80"},
                # Money the plans SAVED over the window: must never be read as waste.
                "Savings": {"NetSavings": "9000", "OnDemandCostEquivalent": "12000"},
                "AmortizedCommitment": {"TotalAmortizedCommitment": "3000"},
            },
        }

    def get_reservation_utilization(self, **kw):
        self.windows.append(kw["TimePeriod"])
        return {
            "UtilizationsByTime": [],
            "Total": {
                "UtilizationPercentage": "75",
                "PurchasedHours": "4000", "TotalActualHours": "3000",
                "UnusedHours": "1000",
                "AmortizedUpfrontFee": "300", "AmortizedRecurringFee": "1500",
                "TotalAmortizedFee": "1800",
                "RICostForUnusedHours": "450",
                "NetRISavings": "2000",
            },
        }

    def get_reservation_coverage(self, **kw):
        self.windows.append(kw["TimePeriod"])
        return {"CoveragesByTime": [], "Total": {"CoverageHours": {
            "OnDemandHours": "500", "ReservedHours": "500",
            "TotalRunningHours": "1000", "CoverageHoursPercentage": "50"}}}

    def get_cost_and_usage(self, **kw):
        self.windows.append(kw["TimePeriod"])
        return {"ResultsByTime": [
            {"Total": {"UnblendedCost": {"Amount": "3000", "Unit": "USD"}}},
            {"Total": {"UnblendedCost": {"Amount": "3100", "Unit": "USD"}}},
            {"Total": {"UnblendedCost": {"Amount": "2900", "Unit": "USD"}}},
        ]}


def _run(monkeypatch, ce):
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: ce)
    return c.analyze_commitments()


def test_sp_coverage_is_spend_weighted_across_every_page():
    ce = _RealShapeCE()
    pct = c._savings_plan_coverage(ce, "2026-06-01", "2026-09-01")
    # (6000 + 2000) / (10000 + 30000), not the 0.0 a Total read produced and not
    # the unweighted mean of the per-period percentages (33.3).
    assert pct == pytest.approx(20.0)
    assert len(ce.sp_coverage_calls) == 2, "the second page was never requested"


def test_a_covered_account_is_not_told_its_coverage_is_zero(monkeypatch, frozen_today):
    analysis = _run(monkeypatch, _RealShapeCE())
    assert analysis is not None
    assert analysis.savings_plan_coverage_pct == pytest.approx(20.0)


def test_lookback_window_is_exactly_three_whole_months(frozen_today):
    # End is exclusive in Cost Explorer, so the first of this month keeps the
    # last day of August. The old window ran 2026-05-01 to 2026-08-31: nearly
    # four months, missing a day, and then divided as if it were three.
    assert c._get_date_range(months_back=3) == ("2026-06-01", "2026-09-01")
    assert c._get_date_range(months_back=1) == ("2026-08-01", "2026-09-01")
    assert c._get_date_range(months_back=12) == ("2025-09-01", "2026-09-01")


def test_unused_sp_is_unused_commitment_per_month_not_net_savings(
        monkeypatch, frozen_today):
    analysis = _run(monkeypatch, _RealShapeCE())
    # 600 unused over 3 months. NetSavings (9000) is the opposite of waste.
    assert analysis.savings_plan_unused_usd == pytest.approx(200.0)
    assert analysis.savings_plan_utilization_pct == pytest.approx(80.0)


def test_ri_waste_and_utilization_read_the_documented_fields(monkeypatch, frozen_today):
    analysis = _run(monkeypatch, _RealShapeCE())
    # RICostForUnusedHours 450 over 3 months.
    assert analysis.ri_unused_usd == pytest.approx(150.0)
    assert analysis.ri_utilization_pct == pytest.approx(75.0)
    assert c.commitment_summary(analysis)["total_waste_usd_per_month"] == pytest.approx(350.0)


def test_every_call_uses_the_same_three_month_window(monkeypatch, frozen_today):
    ce = _RealShapeCE()
    _run(monkeypatch, ce)
    assert ce.windows
    assert all(w == {"Start": "2026-06-01", "End": "2026-09-01"} for w in ce.windows)
