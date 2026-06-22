"""The efficiency router: cheapest model that fits the task, clamped by budget."""
from __future__ import annotations

from finops.slack_bot.llm import route_request


def test_simple_lookup_uses_the_cheapest_tier():
    d = route_request("what's my spend this month?")
    assert d.tier == "simple"          # Haiku, not Sonnet by default
    assert not d.blocked


def test_analytical_question_escalates_to_chat():
    d = route_request("compare my AWS and GCP spend and forecast next month")
    assert d.tier == "chat"


def test_recommendation_agent_escalates_to_chat():
    d = route_request("look for savings", agent="reco")
    assert d.tier == "chat"


def test_rca_trigger_escalates_to_the_top_tier():
    assert route_request("why did my bill spike last week?").tier == "rca"


def test_rca_agent_escalates_to_the_top_tier():
    assert route_request("take a look", agent="rca").tier == "rca"


def test_cheaper_tiers_get_tighter_tool_budgets():
    cheap = route_request("list my accounts")
    deep = route_request("why did costs change?")
    assert cheap.tier == "simple" and deep.tier == "rca"
    assert cheap.max_tool_calls < deep.max_tool_calls


def test_budget_exhausted_blocks():
    d = route_request("why did costs change?", budget_remaining=0.0, budget_total=50.0)
    assert d.blocked is True


def test_low_budget_downgrades_rca_to_chat():
    d = route_request("why did costs spike?", budget_remaining=5.0, budget_total=50.0)  # 10%
    assert d.tier == "chat"
    assert "downgraded" in d.reason


def test_very_low_budget_downgrades_to_simple():
    d = route_request("compare spend across clouds", budget_remaining=1.0, budget_total=50.0)  # 2%
    assert d.tier == "simple"


def test_no_managed_budget_never_degrades_or_blocks():
    d = route_request("why did costs spike?", budget_remaining=None)
    assert d.tier == "rca" and not d.blocked
