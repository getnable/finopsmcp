"""The Compute Savings Plan rate is blended by what the uncovered pool is made of.

GetSavingsPlansCoverage's OnDemandCost is every Savings Plans eligible dollar
no plan covered, EC2 and Fargate alike, and the engine priced all of it at the
EC2 rate: commit 0.74 of it, save 26%. A 1-year no-upfront Compute Savings
Plan bills Fargate at 0.80 of on-demand (AWS Price List, Compute SP offer:
0.032384 vs 0.04048 per vCPU-hour), a 20% saving. On the fixture account
$288.32 of $1,503.48 a month of compute is ECS Fargate, and the floor month's
$1,206.40 uncovered was priced at $313.67 saved.

Now the rate is blended by the EC2 and Fargate share of compute spend over
the same window, read from GetCostAndUsage grouped by SERVICE. If that split
was not read, the whole pool is priced at Fargate's 20%, the lower rate, and
the basis says so.
"""
from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError

from finops.recommendations import commitments as c

EC2 = "Amazon Elastic Compute Cloud - Compute"
ECS = "Amazon Elastic Container Service"


def _group(key: str, usd: float) -> dict:
    return {"Keys": [key], "Metrics": {"UnblendedCost": {"Amount": f"{usd}", "Unit": "USD"}}}


def _period(start: str, end: str, groups: list[dict]) -> dict:
    return {"TimePeriod": {"Start": start, "End": end}, "Total": {}, "Groups": groups,
            "Estimated": False}


def _request(token: str | None = None) -> dict:
    req = {"TimePeriod": {"Start": "2026-07-01", "End": "2026-10-01"},
           "Granularity": "MONTHLY",
           "Filter": {"Dimensions": {"Key": "SERVICE", "Values": list(c.SP_COMPUTE_CE_SERVICES)}},
           "Metrics": ["UnblendedCost"],
           "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}]}
    if token:
        req["NextPageToken"] = token
    return req


def test_the_split_is_read_from_every_page_in_the_shape_botocore_documents(billed_stub):
    ce = boto3.client("ce", region_name="us-east-1", aws_access_key_id="testing",
                      aws_secret_access_key="testing")  # a Stubber answers; nothing reaches AWS
    with billed_stub(ce) as stub:
        stub.add_response("get_cost_and_usage", {"ResultsByTime": [
            _period("2026-07-01", "2026-08-01", [_group(EC2, 1215.16), _group(ECS, 288.32)])],
            "NextPageToken": "p2"}, _request())
        stub.add_response("get_cost_and_usage", {"ResultsByTime": [
            _period("2026-08-01", "2026-09-01", [_group(EC2, 1215.16), _group(ECS, 288.32)]),
            _period("2026-09-01", "2026-10-01", [_group(EC2, 1215.16), _group(ECS, 288.32)])]},
            _request("p2"))
        split = c._compute_spend_split(ce, "2026-07-01", "2026-10-01")
        stub.assert_no_pending_responses()
    assert split == {EC2: pytest.approx(3645.48), ECS: pytest.approx(864.96)}


def test_the_rate_is_blended_by_the_ec2_and_fargate_share():
    rate, basis = c.blended_compute_sp_rate({EC2: 1215.16, ECS: 288.32})
    assert rate == pytest.approx((1215.16 * 0.74 + 288.32 * 0.80) / 1503.48)
    assert "Fargate" in basis and "81%" in basis and "19%" in basis, basis
    assert c.blended_compute_sp_rate({EC2: 100.0})[0] == pytest.approx(0.74)


def test_an_unread_split_is_priced_at_the_lower_rate_and_says_so():
    for split in (None, {}, {EC2: 0.0, ECS: 0.0}):
        rate, basis = c.blended_compute_sp_rate(split)
        assert rate == 0.80
        assert "not read" in basis and "20%" in basis, basis


class _CE:
    """Coverage and utilization as in the uncovered-pool tests; the split call
    answers or fails on demand."""

    def __init__(self, split: dict | None):
        self.split = split

    def get_savings_plans_coverage(self, **kw):
        return {"SavingsPlansCoverages": [
            {"TimePeriod": {"Start": s, "End": e},
             "Coverage": {"SpendCoveredBySavingsPlans": "280", "OnDemandCost": str(od),
                          "TotalCost": str(280 + od)}}
            for s, e, od in (("2026-07-01", "2026-08-01", 3000.0),
                             ("2026-08-01", "2026-09-01", 3100.0),
                             ("2026-09-01", "2026-10-01", 2900.0))]}

    def get_savings_plans_utilization(self, **kw):
        return {"Total": {"Utilization": {"UtilizationPercentage": "100",
                                          "TotalCommitment": "600", "UnusedCommitment": "0"}}}

    def get_reservation_utilization(self, **kw):
        return {"Total": {"UtilizationPercentage": "0", "RICostForUnusedHours": "0"}}

    def get_reservation_coverage(self, **kw):
        return {"Total": {"CoverageHours": {"CoverageHoursPercentage": "0"}}}

    def get_cost_and_usage(self, **kw):
        assert kw["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}], kw
        if self.split is None:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                              "GetCostAndUsage")
        return {"ResultsByTime": [_period("2026-07-01", "2026-10-01",
                                          [_group(k, v) for k, v in self.split.items()])]}


def _plan(monkeypatch, split):
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: _CE(split))
    a = c.analyze_commitments()
    [sp] = [r for r in a.recommendations if r["type"] == "savings_plan"]
    return a, sp


def test_the_plan_is_priced_at_the_blended_rate(monkeypatch):
    a, sp = _plan(monkeypatch, {EC2: 3000.0, ECS: 1000.0})
    rate = (3000 * 0.74 + 1000 * 0.80) / 4000          # 0.755
    assert a.compute_sp_rate == pytest.approx(rate)
    assert sp["monthly_savings"] == pytest.approx(2900 * (1 - rate))
    assert sp["commitment_per_month"] == pytest.approx(2900 * rate)
    assert sp["assumed_discount_pct"] == pytest.approx(24.5)
    assert "Fargate" in sp["description"], sp["description"]


def test_without_the_split_the_plan_is_priced_at_twenty_percent(monkeypatch):
    a, sp = _plan(monkeypatch, None)
    assert sp["monthly_savings"] == pytest.approx(2900 * 0.20)
    assert "not read" in sp["description"], sp["description"]
    assert "not read" in a.compute_sp_rate_basis


def test_the_projection_uses_the_same_rate(monkeypatch):
    import asyncio
    import inspect

    from finops import server

    a, _ = _plan(monkeypatch, {EC2: 3000.0, ECS: 1000.0})
    monkeypatch.setattr(c, "analyze_commitments", lambda *x, **k: a)
    out = server.get_commitment_analysis()
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    more = out["actionable_analysis"]["if_you_bought_more"]
    half = 3000.0 / 2                                    # half the 3-month average
    assert more["additional_monthly_commitment_usd"] == pytest.approx(half * 0.755, abs=0.01)
    assert more["projected_monthly_savings_usd"] == pytest.approx(half * 0.245, abs=0.01)
