"""Which change explains which delta, and how sure that is (anomaly/root_cause.py).

Invariants under test:
  - a change is named as the cause only when it is a call that starts that
    usage type's bill, in the same region, did not fail, was made from 24
    hours before the day the cost rose to the end of that day, and (where
    both are known) for the same instance type
  - "confirmed (resource id match)" needs a resource id on both sides;
    "likely" is everything else that lines up; a change that names other
    resources than the costing ones is not attributed, unless none of those
    could have started the rise (no baseline, or costing before it) or the
    change is on a group (an ASG, a fleet, an ECS service, a node group),
    which is at most "likely"
  - a row with no attributed change says so, and says when CloudTrail was
    not read, or read only in part, rather than implying nothing changed
  - the whole answer, end to end on stubbed Cost Explorer and CloudTrail,
    reads as one line per usage type and lists what could not be read
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import boto3
import pytest
from botocore.stub import Stubber

import finops.guard_ledger as gl
from finops.anomaly import change_events as ce
from finops.anomaly import drilldown as dd
from finops.anomaly import root_cause as rc

TODAY = date(2026, 9, 25)
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
EC2 = "Amazon Elastic Compute Cloud - Compute"
P4D = "BoxUsage:p4d.24xlarge"
CI_ROLE = "arn:aws:iam::123456789012:role/ci"
LAUNCH = "2026-09-21T03:12:00+00:00"


def _row(**kw) -> dict:
    row = {"usage_type": P4D, "region": "us-east-1", "onset": "2026-09-21",
           "instance_type": "p4d.24xlarge", "monthly_run_rate_usd": 1240.0,
           "resources": [{"resource_id": "i-0p4d1", "onset": "2026-09-21"}],
           "cloudtrail_read": True, "changes": []}
    row.update(kw)
    return row


def _change(**kw) -> dict:
    ch = {"event": "RunInstances", "time": LAUNCH, "region": "us-east-1",
          "instance_type": "p4d.24xlarge", "count": 8,
          "resource_ids": [f"i-0p4d{i}" for i in range(1, 9)], "who": CI_ROLE,
          "via": "terraform", "guard": "guard: allowed at $128k/mo, session s1",
          "error_code": None}
    ch.update(kw)
    return ch


# ── the rules ─────────────────────────────────────────────────────────────────

def test_a_resource_id_match_is_confirmed():
    row = _row(changes=[_change()])
    [cause] = rc.attribute(row)
    assert cause["attribution"] == rc.CONFIRMED and cause["matched_resources"] == ["i-0p4d1"]


def test_without_resource_ids_the_same_change_is_only_likely():
    row = _row(resources=[], changes=[_change()])
    [cause] = rc.attribute(row)
    assert cause["attribution"] == rc.LIKELY and "no resource id" in cause["why_likely"]
    row = _row(changes=[_change(resource_ids=[])])        # e.g. UpdateAutoScalingGroup
    assert rc.attribute(row)[0]["attribution"] == rc.LIKELY


@pytest.mark.parametrize("change, because", [
    ({"time": "2026-09-22T00:00:00+00:00"}, "after the cost had already risen"),
    ({"time": "2026-09-19T23:59:00+00:00"}, "more than 24 hours before"),
    ({"region": "eu-west-1"}, "in eu-west-1, the cost is in us-east-1"),
    ({"instance_type": "g5.xlarge"}, "for g5.xlarge, the cost is p4d.24xlarge"),
    ({"error_code": "InsufficientInstanceCapacity"}, "the call failed"),
    ({"event": "ModifyInstanceMetadataOptions"}, "not a call that starts"),
    ({"resource_ids": ["i-0other"]}, "names other resources"),
])
def test_what_does_not_line_up_is_not_attributed(change, because):
    row = _row(changes=[_change(**change)])
    assert rc.attribute(row) == []
    assert because in row["changes"][0]["not_attributed_because"]
    assert row["changes"][0]["attribution"] is None


def test_the_day_before_the_onset_still_lines_up():
    row = _row(resources=[], changes=[_change(time="2026-09-20T18:00:00+00:00")])
    assert rc.attribute(row)[0]["attribution"] == rc.LIKELY


def test_a_resources_own_onset_is_the_clock_when_the_change_names_it():
    """The usage type rose on the 19th (another instance), this one on the 21st."""
    row = _row(onset="2026-09-19", changes=[_change()])
    assert rc.attribute(row)[0]["attribution"] == rc.CONFIRMED


def test_resources_that_could_not_have_started_the_rise_do_not_rule_a_change_out():
    """No baseline read (resource-level data covers 14 days): the steady
    instances were costing before the rise, so a launch naming another one
    is not ruled out by them."""
    steady = [{"resource_id": f"i-steady{i}", "onset": "2026-09-11"} for i in range(5)]
    row = _row(onset="2026-09-20", instance_type="m5.24xlarge", resources=steady,
               changes=[_change(time="2026-09-20T03:00:00+00:00", instance_type="m5.24xlarge",
                                resource_ids=["i-new"], count=1)])
    [cause] = rc.attribute(row)
    assert cause["attribution"] == rc.LIKELY and cause["resource_ids"] == ["i-new"]
    flagged = [{"resource_id": f"i-steady{i}", "onset": None, "no_baseline": True,
                "new_in_window": False} for i in range(5)]
    row = _row(onset="2026-09-20", instance_type="m5.24xlarge", resources=flagged,
               changes=[_change(time="2026-09-20T03:00:00+00:00", instance_type="m5.24xlarge",
                                resource_ids=["i-new"], count=1)])
    assert rc.attribute(row)[0]["attribution"] == rc.LIKELY
    new = {"resource_id": "i-new", "onset": "2026-09-20", "no_baseline": True,
           "new_in_window": True}
    row = _row(onset="2026-09-20", instance_type="m5.24xlarge", resources=[new, *flagged],
               changes=[_change(time="2026-09-20T03:00:00+00:00", instance_type="m5.24xlarge",
                                resource_ids=["i-new"], count=1)])
    assert rc.attribute(row)[0]["attribution"] == rc.CONFIRMED


def test_a_new_resource_behind_the_rise_still_rules_out_a_launch_of_another():
    new = {"resource_id": "i-new", "onset": "2026-09-21", "no_baseline": True,
           "new_in_window": True}
    row = _row(resources=[new], changes=[_change(resource_ids=["i-0other"])])
    assert rc.attribute(row) == []
    assert "names other resources" in row["changes"][0]["not_attributed_because"]


@pytest.mark.parametrize("event", sorted(ce.CONTAINER_EVENTS))
def test_a_change_to_a_group_is_likely_never_confirmed_or_ruled_out(event):
    """SetDesiredCapacity names the Auto Scaling group, not the instances it
    launches, so the billed ids cannot confirm it or rule it out."""
    ch = _change(event=event, resource_ids=["web-asg", "i-0p4d1"], instance_type=None,
                 time="2026-09-21T02:00:00+00:00")
    row = _row(usage_type="BoxUsage:m5.large", instance_type="m5.large",
               resources=[{"resource_id": "i-0aaa", "onset": "2026-09-21"},
                          {"resource_id": "i-0p4d1", "onset": "2026-09-21"}],
               changes=[ch])
    causes = rc.attribute(row)
    if event not in {n for n, _ in ce.family_events("BoxUsage:m5.large")}:
        assert causes == [] and "not a call that starts" in ch["not_attributed_because"]
        return
    [cause] = causes
    assert cause["attribution"] == rc.LIKELY and "group" in cause["why_likely"]
    late = _change(event=event, resource_ids=["web-asg"], instance_type=None,
                   time="2026-09-22T02:00:00+00:00")
    assert rc.attribute(_row(changes=[late])) == []


def test_the_asg_scale_up_from_the_review_is_likely():
    ch = _change(event="SetDesiredCapacity", resource_ids=["web-asg"], instance_type=None,
                 count=40, time="2026-09-21T02:00:00+00:00", who="ops", via="console")
    row = _row(usage_type="BoxUsage:m5.large", instance_type="m5.large",
               resources=[{"resource_id": "i-0aaa", "onset": "2026-09-21"},
                          {"resource_id": "i-0bbb", "onset": "2026-09-21"}], changes=[ch])
    [cause] = rc.attribute(row)
    assert cause["attribution"] == rc.LIKELY


def test_unknown_instance_type_on_either_side_does_not_block():
    row = _row(resources=[], changes=[_change(instance_type=None)])
    assert rc.attribute(row)[0]["attribution"] == rc.LIKELY


def test_confirmed_ranks_before_likely():
    likely = _change(resource_ids=[], time="2026-09-21T00:30:00+00:00", who="arn:x:likely")
    confirmed = _change(time="2026-09-20T12:00:00+00:00", who="arn:x:confirmed")
    causes = rc.attribute(_row(changes=[likely, confirmed]))
    assert [c["who"] for c in causes] == ["arn:x:confirmed", "arn:x:likely"]


# ── the words ─────────────────────────────────────────────────────────────────

def test_the_line_names_the_resource_the_change_and_the_guard():
    row = _row(changes=[_change()])
    assert rc.sentence(EC2, row, rc.attribute(row)) == (
        "EC2 +$1,240/mo since Sep 21: 8x p4d.24xlarge launched by "
        "arn:aws:iam::123456789012:role/ci via terraform "
        "(guard: allowed at $128k/mo, session s1) [confirmed (resource id match)]")


def test_no_cause_is_said_and_unread_cloudtrail_is_not_passed_off_as_nothing():
    assert rc.sentence(EC2, _row(), []) == (
        "EC2 +$1,240/mo since Sep 21: BoxUsage:p4d.24xlarge in us-east-1; "
        "no change event lines up with it")
    assert "CloudTrail for it was not read" in rc.sentence(EC2, _row(cloudtrail_read=False), [])
    row = _row(changes=[_change(time="2026-09-22T09:00:00+00:00")])
    assert "1 change(s) found nearby, none lines up" in rc.sentence(EC2, row, rc.attribute(row))


def test_a_partly_read_row_never_says_nothing_lines_up():
    row = _row(cloudtrail_read=False, cloudtrail_status=ce.PARTLY_READ,
               cloudtrail_gaps=["3 of its 7 lookups were cut short by the call or page cap"])
    text = rc.sentence(EC2, row, [])
    assert "no change event lines up" not in text
    assert "CloudTrail for it was partly read (3 of its 7 lookups" in text
    row["changes"] = [_change(time="2026-09-22T09:00:00+00:00")]
    text = rc.sentence(EC2, row, rc.attribute(row))
    assert "partly read" in text and "none of the 1 change(s) found lines up" in text
    row = _row(cloudtrail_read=True, cloudtrail_status=ce.NOT_READ)
    assert "CloudTrail for it was not read" in rc.sentence(EC2, row, [])


@pytest.mark.parametrize("change, text", [
    ({"event": "CreateNatGateway", "resource_ids": ["nat-0abc"], "instance_type": None},
     "CreateNatGateway nat-0abc"),
    ({"event": "ModifyDBInstance", "resource_ids": ["prod-db"], "instance_type": "db.r6g.4xlarge"},
     "ModifyDBInstance prod-db (db.r6g.4xlarge)"),
    ({"count": None}, "p4d.24xlarge launched"),
])
def test_describe(change, text):
    assert rc.describe(_change(**change)) == text


def test_short_names():
    assert rc.short_name(EC2) == "EC2"
    assert rc.short_name("Amazon Relational Database Service") == "RDS"
    assert rc.short_name("Amazon Kinesis") == "Kinesis"


# ── end to end ────────────────────────────────────────────────────────────────

def _ce():
    return boto3.client("ce", region_name="us-east-1", aws_access_key_id="testing",
                        aws_secret_access_key="testing")  # pragma: allowlist secret


def _ct():
    return boto3.client("cloudtrail", region_name="us-east-1", aws_access_key_id="testing",
                        aws_secret_access_key="testing")  # pragma: allowlist secret


class _Session:
    def __init__(self, ct):
        self.ct = ct

    def client(self, service, region_name=None, config=None):
        assert service == "cloudtrail" and region_name == "us-east-1"
        return self.ct


def _ce_day(d: date, groups) -> dict:
    return {"TimePeriod": {"Start": d.isoformat(), "End": (d + timedelta(days=1)).isoformat()},
            "Estimated": False, "Total": {},
            "Groups": [{"Keys": keys,
                        "Metrics": {"UnblendedCost": {"Amount": str(usd), "Unit": "USD"},
                                    "UsageQuantity": {"Amount": str(q), "Unit": "Hrs"}}}
                       for keys, usd, q in groups]}


def _stub_ce(stub, cur, base, *, resources: bool):
    usage = []
    res = []
    for d in base.dates() + cur.dates():
        on = d >= date(2026, 9, 21)
        usage.append(_ce_day(d, [([P4D, "us-east-1"], 289.33, 192.0)] if on else []))
        res.append(_ce_day(d, [(["i-0p4d1", P4D], 289.33, 0.0)] if on else []))
    stub.add_response("get_cost_and_usage", {"ResultsByTime": usage})
    if resources:
        stub.add_response("get_cost_and_usage_with_resources", {"ResultsByTime": res})
    else:
        stub.add_client_error("get_cost_and_usage_with_resources",
                              service_error_code="DataUnavailableException",
                              service_message="not enabled", http_status_code=400)


def _run_instances() -> dict:
    ids = [f"i-0p4d{i}" for i in range(1, 9)]
    detail = {"eventName": "RunInstances", "awsRegion": "us-east-1",
              "userAgent": "APN/1.0 HashiCorp/1.0 Terraform/1.9.5",
              "userIdentity": {"type": "AssumedRole",
                               "arn": "arn:aws:sts::123456789012:assumed-role/ci/run-77",
                               "sessionContext": {"sessionIssuer": {"arn": CI_ROLE}}},
              "requestParameters": {"instanceType": "p4d.24xlarge"},
              "responseElements": {"instancesSet": {"items": [
                  {"instanceId": i, "instanceType": "p4d.24xlarge"} for i in ids]}}}
    return {"EventId": "ev-1", "EventName": "RunInstances",
            "EventTime": datetime(2026, 9, 21, 3, 12, tzinfo=UTC),
            "EventSource": "ec2.amazonaws.com", "Username": "run-77",
            "Resources": [{"ResourceType": "AWS::EC2::Instance", "ResourceName": i}
                          for i in ids],
            "CloudTrailEvent": json.dumps(detail)}


def _stub_ct(stub, *, resource: bool, run=None):
    if resource:
        stub.add_response("lookup_events", {"Events": [run] if run else []})
    for name, _ in ce.family_events(P4D):
        stub.add_response("lookup_events",
                          {"Events": [run] if run and name == "RunInstances" else []})


def _explain(billed_stub, *, resources=True, ct_denied=False, monkeypatch=None, **kw):
    cur, base = dd.windows_for_period(7, TODAY)
    c, t = _ce(), _ct()
    with billed_stub(c) as s1, Stubber(t) as s2:
        _stub_ce(s1, cur, base, resources=resources)
        if ct_denied:
            s2.add_client_error("lookup_events", service_error_code="AccessDeniedException",
                                service_message="no", http_status_code=400)
        else:
            _stub_ct(s2, resource=resources, run=_run_instances())
        r = rc.explain(EC2, cur, base, session=_Session(t), meter=dd.Meter(c), today=TODAY,
                       now=NOW, use_cur=False, sleep=lambda s: None, **kw)
        s1.assert_no_pending_responses()
        s2.assert_no_pending_responses()
    return r


def test_explain_end_to_end_confirmed(billed_stub):
    gl.append({"ts": "2026-09-21T03:10:00+00:00", "decision": "allow",
               "action_type": "infra_apply", "command": "terraform apply", "session": "s1",
               "monthly_usd": 128000.0})
    r = _explain(billed_stub)
    # 4 days at $289.33 against nothing, over 7 days: $165.33/day, $4,960/mo.
    assert r["lines"] == [(
        "EC2 +$4,960/mo since Sep 21: 8x p4d.24xlarge launched by "
        "arn:aws:iam::123456789012:role/ci via terraform "
        "(guard: allowed at $128k/mo, session s1) [confirmed (resource id match)]")]
    assert r["rows"][0]["cause"]["event_id"] == "ev-1"
    assert r["cost_explorer_requests"] == 2 and r["lookup_calls"] == 7
    assert r["resource_source"] == "ce_resources"
    assert any("No CUR source" in n for n in r["not_read"])
    assert "24 hours" in r["attribution_rules"]


def test_explain_says_cloudtrail_reads_only_this_account(billed_stub):
    r = _explain(billed_stub)
    assert rc.ACCOUNT_NOTE in r["not_read"]
    assert r["rows"][0]["cloudtrail_status"] == ce.READ


def test_explain_with_the_call_cap_hit_says_partly_read_not_nothing(billed_stub):
    cur, base = dd.windows_for_period(7, TODAY)
    c, t = _ce(), _ct()
    with billed_stub(c) as s1, Stubber(t) as s2:
        _stub_ce(s1, cur, base, resources=True)
        for _ in range(3):
            s2.add_response("lookup_events", {"Events": []})
        r = rc.explain(EC2, cur, base, session=_Session(t), meter=dd.Meter(c), today=TODAY,
                       now=NOW, use_cur=False, sleep=lambda s: None, max_calls=3)
        s2.assert_no_pending_responses()
    [line] = r["lines"]
    assert "no change event lines up" not in line
    assert "CloudTrail for it was partly read (4 of its 7 lookups" in line
    assert r["rows"][0]["cloudtrail_status"] == ce.PARTLY_READ
    assert r["rows"][0]["cloudtrail_read"] is False


def test_explain_without_resource_level_data_is_likely_and_says_why(billed_stub):
    r = _explain(billed_stub, resources=False)
    assert r["lines"][0].endswith("(guard: no record) [likely]")
    assert any("not enabled" in n for n in r["not_read"])


def test_explain_with_cloudtrail_denied_keeps_the_cost_answer(billed_stub):
    r = _explain(billed_stub, ct_denied=True)
    assert r["lines"] == [(
        "EC2 +$4,960/mo since Sep 21: BoxUsage:p4d.24xlarge in us-east-1; CloudTrail for "
        "it was not read, so no change is named")]
    assert r["rows"][0]["resources"][0]["resource_id"] == "i-0p4d1"
    assert any("cloudtrail:LookupEvents" in n and "denied" in n for n in r["not_read"])


def test_explain_can_skip_cloudtrail(billed_stub):
    cur, base = dd.windows_for_period(7, TODAY)
    c = _ce()
    with billed_stub(c) as s1:
        _stub_ce(s1, cur, base, resources=True)
        r = rc.explain(EC2, cur, base, session=object(), meter=dd.Meter(c), today=TODAY,
                       use_cur=False, read_cloudtrail=False)
    assert "CloudTrail was not read for this answer" in " ".join(r["not_read"])
    assert r["lookup_calls"] == 0 and r["rows"][0]["cause"] is None


def test_explain_when_cost_explorer_is_denied(billed_stub):
    cur, base = dd.windows_for_period(7, TODAY)
    c = _ce()
    with billed_stub(c) as s1:
        s1.add_client_error("get_cost_and_usage", service_error_code="AccessDeniedException",
                            service_message="no", http_status_code=400)
        r = rc.explain(EC2, cur, base, session=object(), meter=dd.Meter(c), today=TODAY,
                       use_cur=False)
    assert r["rows"] == [] and "ce:GetCostAndUsage" in r["lines"][0]
    assert r["cost_explorer_requests"] == 1


def test_explain_when_nothing_rose(billed_stub):
    cur, base = dd.windows_for_period(7, TODAY)
    c = _ce()
    with billed_stub(c) as s1:
        s1.add_response("get_cost_and_usage", {"ResultsByTime": []})
        r = rc.explain(EC2, cur, base, session=object(), meter=dd.Meter(c), today=TODAY,
                       use_cur=False)
    assert r["lines"] == ["EC2: no usage type rose by $1 or more between the two windows."]


def test_explain_goes_through_the_cost_explorer_gate(monkeypatch):
    """Scheduled work, demo mode and NABLE_NO_COST_EXPLORER never reach the
    drill-down's billed requests; the answer says why instead of raising."""
    from finops.billing_access import unattended_context

    cur, base = dd.windows_for_period(7, TODAY)
    with unattended_context():
        r = rc.explain(EC2, cur, base, session=object(), today=TODAY)
    assert r["cost_explorer_requests"] == 0 and r["rows"] == []
    assert "scheduled or background work" in r["not_read"][0]
    monkeypatch.setenv("NABLE_NO_COST_EXPLORER", "1")
    r = rc.explain(EC2, cur, base, session=object(), today=TODAY)
    assert "disabled here" in r["lines"][0]
