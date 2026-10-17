"""get_view's pre-built views, held to the same rules as the cost tools.

Reproduced through the MCP dispatch on a flat bill (the same spend every day):

  get_view("wow")          "+16.7%": this week was 7 days, last week 6
  get_view("mom")          a provider whose read failed shown as "$0.00",
                           "-100.0%"
  get_view("by_service")   every provider failed -> "services": [] and no error
  get_view("top_spenders") the same
  get_view("dod")          "Daily granularity not available" with AWS
                           connected: the view read an attribute no connector
                           sets, after paying for a Cost Explorer call it
                           threw away
  list_views()             "mom" described as "the last two full months"
                           while it compares month to date with last month
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.mcp_support import Denied, Reads, connectors_fixture
from tests.mcp_support import call as _call

_EC2 = {"Amazon Elastic Compute Cloud - Compute": 1200.0}


class _PerDay(Reads):
    """A flat bill: `per_day` dollars for every day of any window, with the
    AWS connector's daily series. The end of a window is exclusive, as in
    Cost Explorer."""

    def __init__(self, per_day: float = 40.0):
        super().__init__("aws", {})
        self.per_day = per_day

    async def get_costs(self, start, end, granularity="MONTHLY", **kw):
        self.asked.append((start, end))
        from tests.mcp_support import summary
        return summary("aws", start, end,
                       {"Amazon Elastic Compute Cloud - Compute":
                        self.per_day * max(0, (end - start).days)})

    async def get_daily_totals(self, start, end):
        days = (end - start).days
        return [{"date": (start + timedelta(days=i)).isoformat(), "total": self.per_day}
                for i in range(days)]


@pytest.fixture
def connectors(monkeypatch, tmp_path):
    yield from connectors_fixture(monkeypatch, tmp_path)


def test_week_over_week_on_a_flat_bill_is_flat(connectors):
    connectors(aws=_PerDay())
    out = _call("get_view", {"view": "wow"})

    assert out["change_pct"] == "+0.0%", out
    assert out["this_week"]["total"] == out["last_week"]["total"]


@pytest.mark.parametrize("view", ["mom", "wow", "by_service", "top_spenders"])
def test_a_view_over_a_failed_read_is_no_cost_data_not_zero(connectors, view):
    connectors(aws=Denied("aws"))
    out = _call("get_view", {"view": view})

    assert out.get("error") == "no_cost_data", out
    assert "$0.00" not in str(out)
    assert "not a finding of zero spend" in out["note"]


def test_a_view_with_one_failed_provider_is_partial(connectors):
    connectors(aws=Reads("aws", _EC2), datadog=Denied("datadog"))
    out = _call("get_view", {"view": "mom"})

    assert out.get("partial") is True, out
    assert "datadog" in out["failed_providers"]
    assert all(r["provider"] != "datadog" or "could not be read" in r["this_month"]
               for r in out["by_provider"])


@pytest.mark.parametrize("view", ["dod", "daily_trend"])
def test_the_daily_views_return_days(connectors, view):
    aws = _PerDay()
    connectors(aws=aws)
    out = _call("get_view", {"view": view, "days": 5})

    rows = out["daily_spend"]
    assert isinstance(rows, list) and len(rows) == 5, out
    assert rows[0]["spend"] == "$40.00"
    assert aws.asked == [], "the daily views paid for a summary they never used"


def test_the_month_over_month_view_says_what_it_compares(connectors):
    views = {v["id"]: v["description"] for v in _call("list_views")["views"]}
    assert "full months" not in views["mom"]
    assert "month to date" in views["mom"].lower()

    connectors(aws=_PerDay())
    out = _call("get_view", {"view": "mom"})
    if "note" not in out:          # not the 1st: a month-to-date comparison
        assert "month to date" in out["comparison"].lower()
