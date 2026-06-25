"""The check_action_policy MCP tool: composes the cost preflight with the policy gate.

Gate logic is covered in test_policy.py; here we exercise the tool's wiring.
"""
from __future__ import annotations

import asyncio

from finops import server


def test_one_way_door_escalates_and_carries_cost(monkeypatch):
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", lambda **k: [])
    r = asyncio.run(server.check_action_policy(action_type="idle_cleanup", monthly_delta_usd=-300.0))
    assert r["gate"] == "escalate"
    assert r["door"] == "one_way"
    assert "cost" in r and r["cost"]["monthly_delta_usd"] == -300.0
    assert "never executes" in r["policy_note"]


def test_reversible_saving_is_allowed(monkeypatch):
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", lambda **k: [])
    r = asyncio.run(server.check_action_policy(action_type="rightsizing", monthly_delta_usd=-120.0))
    assert r["gate"] == "allow"
    assert r["cost"]["monthly_delta_usd"] == -120.0


def test_works_without_a_cost_input():
    # No change described -> pure policy gate, no cost preflight, no DB.
    r = asyncio.run(server.check_action_policy(action_type="rightsizing"))
    assert r["gate"] == "allow"
    assert "cost" not in r


def test_unknown_action_is_blocked():
    r = asyncio.run(server.check_action_policy(action_type="nuke_everything"))
    assert r["gate"] == "block"


def test_over_budget_change_escalates_even_for_a_reversible_action(monkeypatch):
    monkeypatch.setattr(
        "finops.budget.enforcer.list_budgets",
        lambda **k: [{"name": "prod", "limit_usd": 1000, "alert_at_pct": 80}])
    monkeypatch.setattr(
        "finops.budget.enforcer.check_budget",
        lambda b, conn=None: {"name": "prod", "limit": 1000.0, "run_rate_monthly": 950.0})
    # rightsizing that ADDS $200/mo: 950 + 200 = 1150 over the 1000 limit -> escalate
    r = asyncio.run(server.check_action_policy(action_type="rightsizing", monthly_delta_usd=200.0))
    assert r["gate"] == "escalate"
    assert r["cost"]["verdict"] == "over_budget"
