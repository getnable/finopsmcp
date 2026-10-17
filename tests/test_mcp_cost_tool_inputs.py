"""What the cost tools do with an argument they cannot honour.

Each case is a tool call a model really makes, and the answer it used to get:

  get_cost_summary(provider="AWS")      "No cloud accounts connected yet" with AWS connected
  get_cost_summary(provider="awz")      the same, instead of "unknown provider"
  get_cost_summary(provider="azure")    the same, with AWS connected and Azure not
  get_cost_trends(provider="Datadog")   every provider's spend, unfiltered
  get_cost_summary(category="clod")     every provider's spend, unfiltered
  get_cost_summary(start_date="2026-13-01")         a raw "month must be in 1..12"
  get_cost_summary(start > end)                     sent to the provider as asked
  get_cost_summary(start_date two years back)       period.start two years back,
                                                    over a total for the last 365 days
  get_cost_trends(days=-7)              a window that starts a week in the future
  get_cost_trends(granularity="WEEKLY") passed through to Cost Explorer, which rejects it
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.mcp_support import Reads as _Reads
from tests.mcp_support import call as _call
from tests.mcp_support import connectors_fixture

_Recording = _Reads   # remembers every window it was asked for


@pytest.fixture
def connectors(monkeypatch, tmp_path):
    yield from connectors_fixture(monkeypatch, tmp_path)


_EC2 = {"Amazon Elastic Compute Cloud - Compute": 1200.0, "Amazon Simple Storage Service": 80.0}
_DD = {"Datadog Infrastructure": 900.0}


# ── provider names ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["AWS", " aws ", "Aws"])
def test_a_provider_name_in_any_case_is_that_provider(connectors, name):
    connectors(aws=_Reads("aws", _EC2), datadog=_Reads("datadog", _DD))
    out = _call("get_cost_summary", {"provider": name})

    assert "error" not in out, out
    assert set(out["by_provider"]) == {"aws"}


@pytest.mark.parametrize("tool", ["get_cost_summary", "get_costs_by_service",
                                  "get_cost_trends", "get_top_cost_drivers"])
def test_an_unknown_provider_is_named_as_unknown(connectors, tool):
    connectors(aws=_Reads("aws", _EC2))
    out = _call(tool, {"provider": "awz"})

    assert "Unknown provider 'awz'" in out.get("error", ""), out
    assert "aws" in out["known_providers"]
    assert "No cloud accounts connected" not in str(out)


def test_a_known_provider_that_is_not_connected_says_which(connectors):
    connectors(aws=_Reads("aws", _EC2))
    out = _call("get_cost_summary", {"provider": "azure"})

    assert "azure is not connected" in out["error"], out
    assert out["connected_providers"] == ["aws"]
    assert "No cloud accounts connected" not in out["error"]


def test_cost_trends_for_one_provider_is_that_provider_only(connectors):
    connectors(aws=_Reads("aws", _EC2), datadog=_Reads("datadog", _DD))
    out = _call("get_cost_trends", {"provider": "Datadog"})

    assert set(out["by_provider"]) == {"datadog"}, out["by_provider"].keys()
    assert out["grand_total_usd"] == pytest.approx(900.0)


@pytest.mark.parametrize("tool", ["get_cost_summary", "get_costs_by_service",
                                  "get_cost_trends", "compare_providers"])
def test_an_unknown_category_is_refused_not_ignored(connectors, tool):
    connectors(aws=_Reads("aws", _EC2))
    out = _call(tool, {"category": "clod"})

    assert "category" in out.get("error", ""), out
    assert "grand_total_usd" not in out


# ── dates ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", ["get_cost_summary", "get_costs_by_service",
                                  "get_total_spend_all_sources", "compare_providers"])
def test_an_impossible_date_is_a_clear_error(connectors, tool):
    connectors(aws=_Reads("aws", _EC2))
    out = _call(tool, {"start_date": "2026-13-01"})

    assert "YYYY-MM-DD" in out.get("error", ""), out


@pytest.mark.parametrize("tool", ["get_cost_summary", "get_costs_by_service",
                                  "get_total_spend_all_sources", "compare_providers"])
def test_a_start_after_the_end_is_refused_before_any_read(connectors, tool):
    aws = _Recording("aws", _EC2)
    connectors(aws=aws)
    out = _call(tool, {"start_date": "2026-10-10", "end_date": "2026-10-01"})

    assert "before" in out.get("error", ""), out
    assert aws.asked == []


@pytest.mark.parametrize("tool", ["get_cost_summary", "get_costs_by_service",
                                  "get_total_spend_all_sources", "compare_providers"])
def test_a_window_past_the_lookback_cap_says_what_it_covers(connectors, tool):
    """Asked for two years, the provider is asked for 365 days, and the period
    in the answer is the 365 days that were read, with a note saying so."""
    from finops import server

    aws = _Recording("aws", _EC2)
    connectors(aws=aws)
    end = server.date.today()
    start = end - timedelta(days=730)
    out = _call(tool, {"start_date": start.isoformat(), "end_date": end.isoformat()})

    read_from = end - timedelta(days=365)
    assert aws.asked == [(read_from, end)]
    assert out["period"]["start"] == read_from.isoformat()
    assert "365" in out["period_note"]


@pytest.mark.parametrize("days", [-7, 0, 100000])
def test_cost_trends_refuses_a_window_it_cannot_read(connectors, days):
    aws = _Recording("aws", _EC2)
    connectors(aws=aws)
    out = _call("get_cost_trends", {"days": days})

    assert "days" in out.get("error", ""), out
    assert aws.asked == []


@pytest.mark.parametrize(("tool", "args"), [
    ("get_cost_trends", {"granularity": "WEEKLY"}),
    ("get_cost_summary", {"granularity": "hourly"}),
])
def test_a_granularity_cost_explorer_does_not_take_is_refused(connectors, tool, args):
    aws = _Recording("aws", _EC2)
    connectors(aws=aws)
    out = _call(tool, args)

    assert "granularity" in out.get("error", ""), out
    assert aws.asked == []
