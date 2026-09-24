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
                "model": "claude-sonnet-4-5-20250929",
                "date": "2026-06-01",
                "input_tokens": 120_000,
                "output_tokens": 60_000,
                "cache_read_input_tokens": 480_000,
                "cache_creation_input_tokens": 30_000,
            },
            {
                "model": "claude-sonnet-4-5-20250929",
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
    sonnet = data["by_model_tokens"]["claude-sonnet-4-5-20250929"]

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
    assert "claude-sonnet-4-5-20250929" in pe["by_model"]

    # Context-window lever also sees token detail now.
    cwu = report["context_window_utilization"]
    assert cwu["by_model"]


def test_error_keys_absent_when_api_omits_request_counts():
    # The token-only Usage API carries no request/error counts, so the connector
    # leaves those keys unset and error_spend_estimate degrades gracefully.
    data = anthropic_usage._parse_usage(_anthropic_payload(), source="api")
    assert "total_requests" not in data
    assert "error_requests" not in data


# ── priced per model from llm_prices ─────────────────────────────────────────

def _usage(model, **tok):
    return {"data": [{"model": model, "date": "2026-06-01", **tok}]}


def test_the_estimate_prices_current_models_and_every_token_class():
    # claude-opus-4-8 was not in the connector's own table, so its spend
    # estimated at $0; and cache reads and writes were never priced at all.
    data = anthropic_usage._parse_usage(_usage(
        "claude-opus-4-8", input_tokens=1_000_000, output_tokens=1_000_000,
        cache_read_input_tokens=10_000_000, cache_creation_input_tokens=1_000_000,
    ), source="estimated")
    # $5 in + $25 out + 10M reads at $0.50 + 1M 5-minute writes at $6.25
    assert data["total_usd"] == 5 + 25 + 5 + 6.25
    assert "unpriced_models" not in data


def test_one_hour_cache_writes_are_priced_at_their_own_rate():
    data = anthropic_usage._parse_usage(_usage(
        "claude-sonnet-4-6", cache_creation_input_tokens=1_000_000,
        cache_creation={"ephemeral_1h_input_tokens": 1_000_000},
    ), source="estimated")
    assert data["total_usd"] == 6.0


def test_an_unpriced_model_is_listed_not_counted_as_zero():
    data = anthropic_usage._parse_usage({"data": [
        {"model": "claude-3-opus-20240229", "date": "2026-06-01",
         "input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 100},
        {"model": "claude-haiku-4-5", "date": "2026-06-01",
         "input_tokens": 1_000_000, "output_tokens": 0},
    ]}, source="estimated")
    assert data["total_usd"] == 1.0
    assert data["unpriced_models"] == {
        "claude-3-opus-20240229": {"input_tokens": 1100, "output_tokens": 500}}
    assert "claude-3-opus-20240229" not in data["by_model"]
    assert "claude-3-opus-20240229" in data["note"]
    # Its tokens still reach the KPI layer.
    assert data["by_model_tokens"]["claude-3-opus-20240229"]["input_tokens"] == 1000


def test_cache_savings_use_each_models_read_rate():
    from finops.analytics.ai_kpis import cache_hit_rate

    # Opus 5.5 reads at $0.20 against $4 input, so a read saves $3.80 per 1M,
    # not the 90% of input a flat 0.1x assumption gives.
    r = cache_hit_rate({"by_model_tokens": {"claude-opus-5-5": {
        "input_tokens": 0, "cache_read_input_tokens": 1_000_000}}})
    assert r["estimated_savings_usd"] == 3.8
    assert "unpriced_models" not in r


def test_cache_savings_do_not_invent_a_price_for_an_unknown_model():
    from finops.analytics.ai_kpis import cache_hit_rate

    # The old fallback priced any unknown model's reads at $3/1M input.
    r = cache_hit_rate({"by_model_tokens": {"claude-nova-9": {
        "input_tokens": 0, "cache_read_input_tokens": 1_000_000}}})
    assert r["estimated_savings_usd"] == 0.0
    assert r["unpriced_models"] == ["claude-nova-9"]
    assert r["hit_rate_pct"] == 100.0
