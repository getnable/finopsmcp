"""Uncovered on-demand compute is read from Savings Plans coverage, not guessed.

The commitment engine sized a Compute Savings Plan on GetCostAndUsage filtered
to SERVICE in ["Amazon Elastic Compute Cloud - Compute", "AWS Fargate"] and
PURCHASE_TYPE ["On Demand"]. Neither value could be checked against a live
Cost Explorer: botocore's ce model only says PURCHASE_TYPE "Examples include
On-Demand Instances", and nothing in the repo shows Fargate billed under an
"AWS Fargate" service. A filter value Cost Explorer does not use matches no
rows, which reads as $0 uncovered and no plan, on every account.

GetSavingsPlansCoverage already answers the question in AWS's own terms: per
period, Coverage.OnDemandCost is the Savings Plans eligible spend no plan
covered. The engine reads it at MONTHLY granularity, every page, and a call
that fails is "not read", never $0. (A tag slice is read by PURCHASE_TYPE
instead, since GetSavingsPlansCoverage documents no tag filter:
test_tag_scoped_coverage.py.)
"""
from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from finops.recommendations import commitments as c


def _row(start: str, end: str, covered: float, on_demand: float) -> dict:
    return {"Attributes": {}, "TimePeriod": {"Start": start, "End": end},
            "Coverage": {"SpendCoveredBySavingsPlans": str(covered),
                         "OnDemandCost": str(on_demand),
                         "TotalCost": str(covered + on_demand),
                         "CoveragePercentage": str(covered / (covered + on_demand) * 100)}}


class _CE:
    """Three months over two pages; GetCostAndUsage must not be what sizes the plan."""

    def __init__(self, fail_coverage: bool = False):
        self.fail_coverage = fail_coverage
        self.coverage_calls: list[dict] = []

    def get_savings_plans_coverage(self, **kw):
        self.coverage_calls.append(kw)
        if self.fail_coverage:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                              "GetSavingsPlansCoverage")
        if kw.get("NextToken") is None:
            return {"SavingsPlansCoverages": [
                _row("2026-07-01", "2026-08-01", 280.0, 3000.0),
                _row("2026-08-01", "2026-09-01", 280.0, 3100.0)], "NextToken": "p2"}
        return {"SavingsPlansCoverages": [_row("2026-09-01", "2026-10-01", 270.0, 2900.0)]}

    def get_savings_plans_utilization(self, **kw):
        return {"Total": {"Utilization": {"UtilizationPercentage": "100",
                                          "TotalCommitment": "600", "UnusedCommitment": "0"}}}

    def get_reservation_utilization(self, **kw):
        return {"Total": {"UtilizationPercentage": "0", "RICostForUnusedHours": "0"}}

    def get_reservation_coverage(self, **kw):
        return {"Total": {"CoverageHours": {"CoverageHoursPercentage": "0"}}}

    def get_cost_and_usage(self, **kw):
        # Only the EC2/Fargate split that prices the pool (all EC2 here, so the
        # 26% EC2 rate); the pool itself must not come from GetCostAndUsage.
        if kw.get("GroupBy") != [{"Type": "DIMENSION", "Key": "SERVICE"}]:
            raise AssertionError("the plan was sized from GetCostAndUsage filter strings")
        return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-07-01", "End": "2026-10-01"},
                                   "Groups": [{"Keys": ["Amazon Elastic Compute Cloud - Compute"],
                                               "Metrics": {"UnblendedCost": {"Amount": "9840"}}}]}]}


def _analyze(monkeypatch, ce, **kw):
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: ce)
    return c.analyze_commitments(**kw)


def test_uncovered_is_on_demand_cost_from_every_coverage_page(monkeypatch):
    ce = _CE()
    assert c._uncovered_on_demand_monthly(ce, "2026-07-01", "2026-10-01") == [3000.0, 3100.0, 2900.0]
    assert all(call["Granularity"] == "MONTHLY" for call in ce.coverage_calls)
    a = _analyze(monkeypatch, _CE())
    assert a.uncovered_on_demand_usd == pytest.approx(9000.0)
    [sp] = [r for r in a.recommendations if r["type"] == "savings_plan"]
    # Sized to the floor month: 2,900 x 0.26 = 754 saved on 2,146 committed.
    assert sp["baseline_monthly_uncovered_usd"] == 2900.0
    assert sp["monthly_savings"] == pytest.approx(754.0)


def test_the_tag_filter_never_reaches_the_coverage_call():
    # GetSavingsPlansCoverage documents no Tags filter; the tag slice is read
    # from GetCostAndUsage (which this fake refuses for anything but the split).
    ce = _CE()
    assert c._uncovered_on_demand_monthly(
        ce, "2026-07-01", "2026-10-01", {"team": "platform"}) is None
    assert ce.coverage_calls == []


def test_an_uncovered_read_that_failed_is_not_zero(monkeypatch):
    ce = _CE(fail_coverage=True)
    assert c._uncovered_on_demand_monthly(ce, "2026-07-01", "2026-10-01") is None
    a = _analyze(monkeypatch, _CE(fail_coverage=True))
    assert a.uncovered_on_demand_usd is None
    assert not [r for r in a.recommendations if r["type"] == "savings_plan"]
    assert c.commitment_summary(a)["uncovered_on_demand_usd_3mo"] is None


def test_get_commitment_analysis_says_the_uncovered_spend_was_not_read(monkeypatch):
    import asyncio
    import inspect

    from finops import server

    a = _analyze(monkeypatch, _CE(fail_coverage=True))
    monkeypatch.setattr(c, "analyze_commitments", lambda *x, **k: a)
    out = server.get_commitment_analysis()
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    act = out["actionable_analysis"]
    assert act["monthly_uncovered_on_demand_usd"] is None, act
    assert "not read" in str(act).lower(), act
