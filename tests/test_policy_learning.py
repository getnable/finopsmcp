"""The learning-aware action gate (Agent 1: Budget Guard).

The static gate is the floor of caution. This layer folds in what the customer
actually approves. The safety invariant under test: learning is a one-way ratchet
toward caution. It can tighten an ALLOW to ESCALATE, but it can NEVER loosen a
BLOCK or ESCALATE into ALLOW. If learning could ever make the gate more permissive,
an agent could act more aggressively than policy allows, so these tests are the
guardrail on the moat.
"""
from __future__ import annotations

from finops.policy import (
    evaluate_action_gate, GATE_ALLOW, GATE_BLOCK, GATE_ESCALATE,
)


def _sig(verdict, act_rate=0.1, accuracy=0.9, coverage="WARM"):
    return {"verdict": verdict, "act_rate": act_rate, "accuracy": accuracy,
            "coverage": coverage, "why": "test signal"}


# ── the tighten case: learning turns allow into escalate ──────────────────────
def test_suppress_signal_tightens_allow_to_escalate():
    # rightsizing is two-way + allowlisted + a saving => static gate ALLOWs.
    base = evaluate_action_gate("rightsizing", monthly_delta_usd=-200.0, cost_verdict="ok")
    assert base["gate"] == GATE_ALLOW
    # But the customer usually declines rightsizing => escalate.
    learned = evaluate_action_gate("rightsizing", monthly_delta_usd=-200.0,
                                   cost_verdict="ok", signal=_sig("suppress"))
    assert learned["gate"] == GATE_ESCALATE
    assert learned["learned"]["adjustment"] == "allow_to_escalate"
    assert "usually decline" in learned["reason"]


# ── the confidence case: learning annotates an allow it stays ─────────────────
def test_boost_signal_annotates_but_keeps_allow():
    learned = evaluate_action_gate("rightsizing", monthly_delta_usd=-200.0,
                                   cost_verdict="ok", signal=_sig("boost", act_rate=0.95))
    assert learned["gate"] == GATE_ALLOW
    assert learned["learned"]["adjustment"] == "confidence_added"
    assert "usually approve" in learned["reason"]


# ── THE SAFETY INVARIANT: learning never loosens ──────────────────────────────
def test_learning_never_loosens_a_block():
    # An action not in the allowlist BLOCKs. Even a strong "you approve these"
    # signal must not turn that into ALLOW.
    r = evaluate_action_gate("delete_bucket", monthly_delta_usd=-50.0,
                             signal=_sig("boost", act_rate=0.99))
    assert r["gate"] == GATE_BLOCK


def test_learning_never_loosens_a_one_way_escalate():
    # One-way door escalates. A boost signal must not downgrade it to allow.
    r = evaluate_action_gate("idle_cleanup", monthly_delta_usd=-300.0,
                             cost_verdict="ok", signal=_sig("boost", act_rate=0.99))
    assert r["gate"] == GATE_ESCALATE


def test_learning_never_loosens_over_budget():
    r = evaluate_action_gate("rightsizing", monthly_delta_usd=50.0,
                             cost_verdict="over_budget", signal=_sig("boost", act_rate=0.99))
    assert r["gate"] == GATE_ESCALATE


# ── sparse/absent signal is a silent no-op ────────────────────────────────────
def test_neutral_signal_is_noop():
    with_sig = evaluate_action_gate("rightsizing", -200.0, "ok", signal=_sig("neutral"))
    without = evaluate_action_gate("rightsizing", -200.0, "ok")
    assert with_sig["gate"] == without["gate"] == GATE_ALLOW
    assert "learned" not in with_sig  # no learned block when there's no real signal


def test_no_signal_matches_legacy_behavior():
    # Backward compat: no signal arg => identical to before the learning layer.
    r = evaluate_action_gate("rightsizing", monthly_delta_usd=-200.0, cost_verdict="ok")
    assert r["gate"] == GATE_ALLOW
    assert "learned" not in r
