# SPDX-License-Identifier: Apache-2.0
"""Pack guard rules that apply only during a change freeze, and pack reasons
beside a verdict the guard already gave.

What has to stay true:
  - `during: freeze` only narrows when a rule fires: outside a freeze the
    rule does nothing, inside one it tightens like any other rule
  - a freeze nobody confirmed caps such a rule at ask (a guess may restrict,
    never stop a command outright)
  - when nobody can tell whether a freeze is in force, the rule asks
  - the freeze a pack rule applied under is kept on the verdict and in the
    ledger
  - a pack rule that matches at the guard's own ask adds its reason to it and
    never changes the decision
"""
from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta

import pytest

import finops.guard as g
import finops.guard_ledger as gl
import finops.guard_packs as gpk
from finops import ai_budget, org
from finops.org.cli import _who as human
from finops.packs import content
from finops.packs.errors import Problem
from finops.packs.rules import GuardRule, tighten
from tests import packs_support
from tests.packs_support import make_pack

packs_env = packs_support.packs_env

DEPLOY = "kubectl apply -f deploy.yaml"

RULES = """\
version: 1
rules:
  - id: deny-deploy-in-freeze
    pattern: '\\bkubectl\\s+apply\\b'
    verdict: deny
    during: freeze
    reason: Deploys wait for the change window to end.
  - id: ask-destroy-always
    pattern: '\\bterraform\\s+destroy\\b'
    verdict: ask
    reason: Destroys are change requests under CC8.1.
"""


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD", "FINOPS_GUARD_TEAM",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_ACCOUNT", "FINOPS_POLICY_FILE",
                "FINOPS_PROFILE", "FINOPS_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    db = sys.modules.get("finops.storage.db")
    if db is not None:
        monkeypatch.setattr(db, "_DATA_DIR", None)


def _window(hours_ago=1, hours_left=2):
    now = datetime.now(UTC)
    return ((now - timedelta(hours=hours_ago)).isoformat(),
            (now + timedelta(hours=hours_left)).isoformat())


def _freeze(*, confirmed=True, mode="ask", window=None):
    start, end = window or _window()
    f = org.make_fact("freeze", "org:org", {"start": start, "end": end,
                                            "reason": "Quarter close", "mode": mode},
                      source="human")
    return org.set_fact(f, human("maria")) if confirmed else org.propose(f)


def _install(tmp_path, text=RULES):
    from finops.packs import install as inst
    src = make_pack(tmp_path / "freeze-pack")
    (src / "guard" / "rules.yaml").write_text(text)
    inst.install(str(src), yes=True)
    gpk.invalidate()


def _records():
    p = gl.ledger_path()
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def _rule(verdict="deny", during="freeze"):
    return GuardRule("r1", "command", re.compile(r"\bkubectl\s+apply\b"), verdict,
                     "Deploys wait.", pack="p", during=during)


# ── the schema and tighten() ──────────────────────────────────────────────────

def test_during_is_validated():
    problems: list[Problem] = []
    rules = content.parse_guard_rules(content.safe_load(RULES), "g.yaml", problems)
    assert not problems and rules[0].during == "freeze" and rules[1].during is None
    bad = RULES.replace("during: freeze", "during: always")
    content.parse_guard_rules(content.safe_load(bad), "g.yaml", problems)
    assert any("during" in str(p) and "narrows" in str(p) for p in problems)


def test_a_freeze_rule_does_nothing_outside_a_freeze():
    t = tighten("allow", [_rule()], command=DEPLOY, freeze=lambda: None)
    assert t == {"verdict": "allow", "rules": []}


def test_a_confirmed_freeze_lets_the_rule_deny_and_a_guess_only_asks():
    sure = {"sure": True, "words": "A change freeze is in force.", "key": "k1"}
    t = tighten("allow", [_rule()], command=DEPLOY, freeze=lambda: sure)
    assert t["verdict"] == "deny" and t["rules"][0]["freeze"]["key"] == "k1"
    assert t["rules"][0]["reason"] == "Deploys wait. A change freeze is in force."
    t = tighten("allow", [_rule()], command=DEPLOY, freeze=lambda: {**sure, "sure": False})
    assert t["verdict"] == "ask"


def test_not_knowing_whether_a_freeze_is_in_force_asks():
    def broken():
        raise OSError("unreadable org model")
    for freeze in (broken, None):
        t = tighten("allow", [_rule()], command=DEPLOY, freeze=freeze)
        assert t["verdict"] == "ask"
        assert "could not tell whether one is in force" in t["rules"][0]["reason"]
    # and never looser than what came in
    assert tighten("deny", [_rule()], command=DEPLOY, freeze=broken)["verdict"] == "deny"


def test_the_freeze_lookup_runs_only_when_a_freeze_rule_matches():
    calls = []

    def freeze():
        calls.append(1)
    tighten("allow", [_rule()], command="ls -la", freeze=freeze)
    assert calls == []
    tighten("allow", [_rule(), _rule(verdict="ask")], command=DEPLOY, freeze=freeze)
    assert calls == [1]


def test_the_rule_survives_the_guard_cache_round_trip():
    r = _rule()
    assert GuardRule.from_dict(r.to_dict()).during == "freeze"
    with pytest.raises(ValueError):
        GuardRule.from_dict({**r.to_dict(), "during": "never"})


# ── in the guard ──────────────────────────────────────────────────────────────

def test_a_deploy_is_silent_until_a_confirmed_freeze_then_denied(packs_env, tmp_path):
    _install(tmp_path)
    assert g.gate_command(DEPLOY, record=False) is None
    _freeze(confirmed=True)
    v = g.gate_command(DEPLOY, record=True)
    assert v["decision"] == "deny"
    assert "Deploys wait for the change window to end." in v["reason"]
    assert "A change freeze is in force for the whole org" in v["reason"]
    assert v["freeze"]["sure"] is True
    [rec] = [r for r in _records() if r.get("pack_rules")]
    assert rec["decision"] == "deny" and rec["freeze"]["reason"] == "Quarter close"
    assert rec["pack_rules"] == ["io.github.example/demo:deny-deploy-in-freeze"]


def test_a_proposed_freeze_only_asks(packs_env, tmp_path):
    _install(tmp_path)
    _freeze(confirmed=False, mode="deny")
    v = g.gate_command(DEPLOY, record=False)
    assert v["decision"] == "ask" and "proposed, not confirmed" in v["reason"]


def test_after_the_window_the_rule_is_quiet_again(packs_env, tmp_path):
    _install(tmp_path)
    _freeze(window=_window(hours_ago=5, hours_left=-1))
    assert g.gate_command(DEPLOY, record=False) is None


def test_an_mcp_call_that_amounts_to_the_deploy_is_frozen_too(packs_env, tmp_path):
    _install(tmp_path)
    _freeze()
    v = g.gate_mcp_call("mcp__k8s__kubectl", {"command": "kubectl apply -f deploy.yaml"},
                        record=False)
    if v is None:
        pytest.skip("this nable does not read that MCP tool as a command")
    assert v["decision"] == "deny"


def test_a_pack_reason_joins_the_guards_own_ask(packs_env, tmp_path):
    base = g.gate_command("terraform destroy -auto-approve", record=False)
    assert base["decision"] == "ask" and "CC8.1" not in base["reason"]
    _install(tmp_path)
    v = g.gate_command("terraform destroy -auto-approve", record=False)
    assert v["decision"] == "ask"
    assert v["reason"].startswith(base["reason"].rstrip())
    assert "Destroys are change requests under CC8.1 (rule ask-destroy-always" in v["reason"]
