"""Free tier = read-only, talk to your bill. The agent team is Pro.

These tests pin the tier boundary for the three agents:
  - Budget Guard: check_action_policy + the guard hook
  - Savings Analyst actions: generate_terraform_tag_fixes (PR tools already gated)
  - The Ledger: mark_recommendation_acted_on / verify_savings / get_recommendation_learning

A free user gets one compact upgrade payload (error=pro_required with the activate
path), never a crash and never the feature. A pro user passes straight through.
"""
from __future__ import annotations

import asyncio

import pytest

from finops import server
from finops.license import LicenseStatus


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def free(monkeypatch):
    monkeypatch.delenv("FINOPS_DEMO_MODE", raising=False)
    monkeypatch.setattr(
        "finops.license.get_status",
        lambda: LicenseStatus(mode="free", email="", issued="", message=""),
    )


@pytest.fixture
def pro(monkeypatch):
    monkeypatch.delenv("FINOPS_DEMO_MODE", raising=False)
    monkeypatch.setattr(
        "finops.license.get_status",
        lambda: LicenseStatus(mode="pro", email="dev@acme.com", issued="2026-07-01", message=""),
    )


def _assert_upgrade_payload(r, feature):
    assert r["error"] == "pro_required"
    assert r["feature"] == feature
    assert r["upgrade_url"]
    assert r["activate_command"]
    # The pitch names the agent team, not just a generic "upgrade".
    assert "Budget Guard" in r["message"]


# ── Budget Guard ───────────────────────────────────────────────────────────────

def test_free_check_action_policy_returns_upgrade(free):
    r = _run(server.check_action_policy(action_type="rightsizing", monthly_delta_usd=-100.0))
    _assert_upgrade_payload(r, "agent_gate")


def test_pro_check_action_policy_passes(pro, monkeypatch):
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", lambda **k: [])
    r = _run(server.check_action_policy(action_type="rightsizing", monthly_delta_usd=-100.0))
    assert "gate" in r and r.get("error") is None


# ── The Ledger (learning loop) ─────────────────────────────────────────────────

def test_free_mark_acted_on_returns_upgrade(free):
    _assert_upgrade_payload(_run(server.mark_recommendation_acted_on(1)), "agent_learning")


def test_free_verify_savings_returns_upgrade(free):
    _assert_upgrade_payload(_run(server.verify_savings()), "agent_learning")


def test_free_learning_returns_upgrade(free):
    _assert_upgrade_payload(_run(server.get_recommendation_learning()), "agent_learning")


# ── Savings Analyst drafting ───────────────────────────────────────────────────

def test_free_generate_tag_fixes_returns_upgrade(free):
    _assert_upgrade_payload(_run(server.generate_terraform_tag_fixes(tf_dir="/tmp")), "remediation")


# ── Free stays useful: read-only talk-to-your-bill is NOT gated ────────────────

def test_free_can_still_read_the_ledger(free):
    # get_savings_summary is read-only: free users can see what nable found.
    r = _run(server.get_savings_summary())
    assert r.get("error") != "pro_required"


def test_free_estimate_change_cost_stays_free(free, monkeypatch):
    # The preflight estimate is the on-ramp: "what would this change cost" is
    # talking to your bill, so it stays free.
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", lambda **k: [])
    r = _run(server.estimate_change_cost(monthly_delta_usd=42.0))
    assert r.get("error") != "pro_required"


# ── the agent-team surface ─────────────────────────────────────────────────────

def test_agent_team_free_shows_unlock_path(free):
    r = _run(server.get_agent_team())
    assert r["plan"] == "free"
    assert len(r["agents"]) == 3
    for a in r["agents"]:
        assert a["status"] == "pro_required"
        assert any("activate_pro" in s or "login" in s for s in a["setup"])
    assert "Propose-only" in r["note"]


def test_agent_team_pro_reports_setup_state(pro, monkeypatch):
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", lambda **k: [])
    r = _run(server.get_agent_team())
    names = [a["agent"] for a in r["agents"]]
    assert names == ["Budget Guard", "Savings Analyst", "the Ledger"]
    guard = r["agents"][0]
    # No hook + no budget on this box -> needs_setup with concrete steps.
    assert guard["status"] in ("needs_setup", "active")
    if guard["status"] == "needs_setup":
        assert any("guard install" in s or "budget" in s for s in guard["setup"])
    ledger = r["agents"][2]
    assert ledger["learning"] is not None and "state" in ledger["learning"]
