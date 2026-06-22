"""Tests for the Anthropic Cost API path (actual billed USD, not estimated)."""
from __future__ import annotations

from datetime import date

import httpx

from finops.connectors.saas import anthropic_usage as a


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def test_cost_report_converts_cents_to_dollars(monkeypatch):
    # amount is in the lowest currency unit (cents): "12345.67" -> $123.4567
    payload = {
        "data": [
            {
                "starting_at": "2026-06-01T00:00:00Z",
                "ending_at": "2026-06-02T00:00:00Z",
                "results": [
                    {"amount": "12345.67", "currency": "USD",
                     "model": "claude-opus-4-6", "cost_type": "tokens"},
                    {"amount": "5000", "currency": "USD",
                     "model": "claude-haiku-4-5", "cost_type": "tokens"},
                ],
            },
            {
                "starting_at": "2026-06-02T00:00:00Z",
                "ending_at": "2026-06-03T00:00:00Z",
                "results": [
                    {"amount": "100.00", "model": "claude-opus-4-6"},
                    {"amount": "0", "model": "skipme"},        # zero -> skipped
                    {"amount": None, "model": "skipme2"},      # null -> skipped
                    {"amount": "250", "cost_type": "web_search"},  # model null -> labelled by cost_type
                ],
            },
        ],
        "has_more": False,
        "next_page": None,
    }
    monkeypatch.setattr(httpx, "get", lambda *a_, **k_: _FakeResp(payload))

    out = a.get_cost_report("sk-ant-admin-x", date(2026, 6, 1), date(2026, 6, 2))

    assert out["source"] == "cost_api"
    # (12345.67 + 5000 + 100 + 250) cents / 100
    assert out["total_usd"] == round((12345.67 + 5000 + 100 + 250) / 100, 4)
    assert out["by_model"]["claude-opus-4-6"] == round((12345.67 + 100) / 100, 4)
    assert out["by_model"]["web_search"] == round(250 / 100, 4)  # cost_type fallback label
    assert "skipme" not in out["by_model"]
    assert len(out["daily"]) == 2
    assert out["daily"][0]["date"] == "2026-06-01"


def test_cost_report_uses_exclusive_end_and_admin_auth(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _FakeResp({"data": [], "has_more": False})

    monkeypatch.setattr(httpx, "get", fake_get)
    a.get_cost_report("sk-ant-admin-x", date(2026, 6, 1), date(2026, 6, 30))

    assert captured["url"].endswith("/v1/organizations/cost_report")
    assert captured["params"]["starting_at"] == "2026-06-01T00:00:00Z"
    # ending_at is exclusive, so end_date + 1 day to include the full final day
    assert captured["params"]["ending_at"] == "2026-07-01T00:00:00Z"
    assert captured["params"]["bucket_width"] == "1d"
    assert captured["headers"]["x-api-key"] == "sk-ant-admin-x"


def test_cost_report_paginates(monkeypatch):
    pages = [
        {"data": [{"starting_at": "2026-06-01T00:00:00Z",
                   "results": [{"amount": "1000", "model": "m"}]}],
         "has_more": True, "next_page": "CURSOR2"},
        {"data": [{"starting_at": "2026-06-02T00:00:00Z",
                   "results": [{"amount": "2000", "model": "m"}]}],
         "has_more": False, "next_page": None},
    ]
    calls = {"n": 0, "pages_seen": []}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["pages_seen"].append(params.get("page"))
        resp = _FakeResp(pages[calls["n"]])
        calls["n"] += 1
        return resp

    monkeypatch.setattr(httpx, "get", fake_get)
    out = a.get_cost_report("sk-ant-admin-x", date(2026, 6, 1), date(2026, 6, 2))

    assert calls["n"] == 2
    assert calls["pages_seen"] == [None, "CURSOR2"]  # second call carries the cursor
    assert out["total_usd"] == round(3000 / 100, 4)


def test_cost_report_error_falls_back_to_empty(monkeypatch):
    def boom(*a_, **k_):
        raise httpx.HTTPError("403 forbidden")

    monkeypatch.setattr(httpx, "get", boom)
    out = a.get_cost_report("sk-ant-admin-x", date(2026, 6, 1), date(2026, 6, 2))

    # not "cost_api", so get_costs() falls through to the usage/estimate path
    assert out["source"] != "cost_api"
    assert out["total_usd"] == 0.0


def test_get_costs_prefers_cost_api_when_admin_key_present(monkeypatch):
    env = {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-x"}
    monkeypatch.setattr("finops.security.env.get_env",
                        lambda k, default=None: env.get(k, default))
    monkeypatch.setattr(a, "get_cost_report", lambda k, s, e: {
        "source": "cost_api", "total_usd": 42.0,
        "by_model": {"claude-opus-4-6": 42.0}, "by_model_tokens": {}, "daily": []})

    out = a.get_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert out["source"] == "cost_api"
    assert out["total_usd"] == 42.0
