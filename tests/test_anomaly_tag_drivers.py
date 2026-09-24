"""get_tag_drivers: which tag drove an AWS spike, on the same scale as the spike.

The fake sits where Cost Explorer sits: a boto3 module whose "ce" client answers
get_cost_and_usage by summing a per-day spend function over the requested
window, the way the real service totals a TimePeriod.
"""
from __future__ import annotations

import sys
import types
from datetime import date, timedelta

import pytest

from finops.anomaly.detector import get_tag_drivers

SPIKE_DAY = date(2026, 9, 20)


class _FakeCE:
    def __init__(self, daily: dict[str, dict[str, callable]]):
        # daily[tag_key][tag_value] = f(day) -> USD for that day
        self.daily = daily
        self.keys_queried: list[str] = []

    def get_cost_and_usage(self, TimePeriod, GroupBy, **kw):
        key = GroupBy[0]["Key"]
        self.keys_queried.append(key)
        start = date.fromisoformat(TimePeriod["Start"])
        end = date.fromisoformat(TimePeriod["End"])
        days = [start + timedelta(days=i) for i in range((end - start).days)]
        groups = [
            {"Keys": [f"{key}${val}"],
             "Metrics": {"UnblendedCost": {"Amount": str(sum(f(d) for d in days))}}}
            for val, f in self.daily.get(key, {}).items()
        ]
        return {"ResultsByTime": [{"Groups": groups}]}


@pytest.fixture
def fake_ce(monkeypatch):
    holder: dict = {}

    def install(daily):
        ce = _FakeCE(daily)
        mod = types.ModuleType("boto3")
        mod.client = lambda *a, **kw: ce
        monkeypatch.setitem(sys.modules, "boto3", mod)
        holder["ce"] = ce
        return ce

    return install


def test_pct_of_anomaly_compares_like_with_like(fake_ce):
    """team=platform went from $100/day to $1,100/day a week ago and stayed
    there. The anomaly is a $1,000/day delta and platform is all of it. Summing
    seven days of tag delta against one day of anomaly delta reported 700%."""
    week_start = SPIKE_DAY - timedelta(days=6)
    fake_ce({"team": {"platform": lambda d: 1_100.0 if d >= week_start else 100.0}})

    drivers = get_tag_drivers("Amazon EC2", SPIKE_DAY, 1_000.0, tag_keys=["team"])

    assert len(drivers) == 1
    assert drivers[0]["delta_usd"] == pytest.approx(1_000.0)
    assert drivers[0]["pct_of_anomaly"] == pytest.approx(100.0)


def test_tag_keys_are_case_sensitive(fake_ce):
    """Cost Explorer tag keys are case-sensitive: "team" and "Team" are two
    different tags, and an org that uses "Team" was never queried at all."""
    ce = fake_ce({"Team": {"data": lambda d: 500.0 if d == SPIKE_DAY else 0.0}})

    drivers = get_tag_drivers("Amazon EC2", SPIKE_DAY, 500.0, tag_keys=["team", "Team"])

    assert ce.keys_queried.count("Team") == 2  # current and baseline window
    assert [(d["tag_key"], d["tag_value"]) for d in drivers] == [("Team", "data")]
