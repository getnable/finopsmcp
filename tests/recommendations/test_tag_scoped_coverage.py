"""Tag-scoped commitment figures do not depend on a filter the API does not document.

botocore's ce model documents GetSavingsPlansCoverage's Filter as LINKED_ACCOUNT,
REGION, SERVICE, INSTANCE_FAMILY and cost category: no tags. The tag-scoped
uncovered figure sent {"Tags": ...} there anyway. It now comes from
GetCostAndUsage filtered by the tag and the Savings Plans eligible SERVICEs,
grouped by PURCHASE_TYPE, taking the groups whose key starts with "ondemand"
once lower-cased with spaces and hyphens removed ("On Demand Instances",
"On-Demand Instances", "On Demand"). Spend with no such group is "on-demand
could not be identified", not read, never $0.

The tag-scoped coverage percentage still asks GetSavingsPlansCoverage; when it
refuses the tag filter, the analysis says that is why it was not read, not
"grant ce:GetSavingsPlansCoverage".
"""
from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError

from finops.recommendations import commitments as c

TAG = {"team": "platform"}


def _ce():
    return boto3.client("ce", region_name="us-east-1", aws_access_key_id="testing",
                        aws_secret_access_key="testing")  # pragma: allowlist secret


def _period(start, end, groups):
    return {"TimePeriod": {"Start": start, "End": end}, "Total": {}, "Estimated": False,
            "Groups": [{"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": str(v),
                                                                   "Unit": "USD"}}}
                       for k, v in groups]}


def _request():
    return {"TimePeriod": {"Start": "2026-07-01", "End": "2026-10-01"},
            "Granularity": "MONTHLY",
            "Filter": {"And": [
                {"Dimensions": {"Key": "SERVICE", "Values": list(c.SP_COMPUTE_CE_SERVICES)}},
                {"Tags": {"Key": "team", "Values": ["platform"]}}]},
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "PURCHASE_TYPE"}]}


def test_tag_scoped_uncovered_is_read_by_purchase_type_not_from_coverage(billed_stub):
    ce = _ce()
    with billed_stub(ce) as stub:   # an unqueued get_savings_plans_coverage would raise
        stub.add_response("get_cost_and_usage", {"ResultsByTime": [
            _period("2026-07-01", "2026-08-01",
                    [("On Demand Instances", 1000.0), ("Savings Plans", 300.0),
                     ("Spot Instances", 50.0)]),
            _period("2026-08-01", "2026-09-01", [("On-Demand Instances", 1100.0)]),
            _period("2026-09-01", "2026-10-01", [("On Demand", 900.0)]),
        ]}, _request())
        got = c._uncovered_on_demand_monthly(ce, "2026-07-01", "2026-10-01", TAG)
        stub.assert_no_pending_responses()
    assert got == [1000.0, 1100.0, 900.0]


def test_spend_with_no_on_demand_group_is_not_read(billed_stub):
    ce = _ce()
    why: list[str] = []
    with billed_stub(ce) as stub:
        stub.add_response("get_cost_and_usage", {"ResultsByTime": [
            _period("2026-07-01", "2026-10-01", [("Usage", 3000.0)])]}, _request())
        got = c._uncovered_on_demand_monthly(ce, "2026-07-01", "2026-10-01", TAG, why)
    assert got is None
    assert why and "could not be identified" in why[0] and "Usage" in why[0], why


def test_no_tagged_compute_spend_is_a_real_zero(billed_stub):
    ce = _ce()
    with billed_stub(ce) as stub:
        stub.add_response("get_cost_and_usage", {"ResultsByTime": [
            _period("2026-07-01", "2026-10-01", [])]}, _request())
        assert c._uncovered_on_demand_monthly(ce, "2026-07-01", "2026-10-01", TAG) == [0.0]


def test_a_refused_tag_filter_on_coverage_is_named_not_read(billed_stub):
    ce = _ce()
    why: list[str] = []
    with billed_stub(ce) as stub:
        stub.add_client_error("get_savings_plans_coverage",
                              service_error_code="ValidationException",
                              service_message="Tags is not a supported filter")
        got = c._savings_plan_coverage(ce, "2026-07-01", "2026-10-01", TAG, why)
    assert got is None
    assert why and "ValidationException" in why[0] and "team=platform" in why[0], why


class _CE:
    def get_savings_plans_coverage(self, **kw):
        assert "Tags" in str(kw.get("Filter")), kw   # only the percentage asks with a tag
        raise ClientError({"Error": {"Code": "ValidationException",
                                     "Message": "Tags is not a supported filter"}},
                          "GetSavingsPlansCoverage")

    def get_savings_plans_utilization(self, **kw):
        return {"Total": {"Utilization": {"UtilizationPercentage": "100",
                                          "TotalCommitment": "600", "UnusedCommitment": "0"}}}

    def get_reservation_utilization(self, **kw):
        return {"Total": {"UtilizationPercentage": "0", "RICostForUnusedHours": "0"}}

    def get_reservation_coverage(self, **kw):
        return {"Total": {"CoverageHours": {"CoverageHoursPercentage": "0"}}}

    def get_cost_and_usage(self, **kw):
        key = kw["GroupBy"][0]["Key"]
        groups = ([("On Demand Instances", 2000.0)] if key == "PURCHASE_TYPE"
                  else [("Amazon Elastic Compute Cloud - Compute", 2000.0)])
        return {"ResultsByTime": [_period("2026-07-01", "2026-10-01", groups)]}


def test_the_analysis_says_why_tag_scoped_coverage_was_not_read(monkeypatch):
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: _CE())
    a = c.analyze_commitments(tag_filter=TAG)
    assert a.savings_plan_coverage_pct is None
    assert a.uncovered_on_demand_usd == pytest.approx(2000.0)
    [rec] = a.recommendations
    assert rec["type"] == "coverage_unavailable"
    assert "ValidationException" in rec["detail"], rec["detail"]
    assert "Grant ce:GetSavingsPlansCoverage" not in rec["detail"]
    assert "ValidationException" in a.not_read["savings_plan_coverage"]
