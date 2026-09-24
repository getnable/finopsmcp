"""LLM token prices have one source: finops.llm_prices.

The rows are pinned to the published figures so a typo is a failing test, not
a quietly wrong estimate, and the resolver is pinned on the spellings nable
actually meets: API ids with a date, Bedrock profiles, Vertex ids and Cost
Explorer SKU names.
"""
from __future__ import annotations

from datetime import date

import pytest

from finops import llm_prices
from finops.llm_prices import (
    FAST_MODE,
    MODEL_PRICES,
    US_ONLY_MULTIPLIER,
    canonical_model,
    price_for,
)

# (input, output, 5m write, 1h write, cache read) per million tokens, as the
# Claude pricing page lists them on AS_OF.
_ANTHROPIC_PUBLISHED = {
    "claude-fable-5-1": (10, 50, 12.50, 20, 0.25),
    "claude-fable-5": (10, 50, 12.50, 20, 1.00),
    "claude-opus-5-5": (4, 20, 5, 8, 0.20),
    "claude-opus-5": (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-8": (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-5": (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-1": (15, 75, 18.75, 30, 1.50),
    "claude-sonnet-5": (2, 10, 2.50, 4, 0.20),
    "claude-sonnet-4-6": (3, 15, 3.75, 6, 0.30),
    "claude-haiku-4-5": (1, 5, 1.25, 2, 0.10),
    "claude-3-5-haiku": (0.80, 4, 1, 1.60, 0.08),
}


@pytest.mark.parametrize("model,row", sorted(_ANTHROPIC_PUBLISHED.items()))
def test_anthropic_rows_are_the_published_rates(model, row):
    p = MODEL_PRICES[model]
    assert (p.input, p.output, p.cache_write_5m, p.cache_write_1h, p.cache_read) == row
    assert p.provider == "anthropic"


def test_anthropic_cache_writes_follow_the_published_multipliers():
    # 1.25x input for a 5-minute write and 2x for a 1-hour write, on every model.
    for m, p in MODEL_PRICES.items():
        if p.provider != "anthropic":
            continue
        assert p.cache_write_5m == pytest.approx(p.input * 1.25), m
        assert p.cache_write_1h == pytest.approx(p.input * 2), m


def test_cache_reads_are_a_tenth_of_input_except_the_three_published_exceptions():
    exceptions = {"claude-fable-5-1": 0.025, "claude-mythos-5-1": 0.025, "claude-opus-5-5": 0.05}
    for m, p in MODEL_PRICES.items():
        if p.provider != "anthropic":
            continue
        assert p.cache_read == pytest.approx(p.input * exceptions.get(m, 0.1)), m


def test_openai_rows():
    assert (MODEL_PRICES["gpt-4o"].input, MODEL_PRICES["gpt-4o"].output) == (2.50, 10.00)
    assert MODEL_PRICES["gpt-4o"].cache_read == 1.25
    # o3 was carried at $10/$40 in openai_usage; it lists at $2/$8.
    assert (MODEL_PRICES["o3"].input, MODEL_PRICES["o3"].output) == (2.00, 8.00)
    assert MODEL_PRICES["o4-mini"].cache_read == 0.275
    for p in MODEL_PRICES.values():
        if p.provider == "openai":
            assert p.cache_write_5m is None and p.cache_write_1h is None


def test_the_table_says_when_and_where():
    assert date.fromisoformat(llm_prices.AS_OF)
    assert set(llm_prices.SOURCES) == {p.provider for p in MODEL_PRICES.values()}
    assert all(u.startswith("https://") for u in llm_prices.SOURCES.values())


# ── resolving the ids nable meets ────────────────────────────────────────────

@pytest.mark.parametrize("raw,canonical", [
    ("claude-opus-5-5", "claude-opus-5-5"),
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
    ("claude-opus-4-1-20250805", "claude-opus-4-1"),
    ("claude-opus-4-20250514", "claude-opus-4"),
    ("claude-opus-4-0", "claude-opus-4"),
    ("claude-opus-4-6[1m]", "claude-opus-4-6"),
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
    ("anthropic.claude-3-5-haiku-20241022-v1:0", "claude-3-5-haiku"),
    ("global.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5"),
    ("arn:aws:bedrock:us-east-1:000000000000:inference-profile/"
     "us.anthropic.claude-opus-4-1-20250805-v1:0", "claude-opus-4-1"),
    ("bedrock/anthropic.claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("claude-sonnet-4-5@20250929", "claude-sonnet-4-5"),
    ("Claude Sonnet 4.5 (Amazon Bedrock Edition)", "claude-sonnet-4-5"),
    ("Claude Haiku 4.5", "claude-haiku-4-5"),
    ("Claude 3.5 Haiku", "claude-3-5-haiku"),
    ("claude-haiku-3-5", "claude-3-5-haiku"),
    ("openai/gpt-4o", "gpt-4o"),
    ("gpt-3.5-turbo", "gpt-3.5-turbo"),
])
def test_canonical_model(raw, canonical):
    assert canonical_model(raw) == canonical
    assert price_for(raw) is MODEL_PRICES[canonical]


@pytest.mark.parametrize("raw", [
    # Not on the current Claude pricing page.
    "claude-3-opus-20240229", "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-haiku-20240307", "Claude 3 Opus",
    # A Claude model this table does not know. It must not borrow a sibling's
    # rate the way bedrock_routing's family match did.
    "claude-haiku-9-9",
    # OpenAI: unconfirmed, and a dated gpt-4o snapshot that is not the alias's price.
    "gpt-4-turbo", "gpt-4o-2024-05-13",
    "", "<synthetic>",
])
def test_unconfirmed_models_are_unpriced(raw):
    assert price_for(raw) is None


# ── cost ─────────────────────────────────────────────────────────────────────

def test_cost_prices_each_category_at_its_own_rate():
    p = MODEL_PRICES["claude-opus-5-5"]
    usd = p.cost(input_tokens=1_000_000, output_tokens=1_000_000,
                 cache_write_5m_tokens=1_000_000, cache_write_1h_tokens=1_000_000,
                 cache_read_tokens=1_000_000)
    assert usd == pytest.approx(4 + 20 + 5 + 8 + 0.20)


def test_fast_mode_uses_the_published_fast_rates_and_scales_cache():
    p = MODEL_PRICES["claude-opus-5"]
    assert p.cost(input_tokens=1_000_000, output_tokens=1_000_000, fast=True) == pytest.approx(60)
    # Caching multipliers apply on top of fast pricing: 1.25x of $10.
    assert p.cost(cache_write_5m_tokens=1_000_000, fast=True) == pytest.approx(12.50)
    # A model without fast mode is billed at standard rates.
    assert "claude-opus-4-7" not in FAST_MODE
    assert (MODEL_PRICES["claude-opus-4-7"].cost(output_tokens=1_000_000, fast=True)
            == pytest.approx(25))


def test_us_only_inference_is_ten_percent_on_everything():
    p = MODEL_PRICES["claude-sonnet-4-6"]
    base = p.cost(input_tokens=1000, output_tokens=1000, cache_read_tokens=1000)
    assert p.cost(input_tokens=1000, output_tokens=1000, cache_read_tokens=1000,
                  us_only=True) == pytest.approx(base * US_ONLY_MULTIPLIER)


def test_openai_cache_writes_bill_as_input_and_reads_at_the_cached_rate():
    p = MODEL_PRICES["gpt-4o"]
    assert p.cost(cache_write_5m_tokens=1_000_000) == pytest.approx(2.50)
    assert p.cost(cache_read_tokens=1_000_000) == pytest.approx(1.25)
    # No cached rate published: cached tokens bill as input.
    assert MODEL_PRICES["gpt-3.5-turbo"].cost(cache_read_tokens=1_000_000) == pytest.approx(0.50)
