"""A configured AI provider that could not be read is listed, never merged as $0.

get_all_llm_costs merged a provider's `_empty_result("api_error")` (total_usd
0.0) into by_provider as $0, and a fetcher that raised was logged at debug and
vanished. The 12h cache key had no notion of which providers were configured,
so connecting one kept serving the old total.

OpenAI's usage endpoints cap bucket_width=1d at 31 buckets per page and the
connector asked for 180 without following next_page; models missing from the
price table were silently priced at $0 in the usage estimate.
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from finops import cache
from finops.connectors import llm_costs
from finops.connectors.saas import (
    anthropic_usage,
    litellm,
    openai_usage,
    openrouter,
    vertex_costs,
)

START, END = date(2026, 9, 1), date(2026, 9, 20)


def _ok(total: float, model: str) -> dict:
    return {"total_usd": total, "by_model": {model: total}, "by_model_tokens": {},
            "daily": [], "source": "api"}


@pytest.fixture(autouse=True)
def _providers(monkeypatch, tmp_path):
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cache, "_DISK_DISABLED", True)
    cache.clear()
    state = {"openai": True, "anthropic": True}

    def _conf(name):
        async def _c():
            return state.get(name, False)
        return _c

    for mod, name in ((openai_usage, "openai"), (anthropic_usage, "anthropic"),
                      (vertex_costs, "vertex"), (openrouter, "openrouter"),
                      (litellm, "litellm")):
        monkeypatch.setattr(mod, "is_configured", _conf(name))
    monkeypatch.setattr(anthropic_usage, "get_costs",
                        lambda s, e: _ok(40.0, "claude-sonnet-4-5"))
    yield state
    cache.clear()


def _run():
    return llm_costs.get_all_llm_costs(START, END, exclude_cloud_native=True)


def test_a_provider_api_error_is_listed_not_merged_as_zero(monkeypatch):
    monkeypatch.setattr(openai_usage, "get_costs",
                        lambda s, e: openai_usage._empty_result("api_error"))
    out = _run()
    assert out["total_usd"] == 40.0
    assert "openai" not in out["by_provider"]
    assert out["failed_providers"] == {"openai": "api_error"}
    assert out["partial"] is True


def test_a_fetcher_exception_is_listed_not_dropped(monkeypatch):
    def _boom(s, e):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(openai_usage, "get_costs", _boom)
    out = _run()
    assert "ConnectError" in out["failed_providers"]["openai"]
    assert out["partial"] is True


def test_every_provider_failing_is_an_error(monkeypatch, _providers):
    _providers["anthropic"] = False
    monkeypatch.setattr(openai_usage, "get_costs",
                        lambda s, e: openai_usage._empty_result("api_error"))
    out = _run()
    assert out.get("error")
    assert out["failed_providers"] == {"openai": "api_error"}


def test_a_partial_result_is_not_cached(monkeypatch):
    monkeypatch.setattr(openai_usage, "get_costs",
                        lambda s, e: openai_usage._empty_result("api_error"))
    _run()
    monkeypatch.setattr(openai_usage, "get_costs", lambda s, e: _ok(10.0, "gpt-4o"))
    out = _run()
    assert out["by_provider"]["openai"] == 10.0
    assert "failed_providers" not in out


def test_a_refused_openai_key_is_listed_not_merged_as_a_cached_zero(monkeypatch):
    """OpenAI refusing the key on the usage API is source="error", not "none".
    It used to fall past the unread check and merge as a clean, cached $0."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-standard")
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)

    def _refuse(url, params=None, headers=None, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(401, request=request, json={"error": "no"})

    monkeypatch.setattr(httpx, "get", _refuse)
    out = _run()
    assert out["total_usd"] == 40.0
    assert "openai" not in out["by_provider"] and "openai" not in out["sources"]
    assert out["failed_providers"]["openai"].startswith("credential_invalid")
    assert "Admin key" in out["failed_providers"]["openai"]
    assert out["partial"] is True

    # Not cached: the key fixed a minute later is read at once.
    monkeypatch.setattr(openai_usage, "get_costs", lambda s, e: _ok(10.0, "gpt-4o"))
    again = _run()
    assert again["by_provider"]["openai"] == 10.0 and "failed_providers" not in again


def test_connecting_a_provider_is_not_masked_by_the_cache(monkeypatch, _providers):
    _providers["openai"] = False
    monkeypatch.setattr(openai_usage, "get_costs", lambda s, e: _ok(10.0, "gpt-4o"))
    first = _run()
    _providers["openai"] = True
    second = _run()
    assert "openai" not in first["by_provider"]
    assert second["by_provider"]["openai"] == 10.0


# ── OpenAI Organization API paging and pricing ──────────────────────────────


class _Resp:
    def __init__(self, body):
        self._body = body
        self.status_code = 200

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


def _bucket(day_ts: int, model: str, inp: int, out: int) -> dict:
    """A usage/completions bucket, as the Organization API returns it."""
    return {"object": "bucket", "start_time": day_ts, "end_time": day_ts + 86400,
            "results": [{"object": "organization.usage.completions.result",
                         "input_tokens": inp, "output_tokens": out,
                         "input_cached_tokens": 0, "num_model_requests": 1,
                         "model": model, "model_id": model}]}


def _fake_usage_pages(monkeypatch, pages: list[list[dict]]) -> list[dict]:
    seen: list[dict] = []

    def _get(url, params=None, headers=None, timeout=None):
        seen.append(dict(params or {}))
        if params.get("bucket_width") == "1d" and params.get("limit", 7) > 31:
            resp = _Resp({"error": {"message": "limit must be <= 31"}})
            resp.status_code = 400

            def _raise():
                raise httpx.HTTPStatusError("400", request=None, response=None)
            resp.raise_for_status = _raise
            return resp
        idx = int(params.get("page") or 0)
        more = idx + 1 < len(pages)
        return _Resp({"object": "page", "data": pages[idx], "has_more": more,
                      "next_page": str(idx + 1) if more else None})

    monkeypatch.setattr(httpx, "get", _get)
    return seen


def test_usage_estimate_reads_every_page_within_the_limit(monkeypatch):
    day = 1788220800  # 2026-09-01
    pages = [[_bucket(day + i * 86400, "gpt-4o", 1_000_000, 0) for i in range(31)],
             [_bucket(day + 31 * 86400, "gpt-4o", 1_000_000, 0)]]
    seen = _fake_usage_pages(monkeypatch, pages)
    out = openai_usage._estimate_from_usage(START, END, "sk-test", None)
    assert out["source"] == "estimated"
    assert out["total_usd"] == pytest.approx(32 * 2.50)
    assert all(p["limit"] <= 31 for p in seen)
    assert len(seen) == 2


def test_usage_estimate_flags_unknown_models_instead_of_pricing_them_zero(monkeypatch):
    day = 1788220800
    _fake_usage_pages(monkeypatch, [[
        _bucket(day, "gpt-4o", 1_000_000, 0),
        _bucket(day, "gpt-9-ultra", 5_000_000, 1_000_000),
    ]])
    out = openai_usage._estimate_from_usage(START, END, "sk-test", None)
    assert out["total_usd"] == pytest.approx(2.50)
    assert "gpt-9-ultra" not in out["by_model"]
    assert out["unpriced_models"] == {"gpt-9-ultra": {"input_tokens": 5_000_000,
                                                      "output_tokens": 1_000_000}}


def test_costs_endpoint_follows_next_page(monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-test")
    day = 1788220800

    def _cost_bucket(i):
        return {"object": "bucket", "start_time": day + i * 86400, "end_time": day + (i + 1) * 86400,
                "results": [{"object": "organization.costs.result",
                             "amount": {"value": 1.5, "currency": "usd"},
                             "line_item": None, "project_id": "proj_1", "model_id": "gpt-4o"}]}

    pages = [[_cost_bucket(i) for i in range(7)], [_cost_bucket(7)]]

    def _get(url, params=None, headers=None, timeout=None):
        if url.endswith("/organization/costs"):
            idx = int(params.get("page") or 0)
            more = idx + 1 < len(pages)
            return _Resp({"object": "page", "data": pages[idx], "has_more": more,
                          "next_page": str(idx + 1) if more else None})
        return _Resp({"object": "page", "data": [], "has_more": False, "next_page": None})

    monkeypatch.setattr(httpx, "get", _get)
    out = openai_usage.get_costs(START, END)
    assert out["source"] == "api"
    assert out["total_usd"] == pytest.approx(8 * 1.5)
