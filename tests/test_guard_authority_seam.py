# SPDX-License-Identifier: Apache-2.0
"""Seams S1 and S2: the authority ladder in the guard, its standing in `nable cloud`.

The ladder itself (finops.guard_authority, finops.authority_standing) is
another workstream's. These tests hold the guard's side of the seam against
stub modules put in sys.modules, so they pass whether or not the real
modules are installed:

  - S1: after its own verdict on a shell command or an MCP call, the guard
    asks guard_authority.ladder_verdict(command, cwd=, harness=, scope=,
    mcp=). The stricter decision answers: an ask turns an allow into an ask,
    and an allow never softens an ask or a deny (a production context, a
    threshold, a purchase); the guard's figures, rule and estimate stand, and
    when both ask the ladder's sentence follows the guard's. authority_class,
    rung and within_bound go onto the verdict and its ledger line. A call the
    guard would pass in silence that the ladder places in a class is recorded
    as an allow line with its tool_use_id. An absent module changes nothing;
    a raising one changes no decision and writes authority_trouble.
    doctor() calls guard_authority.doctor(covered, gaps, fix).
  - S2: `nable cloud status` and `nable cloud connect --send` call
    authority_standing.keep with the status answer's `authority` block, the
    box, its workspace_ref and when it was fetched, and print
    authority_standing.text. An absent module keeps nothing and says nothing.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import types
from datetime import UTC, datetime

import pytest

import finops.guard as g
import finops.guard_ledger as gl
from finops import ai_budget, cli_cloud, cloud_standing
from finops import cloud_connect as cc
from tests.test_cloud_connect import BOX, KEY, Box, records, write_ledger

AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
WS_A = "ws_" + "a" * 32
TAG = "aws ec2 create-tags --resources i-0123456789abcdef0 --tags Key=team,Value=payments"
SMALL = "aws ec2 run-instances --region us-east-1 --instance-type t3.micro"
BIG = "aws ec2 run-instances --region us-east-1 --instance-type m5.4xlarge --count 4"
PROD = "kubectl delete deployment web -n prod"
BUY = ("aws ec2 purchase-reserved-instances-offering --reserved-instances-offering-id "
       "abc-123 --instance-count 2")


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_GUARD_TEAM", "FINOPS_POLICY_FILE", "FINOPS_PROFILE",
                "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "XDG_CONFIG_HOME",
                "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
                "FINOPS_GUARD_PERSON", "FINOPS_AIRGAP", cc.KEY_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    monkeypatch.setattr(cloud_standing, "now_epoch", lambda: AT.timestamp())


class Ladder:
    """A stub finops.guard_authority: answers what it is told, records how
    it was asked."""

    def __init__(self, answer=None, *, raises: type[BaseException] | None = None):
        self.answer, self.raises, self.calls = answer, raises, []
        self.module = types.ModuleType("finops.guard_authority")
        self.module.ladder_verdict = self.ladder_verdict
        self.module.doctor = self.doctor
        self.doctored = None

    def ladder_verdict(self, command, *, cwd, harness, scope, mcp=None):
        self.calls.append({"command": command, "cwd": cwd, "harness": harness,
                           "scope": scope, "mcp": mcp})
        if self.raises is not None:
            raise self.raises("the ladder broke")
        return self.answer

    def doctor(self, covered, gaps, fix):
        self.doctored = (covered, gaps, fix)
        covered.append("tag, ticket and off-hours schedule calls, at the rung authority.yaml "
                       "names")
        return {"read": True, "classes": {"tag": "approve_each"}}


def _answer(decision, *, cls="tag", rung="approve_each", within=None, reason=None):
    out = {"decision": decision, "action_type": "authority_class", "authority_class": cls,
           "rung": rung, "within_bound": within}
    if decision != "allow":
        out["reason"] = reason or (f"nable guard: this {cls} call is at {rung}; a person "
                                   "approves each (approvers: email:lead@acme.example).")
    return out


@pytest.fixture
def ladder(monkeypatch):
    def install(answer=None, **kw):
        stub = Ladder(answer, **kw)
        monkeypatch.setitem(sys.modules, "finops.guard_authority", stub.module)
        return stub
    return install


@pytest.fixture
def no_ladder(monkeypatch):
    """The module is not installed, whether or not this checkout has one."""
    monkeypatch.setitem(sys.modules, "finops.guard_authority", None)


def _records():
    p = gl.ledger_path()
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _last(**match):
    for r in reversed(_records()):
        if all(r.get(k) == v for k, v in match.items()):
            return r
    return None


# ── S1: the stricter decision answers ───────────────────────────────────────

def test_a_ladder_ask_turns_an_allow_into_an_ask(ladder, no_ladder):
    before = g.gate_command(SMALL, record=False)
    assert before is None or before["decision"] in ("allow", "warn")
    stub = ladder(_answer("ask"))
    v = g.gate_command(SMALL)
    assert v["decision"] == "ask"
    assert v["authority_class"] == "tag" and v["rung"] == "approve_each"
    assert v["within_bound"] is None
    assert "a person approves each" in v["reason"]
    assert v["estimate"]["monthly_usd"] > 0, "the guard's estimate stands"
    rec = _last(decision="ask")
    assert (rec["authority_class"], rec["rung"], rec["within_bound"]) == (
        "tag", "approve_each", None)
    assert stub.calls and stub.calls[0]["command"] == SMALL
    assert stub.calls[0]["harness"] == "claude-code" and stub.calls[0]["mcp"] is None


@pytest.mark.parametrize("cmd, decision, rule", [
    (BIG, "ask", "threshold"), (PROD, "ask", None), (BUY, "ask", None)],
    ids=["threshold", "production context", "purchase"])
def test_a_ladder_allow_never_softens_an_ask(ladder, monkeypatch, cmd, decision, rule):
    monkeypatch.setitem(sys.modules, "finops.guard_authority", None)
    want = g.gate_command(cmd, record=False)
    assert want["decision"] == decision
    ladder(_answer("allow", rung="bounded", within=True))
    v = g.gate_command(cmd)
    assert v["decision"] == want["decision"] and v.get("rule") == want.get("rule")
    if rule:
        assert v.get("rule") == rule
    assert v.get("estimate") == want.get("estimate")
    assert v.get("action_type") == want.get("action_type")
    assert v["reason"] == want["reason"], "an allow added nothing to the guard's reason"
    assert v["within_bound"] is True and v["rung"] == "bounded"
    rec = [r for r in _records() if r.get("decision") == decision][-1]
    assert rec["within_bound"] is True and rec["authority_class"] == "tag"


def test_a_ladder_allow_never_softens_a_deny(ladder, monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "none")
    monkeypatch.setitem(sys.modules, "finops.guard_authority", None)
    want = g.gate_command("terraform destroy -auto-approve", record=False)
    if want is None or want["decision"] != "deny":
        monkeypatch.setenv("FINOPS_GUARD_STRICT", "1")
        want = g.gate_command("terraform destroy -auto-approve", record=False)
    assert want["decision"] in ("deny", "ask")
    ladder(_answer("allow", rung="bounded", within=True))
    v = g.gate_command("terraform destroy -auto-approve")
    assert v["decision"] == want["decision"] and v["reason"] == want["reason"]
    assert v["rung"] == "bounded" and v["within_bound"] is True


def test_when_both_ask_the_ladders_sentence_follows_the_guards(ladder, no_ladder):
    want = g.gate_command(BIG, record=False)
    ladder(_answer("ask", reason="nable guard: the tag class is at approve_each here."))
    v = g.gate_command(BIG)
    assert v["decision"] == "ask" and v["rule"] == "threshold"
    assert v["reason"].startswith(want["reason"])
    assert v["reason"].endswith("the tag class is at approve_each here.")
    assert v["monthly_delta_usd"] == want["monthly_delta_usd"]


def test_a_ladder_deny_is_stricter_than_an_ask(ladder):
    ladder(_answer("deny", rung="off", reason="nable guard: the tag class is off here; "
                                              "a person tags it."))
    v = g.gate_command(BIG)
    assert v["decision"] == "deny" and v["rule"] == "threshold"
    assert "the tag class is off here" in v["reason"]


# ── S1: a silent call in a class is recorded ────────────────────────────────

def test_a_silent_call_in_a_class_is_recorded_as_an_allow_with_its_tool_use_id(ladder,
                                                                            monkeypatch):
    monkeypatch.setitem(sys.modules, "finops.guard_authority", None)
    n = len(_records())
    assert g.gate_command(TAG) is None and len(_records()) == n, "the guard is silent on it"
    ladder(_answer("allow", rung="bounded", within=True))
    token = g._TOOL_USE_ID.set("toolu_01AbCdEfGh")
    try:
        assert g.gate_command(TAG) is None
    finally:
        g._TOOL_USE_ID.reset(token)
    rec = _records()[-1]
    assert rec["decision"] == "allow" and rec["action_type"] == "authority_class"
    assert rec["tool_use_id"] == "toolu_01AbCdEfGh"
    assert (rec["authority_class"], rec["rung"], rec["within_bound"]) == ("tag", "bounded",
                                                                         True)
    assert gl.verify()["ok"] is True


AKIA_SHAPED = "AKIA" + "Q" * 16


def _judged(**extra):
    """What the real ladder records of a tag call it judged (its `authority`)."""
    return {"file_rung": "bounded", "entries": ["authority:action_class:tag:k1"],
            "op": "add", "tool": "aws ec2 create-tags",
            "resources": ["i-0123456789abcdef0"], "resources_n": 1, "keys": ["team"],
            "approvers": ["email:lead@acme.example"],
            "bound": {"tag_keys": ["team"], "max_resources_per_call": 20}, **extra}


def test_what_the_ladder_judged_goes_onto_the_line_for_the_box(ladder):
    """The workspace's evidence reads a class line's `authority` (its op,
    resources, tag keys, schedules, group, repo and environments) to tie a
    later removal to it as a reversal. The line keeps those, redacted, and
    not the approvers, which authority.yaml already holds."""
    ladder({**_answer("allow", rung="bounded", within=True),
            "authority": _judged(group=f"nightly {AKIA_SHAPED}")})
    assert g.gate_command(TAG) is None
    rec = _records()[-1]
    a = rec["authority"]
    assert (a["op"], a["resources"], a["keys"], a["file_rung"]) == (
        "add", ["i-0123456789abcdef0"], ["team"], "bounded")
    assert a["bound"] == {"tag_keys": ["team"], "max_resources_per_call": 20}
    assert "approvers" not in a
    assert re.search(r"(?<![\w.@-])lead@acme\.example(?![\w-])", json.dumps(rec)) is None
    assert AKIA_SHAPED not in json.dumps(rec) and a["group"].startswith("nightly ")
    assert gl.verify()["ok"] is True


def test_the_ladders_record_on_the_line_is_bounded(ladder):
    arns = [f"arn:aws:ec2:us-east-1:123456789012:instance/i-{n:017x}" for n in range(20)]
    ladder({**_answer("ask", rung="bounded", within=False),
            "authority": {**_judged(resources=arns, resources_n=20),
                          "calls": [_judged(resources=arns[:2])],
                          "unknown_field": "y" * 5000}})
    assert g.gate_command(TAG)["decision"] == "ask"
    a = _records()[-1]["authority"]
    assert a["resources"] == arns and a["calls"][0]["resources"] == arns[:2]
    assert "unknown_field" not in a and "approvers" not in a["calls"][0]
    long = [f"{arn}/{'z' * 150}" for arn in arns]
    ladder({**_answer("ask", rung="bounded", within=False),
            "authority": {"file_rung": "bounded", "outside": ["x" * 5000],
                          "calls": [_judged(resources=long, resources_n=20)] * 5}})
    assert g.gate_command(TAG)["decision"] == "ask"
    a = _records()[-1]["authority"]
    assert len(json.dumps(a)) <= g._LADDER_RECORD_MAX
    assert a == {"file_rung": "bounded", "trimmed": True}


def test_an_authority_record_out_of_shape_leaves_the_line_without_it(ladder):
    ladder({**_answer("allow", rung="bounded", within=True), "authority": ["not", "a", "map"]})
    assert g.gate_command(TAG) is None
    rec = _records()[-1]
    assert rec["authority_class"] == "tag" and "authority" not in rec


def test_a_call_in_no_class_records_nothing_new(ladder):
    stub = ladder(None)
    n = len(_records())
    assert g.gate_command("ls -la") is None and len(_records()) == n
    assert stub.calls, "the ladder was asked"


# ── S1: absent and raising ───────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [SMALL, BIG, PROD, TAG])
def test_an_absent_ladder_changes_nothing(no_ladder, cmd):
    assert g._ladder_module() is None
    v = g.gate_command(cmd, record=False)
    w = g.gate_command(cmd)
    assert v == w
    rec = _records()[-1] if _records() else {}
    assert "authority_class" not in rec and "authority_trouble" not in rec


@pytest.mark.parametrize("cmd", [BIG, PROD])
def test_a_raising_ladder_changes_no_decision_and_writes_authority_trouble(ladder,
                                                                           monkeypatch, cmd):
    monkeypatch.setitem(sys.modules, "finops.guard_authority", None)
    want = g.gate_command(cmd, record=False)
    ladder(raises=RuntimeError)
    v = g.gate_command(cmd)
    assert v["decision"] == want["decision"] and v["reason"] == want["reason"]
    rec = [r for r in _records() if r.get("decision") == want["decision"]][-1]
    assert rec["authority_trouble"] == "RuntimeError" and "authority_class" not in rec


def test_a_raising_ladder_on_a_silent_call_is_said_on_a_fail_open_line(ladder):
    ladder(raises=KeyError)
    assert g.gate_command(TAG) is None
    rec = _records()[-1]
    assert rec["decision"] == "fail_open" and rec["check"] == "authority"
    assert rec["authority_trouble"] == "KeyError"


@pytest.mark.parametrize("bad", [
    {"decision": "maybe", "action_type": "authority_class", "authority_class": "tag",
     "rung": "bounded", "within_bound": True},
    {"decision": "ask", "action_type": "authority_class", "authority_class": "tag",
     "rung": "bounded", "within_bound": True},
    {"decision": "allow", "action_type": "authority_class", "authority_class": "purchase",
     "rung": "bounded", "within_bound": True},
    {"decision": "allow", "action_type": "authority_class", "authority_class": "tag",
     "rung": "sometimes", "within_bound": True},
    "allow",
], ids=["decision", "ask without reason", "class", "rung", "not a mapping"])
def test_an_answer_out_of_shape_is_trouble_never_a_softer_verdict(ladder, monkeypatch, bad):
    monkeypatch.setitem(sys.modules, "finops.guard_authority", None)
    want = g.gate_command(BIG, record=False)
    ladder(bad)
    v = g.gate_command(BIG)
    assert v["decision"] == want["decision"] == "ask"
    assert _records()[-1]["authority_trouble"] == "ValueError"


# ── S1: the scope and the MCP form ──────────────────────────────────────────

def test_scope_gives_the_production_context_and_no_envs_without_an_org_model(ladder):
    stub = ladder(None)
    g.gate_command(PROD, cwd="/nonexistent")
    envs, prod = stub.calls[-1]["scope"]()
    assert prod == "prod" and envs in (None, ())
    g.gate_command(TAG)
    assert stub.calls[-1]["scope"]()[1] is None


def test_an_mcp_call_is_asked_with_its_tool_and_arguments(ladder):
    stub = ladder(_answer("ask", cls="ticket"))
    args = {"owner": "acme", "repo": "infra", "title": "Tag the payments instances"}
    v = g.gate_mcp_call("mcp__github__create_issue", args)
    assert v["decision"] == "ask" and v["authority_class"] == "ticket"
    call = stub.calls[-1]
    assert call["mcp"] == ("mcp__github__create_issue", args) and call["command"] == ""
    rec = _records()[-1]
    assert rec["authority_class"] == "ticket" and rec["tool"] == "mcp__github__create_issue"


def test_an_mcp_call_the_guard_translates_is_asked_with_its_shell_form(ladder):
    stub = ladder(None)
    g.gate_mcp_call("mcp__aws__run_command", {"command": TAG})
    call = stub.calls[-1]
    assert call["mcp"][0] == "mcp__aws__run_command"
    assert call["command"] in ("", TAG)


def test_an_absent_ladder_changes_no_mcp_answer(no_ladder):
    assert g._ladder_module() is None
    assert g.gate_mcp_call("mcp__github__create_issue", {"title": "x"}) is None


# ── S1: the doctor ───────────────────────────────────────────────────────────

def test_doctor_calls_the_ladders_doctor(ladder):
    stub = ladder(None)
    d = g.doctor()
    covered, gaps, fix = stub.doctored
    assert covered is d["covered"] and gaps is d["not_covered"] and callable(fix)
    assert d["authority_classes"] == {"read": True, "classes": {"tag": "approve_each"}}


def test_doctor_without_the_ladder_says_nothing_of_it(no_ladder):
    assert g._ladder_module() is None
    assert "authority_classes" not in g.doctor()


def test_a_raising_ladder_doctor_is_a_gap(ladder):
    stub = ladder(None)

    def broken(covered, gaps, fix):
        raise OSError("no")
    stub.module.doctor = broken
    d = g.doctor()
    assert d["authority_classes"] == {"read": False, "error": "OSError"}
    assert any("authority ladder" in x for x in d["not_covered"])


# ── S2: the box's standing in `nable cloud` ─────────────────────────────────

AUTHORITY_BLOCK = {"v": 1, "read": True, "workspace_ref": WS_A,
                   "classes": {"tag": {"in_force": "approve_each"}}}


class Standing:
    def __init__(self):
        self.kept: list[dict] = []
        self.module = types.ModuleType("finops.authority_standing")
        self.module.keep = self.keep
        self.module.text = self.text

    def keep(self, block, *, box, workspace_ref, fetched_at):
        self.kept.append({"block": block, "box": box, "workspace_ref": workspace_ref,
                          "fetched_at": fetched_at})
        return {"kept": True, "why": None, "file": "authority-standing.json"}

    def text(self, got, block):
        return f"Authority standing: kept ({got['file']}), {len(block['classes'])} class(es)"


class AuthorityBox(Box):
    def status(self) -> dict:
        return {**super().status(), "reads": [], "workspace_ref": WS_A,
                "accepts": ["org_model", "ledger_line", "agent_usage", "org_file_delete"],
                "authority": AUTHORITY_BLOCK}


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _parse(*argv):
    p = argparse.ArgumentParser(prog="nable")
    sub = p.add_subparsers(dest="cmd")
    cli_cloud.add_parser(sub)
    return p.parse_args(["cloud", *argv])


def _run(box, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli_cloud.run(_parse(*argv), opener=box, environ={cc.KEY_ENV: KEY}, prompt=None,
                         stdout=out, stderr=err, terminal=True, now=AT)
    return code, out.getvalue(), err.getvalue()


def test_status_keeps_and_prints_the_authority_standing(laptop, monkeypatch):
    s = Standing()
    monkeypatch.setitem(sys.modules, "finops.authority_standing", s.module)
    code, out, _err = _run(AuthorityBox(), "status", "--box", BOX)
    assert code == 0
    assert s.kept == [{"block": AUTHORITY_BLOCK, "box": BOX, "workspace_ref": WS_A,
                       "fetched_at": AT}]
    assert "Authority standing: kept (authority-standing.json), 1 class(es)" in out
    code, out, _err = _run(AuthorityBox(), "status", "--box", BOX, "--json")
    assert json.loads(out)["authority_kept"]["kept"] is True


def test_a_send_keeps_and_prints_the_authority_standing(laptop, monkeypatch):
    s = Standing()
    monkeypatch.setitem(sys.modules, "finops.authority_standing", s.module)
    write_ledger(records(2))
    code, out, _err = _run(AuthorityBox(), "connect", "--box", BOX, "--json")
    confirm = json.loads(out)["confirm_sha256"]
    code, out, _err = _run(AuthorityBox(), "connect", "--box", BOX, "--send", "--confirm",
                           confirm)
    assert code == 0, out + _err
    [kept] = s.kept
    assert kept["block"] == AUTHORITY_BLOCK and kept["box"] == BOX
    assert kept["workspace_ref"] == WS_A and kept["fetched_at"].tzinfo is not None
    assert "Authority standing: kept" in out


def test_without_the_module_nothing_is_kept_or_said(laptop, monkeypatch):
    monkeypatch.setitem(sys.modules, "finops.authority_standing", None)
    assert cc.authority_standing() is None
    code, out, _err = _run(AuthorityBox(), "status", "--box", BOX)
    assert code == 0 and "Authority standing" not in out
    code, out, _err = _run(AuthorityBox(), "status", "--box", BOX, "--json")
    assert "authority_kept" not in json.loads(out)
    write_ledger(records(2))
    result = cc.send(cc.build(agent_usage=False), BOX, KEY, opener=AuthorityBox())
    assert "authority" not in result


def test_a_raising_standing_is_said_as_not_kept(laptop, monkeypatch):
    s = Standing()

    def broken(*a, **k):
        raise ValueError("no")
    s.module.keep = broken
    monkeypatch.setitem(sys.modules, "finops.authority_standing", s.module)
    write_ledger(records(2))
    result = cc.send(cc.build(agent_usage=False), BOX, KEY, opener=AuthorityBox())
    assert result["authority"] == {"kept": False, "why": "not kept (ValueError)"}


# ── S1 with the ladder this checkout ships ───────────────────────────────────

@pytest.fixture
def real_ladder(tmp_path, monkeypatch):
    """finops.guard_authority as installed, judging the authority.yaml that
    write() puts in FINOPS_ORG_DIR, at the ladder test's fixed time."""
    from finops import guard_authority
    from tests.test_guard_authority_ladder import NOW, _Fixed
    d = tmp_path / "org"
    d.mkdir()
    monkeypatch.setenv("FINOPS_ORG_DIR", str(d))
    monkeypatch.setattr(guard_authority, "_now", lambda: NOW)
    monkeypatch.setattr(gl, "datetime", _Fixed)

    def write(*entries):
        (d / "authority.yaml").write_text("# nable org model v1\n" + "".join(entries))
    return write


def test_the_guard_records_what_the_real_ladder_judged(real_ladder):
    from tests.test_guard_authority_ladder import entry
    real_ladder(entry("tag", "bounded", tag_keys=["team"]))
    assert g.gate_command(TAG) is None
    rec = _records()[-1]
    assert (rec["decision"], rec["authority_class"], rec["rung"], rec["within_bound"]) == (
        "allow", "tag", "bounded", True)
    assert rec["authority"]["op"] == "add" and rec["authority"]["keys"] == ["team"]
    assert rec["authority"]["resources"] == ["i-0123456789abcdef0"]
    owner = TAG.replace("Key=team", "Key=owner")
    v = g.gate_command(owner)
    assert (v["decision"], v["within_bound"]) == ("ask", False)
    assert _records()[-1]["authority"]["outside"]


def test_the_real_ladder_counts_the_tickets_the_guard_recorded(real_ladder):
    from tests.test_guard_authority_ladder import TICKET, entry
    real_ladder(entry("ticket", "bounded", max_per_day=2, repos=["acme/payments"]))
    assert g.gate_command(TICKET) is None
    assert g.gate_command(TICKET) is None
    v = g.gate_command(TICKET)
    assert (v["decision"], v["authority_class"], v["within_bound"]) == ("ask", "ticket", False)
    assert [r["decision"] for r in _records() if r.get("authority_class") == "ticket"] == [
        "allow", "allow", "ask"]


def test_with_no_authority_yaml_a_ticket_tool_is_let_through_and_recorded(real_ladder,
                                                                           monkeypatch):
    """No file names the class: the call is let through in silence, as
    before, and recorded as an allow line at rung none, so `nable guard
    reverse` can name it and a reversal can reach the workspace."""
    assert g.gate_mcp_call("mcp__github__create_issue", {"title": "x"}) is None
    [rec] = _records()
    assert (rec["decision"], rec["authority_class"], rec["rung"]) == ("allow", "ticket",
                                                                     "none")
    monkeypatch.setattr(ai_budget, "status", lambda **_: {
        "verdict": ai_budget.BUDGET_OVER, "verdict_basis": "tokens", "pct_of_budget": 1.5,
        "billable_tokens_mtd": 15, "budget": {"monthly_tokens": 10}})
    v = g.gate_mcp_call("mcp__github__create_issue", {"title": "x"})
    assert (v["decision"], v["action_type"]) == ("ask", "ai_budget")
    stop, line = _records()[1:]
    assert (stop["action_type"], line["authority_class"]) == ("ai_budget", "ticket")
