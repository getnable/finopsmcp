"""The window a cost answer names is the window that was read, on every provider.

What each provider reads, from the code in this repo:

  AWS   Cost Explorer's TimePeriod End is exclusive. The repo says so where it
        builds requests (connectors/aws.py get_daily_totals, the dashboard,
        the anomaly detector, commitments, cli_scan, slice/spec.py) and the
        view tests model it (tests/test_mcp_views_say_what_was_read.py,
        _PerDay). get_costs(start, end) reads [start, end).
  GCP   connectors/gcp.py filtered DATE(usage_start_time) <= @end_date, so
        get_costs(start, end) read [start, end]: one more day than AWS for
        the same request.

Every caller in the package builds its windows for [start, end): the daily
snapshot asks get_costs(yesterday, today) and stores the answer as
yesterday's; the wow view's weeks are "each ending where the next begins";
the mom view counts month to date as (today - 1st).days days;
_cost_window promises "every provider reads the same window". On GCP each
of those read a day more: the snapshot stored yesterday plus today, the
two weeks shared a day, month to date was a day longer than its count.
GCP now reads [start, end) like AWS.

The month views asked for (1st, last day) and said "2026-07-01 to
2026-07-31" and "all of last month (31 days)", so on AWS every month they
named was read without its last day, and a flat bill showed a pace change
on every day of the month. They now ask for (1st, 1st of the next month).
Their labels, and the wow and daily views', name the last day read.
"""
from __future__ import annotations

import asyncio
import re
import sys
import types
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from finops import cache
from finops import server as _srv
from finops.tools import meta
from tests.mcp_support import Reads

PER_DAY = 40.0


class _PerDay(Reads):
    """A flat bill read the way Cost Explorer reads it: PER_DAY for each day
    of [start, end)."""

    def __init__(self):
        super().__init__("aws", {})

    async def get_costs(self, start, end, granularity="MONTHLY", **kw):
        self.asked.append((start, end))
        from tests.mcp_support import summary
        return summary("aws", start, end,
                       {"Amazon Elastic Compute Cloud - Compute": PER_DAY * max(0, (end - start).days)})

    async def get_daily_totals(self, start, end):
        return [{"date": (start + timedelta(days=i)).isoformat(), "total": PER_DAY}
                for i in range((end - start).days)]


@pytest.fixture
def flat_bill(monkeypatch):
    cache.clear()
    aws = _PerDay()

    async def _active(subset=None):
        return {"aws": aws}

    monkeypatch.setattr(_srv, "_active", _active)

    def pin(day: date):
        monkeypatch.setattr(_srv, "date", SimpleNamespace(today=lambda: day))
    yield aws, pin
    cache.clear()


def _view(name, **kw):
    return asyncio.run(meta.get_view(name, **kw))


# ── GCP reads what Cost Explorer reads ───────────────────────────────────────

def _fake_bigquery(monkeypatch, per_day: dict[date, float]):
    """google.cloud.bigquery whose query applies the SQL's own predicates on
    DATE(usage_start_time) to one row per day."""
    asked = {}

    class _Job:
        def __init__(self, rows):
            self._rows = rows

        def result(self):
            return self._rows

    class _Client:
        def query(self, sql, job_config=None):
            params = {n: v for n, _t, v in job_config}
            asked["sql"] = sql
            preds = re.findall(r"DATE\(usage_start_time\)\s*(>=|<=|>|<)\s*@(\w+)", sql)
            assert preds, sql

            def keep(d):
                for op, name in preds:
                    v = date.fromisoformat(params[name])
                    if not {">=": d >= v, "<=": d <= v, ">": d > v, "<": d < v}[op]:
                        return False
                return True
            total = sum(c for d, c in per_day.items() if keep(d))
            return _Job([{"service": "Compute Engine", "region": "us-central1",
                          "total_cost": total, "currency": "USD"}])

    bq = types.SimpleNamespace(
        Client=lambda *a, **k: _Client(),
        QueryJobConfig=lambda query_parameters=None: list(query_parameters or []),
        ScalarQueryParameter=lambda name, typ, val: (name, typ, val))
    gc = types.ModuleType("google.cloud")
    gc.bigquery = bq
    monkeypatch.setitem(sys.modules, "google.cloud", gc)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bq)
    return asked


def test_gcp_reads_the_same_days_as_cost_explorer_for_the_same_request(monkeypatch, tmp_path):
    from finops.connectors.gcp import GCPConnector

    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cache, "_DISK_DISABLED", True)
    monkeypatch.setenv("GCP_BQ_BILLING_TABLE", "proj.billing.export_v1")
    cache.clear()
    days = {date(2026, 10, 1) + timedelta(days=i): 1.0 for i in range(10)}
    _fake_bigquery(monkeypatch, days)
    gcp = GCPConnector()
    gcp._billing_account_ids = ["01ABCD-EF2345-678901"]

    # The daily snapshot's request: yesterday's spend, stored as yesterday's.
    one_day = asyncio.run(gcp.get_costs(date(2026, 10, 5), date(2026, 10, 6)))
    week = asyncio.run(gcp.get_costs(date(2026, 10, 1), date(2026, 10, 8)))
    cache.clear()

    assert one_day.total_usd == 1.0, "a one-day request read two days"
    assert week.total_usd == 7.0, "[Oct 1, Oct 8) is the 7 days Cost Explorer reads"


# ── month over month ─────────────────────────────────────────────────────────

def test_mom_mid_month_reads_all_of_last_month_and_says_which_days(flat_bill):
    _aws, pin = flat_bill
    pin(date(2026, 8, 14))
    out = _view("mom")

    assert out["last_month"]["total"] == _srv._fmt_usd(31 * PER_DAY), out
    assert out["this_month"]["total"] == _srv._fmt_usd(13 * PER_DAY), out
    assert out["daily_pace_change"] == "+0.0%", "a flat bill is flat"
    assert out["this_month"]["period"] == "2026-08-01 to 2026-08-13"
    assert out["last_month"]["period"] == "2026-07-01 to 2026-07-31"
    assert "(13 days)" in out["comparison"] and "(31 days)" in out["comparison"]


def test_mom_on_the_first_reads_both_closed_months_whole(flat_bill):
    _aws, pin = flat_bill
    pin(date(2026, 8, 1))
    out = _view("mom")

    assert out["this_month"]["total"] == _srv._fmt_usd(31 * PER_DAY), out
    assert out["last_month"]["total"] == _srv._fmt_usd(30 * PER_DAY), out
    assert out["this_month"]["period"] == "2026-07-01 to 2026-07-31"
    assert out["last_month"]["period"] == "2026-06-01 to 2026-06-30"


def test_by_service_on_the_first_is_the_whole_closed_month(flat_bill):
    _aws, pin = flat_bill
    pin(date(2026, 8, 1))
    out = _view("by_service")

    assert out["services"][0]["this_month"] == _srv._fmt_usd(31 * PER_DAY), out
    assert out["services"][0]["last_month"] == _srv._fmt_usd(30 * PER_DAY), out
    assert out["period"] == "2026-07-01 to 2026-07-31"


# ── the week and the days ────────────────────────────────────────────────────

def test_wow_names_two_seven_day_weeks_that_do_not_share_a_day(flat_bill):
    _aws, pin = flat_bill
    pin(date(2026, 8, 14))
    out = _view("wow")

    assert out["this_week"]["total"] == out["last_week"]["total"] == _srv._fmt_usd(7 * PER_DAY)
    assert out["this_week"]["period"] == "2026-08-07 to 2026-08-13"
    assert out["last_week"]["period"] == "2026-07-31 to 2026-08-06"


@pytest.mark.parametrize("view", ["dod", "daily_trend"])
def test_the_daily_views_name_the_days_they_list(flat_bill, view):
    _aws, pin = flat_bill
    pin(date(2026, 8, 14))
    out = _view(view, days=5)

    dates = [r["date"] for r in out["daily_spend"]]
    assert dates == ["2026-08-09", "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"]
    assert out["period"] == f"{dates[0]} to {dates[-1]}"


# ── a window that reads nothing ──────────────────────────────────────────────

@pytest.fixture
def connectors(monkeypatch, tmp_path):
    from tests.mcp_support import connectors_fixture
    yield from connectors_fixture(monkeypatch, tmp_path)


@pytest.mark.parametrize("tool", ["get_cost_summary", "get_total_spend_all_sources"])
def test_an_empty_window_is_refused_before_anything_is_read(connectors, tool):
    """_cost_window accepted start_date == end_date ("must be on or before"),
    and the AWS connector sent Cost Explorer Start == End, which it rejects:
    the answer was no_cost_data. With GCP reading [start, end) as well, the
    same request would read no rows and say so as if the bill were empty."""
    from tests.mcp_support import call

    aws = _PerDay()
    connectors(aws=aws)
    out = call(tool, {"start_date": "2026-10-05", "end_date": "2026-10-05"})

    assert "error" in out and "exclusive" in out["error"], out
    assert "2026-10-06" in out["error"], "say what to pass for that one day"
    assert aws.asked == []
