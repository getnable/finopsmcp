# SPDX-License-Identifier: Apache-2.0
"""AI cost by project, workspace, API key and user from the billing providers.

OpenAI and Anthropic both split org spend by more than model, and nable threw
that away: openai_usage asked the costs endpoint to group by "model", a field
that endpoint does not accept, and anthropic_usage looked for workspace spend
at endpoints the Admin API does not have. These pin the attribution paths
against fake responses shaped from the providers' own published schemas:

  OpenAI    openai/openai-openapi, openapi.yaml: /organization/costs group_by
            enum [project_id, line_item, api_key_id]; CostsResult
            {amount{value,currency}, line_item, project_id, api_key_id};
            /organization/usage/completions group_by [project_id, user_id,
            api_key_id, model, batch, service_tier]; UsageCompletionsResult
            {input_tokens, input_cached_tokens, output_tokens, model, ...};
            /organization/projects, /organization/projects/{id}/api_keys and
            /organization/users (cursor lists: data, has_more, last_id, after).
  Anthropic docs.claude.com Admin API: GET /v1/organizations/cost_report
            group_by [description, workspace_id]; GET
            /v1/organizations/usage_report/messages group_by [account_id,
            api_key_id, context_window, inference_geo, model,
            service_account_id, service_tier, speed, workspace_id]; GET
            /v1/organizations/workspaces, /api_keys, /users (after_id cursor).

No network: httpx.get is replaced for every test.
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from finops.connectors.saas import anthropic_usage as ant
from finops.connectors.saas import openai_usage as oai

D0, D1 = date(2026, 9, 1), date(2026, 9, 30)


def _env(mapping):
    return lambda k, d="": mapping.get(k, d)


class _Resp:
    def __init__(self, payload=None, status=200, url="https://example.invalid"):
        self._payload = payload if payload is not None else {}
        self.status_code = status
        self._url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("GET", self._url)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=req,
                response=httpx.Response(self.status_code, request=req))

    def json(self):
        return self._payload


class _Router:
    """Answer httpx.get by URL suffix and remember every call's params."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params=None, headers=None, timeout=None, **kw):
        self.calls.append((url, dict(params or {})))
        # Longest suffix first, so /projects/{id}/api_keys beats /projects.
        for suffix in sorted(self.routes, key=len, reverse=True):
            if url.endswith(suffix):
                ans = self.routes[suffix]
                return ans(url, params or {}) if callable(ans) else ans
        raise AssertionError(f"unexpected URL in this test: {url}")

    def params_for(self, suffix):
        return [p for u, p in self.calls if u.endswith(suffix)]


# ── OpenAI ────────────────────────────────────────────────────────────────

def _cost_bucket(ts, *results):
    return {"object": "bucket", "start_time": ts, "end_time": ts + 86400,
            "results": [{"object": "organization.costs.result", **r} for r in results]}


def _usd(v):
    return {"value": v, "currency": "usd"}


OAI_KEY = {"OPENAI_ADMIN_KEY": "sk-admin-fake"}  # pragma: allowlist secret


def test_get_costs_groups_by_fields_the_costs_endpoint_accepts(monkeypatch):
    """group_by=model is not in the costs endpoint's enum; the default must be
    project_id + line_item, and by_model comes from the line item."""
    monkeypatch.setattr("finops.security.env.get_env", _env(OAI_KEY))
    costs = {"object": "page", "has_more": False, "next_page": None, "data": [
        _cost_bucket(1756684800,
                     {"amount": _usd(7.5), "line_item": "gpt-4o-2024-08-06, input",
                      "project_id": "proj_a"},
                     {"amount": _usd(2.5), "line_item": "gpt-4o-2024-08-06, output",
                      "project_id": "proj_b"},
                     {"amount": _usd(1.0), "line_item": "web search tool calls",
                      "project_id": None}),
    ]}
    r = _Router({
        "/organization/costs": _Resp(costs),
        "/organization/projects": _Resp({"data": [
            {"id": "proj_a", "name": "Search"}, {"id": "proj_b", "name": "Chat"}],
            "has_more": False}),
        "/organization/usage/completions": _Resp({"data": [], "has_more": False}),
    })
    monkeypatch.setattr(httpx, "get", r)

    out = oai.get_costs(D0, D1)

    assert sorted(r.params_for("/organization/costs")[0]["group_by"]) == ["line_item", "project_id"]
    assert out["source"] == "api"
    assert out["total_usd"] == 11.0
    assert out["by_model"]["gpt-4o-2024-08-06"] == 10.0
    assert out["by_model"]["web search tool calls"] == 1.0
    assert out["by_project_named"] == {"Search": 7.5, "Chat": 2.5, "default": 1.0}


def test_openai_project_attribution_reads_billed_costs_with_names(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(OAI_KEY))
    costs = {"data": [
        _cost_bucket(1756684800, {"amount": _usd(4.0), "project_id": "proj_a"},
                     {"amount": _usd(1.0), "project_id": None}),
        _cost_bucket(1756771200, {"amount": _usd(6.0), "project_id": "proj_a"},
                     {"amount": _usd(3.0), "project_id": "proj_old"}),
    ], "has_more": False}

    def projects(url, params):
        # Two pages, and archived projects asked for: spend can sit on one.
        assert params.get("include_archived") is True
        if params.get("after") == "proj_a":
            return _Resp({"data": [{"id": "proj_old", "name": "Legacy bot",
                                    "status": "archived"}],
                          "has_more": False, "last_id": "proj_old"})
        return _Resp({"data": [{"id": "proj_a", "name": "Support agent"}],
                      "has_more": True, "last_id": "proj_a"})

    r = _Router({"/organization/costs": _Resp(costs), "/organization/projects": projects})
    monkeypatch.setattr(httpx, "get", r)

    out = oai.get_cost_attribution("project", D0, D1)

    assert r.params_for("/organization/costs")[0]["group_by"] == ["project_id"]
    assert out["source"] == "cost_api"
    assert out["total_usd"] == 14.0
    assert out["groups"][0] == {"group": "Support agent", "id": "proj_a", "cost_usd": 10.0}
    labels = {g["group"]: g["cost_usd"] for g in out["groups"]}
    assert labels == {"Support agent": 10.0, "Legacy bot": 3.0, "(no project)": 1.0}


def test_openai_api_key_attribution_names_keys_from_each_project(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(OAI_KEY))
    costs = {"data": [_cost_bucket(1756684800,
                                   {"amount": _usd(8.0), "api_key_id": "key_1"},
                                   {"amount": _usd(2.0), "api_key_id": "key_2"})],
             "has_more": False}
    r = _Router({
        "/organization/costs": _Resp(costs),
        "/organization/projects": _Resp({"data": [{"id": "proj_a", "name": "A"}],
                                         "has_more": False}),
        "/organization/projects/proj_a/api_keys": _Resp({"data": [
            {"object": "organization.project.api_key", "id": "key_1",
             "name": "prod-backend", "redacted_value": "sk-abc...def"}],
            "has_more": False}),
    })
    monkeypatch.setattr(httpx, "get", r)

    out = oai.get_cost_attribution("api_key", D0, D1)

    assert r.params_for("/organization/costs")[0]["group_by"] == ["api_key_id"]
    assert out["source"] == "cost_api"
    assert {g["group"]: g["cost_usd"] for g in out["groups"]} == {"prod-backend": 8.0, "key_2": 2.0}


def test_openai_an_unreadable_amount_is_counted_not_zeroed(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(OAI_KEY))
    costs = {"data": [_cost_bucket(1756684800,
                                   {"amount": _usd(4.0), "project_id": "proj_a"},
                                   {"amount": {"value": "n/a", "currency": "usd"},
                                    "project_id": "proj_b"},
                                   {"amount": None, "project_id": "proj_c"})],
             "has_more": False}
    monkeypatch.setattr(httpx, "get", _Router({
        "/organization/costs": _Resp(costs),
        "/organization/projects": _Resp({"data": [], "has_more": False})}))

    out = oai.get_cost_attribution("project", D0, D1)

    assert [g["id"] for g in out["groups"]] == ["proj_a"]
    assert out["unreadable_rows"] == 2
    assert "no readable amount" in out["note"]


def test_openai_user_attribution_is_an_estimate_from_usage(monkeypatch):
    """Costs carry no user_id, so user spend is priced from usage rows, and
    says so. A model with no known price is listed, never priced at $0."""
    monkeypatch.setattr("finops.security.env.get_env", _env(OAI_KEY))
    usage = {"data": [{"object": "bucket", "start_time": 1756684800, "results": [
        {"object": "organization.usage.completions.result", "user_id": "user_1",
         "model": "gpt-4o-mini", "input_tokens": 1_000_000, "input_cached_tokens": 0,
         "output_tokens": 0, "num_model_requests": 10},
        {"object": "organization.usage.completions.result", "user_id": "user_2",
         "model": "some-unreleased-model", "input_tokens": 5, "output_tokens": 5,
         "num_model_requests": 1},
    ]}], "has_more": False}
    r = _Router({
        "/organization/usage/completions": _Resp(usage),
        "/organization/users": _Resp({"data": [
            {"object": "organization.user", "id": "user_1", "name": "Ada",
             "email": "ada@example.com"}], "has_more": False}),
    })
    monkeypatch.setattr(httpx, "get", r)

    out = oai.get_cost_attribution("user", D0, D1)

    assert sorted(r.params_for("/organization/usage/completions")[0]["group_by"]) == ["model", "user_id"]
    assert out["source"] == "estimated"
    assert out["groups"][0]["group"] == "ada@example.com"
    assert out["groups"][0]["cost_usd"] > 0
    assert "some-unreleased-model" in out["unpriced_models"]
    assert "estimate" in out["note"].lower()


def test_openai_project_falls_back_to_a_usage_estimate_when_costs_fail(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(OAI_KEY))
    usage = {"data": [{"start_time": 1756684800, "results": [
        {"project_id": "proj_a", "model": "gpt-4o-mini", "input_tokens": 1_000_000,
         "output_tokens": 0}]}], "has_more": False}
    r = _Router({
        "/organization/costs": _Resp(status=500),
        "/organization/usage/completions": _Resp(usage),
        "/organization/projects": _Resp({"data": [], "has_more": False}),
    })
    monkeypatch.setattr(httpx, "get", r)

    out = oai.get_cost_attribution("project", D0, D1)

    assert sorted(r.params_for("/organization/usage/completions")[0]["group_by"]) == ["model", "project_id"]
    assert out["source"] == "estimated"
    assert out["groups"][0]["id"] == "proj_a"


def test_openai_a_refused_key_is_unread_not_zero(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"OPENAI_API_KEY": "sk-proj-fake"}))  # pragma: allowlist secret
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(status=401))

    out = oai.get_cost_attribution("project", D0, D1)

    assert out["source"] == "error"
    assert "groups" not in out
    assert "OPENAI_ADMIN_KEY" in out["error"]


def test_openai_without_a_key_is_not_configured(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env({}))
    assert oai.get_cost_attribution("project", D0, D1) == {
        "source": "none", "reason": "not_configured"}


@pytest.mark.parametrize("dim", ["workspace", "team", "tag", "session"])
def test_openai_says_which_dimensions_it_cannot_give(monkeypatch, dim):
    monkeypatch.setattr("finops.security.env.get_env", _env(OAI_KEY))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("no call expected"))
    out = oai.get_cost_attribution(dim, D0, D1)
    assert out["source"] == "unsupported"
    assert out["reason"]


def test_openai_usage_estimate_reads_the_documented_model_field(monkeypatch):
    """UsageCompletionsResult names the model `model`, not `model_id`; reading
    only model_id priced every real row as an unknown model."""
    usage = {"data": [{"start_time": 1756684800, "results": [
        {"model": "gpt-4o-mini", "input_tokens": 1_000_000, "output_tokens": 0}]}],
        "has_more": False}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(usage))
    out = oai._estimate_from_usage(D0, D1, "sk-admin-fake", None)  # pragma: allowlist secret
    assert "gpt-4o-mini" in out["by_model"]
    assert "unpriced_models" not in out


# ── Anthropic ─────────────────────────────────────────────────────────────

ANT_KEY = {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin01-fake"}  # pragma: allowlist secret


def _cost_report(*results, has_more=False, next_page=None):
    return {"data": [{"starting_at": "2026-09-01T00:00:00Z", "ending_at": "2026-09-02T00:00:00Z",
                      "results": list(results)}],
            "has_more": has_more, "next_page": next_page}


def test_anthropic_workspace_attribution_uses_the_cost_report(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(ANT_KEY))
    r = _Router({
        "/v1/organizations/cost_report": _Resp(_cost_report(
            {"amount": "1500", "currency": "USD", "workspace_id": "wrkspc_a",
             "description": "Claude Sonnet Usage - Input Tokens"},
            {"amount": "500", "currency": "USD", "workspace_id": None},
        )),
        "/v1/organizations/workspaces": _Resp({"data": [
            {"id": "wrkspc_a", "name": "Growth team", "type": "workspace"}],
            "has_more": False, "last_id": "wrkspc_a"}),
    })
    monkeypatch.setattr(httpx, "get", r)

    out = ant.get_cost_attribution("workspace", D0, D1)

    assert r.params_for("/v1/organizations/cost_report")[0]["group_by[]"] == ["workspace_id"]
    assert out["source"] == "cost_api"
    # amount is in cents, same convention as get_cost_report
    assert {g["group"]: g["cost_usd"] for g in out["groups"]} == {
        "Growth team": 15.0, "Default workspace": 5.0}
    assert out["total_usd"] == 20.0


def test_anthropic_get_costs_surfaces_by_workspace(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(ANT_KEY))

    def cost_report(url, params):
        if params.get("group_by[]") == ["workspace_id"]:
            return _Resp(_cost_report({"amount": "700", "workspace_id": "wrkspc_a"},
                                      {"amount": "300", "workspace_id": None}))
        return _Resp(_cost_report({"amount": "1000", "model": "claude-sonnet-4-5"}))

    r = _Router({
        "/v1/organizations/cost_report": cost_report,
        "/v1/organizations/workspaces": _Resp({"data": [{"id": "wrkspc_a", "name": "Growth"}],
                                               "has_more": False}),
    })
    monkeypatch.setattr(httpx, "get", r)

    out = ant.get_costs(D0, D1)

    assert out["source"] == "cost_api"
    assert out["by_workspace"] == {"Growth": 7.0, "Default workspace": 3.0}


def test_anthropic_api_key_attribution_prices_the_usage_report(monkeypatch):
    """The cost report cannot group by API key; the messages usage report can,
    so key spend is priced from tokens and labelled an estimate."""
    monkeypatch.setattr("finops.security.env.get_env", _env(ANT_KEY))
    usage = {"data": [{"starting_at": "2026-09-01T00:00:00Z", "results": [
        {"api_key_id": "apikey_1", "model": "claude-sonnet-4-5",
         "uncached_input_tokens": 1_000_000, "output_tokens": 0,
         "cache_read_input_tokens": 0,
         "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}},
        {"api_key_id": None, "model": "claude-sonnet-4-5",
         "uncached_input_tokens": 0, "output_tokens": 1_000_000},
    ]}], "has_more": False}
    r = _Router({
        "/v1/organizations/usage_report/messages": _Resp(usage),
        "/v1/organizations/api_keys": _Resp({"data": [
            {"id": "apikey_1", "name": "Developer Key", "type": "api_key"}],
            "has_more": False}),
    })
    monkeypatch.setattr(httpx, "get", r)

    out = ant.get_cost_attribution("api_key", D0, D1)

    assert sorted(r.params_for("/v1/organizations/usage_report/messages")[0]["group_by[]"]) == [
        "api_key_id", "model"]
    assert out["source"] == "estimated"
    labels = {g["group"]: g["cost_usd"] for g in out["groups"]}
    assert labels["Developer Key"] > 0
    assert "(no API key)" in labels


def test_anthropic_user_attribution_names_accounts(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(ANT_KEY))
    usage = {"data": [{"starting_at": "2026-09-01T00:00:00Z", "results": [
        {"account_id": "user_1", "model": "claude-sonnet-4-5",
         "uncached_input_tokens": 1_000_000, "output_tokens": 0}]}], "has_more": False}
    r = _Router({
        "/v1/organizations/usage_report/messages": _Resp(usage),
        "/v1/organizations/users": _Resp({"data": [
            {"id": "user_1", "email": "jane@example.com", "name": "Jane Doe", "type": "user"}],
            "has_more": False}),
    })
    monkeypatch.setattr(httpx, "get", r)

    out = ant.get_cost_attribution("user", D0, D1)

    assert sorted(r.params_for("/v1/organizations/usage_report/messages")[0]["group_by[]"]) == [
        "account_id", "model"]
    assert out["groups"][0]["group"] == "jane@example.com"


def test_anthropic_needs_the_admin_key_and_says_so(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"ANTHROPIC_API_KEY": "sk-ant-api03-fake"}))  # pragma: allowlist secret
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("no call expected"))
    out = ant.get_cost_attribution("workspace", D0, D1)
    assert out["source"] == "none"
    assert out["reason"] == "admin_key_required"
    assert "ANTHROPIC_ADMIN_KEY" in out["error"]


def test_anthropic_a_failed_report_is_unread_not_zero(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(ANT_KEY))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(status=503))
    out = ant.get_cost_attribution("workspace", D0, D1)
    assert out["source"] == "none"
    assert "groups" not in out


@pytest.mark.parametrize("dim", ["project", "team", "tag", "session"])
def test_anthropic_says_which_dimensions_it_cannot_give(monkeypatch, dim):
    monkeypatch.setattr("finops.security.env.get_env", _env(ANT_KEY))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("no call expected"))
    out = ant.get_cost_attribution(dim, D0, D1)
    assert out["source"] == "unsupported"
    assert out["reason"]
