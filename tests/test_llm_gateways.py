"""Tests for the gateway + GPU-infra connectors and the OpenAI token / KPI fix.

These guard the new AI-native coverage:
  - OpenRouter activity parsing (per-model tokens + cost) and the standard-key
    fallback to a credits-only summary.
  - LiteLLM proxy spend-log aggregation.
  - Modal/Together/Replicate honest "limited" status (billing gated).
  - OpenAI now emits by_model_tokens, and the KPI engine consumes tokens from
    ALL providers (the Anthropic-only bug), so OpenAI accounts get real
    context-window and prompt-efficiency analysis.
"""
from datetime import date

import httpx
import pytest

from finops.connectors.saas import openrouter, litellm, gpu_infra, openai_usage
from finops.analytics.ai_kpis import full_kpi_report


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _env(mapping):
    return lambda k, d="": mapping.get(k, d)


# ── OpenRouter ────────────────────────────────────────────────────────────────

def test_openrouter_activity_parses_tokens_and_cost(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"OPENROUTER_PROVISIONING_KEY": "pk"}))
    payload = {"data": [
        {"date": "2026-06-01", "model": "openai/gpt-4o", "usage": 1.5,
         "prompt_tokens": 1000, "completion_tokens": 500, "requests": 3},
        {"date": "2026-06-02", "model": "anthropic/claude-3.5-sonnet", "usage": 2.0,
         "prompt_tokens": 2000, "completion_tokens": 800, "requests": 4},
        {"date": "2026-05-15", "model": "openai/gpt-4o", "usage": 99.0,  # out of range
         "prompt_tokens": 1, "completion_tokens": 1, "requests": 1},
    ]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResp(200, payload))

    res = openrouter.get_costs(date(2026, 6, 1), date(2026, 6, 3))
    assert res["source"] == "api"
    assert round(res["total_usd"], 2) == 3.5  # out-of-range row excluded
    gpt = res["by_model_tokens"]["openai/gpt-4o"]
    assert gpt["input_tokens"] == 1000
    assert gpt["output_tokens"] == 500
    assert gpt["request_count"] == 3
    assert len(res["daily"]) == 2


def test_openrouter_falls_back_to_credits_without_provisioning_key(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"OPENROUTER_API_KEY": "sk-or"}))

    def fake_get(url, **kw):
        if "/activity" in url:
            return FakeResp(403)            # standard key cannot read activity
        if "/credits" in url:
            return FakeResp(200, {"data": {"total_credits": 10.0, "total_usage": 3.0}})
        return FakeResp(404)

    monkeypatch.setattr(httpx, "get", fake_get)
    res = openrouter.get_costs(date(2026, 6, 1), date(2026, 6, 3))
    assert res["source"] == "limited"
    assert res["total_usd"] == 0.0          # never pollute the range total
    assert res["lifetime_usage_usd"] == 3.0
    assert res["credits_remaining_usd"] == 7.0


def test_openrouter_not_configured(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env({}))
    res = openrouter.get_costs(date(2026, 6, 1), date(2026, 6, 3))
    assert res["source"] == "none"
    assert res["reason"] == "not_configured"


# ── LiteLLM ───────────────────────────────────────────────────────────────────

def test_litellm_aggregates_spend_logs(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"LITELLM_PROXY_URL": "http://localhost:4000/",
                              "LITELLM_MASTER_KEY": "sk-x"}))
    logs = [
        {"model": "gpt-4o", "spend": 0.5, "prompt_tokens": 1000,
         "completion_tokens": 200, "startTime": "2026-06-01T10:00:00"},
        {"model": "gpt-4o", "spend": 0.25, "prompt_tokens": 500,
         "completion_tokens": 100, "startTime": "2026-06-01T12:00:00"},
    ]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResp(200, logs))

    res = litellm.get_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert res["source"] == "api"
    assert round(res["total_usd"], 2) == 0.75
    gpt = res["by_model_tokens"]["gpt-4o"]
    assert gpt["input_tokens"] == 1500
    assert gpt["output_tokens"] == 300
    assert gpt["request_count"] == 2
    assert res["daily"][0]["date"] == "2026-06-01"


def test_litellm_not_configured_without_url(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"LITELLM_MASTER_KEY": "sk-x"}))  # no URL
    res = litellm.get_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert res["source"] == "none"


# ── GPU infra (Modal / Together / Replicate) ─────────────────────────────────

def test_gpu_infra_reports_limited_for_configured_provider(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"MODAL_TOKEN_ID": "ak", "MODAL_TOKEN_SECRET": "as"}))
    res = gpu_infra.get_all_gpu_infra_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert res["configured_count"] == 1
    assert res["providers"]["modal"]["source"] == "limited"
    assert "Team/Enterprise" in res["providers"]["modal"]["note"]


def test_gpu_infra_probes_reachability(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env",
                        _env({"REPLICATE_API_TOKEN": "r8_x"}))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResp(200, {}))
    res = gpu_infra.replicate_get_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert res["source"] == "limited"
    assert res["credential_reachable"] is True


def test_gpu_infra_empty_when_unconfigured(monkeypatch):
    monkeypatch.setattr("finops.security.env.get_env", _env({}))
    res = gpu_infra.get_all_gpu_infra_costs(date(2026, 6, 1), date(2026, 6, 2))
    assert res["configured_count"] == 0
    assert res["providers"] == {}


# ── OpenAI tokens + KPI engine coverage ──────────────────────────────────────

def test_openai_accumulate_tokens_uses_fresh_input():
    bmt: dict = {}
    openai_usage._accumulate_tokens(
        {"model_id": "gpt-4o", "input_tokens": 1000, "output_tokens": 500,
         "input_cached_tokens": 200, "num_model_requests": 3}, bmt)
    gpt = bmt["gpt-4o"]
    # fresh input = total(1000) - cached(200) so cache-hit math matches Anthropic
    assert gpt["input_tokens"] == 800
    assert gpt["cache_read_input_tokens"] == 200
    assert gpt["output_tokens"] == 500
    assert gpt["request_count"] == 3


def test_kpi_engine_covers_openai_not_just_anthropic():
    """The core bug: full_kpi_report built combined_tokens from [anthropic_data]
    only, so an OpenAI-only account got empty context-window / prompt-efficiency.
    Now the aggregate's by_model_tokens drives both."""
    llm_result = {
        "period": "2026-06-01 → 2026-06-30",
        "total_usd": 100.0,
        "by_provider": {"openai": 100.0},
        "by_model": {"gpt-4o": 100.0},
        "by_model_tokens": {
            "gpt-4o": {"input_tokens": 2_000_000, "output_tokens": 4_000_000,
                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                       "request_count": 20},
        },
        "daily": [],
    }
    report = full_kpi_report(llm_result)  # no anthropic_data at all

    assert report["context_window_utilization"]["by_model"], "OpenAI tokens ignored"
    assert "gpt-4o" in report["context_window_utilization"]["by_model"]
    assert report["prompt_efficiency"]["by_model"]
    assert "gpt-4o" in report["prompt_efficiency"]["by_model"]


def test_kpi_does_not_double_count_anthropic_when_both_passed():
    """When the aggregate already carries Anthropic tokens AND anthropic_data is
    passed (the production path), tokens must not be summed twice."""
    tokens = {"claude-sonnet-4-5-20250929": {
        "input_tokens": 1_000_000, "output_tokens": 500_000,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
    llm_result = {
        "period": "x", "total_usd": 10.0, "by_provider": {"anthropic": 10.0},
        "by_model": {"claude-sonnet-4-5-20250929": 10.0},
        "by_model_tokens": {k: dict(v) for k, v in tokens.items()},
        "daily": [],
    }
    anthropic_data = {"by_model_tokens": {k: dict(v) for k, v in tokens.items()},
                      "total_usd": 10.0, "by_model": {}}
    report = full_kpi_report(llm_result, anthropic_data=anthropic_data)
    cwu = report["context_window_utilization"]["by_model"]["claude-sonnet-4-5-20250929"]
    # request_count defaults to 1, so avg_input_tokens == summed input_tokens.
    # It must be 1M (counted once), not 2M (counted twice).
    assert cwu["avg_input_tokens"] == 1_000_000
