# SPDX-License-Identifier: Apache-2.0
"""AI cost by team, key, user, tag and session from LiteLLM and Langfuse.

Both connectors aggregated by model and day only, so the one place a team
already labels its AI traffic (a LiteLLM team or virtual key, a Langfuse trace
tag or user id) never reached nable. These pin the attribution reads against
fake responses shaped from each project's own source:

  LiteLLM   BerriAI/litellm: GET /team/daily/activity, /user/daily/activity
            and /tag/daily/activity return SpendAnalyticsPaginatedResponse
            {results: [{date, metrics, breakdown: {api_keys: {hash: {metrics,
            metadata: {key_alias, ...}}}, entities: {id: {metrics, metadata}}}}],
            metadata: {total_spend, page, total_pages, has_more}}
            (litellm/types/proxy/management_endpoints/common_daily_activity.py;
            team metadata carries team_alias, user metadata user_email).
  Langfuse  langfuse/langfuse web/public/generated/api/openapi.yml:
            GET /api/public/v2/metrics?query= (observations view, `tags`
            dimension; userId and sessionId are high-cardinality and rejected)
            and GET /api/public/metrics?query= (traces view, userId and
            sessionId dimensions). Rows are keyed by dimension field and
            "<aggregation>_<measure>", e.g. sum_totalCost.

No network: httpx.get is replaced for every test.
"""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from finops.connectors.saas import langfuse as lf
from finops.connectors.saas import litellm as ll

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
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url, params=None, headers=None, timeout=None, **kw):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        for suffix in sorted(self.routes, key=len, reverse=True):
            if url.endswith(suffix):
                ans = self.routes[suffix]
                return ans(url, params or {}) if callable(ans) else ans
        raise AssertionError(f"unexpected URL in this test: {url}")


# ── LiteLLM ───────────────────────────────────────────────────────────────

LL_ENV = {"LITELLM_PROXY_URL": "http://proxy.internal:4000",
          "LITELLM_MASTER_KEY": "sk-litellm-master-fake"}  # pragma: allowlist secret


def _metric(spend):
    return {"metrics": {"spend": spend, "prompt_tokens": 0, "completion_tokens": 0,
                        "api_requests": 1}}


def _day(day, entities=None, api_keys=None):
    return {"date": day, "metrics": {"spend": 0},
            "breakdown": {"models": {}, "api_keys": api_keys or {},
                          "entities": entities or {}}}


def test_litellm_team_spend_sums_every_page_and_names_teams(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(LL_ENV))

    def team_activity(url, params):
        assert params["start_date"] == "2026-09-01"
        assert params["end_date"] == "2026-09-30"
        if params["page"] == 1:
            return _Resp({"results": [_day("2026-09-02", entities={
                "team-1": {**_metric(10.0), "metadata": {"team_alias": "Search"}},
                "Unassigned": {**_metric(1.5), "metadata": {}}})],
                "metadata": {"page": 1, "total_pages": 2, "has_more": True}})
        return _Resp({"results": [_day("2026-09-01", entities={
            "team-1": {**_metric(5.0), "metadata": {"team_alias": "Search"}},
            "team-2": {**_metric(2.0), "metadata": {"team_alias": "Support"}}})],
            "metadata": {"page": 2, "total_pages": 2, "has_more": False}})

    r = _Router({"/team/daily/activity": team_activity})
    monkeypatch.setattr(httpx, "get", r)

    out = ll.get_cost_attribution("team", D0, D1)

    assert [c[1]["page"] for c in r.calls] == [1, 2]
    assert r.calls[0][2]["Authorization"] == "Bearer sk-litellm-master-fake"  # pragma: allowlist secret
    assert out["source"] == "api"
    assert {g["group"]: g["cost_usd"] for g in out["groups"]} == {
        "Search": 15.0, "Support": 2.0, "(no team)": 1.5}
    assert out["total_usd"] == 18.5


def test_litellm_key_spend_uses_key_aliases(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(LL_ENV))
    r = _Router({"/user/daily/activity": _Resp({"results": [_day("2026-09-01", api_keys={
        "88dc28d0f030c55ed4ab77ed8faf098196cb1c05df778539800c9f1243fe6b4b": {  # pragma: allowlist secret
            **_metric(9.0), "metadata": {"key_alias": "checkout-service"}},
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855": {  # pragma: allowlist secret
            **_metric(1.0), "metadata": {"key_alias": None}}})],
        "metadata": {"has_more": False}})})
    monkeypatch.setattr(httpx, "get", r)

    out = ll.get_cost_attribution("api_key", D0, D1)

    labels = {g["group"]: g["cost_usd"] for g in out["groups"]}
    assert labels["checkout-service"] == 9.0
    assert labels["key e3b0c44298fc"] == 1.0


def test_litellm_user_spend_labels_by_email(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(LL_ENV))
    r = _Router({"/user/daily/activity": _Resp({"results": [_day("2026-09-01", entities={
        "u-1": {**_metric(3.0), "metadata": {"user_email": "dev@example.com"}}})],
        "metadata": {"has_more": False}})})
    monkeypatch.setattr(httpx, "get", r)

    out = ll.get_cost_attribution("user", D0, D1)
    assert out["groups"] == [{"group": "dev@example.com", "id": "u-1", "cost_usd": 3.0}]


def test_litellm_tag_spend_is_flagged_as_overlapping(monkeypatch):
    """LiteLLM writes a request's spend once per tag, so tags can sum past
    the bill; the result has to say so."""
    monkeypatch.setattr("finops.security.env.get_env", _env(LL_ENV))
    r = _Router({"/tag/daily/activity": _Resp({"results": [_day("2026-09-01", entities={
        "feature:chat": {**_metric(4.0), "metadata": {}},
        "customer:acme": {**_metric(4.0), "metadata": {}}})],
        "metadata": {"has_more": False}})})
    monkeypatch.setattr(httpx, "get", r)

    out = ll.get_cost_attribution("tag", D0, D1)
    assert out["groups_overlap"] is True
    assert {g["group"] for g in out["groups"]} == {"feature:chat", "customer:acme"}
    assert "tag" in out["note"].lower()


def test_litellm_missing_endpoint_is_unread_not_zero(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(LL_ENV))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(status=404))
    out = ll.get_cost_attribution("team", D0, D1)
    assert out["source"] == "none"
    assert "groups" not in out
    assert "/team/daily/activity" in out["error"]


def test_litellm_not_configured(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env({}))
    assert ll.get_cost_attribution("team", D0, D1)["reason"] == "not_configured"


@pytest.mark.parametrize("dim", ["project", "workspace", "session"])
def test_litellm_says_which_dimensions_it_cannot_give(monkeypatch, dim):
    monkeypatch.setattr("finops.security.env.get_env", _env(LL_ENV))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("no call expected"))
    assert ll.get_cost_attribution(dim, D0, D1)["source"] == "unsupported"


# ── Langfuse ──────────────────────────────────────────────────────────────

LF_ENV = {"LANGFUSE_PUBLIC_KEY": "pk-lf-fake",
          "LANGFUSE_SECRET_KEY": "sk-lf-fake",  # pragma: allowlist secret
          "LANGFUSE_HOST": "https://langfuse.internal"}


def _query(params):
    return json.loads(params["query"])


def test_langfuse_tag_spend_reads_v2_metrics_and_splits_tag_sets(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(LF_ENV))
    seen = {}

    def v2(url, params):
        seen.update(_query(params))
        return _Resp({"data": [
            {"tags": ["feature:chat", "customer:acme"], "sum_totalCost": "6.5"},
            {"tags": ["feature:search"], "sum_totalCost": 2},
            {"tags": [], "sum_totalCost": 1.25},
        ]})

    r = _Router({"/api/public/v2/metrics": v2})
    monkeypatch.setattr(httpx, "get", r)

    out = lf.get_cost_attribution("tag", D0, D1)

    assert r.calls[0][0] == "https://langfuse.internal/api/public/v2/metrics"
    assert r.calls[0][2]["Authorization"].startswith("Basic ")
    assert seen["view"] == "observations"
    assert seen["dimensions"] == [{"field": "tags"}]
    assert seen["metrics"] == [{"measure": "totalCost", "aggregation": "sum"}]
    assert seen["fromTimestamp"] == "2026-09-01T00:00:00Z"
    assert seen["toTimestamp"] == "2026-10-01T00:00:00Z"
    labels = {g["group"]: g["cost_usd"] for g in out["groups"]}
    assert labels == {"feature:chat": 6.5, "customer:acme": 6.5,
                      "feature:search": 2.0, "(untagged)": 1.25}
    assert out["groups_overlap"] is True
    # The total is the spend, not the sum of overlapping tag rows.
    assert out["total_usd"] == 9.75


def test_langfuse_tag_falls_back_to_v1_traces_on_older_servers(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(LF_ENV))
    seen = {}

    def v1(url, params):
        seen.update(_query(params))
        return _Resp({"data": [{"tags": ["prod"], "sum_totalCost": 3.0}]})

    r = _Router({"/api/public/v2/metrics": _Resp(status=404), "/api/public/metrics": v1})
    monkeypatch.setattr(httpx, "get", r)

    out = lf.get_cost_attribution("tag", D0, D1)
    assert seen["view"] == "traces"
    assert out["groups"][0]["group"] == "prod"


@pytest.mark.parametrize("dim,field", [("user", "userId"), ("session", "sessionId")])
def test_langfuse_user_and_session_read_the_traces_view(monkeypatch, dim, field):
    monkeypatch.setattr("finops.security.env.get_env", _env(LF_ENV))
    seen = {}

    def v1(url, params):
        seen.update(_query(params))
        return _Resp({"data": [{field: "abc", "sum_totalCost": 4.0},
                               {field: None, "sum_totalCost": 1.0}]})

    r = _Router({"/api/public/metrics": v1})
    monkeypatch.setattr(httpx, "get", r)

    out = lf.get_cost_attribution(dim, D0, D1)
    assert seen["view"] == "traces"
    assert seen["dimensions"] == [{"field": field}]
    assert out["groups"][0] == {"group": "abc", "id": "abc", "cost_usd": 4.0}
    assert len(out["groups"]) == 2


def test_langfuse_an_unreadable_cost_is_counted_not_zeroed(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(LF_ENV))
    monkeypatch.setattr(httpx, "get", _Router({"/api/public/metrics": _Resp({"data": [
        {"userId": "a", "sum_totalCost": 2.0}, {"userId": "b", "sum_totalCost": "NaN?"}]})}))
    out = lf.get_cost_attribution("user", D0, D1)
    assert [g["id"] for g in out["groups"]] == ["a"]
    assert out["unreadable_rows"] == 1
    assert out["total_usd"] == 2.0


def test_langfuse_full_page_is_marked_truncated(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(LF_ENV))
    rows = [{"userId": f"u{i}", "sum_totalCost": 1.0} for i in range(lf._ROW_LIMIT)]
    monkeypatch.setattr(httpx, "get", _Router({"/api/public/metrics": _Resp({"data": rows})}))
    out = lf.get_cost_attribution("user", D0, D1)
    assert out["truncated"] is True


def test_langfuse_without_v1_metrics_cannot_group_by_user(monkeypatch):
    """Langfuse v4 drops the v1 endpoint and v2 rejects userId as a grouping
    dimension: that is a limit of the server, said as one."""
    monkeypatch.setattr("finops.security.env.get_env", _env(LF_ENV))
    monkeypatch.setattr(httpx, "get", _Router({"/api/public/metrics": _Resp(status=404)}))
    out = lf.get_cost_attribution("user", D0, D1)
    assert out["source"] == "unsupported"
    assert "userId" in out["reason"]


def test_langfuse_a_rejected_key_is_unread(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env(LF_ENV))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(status=401))
    out = lf.get_cost_attribution("tag", D0, D1)
    assert out["source"] == "error"
    assert "groups" not in out


@pytest.mark.parametrize("dim", ["project", "workspace", "api_key", "team"])
def test_langfuse_says_which_dimensions_it_cannot_give(monkeypatch, dim):
    monkeypatch.setattr("finops.security.env.get_env", _env(LF_ENV))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("no call expected"))
    assert lf.get_cost_attribution(dim, D0, D1)["source"] == "unsupported"


def test_langfuse_not_configured(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env({}))
    assert lf.get_cost_attribution("tag", D0, D1)["reason"] == "not_configured"
