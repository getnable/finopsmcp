"""Demo mode must answer the headline questions, not error or return empty.

A cold trial (finops welcome --demo) hits these with no credentials. Regression
guard after optimize_ai_spend showed "No AI spend detected" and
explain_recent_cost_drivers returned "No providers connected" in demo mode.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch


def test_optimize_ai_spend_demo_shows_real_plan():
    import finops.server as srv
    with patch("finops.demo_data.DEMO_MODE", True):
        r = asyncio.run(srv.optimize_ai_spend())
    assert r.get("ai_spend_monthly_usd", 0) > 0, "demo AI spend should be non-zero"
    assert r.get("spend_shape", {}).get("primary_driver") != "none"
    assert r.get("levers"), "demo should surface optimization levers"
    titles = " ".join(l.get("title", "") for l in r["levers"]).lower()
    assert "caching" in titles, "the signature caching lever should appear"


def test_explain_cost_drivers_demo_answers():
    import finops.server as srv
    with patch("finops.demo_data.DEMO_MODE", True):
        r = asyncio.run(srv.explain_recent_cost_drivers())
    assert "error" not in r, f"demo should answer, got {r.get('error')}"
    assert r.get("net_change_pct") is not None
    assert r.get("top_increases"), "demo should list cost increases"


def test_demo_registry_has_headline_tools():
    # optimize_ai_spend is wired directly in the tool (real planner over demo
    # data), so it is covered by its own test, not the registry.
    import finops.demo_data as dd
    for tool in ("get_cost_summary", "explain_recent_cost_drivers"):
        assert tool in dd.DEMO_RESPONSES, f"{tool} missing from demo registry"
