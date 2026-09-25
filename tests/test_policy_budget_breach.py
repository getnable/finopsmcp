"""on_budget_breach: what an over-budget change gets, set once in the policy.

Invariants under test:
  - the default is "ask": an over-budget change escalates to a human
  - `on_budget_breach: deny` in nable.policy.yaml (or FINOPS_POLICY_ON_BUDGET_BREACH)
    makes evaluate_action_gate block it, for the MCP gate and the guard alike
  - anything else in the key is ignored, never a looser gate
  - FINOPS_GUARD_STOP_ON_BUDGET overrides the policy for the guard, both ways
  - the policy file is read from FINOPS_POLICY_FILE or nable's data directory,
    never from the working directory
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import finops.guard as g
from finops import ai_budget, policy
from finops.budget import summary as bs

M5_2XL = "aws ec2 run-instances --instance-type m5.2xlarge"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_POLICY_LOOP_COUNT", "FINOPS_GUARD_TEAM", "FINOPS_GUARD_ACCOUNT",
                "FINOPS_GUARD_BUDGET_MAX_AGE_HOURS", "FINOPS_POLICY_ON_BUDGET_BREACH"):
        monkeypatch.delenv(var, raising=False)
    # No policy file unless a test writes one.
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(tmp_path / "nable.policy.yaml"))
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


def _policy_file(tmp_path, text: str) -> None:
    (tmp_path / "nable.policy.yaml").write_text(text)


def _over_budget_summary() -> None:
    today = datetime.now().astimezone().date()
    start = today.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    bs.write_summary([{"name": "Cloud total", "scope_type": "total", "scope_value": "*",
                       "period": "monthly", "period_start": start.isoformat(),
                       "period_end": end.isoformat(), "spent": 49_999.0,
                       "limit": 50_000.0}],
                     spend_through=today.isoformat(),
                     now=datetime.now(UTC) - timedelta(hours=1))


# ── the policy gate ───────────────────────────────────────────────────────────

def test_the_default_is_ask():
    assert policy.load_policy()["on_budget_breach"] == "ask"
    gate = policy.evaluate_action_gate("infra_apply", 100.0, "over_budget")
    assert gate["gate"] == policy.GATE_ESCALATE and gate["rule"] == "over_budget"


def test_the_policy_file_can_make_it_a_deny(tmp_path):
    _policy_file(tmp_path, "# team policy\non_budget_breach: deny\n")
    assert policy.load_policy()["on_budget_breach"] == "deny"
    gate = policy.evaluate_action_gate("infra_apply", 100.0, "over_budget")
    assert gate["gate"] == policy.GATE_BLOCK and gate["rule"] == "over_budget"
    assert "on_budget_breach" in gate["reason"]


def test_deny_applies_to_one_way_doors_too(tmp_path):
    _policy_file(tmp_path, "on_budget_breach: deny\n")
    gate = policy.evaluate_action_gate("purchase_commitment", 3_650.0, "over_budget")
    assert gate["gate"] == policy.GATE_BLOCK


def test_within_budget_the_key_changes_nothing(tmp_path):
    _policy_file(tmp_path, "on_budget_breach: deny\n")
    assert policy.evaluate_action_gate("infra_apply", 100.0, "ok")["gate"] == policy.GATE_ALLOW
    assert policy.evaluate_action_gate("infra_apply", 100.0)["gate"] == policy.GATE_ALLOW


def test_the_env_var_sets_it_and_wins_over_the_file(tmp_path, monkeypatch):
    _policy_file(tmp_path, "on_budget_breach: deny\n")
    monkeypatch.setenv("FINOPS_POLICY_ON_BUDGET_BREACH", "ask")
    assert policy.load_policy()["on_budget_breach"] == "ask"
    monkeypatch.setenv("FINOPS_POLICY_ON_BUDGET_BREACH", "DENY")
    _policy_file(tmp_path, "")
    assert policy.load_policy()["on_budget_breach"] == "deny"


@pytest.mark.parametrize("text", ["on_budget_breach: allow\n", "on_budget_breach: [deny]\n",
                                  "- not a mapping\n", "on_budget_breach: {\n"])
def test_a_value_that_is_not_ask_or_deny_is_ignored(tmp_path, text):
    _policy_file(tmp_path, text)
    assert policy.load_policy()["on_budget_breach"] == "ask"
    # ...but not silently: doctor says which file and why the default applies.
    [problem] = policy.policy_problems()
    assert str(tmp_path / "nable.policy.yaml") in problem and "default" in problem
    assert any(problem in r for r in g.doctor()["recommendations"])


def test_a_file_that_parses_says_nothing_to_doctor(tmp_path):
    _policy_file(tmp_path, "on_budget_breach: deny\n")
    assert policy.policy_problems() == []


def test_the_file_is_read_as_utf8(tmp_path):
    (tmp_path / "nable.policy.yaml").write_bytes("# Budgets: 5 €/mo\non_budget_breach: deny\n"
                                                 .encode())
    assert policy.load_policy()["on_budget_breach"] == "deny"
    assert policy.policy_problems() == []


@pytest.mark.parametrize("env,value", [
    ("FINOPS_POLICY_MAX_AUTO_USD", "nan"), ("FINOPS_POLICY_MAX_AUTO_USD", "inf"),
    ("FINOPS_POLICY_MAX_AUTO_USD", "-5"), ("FINOPS_POLICY_MAX_AUTO_USD", "1e999"),
    ("FINOPS_POLICY_VELOCITY_CAP_USD", "nan"), ("FINOPS_POLICY_VELOCITY_CAP_USD", "-1"),
    ("FINOPS_POLICY_VELOCITY_WINDOW_MIN", "0"), ("FINOPS_POLICY_VELOCITY_WINDOW_MIN", "inf"),
    ("FINOPS_POLICY_LOOP_WINDOW_MIN", "-10"), ("FINOPS_POLICY_LOOP_COUNT", "-3"),
])
def test_an_env_value_that_would_switch_a_gate_off_keeps_the_default(env, value, monkeypatch):
    """float("nan") compares False with everything, so a threshold of nan let
    every priced change through; inf and negatives did the same one way or
    the other."""
    monkeypatch.setenv(env, value)
    pol = policy.load_policy()
    assert pol == {**policy.DEFAULT_POLICY,
                   "allowed_action_types": list(policy.DEFAULT_POLICY["allowed_action_types"])}
    assert policy.velocity_cap(pol) == 4 * policy.DEFAULT_POLICY["max_auto_monthly_usd"]
    assert any(f"{env}={value}" in p for p in policy.policy_problems())
    v = g.gate_command("aws ec2 run-instances --instance-type p4d.24xlarge --count 8",
                       record=False)
    assert v and v["decision"] == "ask"


@pytest.mark.parametrize("env,value,key,want", [
    ("FINOPS_POLICY_MAX_AUTO_USD", "0", "max_auto_monthly_usd", 0.0),
    ("FINOPS_POLICY_VELOCITY_CAP_USD", "0", "velocity_cap_monthly_usd", 0.0),
    ("FINOPS_POLICY_LOOP_COUNT", "0", "loop_repeat_count", 0),
    ("FINOPS_POLICY_VELOCITY_WINDOW_MIN", "15", "velocity_window_minutes", 15.0),
])
def test_documented_values_still_apply(env, value, key, want, monkeypatch):
    monkeypatch.setenv(env, value)
    assert policy.load_policy()[key] == want
    assert policy.policy_problems() == []


def test_the_file_is_not_read_from_the_working_directory(tmp_path, monkeypatch):
    import sys
    monkeypatch.delenv("FINOPS_POLICY_FILE")
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    db = sys.modules.get("finops.storage.db")
    if db is not None:     # another test imported it: its data_dir() is cached
        monkeypatch.setattr(db, "_DATA_DIR", None)
    monkeypatch.chdir(tmp_path)
    _policy_file(tmp_path, "on_budget_breach: deny\n")
    assert policy.load_policy()["on_budget_breach"] == "ask"
    assert policy.policy_file_path() == tmp_path / "data" / "nable.policy.yaml"


def test_an_edited_file_is_read_again(tmp_path):
    _policy_file(tmp_path, "on_budget_breach: ask\n")
    assert policy.load_policy()["on_budget_breach"] == "ask"
    p = tmp_path / "nable.policy.yaml"
    p.write_text("on_budget_breach: deny\n")
    import os
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert policy.load_policy()["on_budget_breach"] == "deny"


# ── the guard ─────────────────────────────────────────────────────────────────

def test_the_guard_denies_an_over_budget_change_when_the_policy_says_so(tmp_path):
    _policy_file(tmp_path, "on_budget_breach: deny\n")
    _over_budget_summary()
    v = g.gate_command(M5_2XL)
    assert v["decision"] == "deny"
    assert "Stopped because your policy sets on_budget_breach: deny" in v["reason"]
    assert "'Cloud total'" in v["reason"]


def test_the_guard_env_var_downgrades_a_policy_deny(tmp_path, monkeypatch):
    _policy_file(tmp_path, "on_budget_breach: deny\n")
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "0")
    _over_budget_summary()
    assert g.gate_command(M5_2XL)["decision"] == "ask"


def test_the_guard_env_var_still_hard_stops_under_an_ask_policy(tmp_path, monkeypatch):
    _policy_file(tmp_path, "on_budget_breach: ask\n")
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    _over_budget_summary()
    assert g.gate_command(M5_2XL)["decision"] == "deny"


def test_a_policy_deny_on_a_commitment_names_the_budget(tmp_path):
    _policy_file(tmp_path, "on_budget_breach: deny\n")
    _over_budget_summary()
    v = g.gate_command("aws savingsplans create-savings-plan "
                       "--savings-plan-offering-id x --commitment 5")
    assert v["decision"] == "deny" and "'Cloud total'" in v["reason"]


def test_the_ask_says_how_to_make_it_a_hard_stop():
    _over_budget_summary()
    v = g.gate_command(M5_2XL)
    assert v["decision"] == "ask"
    assert "on_budget_breach: deny" in v["reason"]
