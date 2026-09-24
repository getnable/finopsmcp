"""The OpenAI token-usage estimate prices from llm_prices.

When the Costs API is not available, openai_usage multiplies token counts by
published rates. The rates it carried had drifted (o3 at $10/$40 against a $2/$8
list) and every input token, cached or not, was billed at the full input rate.
"""
from __future__ import annotations

from datetime import date

import pytest

from finops.connectors.saas import openai_usage


def _estimate(monkeypatch, results):
    monkeypatch.setattr(openai_usage, "_get_all_buckets",
                        lambda *a, **k: [{"start_time": 1780272000, "results": results}])
    return openai_usage._estimate_from_usage(date(2026, 6, 1), date(2026, 6, 2), "k", None)


def test_the_connector_keeps_no_price_table_of_its_own():
    assert not hasattr(openai_usage, "_MODEL_PRICING")


def test_o3_is_priced_at_its_current_list_rate(monkeypatch):
    out = _estimate(monkeypatch, [
        {"model_id": "o3", "input_tokens": 1_000_000, "output_tokens": 1_000_000}])
    assert out["total_usd"] == pytest.approx(10.0)       # $2 + $8, not $10 + $40


def test_cached_input_is_billed_at_the_cached_rate(monkeypatch):
    out = _estimate(monkeypatch, [
        {"model_id": "gpt-4o", "input_tokens": 1_000_000, "input_cached_tokens": 600_000,
         "output_tokens": 0}])
    # 400k fresh at $2.50 + 600k cached at $1.25
    assert out["total_usd"] == pytest.approx(0.4 * 2.50 + 0.6 * 1.25)


def test_an_unconfirmed_model_is_listed_not_priced(monkeypatch):
    out = _estimate(monkeypatch, [
        {"model_id": "gpt-4-turbo", "input_tokens": 1000, "output_tokens": 10},
        {"model_id": "gpt-4o-mini", "input_tokens": 1_000_000, "output_tokens": 0}])
    assert out["total_usd"] == pytest.approx(0.15)
    assert out["unpriced_models"] == {"gpt-4-turbo": {"input_tokens": 1000, "output_tokens": 10}}
    assert "gpt-4-turbo" in out["note"]
