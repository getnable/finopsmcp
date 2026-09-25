"""The sample dataset tells one story, whichever tool is asked.

Dogfood of 0.8.216 found the demo contradicting itself: AWS was $2,407,600
"month to date" in get_cost_summary and $2,002,363 summed from get_cost_trends;
the all-provider total was $4,281,177 in one answer and $5,147,600 in another;
explain_recent_cost_drivers called an AWS-only figure "all providers"; the
forecast ($5.6M) sat above a month-to-date already at $5.15M on the 23rd; and
every period argument (days, compare_days, horizon_days) was ignored.

Every figure now sums one daily curve (demo_data._svc_day), so the tools are
compared here against each other, through the same path an MCP client uses.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from finops import server


@pytest.fixture(autouse=True)
def demo(monkeypatch):
    from finops import demo_data

    monkeypatch.setattr(demo_data, "DEMO_MODE", True)
    monkeypatch.setenv("FINOPS_DEMO_FORCE", "1")


def _run(name, **kwargs):
    return asyncio.run(server.mcp._tool_manager.get_tool(name).fn(**kwargs))


def _close(a, b, tol=0.05):
    return abs(a - b) <= tol


def test_every_total_for_the_same_window_agrees():
    summary = _run("get_cost_summary")["grand_total_usd"]
    assert _close(_run("get_cost_trends")["total_usd"], summary)
    assert _close(_run("compare_providers")["grand_total_usd"], summary)
    assert _close(_run("get_total_spend_all_sources")["total_usd"], summary)
    assert _close(_run("get_cost_summary_all_accounts")["total_usd"], summary)
    assert _close(_run("slice_costs")["result"]["total"], summary)
    assert _close(_run("slice_costs", dimensions=["service"], limit=500)["result"]["total"],
                  summary)
    drivers = _run("explain_recent_cost_drivers")
    assert _close(drivers["total_current_usd"], summary)
    assert "all 9 sample providers" in drivers["summary"]


def test_aws_figures_agree_across_tools():
    aws = _run("get_cost_summary", provider="aws")["grand_total_usd"]
    trends = _run("get_cost_trends", provider="aws")
    assert _close(trends["total_usd"], aws)
    assert _close(_run("get_costs_by_team")["total_usd"], aws)
    assert _close(_run("get_tag_cost_breakdown_cur")["total_usd"], aws)
    assert _close(_run("slice_costs", dimensions=["account"])["result"]["total"], aws, 1.0)
    everything = _run("get_cost_summary")
    assert _close(everything["by_provider"]["aws"]["total_usd"], aws)


def test_period_arguments_change_the_answer():
    d30 = _run("get_cost_trends", days=30)["total_usd"]
    d7 = _run("get_cost_trends", days=7)["total_usd"]
    assert 0.15 * d30 < d7 < 0.35 * d30  # about a quarter of 30 days, not 30 days
    start = (date.today() - timedelta(days=7)).isoformat()
    assert _close(_run("get_cost_summary", start_date=start)["grand_total_usd"], d7)

    week = _run("explain_recent_cost_drivers", days=7)
    month = _run("explain_recent_cost_drivers", days=30)
    assert week["period"]["days"] == 7 and month["period"]["days"] == 30
    assert week["total_current_usd"] < month["total_current_usd"]
    assert _close(_run("explain_cost_change", compare_days=7)["total_current_usd"],
                  week["total_current_usd"])

    llm7 = _run("get_llm_costs", days=7)["total_usd"]
    llm30 = _run("get_llm_costs", days=30)["total_usd"]
    assert llm7 < llm30 / 2

    short = _run("forecast_costs", horizon_days=7)
    long = _run("forecast_costs", horizon_days=90)
    assert short["forecast_next_days_usd"] < long["forecast_next_days_usd"]


def test_month_over_month_change_is_the_same_everywhere():
    summary = _run("get_cost_summary")
    drivers = _run("explain_recent_cost_drivers")
    assert summary["vs_previous_period_pct"] == drivers["net_change_pct"]
    assert _close(summary["previous_period_total_usd"], drivers["total_previous_usd"])


def test_forecast_is_an_estimate_with_a_range_above_month_to_date():
    f = _run("forecast_costs")
    assert f["estimate"] is True
    assert "estimate" in f["note"].lower() and "estimate" in f["method"].lower()
    rng = f["projected_range"]
    assert rng["low"] <= f["projected_month_total"] <= rng["high"]
    assert rng["low"] >= f["month_to_date_usd"]  # never below what is already spent
    today = date.today()
    if today.day > 1:
        mtd = _run("get_cost_summary", start_date=today.replace(day=1).isoformat(),
                   end_date=today.isoformat())["grand_total_usd"]
        assert _close(f["month_to_date_usd"], mtd)


def test_budgets_use_the_same_month_to_date():
    from finops.demo_data import month_forecast

    rows = {b["provider"]: b for b in _run("list_budgets")["budgets"]}
    for prov in ("aws", "snowflake", "gcp"):
        f = month_forecast([prov])
        assert _close(rows[prov]["used"], f["month_to_date_usd"])
        assert _close(rows[prov]["projected_month_total"], f["projected_month_total"])


def test_unknown_provider_says_so_instead_of_answering_for_all():
    out = _run("get_cost_summary", provider="oracle")
    assert out.get("not_in_sample") is True
    assert "not in the StreamCo sample" in out["note"]


def test_fixed_window_tools_say_the_window_is_fixed():
    out = _run("get_ai_kpis", days=7)
    assert "fixed 30-day window" in out["sample_window"]
