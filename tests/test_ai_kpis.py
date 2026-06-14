"""Tests for the AI KPI report against real-shaped Anthropic usage data.

Guards the key-name contract between the Anthropic connector and the KPI
consumers. The connector must emit per-model token sub-keys named
``input_tokens`` / ``output_tokens`` / ``cache_read_input_tokens`` /
``cache_creation_input_tokens``, otherwise the caching and prompt-efficiency
levers silently read zeros and collapse to nothing.
"""
from finops.analytics.ai_kpis import full_kpi_report
from finops.connectors.saas import anthropic_usage


def _anthropic_payload():
    """A realistic Anthropic Usage API response with prompt-cache tokens."""
    return {
        "data": [
            {
                "model": "claude-3-5-sonnet-20241022",
                "date": "2026-06-01",
                "input_tokens": 120_000,
                "output_tokens": 60_000,
                "cache_read_input_tokens": 480_000,
                "cache_creation_input_tokens": 30_000,
            },
            {
                "model": "claude-3-5-sonnet-20241022",
                "date": "2026-06-02",
                "input_tokens": 80_000,
                "output_tokens": 40_000,
                "cache_read_input_tokens": 320_000,
                "cache_creation_input_tokens": 10_000,
            },
            {
                "model": "claude-3-5-haiku-20241022",
                "date": "2026-06-02",
                "input_tokens": 200_000,
                "output_tokens": 50_000,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        ]
    }


def _llm_result(anthropic_data):
    """The aggregated get_all_llm_costs() shape, Anthropic-only here."""
    return {
        "period":      "2026-06-01 → 2026-06-02",
        "total_usd":   anthropic_data["total_usd"],
        "by_provider": {"anthropic": anthropic_data["total_usd"]},
        "by_model":    anthropic_data["by_model"],
        "daily":       anthropic_data["daily"],
    }


def test_connector_emits_kpi_token_keys():
    data = anthropic_usage._parse_usage(_anthropic_payload(), source="api")
    sonnet = data["by_model_tokens"]["claude-3-5-sonnet-20241022"]

    # New key names the KPIs read, accumulated across both daily entries.
    assert sonnet["input_tokens"] == 200_000
    assert sonnet["output_tokens"] == 100_000
    assert sonnet["cache_read_input_tokens"] == 800_000
    assert sonnet["cache_creation_input_tokens"] == 40_000

    # Old key names must not come back, or the consumers read zeros again.
    assert "input" not in sonnet
    assert "output" not in sonnet


def test_cache_and_prompt_efficiency_are_live_against_real_data():
    anthropic_data = anthropic_usage._parse_usage(_anthropic_payload(), source="api")
    report = full_kpi_report(_llm_result(anthropic_data), anthropic_data=anthropic_data)

    # Cache lever: a real hit rate instead of the silent 0.
    chr = report["cache_hit_rate"]
    assert chr["fresh_input_tokens"] == 400_000
    assert chr["cache_reads"] == 800_000
    assert chr["hit_rate_pct"] > 0          # 800k / (800k + 400k) = 66.67%
    assert chr["estimated_savings_usd"] > 0

    # Prompt-efficiency lever: by_model is populated, not empty.
    pe = report["prompt_efficiency"]
    assert pe["by_model"]
    assert "claude-3-5-sonnet-20241022" in pe["by_model"]

    # Context-window lever also sees token detail now.
    cwu = report["context_window_utilization"]
    assert cwu["by_model"]


def test_error_keys_absent_when_api_omits_request_counts():
    # The token-only Usage API carries no request/error counts, so the connector
    # leaves those keys unset and error_spend_estimate degrades gracefully.
    data = anthropic_usage._parse_usage(_anthropic_payload(), source="api")
    assert "total_requests" not in data
    assert "error_requests" not in data
