"""`nable guard reconcile`: the ledger against what CloudTrail says happened.

Every CloudTrail answer here goes through botocore's Stubber on a real
cloudtrail client, so request parameters and response shapes are checked
against the service model, not against what this file assumes.

Invariants under test:
  - an event is put in exactly one bucket: seen, denied but happened, no
    guard record, or done by an AWS service
  - matching is conservative: kind, time window and, where the ledger names
    the API, the event name all have to fit
  - LookupEvents is paged, paced at 2 requests a second per region, and is
    the only call made
  - AccessDenied says which action to grant, with a policy to paste
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.exceptions import NoCredentialsError
from botocore.stub import ANY, Stubber

import finops.guard_ledger as gl
import finops.guard_reconcile as gr

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _ts(minutes_ago: float) -> datetime:
    return NOW - timedelta(minutes=minutes_ago)


def _ledger(decision: str, action_type: str, command: str, minutes_ago: float, **kw) -> None:
    gl.append({"ts": _ts(minutes_ago).isoformat(timespec="seconds"), "decision": decision,
               "action_type": action_type, "command": command, **kw})


AGENT_ARN = "arn:aws:sts::123456789012:assumed-role/agent-role/claude"
PERSON_ARN = "arn:aws:iam::123456789012:user/alice"


def _event(name: str, minutes_ago: float, *, arn: str = AGENT_ARN,
           ua: str = "aws-cli/2.15.0 Python/3.11 Linux", eid: str | None = None,
           identity: dict | None = None, error: str | None = None) -> dict:
    ident = identity or {"type": "AssumedRole", "arn": arn,
                         "sessionContext": {"sessionIssuer": {
                             "arn": "arn:aws:iam::123456789012:role/agent-role"}}}
    detail = {"eventName": name, "userIdentity": ident, "userAgent": ua,
              "awsRegion": "us-east-1", "sourceIPAddress": "203.0.113.9"}
    if error:
        detail["errorCode"] = error
    return {"EventId": eid or f"{name}-{minutes_ago}", "EventName": name,
            "EventTime": _ts(minutes_ago), "EventSource": gr.EVENTS[name][1],
            "Username": arn.rsplit("/", 1)[-1], "ReadOnly": "false",
            "Resources": [{"ResourceType": "AWS::EC2::Instance", "ResourceName": "i-0abc"}],
            "CloudTrailEvent": json.dumps(detail)}


def _client(region: str = "us-east-1"):
    return boto3.client("cloudtrail", region_name=region, aws_access_key_id="testing",
                        aws_secret_access_key="testing")  # pragma: allowlist secret


def _stub_all(stub: Stubber, pages: dict[str, list[list[dict]]] | None = None) -> int:
    """Queue one answer per LookupEvents call reconcile will make, in order.
    Returns how many calls that is."""
    pages = pages or {}
    n = 0
    for name in gr.EVENTS:
        for i, events in enumerate(pages.get(name, [[]])):
            last = i == len(pages.get(name, [[]])) - 1
            resp: dict = {"Events": events}
            if not last:
                resp["NextToken"] = f"{name}-page-{i + 1}"
            params = {"LookupAttributes": [{"AttributeKey": "EventName", "AttributeValue": name}],
                      "StartTime": ANY, "EndTime": ANY, "MaxResults": 50}
            if i:
                params["NextToken"] = f"{name}-page-{i}"
            stub.add_response("lookup_events", resp, params)
            n += 1
    return n


class _Session:
    region_name = "us-east-1"

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


def _run(pages=None, *, regions=None, **kw):
    c = _client()
    clock = _Clock()
    with Stubber(c) as stub:
        _stub_all(stub, pages)
        r = gr.reconcile(24, regions, session=_Session({"us-east-1": c}), now=NOW,
                         sleep=clock.sleep, clock=clock, **kw)
        stub.assert_no_pending_responses()
    return r, clock


# ── the buckets ───────────────────────────────────────────────────────────────

def test_each_event_lands_in_the_right_bucket():
    _ledger("allow", "infra_apply", "aws ec2 run-instances --instance-type t3.micro", 10,
            session="s1")
    _ledger("deny", "terminate_instance", "aws ec2 terminate-instances --instance-ids i-1", 30)
    _ledger("allow", "infra_apply", "aws rds create-db-instance --db-instance-class db.t3.micro "
            "--engine mysql", 50)
    r, _ = _run({
        "RunInstances": [[
            _event("RunInstances", 9),
            _event("RunInstances", 120, identity={"type": "AWSService",
                                                  "invokedBy": "cloudformation.amazonaws.com"},
                   ua="cloudformation.amazonaws.com"),
        ]],
        "TerminateInstances": [[_event("TerminateInstances", 29)]],
        "CreateStack": [[_event("CreateStack", 60, arn=PERSON_ARN,
                                ua="console.amazonaws.com")]],
    })
    [seen] = r["seen_and_happened"]
    assert seen["event"] == "RunInstances" and seen["ledger"]["decision"] == "allow"
    assert seen["ledger"]["session"] == "s1"
    assert seen["identity_arn"] == AGENT_ARN and seen["user_agent"].startswith("aws-cli/")
    assert seen["via"] == "aws-cli"
    [bypass] = r["denied_but_happened"]
    assert bypass["event"] == "TerminateInstances" and bypass["ledger"]["decision"] == "deny"
    [outside] = r["console"]
    assert outside["event"] == "CreateStack" and outside["identity_arn"] == PERSON_ARN
    assert outside["via"] == "console" and r["no_guard_record"] == []
    [svc] = r["service_initiated"]
    assert svc["invoked_by"] == "cloudformation.amazonaws.com"
    [quiet] = r["guarded_without_event"]
    assert "create-db-instance" in quiet["command"]
    assert "no resource ids" in r["matching"]


def test_matching_is_conservative():
    _ledger("allow", "infra_apply", "aws ec2 run-instances --instance-type t3.micro", 10)
    _ledger("allow", "infra_apply", "kubectl apply -f app.yaml", 10)
    r, _ = _run({
        "CreateDBInstance": [[_event("CreateDBInstance", 9)]],      # not the API it named
        "TerminateInstances": [[_event("TerminateInstances", 9)]],  # not the kind
        "CreateLoadBalancer": [[_event("CreateLoadBalancer", 9)]],  # kubectl calls no AWS API
        "RunInstances": [[_event("RunInstances", 3 * 60)]],         # outside the window
    })
    assert r["seen_and_happened"] == []
    assert sorted(e["event"] for e in r["no_guard_record"]) == [
        "CreateDBInstance", "CreateLoadBalancer", "RunInstances", "TerminateInstances"]


def test_a_broad_command_accounts_for_any_event_of_its_kind():
    _ledger("allow", "infra_apply", "terraform apply -auto-approve", 20)
    _ledger("ask", "delete_resource", "terraform destroy", 15)
    r, _ = _run({"CreateNatGateway": [[_event("CreateNatGateway", 18)]],
                 "DeleteDBInstance": [[_event("DeleteDBInstance", 12)]]})
    assert [e["event"] for e in r["seen_and_happened"]] == ["CreateNatGateway", "DeleteDBInstance"]


def test_bypass_is_claimed_only_when_a_deny_is_all_that_fits():
    _ledger("deny", "infra_apply", "aws ec2 run-instances --instance-type p4d.24xlarge", 12)
    _ledger("allow", "infra_apply", "aws ec2 run-instances --instance-type t3.micro", 11)
    r, _ = _run({"RunInstances": [[_event("RunInstances", 10)]]})
    assert len(r["seen_and_happened"]) == 1 and r["denied_but_happened"] == []


def test_a_failed_call_is_reported_with_its_error():
    r, _ = _run({"RunInstances": [[_event("RunInstances", 5, error="UnauthorizedOperation")]]})
    assert r["attempted_but_failed"][0]["error_code"] == "UnauthorizedOperation"
    assert r["no_guard_record"] == []


def test_a_failed_call_is_never_a_happened_or_a_bypass():
    """A denied terminate that AWS also refused did not happen anyway."""
    _ledger("deny", "terminate_instance", "aws ec2 terminate-instances --instance-ids i-1", 30)
    _ledger("allow", "infra_apply", "aws ec2 run-instances --instance-type t3.micro", 10)
    r, _ = _run({"TerminateInstances": [[_event("TerminateInstances", 29,
                                                error="UnauthorizedOperation")]],
                 "RunInstances": [[_event("RunInstances", 9, error="InsufficientInstanceCapacity")]]})
    assert [e["event"] for e in r["attempted_but_failed"]] == ["TerminateInstances",
                                                                "RunInstances"]
    assert r["denied_but_happened"] == [] and r["seen_and_happened"] == []


def test_a_console_action_is_never_explained_by_a_nearby_record():
    """An agent's allowed launch a minute before someone's console launch
    used to account for both, and the console change vanished from the audit."""
    _ledger("allow", "infra_apply", "terraform apply", 10)
    r, _ = _run({"RunInstances": [[
        _event("RunInstances", 9, eid="cli"),
        _event("RunInstances", 8, eid="con", arn=PERSON_ARN, ua="signin.amazonaws.com"),
    ]]})
    assert [e["event_id"] for e in r["seen_and_happened"]] == ["cli"]
    assert [e["event_id"] for e in r["console"]] == ["con"]


def test_a_record_naming_one_api_explains_one_event():
    _ledger("allow", "infra_apply", "aws ec2 run-instances --instance-type t3.micro --count 1", 10)
    r, _ = _run({"RunInstances": [[_event("RunInstances", 9 - i, eid=f"e{i}")
                                   for i in range(3)]]})
    assert len(r["seen_and_happened"]) == 1 and len(r["no_guard_record"]) == 2


def test_a_broad_record_still_explains_every_event_of_its_kind():
    _ledger("allow", "infra_apply", "terraform apply", 10)
    r, _ = _run({"RunInstances": [[_event("RunInstances", 9 - i, eid=f"e{i}")
                                   for i in range(3)]]})
    assert len(r["seen_and_happened"]) == 3 and r["no_guard_record"] == []


# ── the API: paged, paced, read-only ──────────────────────────────────────────

def test_pages_are_followed():
    page1 = [_event("RunInstances", 100 + i, eid=f"a{i}") for i in range(3)]
    page2 = [_event("RunInstances", 200 + i, eid=f"b{i}") for i in range(2)]
    r, _ = _run({"RunInstances": [page1, page2]})
    assert r["events_read"] == 5 and len(r["no_guard_record"]) == 5
    assert r["lookup_calls"] == len(gr.EVENTS) + 1


def test_calls_are_paced_at_two_a_second_per_region():
    r, clock = _run()
    calls = r["lookup_calls"]
    assert calls == len(gr.EVENTS)
    assert clock.t >= (calls - 1) / gr.TPS - 1e-9
    assert all(s <= 0.5 + 1e-9 for s in clock.sleeps)


def test_each_region_is_read_with_its_own_client_and_pacer():
    east, west = _client("us-east-1"), _client("us-west-2")
    clock = _Clock()
    session = _Session({"us-east-1": east, "us-west-2": west})
    with Stubber(east) as s1, Stubber(west) as s2:
        _stub_all(s1)
        _stub_all(s2, {"RunInstances": [[_event("RunInstances", 5)]]})
        r = gr.reconcile(24, ["us-east-1", "us-west-2"], session=session, now=NOW,
                         sleep=clock.sleep, clock=clock)
    assert session.asked == ["us-east-1", "us-west-2"]
    assert r["regions"] == ["us-east-1", "us-west-2"] and r["events_read"] == 1


def test_access_denied_names_the_action_to_grant():
    c = _client()
    with Stubber(c) as stub:
        stub.add_client_error("lookup_events", service_error_code="AccessDeniedException",
                              service_message="not authorized", http_status_code=400)
        with pytest.raises(gr.ReconcileError) as e:
            gr.reconcile(1, session=_Session({"us-east-1": c}), now=NOW,
                         sleep=lambda s: None)
    msg = str(e.value)
    assert "cloudtrail:LookupEvents" in msg and "read-only" in msg
    assert json.loads(msg[msg.index("{"):])["Statement"][0]["Action"] == ["cloudtrail:LookupEvents"]


def test_another_regions_failure_does_not_stop_the_rest():
    bad, good = _client("ap-east-1"), _client("us-east-1")
    with Stubber(bad) as s1, Stubber(good) as s2:
        s1.add_client_error("lookup_events", service_error_code="UnrecognizedClientException",
                            service_message="region not enabled", http_status_code=400)
        _stub_all(s2)
        r = gr.reconcile(1, ["ap-east-1", "us-east-1"],
                         session=_Session({"ap-east-1": bad, "us-east-1": good}),
                         now=NOW, sleep=lambda s: None)
    assert "UnrecognizedClientException" in r["region_errors"]["ap-east-1"]
    assert r["lookup_calls"] == len(gr.EVENTS)


def test_missing_credentials_say_so():
    class NoCreds:
        def lookup_events(self, **_):
            raise NoCredentialsError()
    with pytest.raises(gr.ReconcileError, match="No AWS credentials"):
        gr.reconcile(1, session=_Session({"us-east-1": NoCreds()}), now=NOW,
                     sleep=lambda s: None)


def test_the_default_region_is_the_sessions():
    r, _ = _run()
    assert r["regions"] == ["us-east-1"]


# ── the IAM text ──────────────────────────────────────────────────────────────

def test_lookup_events_is_listed_for_reconcile_and_nowhere_it_is_not_used():
    """The scan does not call it and the connect key is Get/Describe/List only
    (event history says who did what, a wider read), so it is granted on
    its own, with the policy reconcile prints."""
    from finops import scan_manifest
    from finops.security.iam_setup import _REQUIRED_ACTIONS
    assert ("cloudtrail.lookup_events", "cloudtrail:LookupEvents") in \
        scan_manifest.GUARD_RECONCILE_ACTIONS
    assert "cloudtrail:LookupEvents" not in scan_manifest.iam_actions(include_spend=True)
    assert "cloudtrail:LookupEvents" not in _REQUIRED_ACTIONS
    assert gr.lookup_events_policy()["Statement"][0]["Action"] == ["cloudtrail:LookupEvents"]


# ── the CLI ───────────────────────────────────────────────────────────────────

def _cli(monkeypatch, result=None, error=None, **kw):
    from finops import setup_wizard

    def fake(hours, regions, tolerance_minutes=5):
        fake.args = (hours, regions, tolerance_minutes)
        if error:
            raise gr.ReconcileError(error)
        return result
    monkeypatch.setattr(gr, "reconcile", fake)
    kw.setdefault("guard_json", False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup_wizard._run_guard(argparse.Namespace(guard_action="reconcile", guard_global=False,
                                                   **kw))
    return out.getvalue(), fake


def test_cli_prints_the_buckets_with_who_did_it(monkeypatch):
    _ledger("deny", "terminate_instance", "aws ec2 terminate-instances --instance-ids i-1", 30)
    c = _client()
    with Stubber(c) as stub:
        _stub_all(stub, {"TerminateInstances": [[_event("TerminateInstances", 29)]],
                         "CreateStack": [[_event("CreateStack", 60, arn=PERSON_ARN,
                                                 ua="console.amazonaws.com")]]})
        result = gr.reconcile(24, session=_Session({"us-east-1": c}), now=NOW,
                              sleep=lambda s: None)
    out, fake = _cli(monkeypatch, result, guard_hours=6, guard_regions=["us-east-1"],
                     guard_tolerance=3)
    assert fake.args == (6, ["us-east-1"], 3)
    assert "Denied by the guard, happened anyway (1)" in out
    assert "No guard record (0)" in out
    assert "Made in the AWS console (no agent hook runs there) (1)" in out
    assert PERSON_ARN in out and "via console" in out
    assert "guard: deny at" in out
    assert "no resource ids" in out


def test_cli_json(monkeypatch):
    out, _ = _cli(monkeypatch, {"window": {}, "no_guard_record": []}, guard_json=True)
    assert json.loads(out)["no_guard_record"] == []


def test_cli_access_denied_exits_1_with_the_message(monkeypatch):
    with pytest.raises(SystemExit) as e:
        _cli(monkeypatch, error="CloudTrail refused LookupEvents ... cloudtrail:LookupEvents")
    assert e.value.code == 1


def test_cli_arguments_parse():
    from finops import setup_wizard
    seen = {}
    real = setup_wizard._run_guard
    setup_wizard._run_guard = lambda parsed: seen.update(vars(parsed))
    try:
        setup_wizard.main(["guard", "reconcile", "--hours", "2", "--region", "us-east-1",
                           "--region", "eu-west-1", "--tolerance-minutes", "10", "--json"])
    finally:
        setup_wizard._run_guard = real
    assert seen["guard_action"] == "reconcile" and seen["guard_hours"] == 2
    assert seen["guard_regions"] == ["us-east-1", "eu-west-1"]
    assert seen["guard_tolerance"] == 10 and seen["guard_json"] is True


@pytest.mark.parametrize("ua,via", [
    ("signin.amazonaws.com", "console"),
    ("console.amazonaws.com", "console"),
    ("console.ec2.amazonaws.com", "console"),
    ("aws-cli/2.15.0 Python/3.11 Linux/6 exe/x86_64 prompt/off command/ec2.run-instances", "aws-cli"),
    # A host name embedded in something else is not the console.
    ("evil.example/signin.amazonaws.com.attacker", "sdk"),
])
def test_console_is_matched_by_whole_host(ua, via):
    from finops.guard_reconcile import _via
    got = _via(ua, {"type": "IAMUser"})
    assert (got == "console") == (via == "console"), got
