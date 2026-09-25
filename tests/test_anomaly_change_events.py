"""Who changed what around a cost spike (anomaly/change_events.py).

Every CloudTrail answer goes through botocore's Stubber on a real cloudtrail
client, so request parameters and response shapes are checked against the
service model.

Invariants under test:
  - for each drill-down row, LookupEvents is asked by resource id and by the
    names of the calls that start that usage type's bill, in a window from
    24 hours before the onset day to 24 hours after it
  - only creating and modifying calls are kept, and only from the service
    that owns the usage type
  - each change says who (the role behind the session), via what (terraform,
    console, ...), what it launched, and what the guard ledger says about it
  - paging, pacing and event parsing are guard_reconcile's
  - a denied LookupEvents is said, with the one-line policy, and never fails
    the answer
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import boto3
import pytest
from botocore.exceptions import NoCredentialsError
from botocore.stub import Stubber

import finops.guard_ledger as gl
import finops.guard_reconcile as gr
from finops.anomaly import change_events as ce

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
P4D = "BoxUsage:p4d.24xlarge"
CI_ROLE = "arn:aws:iam::123456789012:role/ci"
CI_SESSION = "arn:aws:sts::123456789012:assumed-role/ci/gha-run-77"
START = datetime(2026, 9, 20, tzinfo=UTC)
END = datetime(2026, 9, 23, tzinfo=UTC)
BOX_EVENTS = [n for n, _ in ce.family_events(P4D)]


def _client(region: str = "us-east-1"):
    return boto3.client("cloudtrail", region_name=region, aws_access_key_id="testing",
                        aws_secret_access_key="testing")  # pragma: allowlist secret


class _Session:
    def __init__(self, clients: dict):
        self.clients = clients
        self.asked: list[str] = []

    def client(self, service, region_name=None, config=None):
        assert service == "cloudtrail"
        self.asked.append(region_name)
        return self.clients[region_name]


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _raw(name: str, when: datetime, *, source: str = "ec2.amazonaws.com",
         ua: str = "APN/1.0 HashiCorp/1.0 Terraform/1.9.5 (+https://www.terraform.io)",
         n: int = 8, itype: str = "p4d.24xlarge", eid: str | None = None,
         identity: dict | None = None, error: str | None = None) -> dict:
    ident = identity or {"type": "AssumedRole", "arn": CI_SESSION,
                         "sessionContext": {"sessionIssuer": {"arn": CI_ROLE}}}
    detail: dict = {"eventName": name, "userIdentity": ident, "userAgent": ua,
                    "awsRegion": "us-east-1", "sourceIPAddress": "203.0.113.9"}
    resources = []
    if name == "RunInstances":
        ids = [f"i-0p4d{i}" for i in range(1, n + 1)]
        detail["requestParameters"] = {"instanceType": itype, "instancesSet": {
            "items": [{"imageId": "ami-1", "minCount": n, "maxCount": n}]}}
        detail["responseElements"] = {"instancesSet": {"items": [
            {"instanceId": i, "instanceType": itype} for i in ids]}}
        resources = [{"ResourceType": "AWS::EC2::Instance", "ResourceName": i} for i in ids]
    if error:
        detail["errorCode"] = error
    return {"EventId": eid or f"{name}-{when.isoformat()}", "EventName": name,
            "EventTime": when, "EventSource": source, "Username": "gha-run-77",
            "ReadOnly": "false", "Resources": resources,
            "CloudTrailEvent": json.dumps(detail)}


def _params(key: str, value: str, token: str | None = None, start=START, end=END) -> dict:
    p = {"LookupAttributes": [{"AttributeKey": key, "AttributeValue": value}],
         "StartTime": start, "EndTime": end, "MaxResults": 50}
    if token:
        p["NextToken"] = token
    return p


def _row(**kw) -> dict:
    row = {"usage_type": P4D, "region": "us-east-1", "onset": "2026-09-21",
           "delta_usd": 3145.72, "resources": [{"resource_id": "i-0p4d1"}]}
    row.update(kw)
    return row


def _stub_row(stub: Stubber, *, by_resource=None, by_name=None, resource="i-0p4d1",
              start=START, end=END) -> int:
    """One answer per lookup the p4d row makes, in the order it makes them."""
    n = 0
    if resource:
        stub.add_response("lookup_events", {"Events": by_resource or []},
                          _params("ResourceName", resource, start=start, end=end))
        n += 1
    for name in BOX_EVENTS:
        stub.add_response("lookup_events", {"Events": (by_name or {}).get(name, [])},
                          _params("EventName", name, start=start, end=end))
        n += 1
    return n


def _attach(rows, stubbing, **kw):
    c = _client()
    clock = _Clock()
    with Stubber(c) as stub:
        stubbing(stub)
        got = ce.attach(rows, _Session({"us-east-1": c}), now=NOW, sleep=clock.sleep,
                        clock=clock, **kw)
        stub.assert_no_pending_responses()
    return got, clock


# ── the vocabulary ────────────────────────────────────────────────────────────

def test_the_calls_that_start_a_usage_types_bill():
    assert BOX_EVENTS[0] == "RunInstances" and "SetDesiredCapacity" in BOX_EVENTS
    assert [n for n, _ in ce.family_events("USE2-NatGateway-Hours")] == ["CreateNatGateway"]
    assert ("CreateCluster", "redshift.amazonaws.com") in ce.family_events("Node:dc2.large")
    assert ("CreateCluster", "eks.amazonaws.com") in ce.family_events("USE1-AmazonEKS-Hours")
    assert ce.family_events("USE1-DataTransfer-Out-Bytes") == ()
    for _, events in ce._FAMILIES:
        assert all(ce.is_change(n) for n, _ in events)
    assert not ce.is_change("TerminateInstances") and not ce.is_change("DescribeInstances")
    assert not ce.is_change("CreateTags") and not ce.is_change("PutBucketTagging")


def test_window_and_region():
    from datetime import date
    assert ce.window_for(date(2026, 9, 21)) == (START, END)
    assert ce.trail_region("global") == "us-east-1" and ce.trail_region("NoRegion") == "us-east-1"
    assert ce.trail_region("eu-west-1") == "eu-west-1"
    assert ce.tail("arn:aws:ec2:us-east-1:123456789012:natgateway/nat-0abc") == "nat-0abc"
    assert ce.tail("arn:aws:rds:us-east-1:123456789012:db:prod-db") == "prod-db"
    assert ce.tail("i-0abc") == "i-0abc"


# ── who changed what ──────────────────────────────────────────────────────────

def test_who_launched_what_via_what_and_the_guards_verdict():
    gl.append({"ts": "2026-09-21T03:10:00+00:00", "decision": "allow",
               "action_type": "infra_apply", "command": "terraform apply -auto-approve",
               "session": "s1", "monthly_usd": 128000.0})
    run = _raw("RunInstances", datetime(2026, 9, 21, 3, 12, tzinfo=UTC))
    rows = [_row()]
    got, _ = _attach(rows, lambda s: _stub_row(s, by_resource=[run],
                                               by_name={"RunInstances": [run]}))
    [change] = rows[0]["changes"]            # found twice, kept once
    assert change["event"] == "RunInstances" and change["time"] == "2026-09-21T03:12:00+00:00"
    assert change["who"] == CI_ROLE and change["identity_arn"] == CI_SESSION
    assert change["via"] == "terraform"
    assert change["instance_type"] == "p4d.24xlarge" and change["count"] == 8
    assert "i-0p4d1" in change["resource_ids"] and len(change["resource_ids"]) == 8
    assert change["guard"] == "guard: allowed at $128k/mo, session s1"
    assert change["guard_ledger"]["command"] == "terraform apply -auto-approve"
    assert got["lookup_calls"] == 1 + len(BOX_EVENTS) and got["not_read"] == []
    assert got["regions_read"] == ["us-east-1"]


def test_only_changes_from_the_owning_service_are_kept():
    when = datetime(2026, 9, 21, 3, 0, tzinfo=UTC)
    rows = [_row()]
    _attach(rows, lambda s: _stub_row(s, by_resource=[
        _raw("DescribeInstances", when), _raw("TerminateInstances", when),
        _raw("CreateTags", when), _raw("ModifyInstanceMetadataOptions", when, eid="kept")],
        by_name={
        "UpdateAutoScalingGroup": [_raw("UpdateAutoScalingGroup", when,
                                        source="ec2.amazonaws.com", eid="wrong-source")]}))
    assert [c["event_id"] for c in rows[0]["changes"]] == ["kept"]


def test_a_change_with_no_guard_record_says_so_and_a_console_change_is_read_as_one():
    when = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
    person = {"type": "IAMUser", "arn": "arn:aws:iam::123456789012:user/alice"}
    rows = [_row()]
    _attach(rows, lambda s: _stub_row(s, by_name={"ModifyInstanceAttribute": [
        _raw("ModifyInstanceAttribute", when, identity=person, ua="console.amazonaws.com")]}))
    [change] = rows[0]["changes"]
    assert change["who"] == "arn:aws:iam::123456789012:user/alice"
    assert change["via"] == "console" and change["guard"] == "guard: no record"


def test_pages_are_followed_and_calls_paced():
    when = datetime(2026, 9, 21, 3, 0, tzinfo=UTC)

    def stubbing(stub):
        stub.add_response("lookup_events", {"Events": [_raw("RunInstances", when, eid="a")],
                                            "NextToken": "t1"},
                          _params("ResourceName", "i-0p4d1"))
        stub.add_response("lookup_events", {"Events": [_raw("StartInstances", when, eid="b")]},
                          _params("ResourceName", "i-0p4d1", token="t1"))
        for name in BOX_EVENTS:
            stub.add_response("lookup_events", {"Events": []}, _params("EventName", name))

    rows = [_row()]
    got, clock = _attach(rows, stubbing)
    assert [c["event_id"] for c in rows[0]["changes"]] == ["a", "b"]
    calls = got["lookup_calls"]
    assert calls == 2 + len(BOX_EVENTS)
    assert clock.t >= (calls - 1) / gr.TPS - 1e-9


def test_denied_is_said_with_the_one_line_policy_and_does_not_fail():
    rows = [_row()]

    def stubbing(stub):
        stub.add_client_error("lookup_events", service_error_code="AccessDeniedException",
                              service_message="not authorized", http_status_code=400)

    got, _ = _attach(rows, stubbing)
    assert rows[0]["changes"] == []
    [note] = got["not_read"]
    assert "denied in us-east-1" in note and "not in the connect key" in note
    assert "\n" not in note
    policy = json.loads(note[note.index("{"):])
    assert policy["Statement"][0]["Action"] == ["cloudtrail:LookupEvents"]


def test_no_credentials_is_said():
    class NoCreds:
        def lookup_events(self, **_):
            raise NoCredentialsError()
    rows = [_row()]
    got = ce.attach(rows, _Session({"us-east-1": NoCreds()}), now=NOW, sleep=lambda s: None)
    assert got["not_read"] == ["CloudTrail not read: no AWS credentials were found."]
    assert rows[0]["changes"] == []


def test_a_regions_failure_is_named_and_the_others_still_read():
    bad, good = _client("ap-east-1"), _client("us-east-1")
    rows = [_row(region="ap-east-1"), _row(resources=[])]
    with Stubber(bad) as s1, Stubber(good) as s2:
        s1.add_client_error("lookup_events", service_error_code="UnrecognizedClientException",
                            service_message="region not enabled", http_status_code=400)
        _stub_row(s2, resource=None)
        got = ce.attach(rows, _Session({"ap-east-1": bad, "us-east-1": good}), now=NOW,
                        sleep=lambda s: None)
    assert any("ap-east-1 not read" in n and "UnrecognizedClientException" in n
               for n in got["not_read"])
    assert got["regions_read"] == ["us-east-1"]


def test_the_call_budget_is_a_hard_stop_and_is_said():
    rows = [_row()]

    def stubbing(stub):
        stub.add_response("lookup_events", {"Events": []}, _params("ResourceName", "i-0p4d1"))
        stub.add_response("lookup_events", {"Events": []}, _params("EventName", "RunInstances"))

    got, _ = _attach(rows, stubbing, max_calls=2)
    assert got["lookup_calls"] == 2
    assert any("stopped at 2 LookupEvents calls" in n for n in got["not_read"])


def test_older_than_cloudtrail_history_is_not_asked_for():
    rows = [_row(onset="2026-05-01")]
    got, _ = _attach(rows, lambda s: None)
    assert got["lookup_calls"] == 0
    assert any("90 days" in n for n in got["not_read"])


def test_a_row_with_no_known_call_and_no_resource_is_said():
    rows = [_row(usage_type="USE1-DataTransfer-Out-Bytes", resources=[])]
    got, _ = _attach(rows, lambda s: None)
    assert got["lookup_calls"] == 0
    assert any("DataTransfer-Out-Bytes" in n for n in got["not_read"])


def test_a_row_without_an_onset_is_not_looked_up():
    rows = [_row(onset=None)]
    got, _ = _attach(rows, lambda s: None)
    assert got["lookup_calls"] == 0 and rows[0]["changes"] == []


def test_global_usage_is_looked_up_in_us_east_1():
    rows = [_row(region="global")]
    got, _ = _attach(rows, lambda s: _stub_row(s))
    assert got["regions_read"] == ["us-east-1"]


def test_the_window_stops_at_now():
    rows = [_row(onset="2026-09-25")]
    start, end = datetime(2026, 9, 24, tzinfo=UTC), NOW
    got, _ = _attach(rows, lambda s: _stub_row(s, start=start, end=end))
    assert got["lookup_calls"] == 1 + len(BOX_EVENTS)


# ── the guard's words ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("verdict, event, text", [
    ({"bucket": "denied_but_happened", "ledger": {"decision": "deny", "monthly_usd": 2400}},
     {}, "guard: denied at $2.4k/mo, happened anyway"),
    ({"bucket": "seen_and_happened", "ledger": {"decision": "ask", "monthly_usd": None,
                                                "session": "abc"}},
     {}, "guard: asked, session abc"),
    ({"bucket": "no_guard_record", "ledger": None}, {}, "guard: no record"),
    (None, {"via": "aws-service", "invoked_by": "autoscaling.amazonaws.com"},
     "done by an AWS service (autoscaling.amazonaws.com)"),
])
def test_guard_label(verdict, event, text):
    assert ce.guard_label(verdict, event) == text


def test_matching_is_reconciles():
    """A narrow command matches only its own API, as in `nable guard reconcile`."""
    when = datetime(2026, 9, 21, 3, 12, tzinfo=UTC)
    records = [{"ts": "2026-09-21T03:10:00+00:00", "decision": "allow",
                "action_type": "infra_apply", "command": "aws rds create-db-instance"}]
    ev = gr._event(_raw("RunInstances", when), "us-east-1")
    assert ce.guard_verdicts([ev], records)[ev["event_id"]]["bucket"] == "no_guard_record"
    records[0]["command"] = "aws ec2 run-instances --instance-type p4d.24xlarge"
    assert ce.guard_verdicts([ev], records)[ev["event_id"]]["bucket"] == "seen_and_happened"
