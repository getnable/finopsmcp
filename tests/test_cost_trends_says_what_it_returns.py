"""get_cost_trends describes the answer it gives.

Its description said "Cost trends over time broken down by day or month" and
offered "Show daily cloud costs for the last 2 weeks" as an example. Called
that way, with AWS connected, it returned one total per provider and service
for the whole window and no per-day or per-month figure at all. The
granularity argument only changed how Cost Explorer was asked (DAILY pages
more rows for the same totals).

A series is not cheap to add here: the GCP connector's BigQuery query sums
the whole window with no date dimension, the SaaS connectors return period
totals, and the tool reads through _gather_costs, which keeps totals only.
The per-day and per-month series already exist in slice_costs with the "date"
dimension, so the description now says what this tool returns and points
there.
"""
from __future__ import annotations

import pytest

from tests.mcp_support import Reads, connectors_fixture
from tests.mcp_support import call as _call

_SERIES_KEYS = ("daily_series", "monthly_series", "series", "by_day", "by_month")


@pytest.fixture
def connectors(monkeypatch, tmp_path):
    yield from connectors_fixture(monkeypatch, tmp_path)


def _description() -> str:
    from finops import server
    return next(t.description for t in server.mcp._tool_manager.list_tools()
                if t.name == "get_cost_trends")


@pytest.mark.parametrize("granularity", ["DAILY", "MONTHLY"])
def test_the_description_promises_only_what_the_answer_has(connectors, granularity):
    connectors(aws=Reads("aws", {"Amazon Elastic Compute Cloud - Compute": 1200.0}))
    out = _call("get_cost_trends", {"days": 14, "granularity": granularity})
    desc = _description().lower()

    assert out["grand_total_usd"] == pytest.approx(1200.0)
    has_series = any(k in out for k in _SERIES_KEYS)
    if not has_series:
        assert "broken down by day or month" not in desc, desc
        assert "show daily cloud costs" not in desc, desc
        assert "total" in desc and "slice_costs" in desc, desc
        assert "slice_costs" in out["note"] and '"date"' in out["note"], out["note"]
