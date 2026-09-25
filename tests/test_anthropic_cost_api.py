"""Tests for the Anthropic Cost API path (actual billed USD, not estimated)."""
from __future__ import annotations

from datetime import date

import httpx

from finops.analytics import ai_kpis
from finops.connectors.saas import anthropic_usage as a

# Minimal valid llm_costs_result for full_kpi_report (carries no tokens itself).
_MIN_LLM = {"by_model": {}, "daily": [], "total_usd": 0.0,
            "by_provider": {}, "by_model_tokens": {}}


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
    # get_costs also reads by_workspace from the Cost API; keep this test offline.
    monkeypatch.setattr(a, "_fetch_by_workspace", lambda *a_: {})
    monkeypatch.setattr(a, "_fetch_usage_report", lambda *a_: {})

    out = a.get_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert out["source"] == "cost_api"
    assert out["total_usd"] == 42.0


def test_cost_report_falls_back_when_pagination_cap_is_hit(monkeypatch):
    # API that always reports more pages must NOT yield a truncated cost_api total;
    # it falls back (source != cost_api) so get_costs uses the estimate instead.
    forever = {"data": [{"starting_at": "2026-06-01T00:00:00Z",
                         "results": [{"amount": "1000", "model": "m"}]}],
               "has_more": True, "next_page": "NEXT"}
    monkeypatch.setattr(httpx, "get", lambda *a_, **k_: _FakeResp(forever))
    out = a.get_cost_report("sk-ant-admin-x", date(2020, 1, 1), date(2026, 6, 1))
    assert out["source"] != "cost_api"          # truncation is never authoritative
    assert out["total_usd"] == 0.0


def test_cost_report_undated_bucket_keeps_total_and_daily_consistent(monkeypatch):
    # A bucket with no starting_at is skipped entirely, so sum(daily) == total_usd.
    payload = {
        "data": [
            {"starting_at": "2026-06-01T00:00:00Z",
             "results": [{"amount": "1000", "model": "m"}]},
            {"starting_at": "",  # undated: must not inflate total beyond daily
             "results": [{"amount": "5000", "model": "m"}]},
        ],
        "has_more": False,
    }
    monkeypatch.setattr(httpx, "get", lambda *a_, **k_: _FakeResp(payload))
    out = a.get_cost_report("sk-ant-admin-x", date(2026, 6, 1), date(2026, 6, 2))
    assert out["total_usd"] == round(sum(d["total_usd"] for d in out["daily"]), 4)
    assert out["total_usd"] == 10.0             # only the dated bucket counts


def test_kpi_tokenless_cost_api_gives_honest_note_not_fabricated_F():
    # Regression: a dollars-only Cost API result (by_model_tokens={}) must not be
    # graded "F / no cache hits"; it should fall to the honest "no data" note.
    cost_api = {"total_usd": 100.0, "by_model": {"claude-opus-4-6": 100.0},
                "by_model_tokens": {}, "daily": [], "source": "cost_api"}
    report = ai_kpis.full_kpi_report(_MIN_LLM, anthropic_data=cost_api)
    chr_ = report["cache_hit_rate"]
    assert "note" in chr_
    assert chr_.get("grade") != "F"


def test_kpi_uses_cache_path_when_tokens_present():
    anth = {"total_usd": 100.0, "by_model": {"claude-opus-4-6": 100.0},
            "by_model_tokens": {"claude-opus-4-6": {
                "input_tokens": 1000, "output_tokens": 500,
                "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0,
                "request_count": 10}},
            "daily": [], "source": "api"}
    report = ai_kpis.full_kpi_report(_MIN_LLM, anthropic_data=anth)
    assert "grade" in report["cache_hit_rate"]   # real cache analysis ran


def _routed(cost=None, usage=None, fail=None):
    """A fake httpx.get answering the cost report, the usage report and the
    workspace list by path; `fail` maps a path to the exception to raise."""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, dict(params or {})))
        for path, exc in (fail or {}).items():
            if path in url:
                raise exc
        if "cost_report" in url:
            return _FakeResp(cost or {"data": [], "has_more": False})
        if "usage_report/messages" in url:
            return _FakeResp(usage or {"data": [], "has_more": False})
        return _FakeResp({"data": [], "has_more": False})
    return fake_get, calls


def _admin_env(monkeypatch, env=None):
    env = {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-x"} if env is None else env
    monkeypatch.setattr("finops.security.env.get_env",
                        lambda k, default=None: env.get(k, default))


_USAGE = {"data": [{
    "starting_at": "2026-06-01T00:00:00Z", "ending_at": "2026-06-02T00:00:00Z",
    "results": [
        {"model": "claude-sonnet-4-6", "uncached_input_tokens": 1_000_000,
         "output_tokens": 100_000, "cache_read_input_tokens": 2_000_000,
         "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000,
                            "ephemeral_1h_input_tokens": 500_000},
         "server_tool_use": {"web_search_requests": 0}},
        {"model": "claude-3-opus-20240229", "uncached_input_tokens": 10,
         "output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation": {}},
    ]}], "has_more": False}


def test_get_costs_reads_tokens_from_the_messages_usage_report(monkeypatch):
    """The token enrichment used a nonexistent /v1/organizations/{org}/usage
    endpoint behind ANTHROPIC_ORGANIZATION_ID. It is the usage report,
    grouped by model, and the Admin key alone names the organization."""
    _admin_env(monkeypatch)
    cost = {"data": [{"starting_at": "2026-06-01T00:00:00Z",
                      "results": [{"amount": "1000", "model": "claude-sonnet-4-6"}]}],
            "has_more": False}
    fake_get, calls = _routed(cost=cost, usage=_USAGE)
    monkeypatch.setattr(httpx, "get", fake_get)

    out = a.get_costs(date(2026, 6, 1), date(2026, 6, 1))
    assert out["source"] == "cost_api" and out["total_usd"] == 10.0
    tok = out["by_model_tokens"]["claude-sonnet-4-6"]
    assert (tok["input_tokens"], tok["output_tokens"], tok["cache_read_input_tokens"],
            tok["cache_creation_input_tokens"]) == (1_000_000, 100_000, 2_000_000, 1_500_000)
    usage_calls = [p for u, p in calls if u.endswith("/v1/organizations/usage_report/messages")]
    assert usage_calls and usage_calls[0]["group_by[]"] == ["model"]
    assert usage_calls[0]["ending_at"] == "2026-06-02T00:00:00Z"
    assert not [u for u, _ in calls if "/v1/usage" in u or "/organizations/org" in u]


def test_without_the_cost_api_the_usage_report_is_an_estimate(monkeypatch):
    """Priced per model at list price by _usage_row_cost and labelled
    estimated: it is never billed dollars."""
    _admin_env(monkeypatch)
    fake_get, _ = _routed(usage=_USAGE, fail={"cost_report": httpx.HTTPError("down")})
    monkeypatch.setattr(httpx, "get", fake_get)

    out = a.get_costs(date(2026, 6, 1), date(2026, 6, 1))
    assert out["source"] == "estimated"
    row = _USAGE["data"][0]["results"][0]
    assert out["total_usd"] == round(a._usage_row_cost(row), 4)
    # $3 in + $1.50 out + 2M reads at $0.30 + 1M 5m writes at $3.75 + 0.5M 1h at $6
    assert out["total_usd"] == round(3 + 1.5 + 0.6 + 3.75 + 3.0, 4)
    assert out["daily"] == [{"date": "2026-06-01", "total_usd": out["total_usd"],
                             "by_model": {"claude-sonnet-4-6": out["total_usd"]}}]
    assert "claude-3-opus-20240229" in out["unpriced_models"]


def test_a_standard_key_alone_is_unread_not_zero(monkeypatch):
    _admin_env(monkeypatch, {"ANTHROPIC_API_KEY": "sk-ant-api-x"})

    def no_call(*a_, **k_):
        raise AssertionError("a standard key cannot read either report")

    monkeypatch.setattr(httpx, "get", no_call)
    out = a.get_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert (out["source"], out["reason"]) == ("none", "admin_key_required")


def test_a_refused_admin_key_is_an_error_not_a_zero(monkeypatch):
    _admin_env(monkeypatch)
    request = httpx.Request("GET", "https://api.anthropic.com/x")
    refused = httpx.HTTPStatusError("401", request=request,
                                    response=httpx.Response(401, request=request))
    fake_get, _ = _routed(fail={"cost_report": refused, "usage_report": refused})
    monkeypatch.setattr(httpx, "get", fake_get)
    out = a.get_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert (out["source"], out["reason"]) == ("error", "credential_invalid")
    assert out["total_usd"] == 0.0


def test_workspaces_that_share_a_name_are_not_overwritten(monkeypatch):
    monkeypatch.setattr(a, "get_cost_attribution", lambda *a_, **k_: {"groups": [
        {"group": "prod", "id": "wrkspc_1", "cost_usd": 5.0},
        {"group": "prod", "id": "wrkspc_2", "cost_usd": 3.0},
        {"group": "dev", "id": "wrkspc_3", "cost_usd": 1.0},
    ]})
    out = a._fetch_by_workspace("sk-ant-admin-x", date(2026, 6, 1), date(2026, 6, 2))
    assert out == {"prod (wrkspc_1)": 5.0, "prod (wrkspc_2)": 3.0, "dev": 1.0}
    assert sum(out.values()) == 9.0


def test_get_costs_cost_api_no_org_id_then_kpi_is_honest(monkeypatch):
    # GAP 4 end to end: an admin key and a usage report with no rows yields a
    # token-less cost_api result, which must produce an honest KPI, not an F.
    _admin_env(monkeypatch)
    payload = {"data": [{"starting_at": "2026-06-01T00:00:00Z",
                         "results": [{"amount": "10000", "model": "claude-opus-4-6"}]}],
               "has_more": False}
    fake_get, _ = _routed(cost=payload)
    monkeypatch.setattr(httpx, "get", fake_get)

    out = a.get_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert out["source"] == "cost_api"
    assert out["by_model_tokens"] == {}

    report = ai_kpis.full_kpi_report(_MIN_LLM, anthropic_data=out)
    assert "note" in report["cache_hit_rate"]
    assert report["cache_hit_rate"].get("grade") != "F"
