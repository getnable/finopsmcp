"""An AI provider that was not read must not report $0.

Dogfooded: get_llm_cost_by_model(provider="anthropic") answered total_usd 0.0
with no error when the Anthropic read failed, and get_llm_costs with nothing
connected answered total_usd 0.0 with no hint. A model relays both as "you
spend nothing on that".
"""
from __future__ import annotations

import asyncio
import inspect

import finops.server  # noqa: F401  (tools register against the server)
from finops.connectors import llm_costs
from finops.tools import llm as llm_tools


def _call(fn, **kw):
    out = fn(**kw)
    return asyncio.run(out) if inspect.isawaitable(out) else out


def _stub(monkeypatch, result):
    def fake(**kw):
        out = dict(result)
        if not kw.get("include_provider_results"):
            out.pop("provider_results", None)
        return out
    monkeypatch.setattr(llm_costs, "get_all_llm_costs", fake)
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: False)


BASE = {"period": {"start": "2026-09-01", "end": "2026-09-24"}, "top_spenders": [],
        "recommendations": []}


def test_failed_provider_is_an_error_not_zero(monkeypatch):
    _stub(monkeypatch, {**BASE, "total_usd": 12.0, "by_provider": {"openai": 12.0},
                        "by_model": {"gpt-4o": 12.0}, "partial": True,
                        "failed_providers": {"anthropic": "api_error"},
                        "provider_results": {"openai": {"by_model": {"gpt-4o": 12.0}}}})
    out = _call(llm_tools.get_llm_cost_by_model, provider="anthropic")
    assert "total_usd" not in out and "could not be read: api_error" in out["error"]
    assert "admin key" in out["hint"]


def test_unconnected_provider_says_so(monkeypatch):
    _stub(monkeypatch, {**BASE, "total_usd": 12.0, "by_provider": {"openai": 12.0},
                        "by_model": {"gpt-4o": 12.0},
                        "provider_results": {"openai": {"by_model": {"gpt-4o": 12.0}}}})
    out = _call(llm_tools.get_llm_cost_by_model, provider="Anthropic")
    assert out["error"] == "anthropic is not connected." and out["connected"] == ["openai"]


def test_provider_filter_shows_only_that_providers_models(monkeypatch):
    _stub(monkeypatch, {**BASE, "total_usd": 20.0,
                        "by_provider": {"openai": 12.0, "anthropic": 8.0},
                        "by_model": {"gpt-4o": 12.0, "claude-sonnet-4-6": 8.0},
                        "provider_results": {"openai": {"by_model": {"gpt-4o": 12.0}},
                                             "anthropic": {"by_model": {"claude-sonnet-4-6": 8.0}}}})
    out = _call(llm_tools.get_llm_cost_by_model, provider="anthropic")
    assert out["total_usd"] == 8.0 and out["by_model"] == {"claude-sonnet-4-6": 8.0}


def test_partial_total_is_labelled(monkeypatch):
    _stub(monkeypatch, {**BASE, "total_usd": 12.0, "by_provider": {"openai": 12.0},
                        "by_model": {"gpt-4o": 12.0}, "partial": True,
                        "failed_providers": {"anthropic": "api_error"}, "note": "Not read: anthropic.",
                        "provider_results": {"openai": {"by_model": {"gpt-4o": 12.0}}}})
    out = _call(llm_tools.get_llm_cost_by_model)
    assert out["partial"] is True and out["failed_providers"] == {"anthropic": "api_error"}


def test_nothing_connected_names_what_is_missing(monkeypatch):
    for name in ("OPENAI_ADMIN_KEY", "OPENAI_API_KEY", "ANTHROPIC_ADMIN_KEY", "ANTHROPIC_API_KEY",
                 "OPENROUTER_API_KEY", "LITELLM_API_KEY", "LITELLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("finops.demo_data.is_demo", lambda: False)
    out = llm_costs.get_all_llm_costs(exclude_cloud_native=True)
    if out.get("by_provider"):
        return  # a provider is configured in this environment; nothing to assert
    assert "No AI provider is connected" in out["error"]
    assert "ADMIN_KEY" in out["error"]
