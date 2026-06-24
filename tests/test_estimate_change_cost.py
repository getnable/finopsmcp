"""The estimate_change_cost MCP tool: input routing + budget fetch + verdict assembly.

The pure verdict logic is covered in test_preflight.py; here we exercise the tool's
orchestration with the estimators and the budget enforcer monkeypatched.
"""
from __future__ import annotations

import asyncio

from finops import server


def test_requires_an_input():
    r = asyncio.run(server.estimate_change_cost())
    assert "error" in r


def test_manual_delta_with_no_budget_configured(monkeypatch):
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", lambda **k: [])
    r = asyncio.run(server.estimate_change_cost(monthly_delta_usd=2140.0))
    assert r["change_kind"] == "manual"
    assert r["verdict"] == "no_budget"
    assert r["monthly_delta_usd"] == 2140.0
    assert r["annual_delta_usd"] == 25680.0


def test_manual_delta_over_budget(monkeypatch):
    monkeypatch.setattr(
        "finops.budget.enforcer.list_budgets",
        lambda **k: [{"name": "prod", "limit_usd": 10000, "alert_at_pct": 80}])
    monkeypatch.setattr(
        "finops.budget.enforcer.check_budget",
        lambda b, conn=None: {"name": "prod", "limit": 10000.0, "run_rate_monthly": 9000.0})
    r = asyncio.run(server.estimate_change_cost(monthly_delta_usd=2000.0))
    assert r["verdict"] == "over_budget"          # 9000 + 2000 = 11000 of 10000
    assert r["budget"]["headroom_usd"] == -1000.0
    assert r["summary"] == r["reason"]


def test_budget_can_be_selected_by_name(monkeypatch):
    budgets = [
        {"name": "dev", "limit_usd": 1000, "alert_at_pct": 80},
        {"name": "prod", "limit_usd": 50000, "alert_at_pct": 80},
    ]
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", lambda **k: budgets)
    monkeypatch.setattr(
        "finops.budget.enforcer.check_budget",
        lambda b, conn=None: {"name": b["name"], "limit": float(b["limit_usd"]), "run_rate_monthly": 100.0})
    r = asyncio.run(server.estimate_change_cost(monthly_delta_usd=500.0, budget_name="prod"))
    assert r["budget"]["name"] == "prod"
    assert r["verdict"] == "ok"                    # 100 + 500 of 50000 is tiny


def test_terraform_path_routes_through_the_estimator(monkeypatch):
    monkeypatch.setattr(
        "finops.connectors.terraform_estimate.estimate_plan",
        lambda data: {"monthly_delta_usd": 500.0,
                      "lines": [{"address": "aws_instance.x", "monthly_delta": 500.0}]})
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", lambda **k: [])
    r = asyncio.run(server.estimate_change_cost(terraform_plan_json='{"resource_changes": []}'))
    assert r["change_kind"] == "terraform"
    assert r["monthly_delta_usd"] == 500.0
    assert r["breakdown"] and r["breakdown"][0]["address"] == "aws_instance.x"


def test_a_budget_db_failure_degrades_to_no_budget_not_an_error(monkeypatch):
    def _boom(**k):
        raise RuntimeError("no database configured")
    monkeypatch.setattr("finops.budget.enforcer.list_budgets", _boom)
    r = asyncio.run(server.estimate_change_cost(monthly_delta_usd=100.0))
    assert r["verdict"] == "no_budget"             # never surfaces the DB error
    assert "error" not in r
