"""The `finops serve` dashboard must render a populated sample bill in demo mode
so `FINOPS_DEMO_MODE=1 finops serve` (and the one-command Docker demo) shows a
real-looking dashboard with no account connected. Locks:

  - demo mode returns a fully populated payload (non-zero spend, opportunities,
    a multi-point trend, a scored scorecard), not the empty "connect an account"
    state;
  - the payload matches the exact shape the dashboard front-end reads;
  - demo yields to real data: with a provider connected, is_demo() is false and
    the demo branch does not fire.
"""
from __future__ import annotations

import asyncio

import pytest

from finops import demo_data
from finops.server_web import _fetch_dashboard_data


# The keys the dashboard front-end reads out of /api/data. If the demo payload
# drops one, a card renders blank; pin the whole contract.
_REQUIRED_KEYS = {
    "generated_at", "account_id", "total_spend_mtd", "total_spend_last_month",
    "projected_month_total", "delta_pct", "finops_grade", "finops_score",
    "top_services", "opportunities_count", "opportunities_total_saving",
    "savings_achieved_mtd", "anomalies_open", "budget_pct_used",
    "recent_opportunities", "recent_savings", "error", "connected_providers",
    "trend", "scorecard",
}


@pytest.fixture
def _demo_on(monkeypatch):
    monkeypatch.setattr(demo_data, "is_demo", lambda: True)
    yield


def test_demo_dashboard_payload_shape():
    d = demo_data.dashboard_data(days=30)
    missing = _REQUIRED_KEYS - set(d)
    assert not missing, f"demo dashboard payload missing keys: {sorted(missing)}"


def test_demo_dashboard_is_populated():
    d = demo_data.dashboard_data(days=30)
    assert d["total_spend_mtd"] > 0
    assert d["opportunities_count"] == len(d["recent_opportunities"]) > 0
    # savings total is the sum of the opportunity line items, not a stray constant
    assert d["opportunities_total_saving"] == pytest.approx(
        sum(o["monthly_saving"] for o in d["recent_opportunities"]), abs=0.01
    )
    assert len(d["trend"]) >= 3
    assert d["scorecard"]["dimensions"], "scorecard has no dimensions"
    assert d["error"] is None


def test_top_services_percentages_sum_to_100():
    d = demo_data.dashboard_data(days=30)
    total_pct = sum(s["pct"] for s in d["top_services"])
    # top 8 of 7 demo services => all of them, so ~100%
    assert 99.0 <= total_pct <= 101.0


def test_fetch_dashboard_data_uses_demo_when_demo_on(_demo_on):
    d = asyncio.run(_fetch_dashboard_data(days=30, provider="all"))
    assert d["total_spend_mtd"] > 0
    assert d["error"] is None
    assert d["connected_providers"]  # demo advertises a connected provider


def test_fetch_dashboard_data_not_demo_does_not_serve_demo(monkeypatch):
    # Demo off => never the canned demo payload. On a machine with no provider
    # it's the empty "connect an account" state; on one with real creds it's real
    # numbers. Either way it must not be the acme-production demo bill.
    monkeypatch.setattr(demo_data, "is_demo", lambda: False)
    d = asyncio.run(_fetch_dashboard_data(days=30, provider="all"))
    demo = demo_data.dashboard_data(days=30)
    assert not (
        d["total_spend_mtd"] == demo["total_spend_mtd"]
        and d["account_id"] == demo["account_id"]
    ), "demo payload leaked into a non-demo dashboard fetch"
