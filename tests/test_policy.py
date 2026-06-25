"""Policy-bounded action gate (B1, advisory). The auto-execute half (B2) is not built."""
from __future__ import annotations

from finops.policy import (
    evaluate_action_gate, load_policy, door_of, is_one_way,
    GATE_ALLOW, GATE_BLOCK, GATE_ESCALATE, DEFAULT_POLICY,
)


# ── classification ────────────────────────────────────────────────────────────
def test_door_classification():
    assert door_of("rightsizing") == "two_way"
    assert door_of("idle_cleanup") == "one_way"
    assert door_of("something_unknown") == "unknown"
    assert is_one_way("purchase_commitment") is True
    assert is_one_way("rightsizing") is False


# ── one-way doors always escalate ─────────────────────────────────────────────
def test_one_way_door_escalates_even_for_a_saving():
    r = evaluate_action_gate("idle_cleanup", monthly_delta_usd=-300.0, cost_verdict="ok")
    assert r["gate"] == GATE_ESCALATE
    assert r["door"] == "one_way"
    assert "human" in r["reason"].lower()


def test_purchase_commitment_escalates():
    assert evaluate_action_gate("purchase_commitment", -1000.0)["gate"] == GATE_ESCALATE


# ── allow: reversible, allowlisted, in budget ─────────────────────────────────
def test_reversible_allowlisted_saving_is_allowed():
    r = evaluate_action_gate("rightsizing", monthly_delta_usd=-120.0, cost_verdict="ok")
    assert r["gate"] == GATE_ALLOW
    assert r["door"] == "two_way"


def test_a_ticket_is_allowed():
    assert evaluate_action_gate("ticket", 0.0, "no_budget")["gate"] == GATE_ALLOW


# ── block: not in the allowlist ───────────────────────────────────────────────
def test_unknown_action_type_is_blocked():
    r = evaluate_action_gate("rm_rf_prod", monthly_delta_usd=0.0)
    assert r["gate"] == GATE_BLOCK


def test_a_two_way_action_removed_from_allowlist_is_blocked():
    pol = {"allowed_action_types": ["rightsizing"], "max_auto_monthly_usd": 500.0,
           "escalate_one_way_doors": True}
    # spot_migration is two-way but not in this narrowed allowlist
    assert evaluate_action_gate("spot_migration", -50.0, policy=pol)["gate"] == GATE_BLOCK
    assert evaluate_action_gate("rightsizing", -50.0, policy=pol)["gate"] == GATE_ALLOW


# ── escalate: over budget / over the cost threshold ───────────────────────────
def test_over_budget_verdict_escalates():
    r = evaluate_action_gate("rightsizing", monthly_delta_usd=50.0, cost_verdict="over_budget")
    assert r["gate"] == GATE_ESCALATE


def test_cost_increase_over_threshold_escalates():
    # +$800/mo with the default $500 cap
    assert evaluate_action_gate("rightsizing", 800.0, "ok")["gate"] == GATE_ESCALATE
    # but a saving of the same size is fine
    assert evaluate_action_gate("rightsizing", -800.0, "ok")["gate"] == GATE_ALLOW


# ── policy loading + env overrides ────────────────────────────────────────────
def test_load_policy_defaults(monkeypatch):
    monkeypatch.delenv("FINOPS_POLICY_MAX_AUTO_USD", raising=False)
    monkeypatch.delenv("FINOPS_POLICY_ALLOWED_ACTIONS", raising=False)
    pol = load_policy()
    assert pol["max_auto_monthly_usd"] == DEFAULT_POLICY["max_auto_monthly_usd"]
    assert "rightsizing" in pol["allowed_action_types"]


def test_env_overrides_threshold_and_allowlist(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_MAX_AUTO_USD", "50")
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "rightsizing, tag_fix")
    pol = load_policy()
    assert pol["max_auto_monthly_usd"] == 50.0
    assert pol["allowed_action_types"] == ["rightsizing", "tag_fix"]
    # spot_migration now blocked under the narrowed env allowlist
    assert evaluate_action_gate("spot_migration", 0.0, policy=pol)["gate"] == GATE_BLOCK
    # a +$80 change now over the $50 env threshold escalates
    assert evaluate_action_gate("rightsizing", 80.0, "ok", policy=pol)["gate"] == GATE_ESCALATE
