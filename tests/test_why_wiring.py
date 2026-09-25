"""The root-cause drill-down, wired in: the tools, impact.enrich, and `nable why`.

Invariants under test:
  - off by default everywhere: get_anomalies, explain_recent_cost_drivers
    and impact.enrich make no billed request unless asked (root_cause=True,
    drill_down=True), and the scheduler's enrich() call is unchanged
  - when asked, AWS spikes and AWS increases get the drill-down, capped, and
    the answer carries the lines, what could not be read, and what the Cost
    Explorer requests cost
  - `nable why` discloses the Cost Explorer cost before the first request,
    prints one line per usage type, and answers --json on stdout alone
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
from datetime import UTC, date, datetime, timedelta

import boto3
import pytest
from botocore.stub import Stubber

from finops.anomaly import change_events as ce
from finops.anomaly import drilldown as dd
from finops.anomaly import impact
from finops.anomaly import root_cause as rc

EC2 = "Amazon Elastic Compute Cloud - Compute"
P4D = "BoxUsage:p4d.24xlarge"


def _fake_explain(calls: list):
    def explain(service, current, baseline, **kw):
        calls.append((service, current, baseline, kw))
        return {"service": service, "current": current.as_dict(),
                "baseline": baseline.as_dict(), "rows": [], "resource_source": None,
                "lines": [f"{rc.short_name(service)} +$100/mo since Sep 21: x"],
                "not_read": ["No CUR source is configured"], "cost_explorer_requests": 2,
                "lookup_calls": 7}
    return explain


# ── impact.enrich ─────────────────────────────────────────────────────────────

def _anomaly(**kw):
    a = {"provider": "aws", "service": EC2, "account_id": "123456789012",
         "direction": "spike", "baseline_mean": 100.0, "current_amount": 900.0,
         "snapshot_date": "2026-09-21"}
    a.update(kw)
    return a


def test_enrich_drills_down_only_when_asked(monkeypatch):
    calls: list = []
    monkeypatch.setattr(rc, "explain", _fake_explain(calls))
    assert "root_cause" not in impact.enrich(_anomaly()) and calls == []
    out = impact.enrich(_anomaly(), drill_down=True)
    assert out["root_cause"]["lines"][0].startswith("EC2 +$100/mo")
    [(service, current, baseline, kw)] = calls
    # The snapshot's account is the one the data was read in (on a payer, the
    # payer): filtering on it would hide the member accounts' spend.
    assert service == EC2 and kw["account_id"] is None
    assert (current.start, current.days) == (date(2026, 9, 21), 1)
    assert (baseline.start, baseline.days) == (date(2026, 9, 14), 7)


@pytest.mark.parametrize("kw", [{"provider": "gcp"}, {"direction": "drop"},
                                {"snapshot_date": None}])
def test_enrich_drill_down_is_for_aws_spikes_only(monkeypatch, kw):
    calls: list = []
    monkeypatch.setattr(rc, "explain", _fake_explain(calls))
    assert "root_cause" not in impact.enrich(_anomaly(**kw), drill_down=True)
    assert calls == []


def test_a_non_account_id_is_not_used_as_a_filter(monkeypatch):
    calls: list = []
    monkeypatch.setattr(rc, "explain", _fake_explain(calls))
    impact.enrich(_anomaly(account_id="default"), drill_down=True)
    assert calls[0][3]["account_id"] is None


def test_only_an_explicit_other_linked_account_is_a_filter(monkeypatch):
    calls: list = []
    monkeypatch.setattr(rc, "explain", _fake_explain(calls))
    impact.enrich(_anomaly(linked_account_id="210987654321"), drill_down=True)
    impact.enrich(_anomaly(linked_account_id="123456789012"), drill_down=True)
    assert [c[3]["account_id"] for c in calls] == ["210987654321", None]


def test_the_next_step_points_at_the_drill_down_for_aws():
    assert "root_cause=True" in impact.impact(_anomaly())["next_step"]
    assert "root_cause" not in impact.impact(_anomaly(provider="azure"))["next_step"]


# ── get_anomalies ─────────────────────────────────────────────────────────────

def _rows(n_aws: int):
    out = []
    for i in range(n_aws):
        out.append({"id": i + 1, "provider": "aws", "service": f"{EC2} {i}",
                    "account_id": "123456789012", "severity": "high", "direction": "spike",
                    "pct_change": 400.0, "current_amount": 900.0, "baseline_mean": 100.0,
                    "z_score": 5.0, "detected_at": "2026-09-22", "snapshot_date": "2026-09-21"})
    out.append({**out[0], "id": 99, "provider": "gcp", "service": "Compute Engine"})
    out.append({**out[0], "id": 98, "direction": "drop", "pct_change": -60.0})
    return out


def _get_anomalies(monkeypatch, rows, **kw):
    import finops.demo_data as demo
    import finops.server as srv
    from finops.anomaly import detector

    monkeypatch.setattr(demo, "is_demo", lambda: False)
    monkeypatch.setattr(srv, "_load_alert_policies", list)
    monkeypatch.setattr(srv, "_team_nudge", lambda *a, **k: None)
    monkeypatch.setattr(detector, "get_active_anomalies", lambda **k: rows)
    out = srv.get_anomalies(**kw)
    if asyncio.iscoroutine(out):
        out = asyncio.run(out)
    return out


def test_get_anomalies_makes_no_billed_request_by_default(monkeypatch):
    calls: list = []
    monkeypatch.setattr(rc, "explain", _fake_explain(calls))
    out = _get_anomalies(monkeypatch, _rows(1))
    assert "root_cause" not in out and calls == []
    assert all("root_cause" not in a for a in out["anomalies"])


def test_get_anomalies_root_cause_drills_into_aws_spikes_only_and_caps(monkeypatch):
    calls: list = []
    monkeypatch.setattr(rc, "explain", _fake_explain(calls))
    out = _get_anomalies(monkeypatch, _rows(4), root_cause=True)
    assert [c[0] for c in calls] == [f"{EC2} {i}" for i in range(3)]
    drilled = [a for a in out["anomalies"] if "root_cause" in a]
    assert [a["id"] for a in drilled] == [1, 2, 3]
    block = out["root_cause"]
    assert len(block["lines"]) == 3 and block["cost_explorer_requests"] == 6
    assert "about $0.06" in block["cost_note"]
    assert block["not_read"] == ["No CUR source is configured"]
    assert "1 more AWS spike" in block["not_drilled"]
    assert "24 hours" in block["attribution_rules"]


def test_get_anomalies_root_cause_with_no_aws_spike_says_so(monkeypatch):
    calls: list = []
    monkeypatch.setattr(rc, "explain", _fake_explain(calls))
    rows = [r for r in _rows(1) if r["provider"] != "aws" or r["direction"] == "drop"]
    out = _get_anomalies(monkeypatch, rows, root_cause=True)
    assert calls == [] and "AWS spikes only" in out["root_cause"]["note"]


# ── explain_recent_cost_drivers ───────────────────────────────────────────────

class _Growing:
    """EC2 $20 in the prior window, $900 in this one; S3 flat."""

    def __init__(self, cutoff):
        self.cutoff = cutoff

    async def is_configured(self):
        return True

    async def get_costs(self, start, end, granularity="MONTHLY", **kw):
        from finops.connectors.base import CostEntry, CostSummary
        ec2 = 900.0 if start >= self.cutoff else 20.0
        by = {EC2: ec2, "Amazon Simple Storage Service": 50.0}
        entries = [CostEntry(provider="aws", account_id="1", account_name="1", service=s,
                             region="us-east-1", amount=v) for s, v in by.items()]
        return CostSummary(provider="aws", start_date=start, end_date=end,
                           total_usd=sum(by.values()), by_service=by, by_account={"1": 1.0},
                           by_region={"us-east-1": 1.0}, entries=entries)


def _drivers(monkeypatch, **kw):
    from finops import cache, server

    cache.clear()
    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    # The tool's own clock (local date), so the windows split where it splits them.
    conn = _Growing(server.date.today() - timedelta(days=7))
    conn._session = "the-connectors-session"

    async def _active(subset=None):
        return {"aws": conn}

    async def _no_credit(*a, **k):
        return None

    monkeypatch.setattr(server, "_active", _active)
    monkeypatch.setattr(server, "_credit_context", _no_credit)
    return asyncio.run(server.explain_recent_cost_drivers(days=7, **kw))


def test_drivers_make_no_billed_request_by_default(monkeypatch):
    calls: list = []
    monkeypatch.setattr(rc, "explain", _fake_explain(calls))
    out = _drivers(monkeypatch)
    assert out["top_increases"][0]["key"] == EC2
    assert "root_cause" not in out and calls == []


def test_drivers_root_cause_drills_into_the_aws_increases(monkeypatch):
    calls: list = []
    monkeypatch.setattr(rc, "explain", _fake_explain(calls))
    out = _drivers(monkeypatch, root_cause=True)
    [(service, current, baseline, kw)] = calls          # S3 did not move
    assert service == EC2 and kw["session"] == "the-connectors-session"
    assert current.days == 7 and baseline.end == current.start
    block = out["root_cause"]
    assert block["lines"] == ["EC2 +$100/mo since Sep 21: x"]
    assert block["services"][0]["service"] == EC2
    assert "2 Cost Explorer requests" in block["cost_note"]


# ── the manifest ──────────────────────────────────────────────────────────────

def test_why_actions_are_the_cost_explorer_calls_the_drill_down_makes():
    import inspect
    import re

    from finops.scan_manifest import GUARD_RECONCILE_ACTIONS, WHY_ACTIONS, iam_actions
    called = set(re.findall(r'\.pages\(\s*"([a-z_]+)"', inspect.getsource(dd)))
    assert {f"ce.{c}" for c in called} == {call for call, _ in WHY_ACTIONS}
    assert "ce:GetCostAndUsageWithResources" not in iam_actions(include_spend=True)
    assert GUARD_RECONCILE_ACTIONS == [("cloudtrail.lookup_events", "cloudtrail:LookupEvents")]


# ── nable why ─────────────────────────────────────────────────────────────────

def _day(d, groups):
    return {"TimePeriod": {"Start": d.isoformat(), "End": (d + timedelta(days=1)).isoformat()},
            "Estimated": False, "Total": {},
            "Groups": [{"Keys": keys,
                        "Metrics": {"UnblendedCost": {"Amount": str(v), "Unit": "USD"},
                                    "UsageQuantity": {"Amount": "1", "Unit": "Hrs"}}}
                       for keys, v in groups]}


class _Session:
    def __init__(self, ce_client, ct_client):
        self.ce, self.ct = ce_client, ct_client

    def client(self, service, region_name=None, config=None):
        return {"ce": self.ce, "cloudtrail": self.ct}[service]


def _clients():
    kw = {"region_name": "us-east-1", "aws_access_key_id": "testing",
          "aws_secret_access_key": "testing"}  # pragma: allowlist secret
    return boto3.client("ce", **kw), boto3.client("cloudtrail", **kw)


def _why(billed_stub, argv, stubbing):
    from finops import cli_why

    parser = argparse.ArgumentParser()
    cli_why.add_parser(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["why", *argv])
    c, t = _clients()
    out, err = io.StringIO(), io.StringIO()
    with billed_stub(c) as s1, Stubber(t) as s2:
        stubbing(s1, s2)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_why.run(args, session=_Session(c, t))
        s1.assert_no_pending_responses()
        s2.assert_no_pending_responses()
    return code, out.getvalue(), err.getvalue()


def _p4d_story(s1, s2, *, days=7, cloudtrail=True):
    cur, base = dd.windows_for_period(days)
    onset = cur.start + timedelta(days=2)
    services, usage = [], []
    for d in base.dates() + cur.dates():
        on = d >= onset
        services.append(_day(d, [([EC2], 300.0 if on else 10.0),
                                 (["Amazon Simple Storage Service"], 5.0)]))
        usage.append(_day(d, [([P4D, "us-east-1"], 290.0)] if on else []))
    s1.add_response("get_cost_and_usage", {"ResultsByTime": services})
    s1.add_response("get_cost_and_usage", {"ResultsByTime": usage})
    s1.add_client_error("get_cost_and_usage_with_resources",
                        service_error_code="DataUnavailableException",
                        service_message="not enabled", http_status_code=400)
    when = datetime.combine(onset, datetime.min.time(), tzinfo=UTC) - timedelta(hours=2)
    detail = {"eventName": "RunInstances", "awsRegion": "us-east-1",
              "userAgent": "aws-cli/2.15.0 Python/3.11",
              "userIdentity": {"type": "IAMUser",
                               "arn": "arn:aws:iam::123456789012:user/alice"},
              "requestParameters": {"instanceType": "p4d.24xlarge"},
              "responseElements": {"instancesSet": {"items": [
                  {"instanceId": "i-0a", "instanceType": "p4d.24xlarge"}]}}}
    run = {"EventId": "ev-1", "EventName": "RunInstances", "EventTime": when,
           "EventSource": "ec2.amazonaws.com", "CloudTrailEvent": json.dumps(detail)}
    for name, _ in ce.family_events(P4D):
        if cloudtrail:
            s2.add_response("lookup_events",
                            {"Events": [run] if name == "RunInstances" else []})
    return onset


def test_nable_why_prints_the_line_and_says_what_it_cost(billed_stub):
    holder = {}

    def stubbing(s1, s2):
        holder["onset"] = _p4d_story(s1, s2)

    code, out, _ = _why(billed_stub, [], stubbing)
    assert code == 0
    first, disclosure = out.splitlines()[:2]
    assert first.startswith("nable why")
    assert "up to about 7 requests, $0.07" in disclosure and "$0.01 each" in disclosure
    onset = holder["onset"]
    assert ("EC2 +$" in out and f"since {onset.strftime('%b')} {onset.day}: 1x p4d.24xlarge "
            "launched by arn:aws:iam::123456789012:user/alice via aws-cli "
            "(guard: no record) [likely]") in out
    assert "Not read" in out and "not enabled" in out
    assert "3 Cost Explorer requests, about $0.03" in out
    assert "6 CloudTrail LookupEvents call(s), free." in out


def test_nable_why_json_is_one_document_on_stdout(billed_stub):
    code, out, err = _why(billed_stub, ["--json", "--service", "ec2"],
                          lambda s1, s2: _p4d_story(s1, s2))
    assert code == 0
    doc = json.loads(out)
    assert doc["command"] == "why" and doc["cost_explorer_requests"] == 3
    assert doc["services"][0]["service"] == EC2
    assert doc["lines"][0].endswith("[likely]")
    assert "$0.01 each" in err


def test_nable_why_no_cloudtrail(billed_stub):
    def stubbing(s1, s2):
        _p4d_story(s1, s2, cloudtrail=False)

    code, out, _ = _why(billed_stub, ["--no-cloudtrail", "--service", "EC2"], stubbing)
    assert code == 0 and "CloudTrail for it was not read" in out
    assert "0 CloudTrail LookupEvents call(s)" in out


def test_nable_why_nothing_rose(billed_stub):
    def stubbing(s1, s2):
        s1.add_response("get_cost_and_usage", {"ResultsByTime": []})

    code, out, _ = _why(billed_stub, [], stubbing)
    assert code == 0 and "No AWS service rose" in out


def test_nable_why_cost_explorer_denied_exits_1(billed_stub):
    def stubbing(s1, s2):
        s1.add_client_error("get_cost_and_usage", service_error_code="AccessDeniedException",
                            service_message="no", http_status_code=400)

    code, _, err = _why(billed_stub, [], stubbing)
    assert code == 1 and "ce:GetCostAndUsage" in err


def test_nable_why_rejects_a_window_it_cannot_read(billed_stub):
    code, out, _ = _why(billed_stub, ["--days", "90", "--json"], lambda s1, s2: None)
    assert code == 2 and "1 to 45" in json.loads(out)["error"]["message"]


@pytest.mark.parametrize("wanted, expected", [
    ("EC2", EC2), ("ec2", EC2), ("rds", "Amazon Relational Database Service"),
    ("Amazon Relational Database Service", "Amazon Relational Database Service"),
    ("storage", "Amazon Simple Storage Service"), ("Amazon Nothing", "Amazon Nothing"),
])
def test_service_names_resolve(wanted, expected):
    from finops.cli_why import resolve_service
    names = [EC2, "Amazon Relational Database Service", "Amazon Simple Storage Service"]
    assert resolve_service(wanted, names) == expected


def test_why_is_a_registered_command_with_its_flags():
    from finops import setup_wizard

    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as e:
        setup_wizard.main(["why", "--help"])
    assert e.value.code == 0
    text = out.getvalue()
    for flag in ("--days", "--service", "--json", "--no-cloudtrail"):
        assert flag in text


def test_nable_why_respects_the_cost_explorer_ban(billed_stub, monkeypatch):
    monkeypatch.setenv("NABLE_NO_COST_EXPLORER", "1")
    code, _, err = _why(billed_stub, [], lambda s1, s2: None)
    assert code == 1 and "NABLE_NO_COST_EXPLORER" in err
