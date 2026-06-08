"""Savings recommendations must be computed from real pricing, not hardcoded %."""
from finops.connectors.llm_costs import _generate_recommendations


def _rec_for(model, spend):
    recs = _generate_recommendations({model: spend}, {})
    return next((r for r in recs if r["model"] == model), None)


def test_gpt4o_savings_from_real_prices():
    # gpt-4o blended 2.50+10.00=12.50; gpt-4o-mini 0.15+0.60=0.75 -> ~94% savings.
    r = _rec_for("gpt-4o", 1000.0)
    assert r is not None
    assert r["estimated_savings_pct"] == "94%"
    assert abs(r["estimated_savings_usd"] - 940.0) < 2.0  # 0.94 * 1000
    assert "gpt-4o-mini" in r["recommendation"]


def test_opus3_to_sonnet_savings():
    # opus-3 blended 15+75=90; 3.5-sonnet 3+15=18 -> 80% savings.
    r = _rec_for("claude-3-opus-20240229", 500.0)
    assert r is not None
    assert r["estimated_savings_pct"] == "80%"
    assert abs(r["estimated_savings_usd"] - 400.0) < 2.0


def test_noise_below_threshold_skipped():
    assert _generate_recommendations({"gpt-4o": 2.0}, {}) == []


def test_bedrock_prefixed_id_still_matches():
    # Provider-prefixed ids must still match the downgrade table.
    recs = _generate_recommendations({"bedrock/anthropic.claude-3-opus-20240229": 300.0}, {})
    assert any("claude-3-5-sonnet" in r["recommendation"] for r in recs)


def test_bedrock_sku_display_name_sonnet_to_haiku():
    # Cost Explorer reports Bedrock spend as SKU display names ("Claude Sonnet
    # 4.5": spaces + dots, no model-id string). These must normalize to the
    # canonical id and fire a Sonnet -> Haiku rec for Bedrock-only users.
    r = _rec_for("Claude Sonnet 4.5", 3224.0)
    assert r is not None
    assert "claude-haiku-3-5" in r["recommendation"]
    # sonnet blended 3+15=18, haiku-3-5 blended 0.8+4=4.8 -> ~73% savings.
    assert r["estimated_savings_pct"] == "73%"
    expected = 3224.0 * (1 - 4.8 / 18.0)
    assert abs(r["estimated_savings_usd"] - expected) < 5.0
    assert r["estimated_savings_usd"] > 0
    # Honesty/basis labeling is preserved.
    assert "price-ratio estimate" in r["basis"]


def test_bedrock_sku_display_name_sonnet_4_6_also_matches():
    r = _rec_for("Claude Sonnet 4.6", 100.0)
    assert r is not None
    assert "claude-haiku-3-5" in r["recommendation"]
    assert r["estimated_savings_usd"] > 0


def test_unknown_model_no_crash_no_rec():
    assert _generate_recommendations({"some-unpriced-model-x": 100.0}, {}) == []
