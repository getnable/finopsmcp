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


def test_active_services_percentages_sum_to_100():
    # The full inventory's shares sum to 100; top_services is the top-8 subset.
    d = demo_data.dashboard_data(days=30, provider="all")
    total_pct = sum(s["pct"] for s in d["active_services"])
    assert 99.0 <= total_pct <= 101.0
    # AWS-only: 7 services, all fit in top_services, so those sum to ~100 too.
    a = demo_data.dashboard_data(days=30, provider="aws")
    assert 99.0 <= sum(s["pct"] for s in a["top_services"]) <= 101.0


def test_dashboard_provider_and_range_vary():
    # Provider filter and range window both move the numbers (real controls).
    aws = demo_data.dashboard_data(days=30, provider="aws")
    allc = demo_data.dashboard_data(days=30, provider="all")
    assert allc["total_spend_mtd"] > aws["total_spend_mtd"] > 0
    conn = set(demo_data.dashboard_data()["connected_providers"])
    assert {"aws", "azure", "gcp", "openai", "anthropic"} <= conn  # cloud + AI, richly wired
    assert len(conn) >= 10
    wide = demo_data.dashboard_data(days=90, provider="aws")
    narrow = demo_data.dashboard_data(days=7, provider="aws")
    assert wide["active_services"][0]["amount"] > narrow["active_services"][0]["amount"]


def test_fetch_dashboard_data_uses_demo_when_demo_on(_demo_on):
    d = asyncio.run(_fetch_dashboard_data(days=30, provider="all"))
    assert d["total_spend_mtd"] > 0
    assert d["error"] is None
    assert d["connected_providers"]  # demo advertises a connected provider


def test_serve_demo_flag_wires_everything(monkeypatch):
    # `finops serve --demo` must set demo mode (forced, so a machine with real
    # creds still shows the sample bill), disable auth, and open the browser.
    import os
    from finops import server_web, setup_wizard

    captured: dict = {}
    monkeypatch.setattr(server_web, "run_server", lambda **kw: captured.update(kw))

    _KEYS = ("FINOPS_DEMO_MODE", "FINOPS_DEMO_FORCE", "FINOPS_DASHBOARD_PASSWORD")
    prior_env = {k: os.environ.get(k) for k in _KEYS}
    prior_demo = demo_data.DEMO_MODE
    # main() calls set_connectors, which fills the module-global shared-connector
    # dict in place; snapshot it or the leak breaks later server_web tests.
    prior_connectors = dict(server_web._SHARED_CONNECTORS)
    for k in _KEYS:
        os.environ.pop(k, None)
    try:
        setup_wizard.main(["serve", "--demo"])
        assert os.environ["FINOPS_DEMO_MODE"] == "1"
        assert os.environ["FINOPS_DEMO_FORCE"] == "1"
        assert os.environ["FINOPS_DASHBOARD_PASSWORD"] == "off"
        assert demo_data.DEMO_MODE is True
        assert captured["open_browser"] is True
    finally:
        for k, v in prior_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        demo_data.DEMO_MODE = prior_demo
        server_web._SHARED_CONNECTORS.clear()
        server_web._SHARED_CONNECTORS.update(prior_connectors)


def test_serve_without_demo_does_not_touch_demo_env(monkeypatch):
    import os
    from finops import server_web, setup_wizard

    monkeypatch.setattr(server_web, "run_server", lambda **kw: None)
    monkeypatch.delenv("FINOPS_DEMO_FORCE", raising=False)
    prior_connectors = dict(server_web._SHARED_CONNECTORS)
    try:
        setup_wizard.main(["serve"])
        assert "FINOPS_DEMO_FORCE" not in os.environ
    finally:
        server_web._SHARED_CONNECTORS.clear()
        server_web._SHARED_CONNECTORS.update(prior_connectors)


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
