"""Regression tests for the AI cost optimizer synthesis layer.

Pure-function tests with synthetic inputs (no network), mirroring the shapes
produced by get_all_llm_costs() and full_kpi_report().
"""
from finops.analytics.ai_optimizer import build_optimization_plan


def _llm():
    return {
        "total_usd": 1000.0,
        "by_provider": {"bedrock": 600.0, "anthropic": 300.0, "openai": 100.0},
        "by_model": {
            "claude-opus-4-20250514": 500.0,
            "claude-3-5-haiku-20241022": 200.0,
            "gpt-4o": 200.0,
            "gpt-4o-mini": 100.0,
        },
        "daily": [],
        "model_efficiency": [
            {"model": "claude-opus-4-20250514", "provider": "anthropic",
             "cost_usd": 500.0, "input_price_per_1m": 15.0, "output_price_per_1m": 75.0},
            {"model": "gpt-4o", "provider": "openai",
             "cost_usd": 200.0, "input_price_per_1m": 2.5, "output_price_per_1m": 10.0},
        ],
        "top_spenders": [],
        "recommendations": [
            {"model": "claude-opus-4-20250514", "current_spend": 500.0,
             "recommendation": "Consider claude-sonnet-4-5-20250929 for lower-complexity tasks",
             "estimated_savings_pct": "70%", "estimated_savings_usd": 350.0, "basis": "price-ratio"},
            {"model": "gpt-4o", "current_spend": 200.0,
             "recommendation": "Consider gpt-4o-mini for lower-complexity tasks",
             "estimated_savings_pct": "94%", "estimated_savings_usd": 188.0, "basis": "price-ratio"},
        ],
    }


def _kpi():
    return {
        "cache_hit_rate": {
            "cache_reads": 100_000, "fresh_input_tokens": 4_000_000,
            "hit_rate_pct": 2.4, "estimated_savings_usd": 0.5, "grade": "F",
            "recommendation": "Enable prompt caching.",
        },
        "prompt_efficiency": {"by_model": {
            # opus is verbose AND in the routing recs, so its output savings must be skipped
            "claude-opus-4-20250514": {"ratio": 4.5, "input_tokens": 1_000_000,
                                       "output_tokens": 4_500_000, "signal": "verbose",
                                       "recommendation": "Trim output."},
        }},
        "error_spend": {"error_rate_pct": 5.0, "estimated_wasted_usd": 50.0,
                        "recommendation": "Add retries."},
        "model_sprawl": {"model_count": 6, "hhi": 3000.0, "concentration": "medium",
                         "flags": ["6 distinct models in use."], "recommendations": ["Consolidate."],
                         "model_shares": {}},
        "context_window_utilization": {"by_model": {}, "low_utilization_models": []},
        "total_estimated_savings_usd": 0.5,
    }


def test_routing_ceiling_is_upside_not_addressable():
    plan = build_optimization_plan(_llm(), _kpi(), days=30)

    cats = [l["category"] for l in plan["levers"]]
    assert "model_routing" in cats
    assert "prompt_caching" in cats
    assert "error_reduction" in cats

    routing = next(l for l in plan["levers"] if l["category"] == "model_routing")
    # Routing is a ceiling: low confidence, reported as upside, never in the headline.
    assert routing["confidence"] == "low"
    assert routing["monthly_savings_usd"] == 538.0
    assert plan["potential_upside_monthly_usd"] == 538.0

    # Addressable = only the realizable medium/high levers (caching 27 + error 50).
    assert plan["addressable_savings_monthly_usd"] == 77.0
    assert plan["addressable_savings_monthly_usd"] < routing["monthly_savings_usd"]

    # Realizable dollar levers rank ahead of the ceiling.
    first = plan["levers"][0]
    assert first["confidence"] in ("high", "medium")
    idx = {l["category"]: i for i, l in enumerate(plan["levers"])}
    assert idx["error_reduction"] < idx["model_routing"]
    assert idx["prompt_caching"] < idx["model_routing"]


def test_output_trim_skipped_for_routed_models_no_double_count():
    plan = build_optimization_plan(_llm(), _kpi(), days=30)
    # opus is the only verbose model and it already has a routing rec, so there
    # must be no output_reduction lever (would double-count its spend).
    assert "output_reduction" not in [l["category"] for l in plan["levers"]]


def test_routing_table_and_spend_shape():
    plan = build_optimization_plan(_llm(), _kpi(), days=30)
    assert len(plan["routing_table"]) == 2
    row = plan["routing_table"][0]
    assert row["recommended_model"] == "claude-sonnet-4-5-20250929"
    assert row["quality_risk"] == "low"

    # Output is 4.5M / 5.5M = 82% of token volume, so output_tokens drives the bill.
    assert plan["spend_shape"]["primary_driver"] == "output_tokens"
    assert plan["spend_shape"]["output_token_share_pct"] > 50


def test_monthly_normalization_scales_for_short_window():
    # Same period savings over 15 days should roughly double when normalized to a month.
    plan = build_optimization_plan(_llm(), _kpi(), days=15)
    routing = next(l for l in plan["levers"] if l["category"] == "model_routing")
    assert routing["monthly_savings_usd"] == round((350.0 + 188.0) * 30 / 15, 2)


def test_bedrock_sku_named_sonnet_gets_audit_lever_no_invented_dollars():
    # Mirrors the real Cost-Explorer Bedrock shape: SKU display names, no token
    # data, no model_efficiency, so the price-ratio recs cannot match.
    llm = {
        "total_usd": 3744.0,
        "by_provider": {"bedrock": 3744.0},
        "by_model": {"Claude Sonnet 4.5": 3224.0, "Claude Sonnet 4.6": 520.0},
        "daily": [],
        "model_efficiency": [],
        "top_spenders": [],
        "recommendations": [],
    }
    plan = build_optimization_plan(llm, {}, days=30)
    routing = [l for l in plan["levers"] if l["category"] == "model_routing"]
    assert len(routing) == 1
    lever = routing[0]
    # Opportunity is flagged but no dollar figure is invented for it.
    assert lever["monthly_savings_usd"] is None
    assert "recommend_bedrock_model_routing" in lever["action"]
    assert plan["addressable_savings_monthly_usd"] == 0.0
    # Spend shape should call out model choice, not "mixed".
    assert plan["spend_shape"]["primary_driver"] == "model_choice"
    assert plan["spend_shape"]["downgradeable_tier_share_pct"] == 100.0


def test_bedrock_input_heavy_uncached_gets_quantified_caching_lever():
    # Mirrors the real account: 88% input, no cache usage, SKU-named models.
    llm = {
        "total_usd": 2845.0,
        "by_provider": {"bedrock": 2845.0},
        "by_model": {"Claude Sonnet 4.5": 2499.0, "Claude Sonnet 4.6": 346.0},
        "daily": [], "model_efficiency": [], "top_spenders": [], "recommendations": [],
    }
    bedrock_split = {
        "input_cost": 2514.0, "output_cost": 331.0,
        "cache_read_cost": 0.0, "cache_write_cost": 0.0, "other_cost": 0.0,
        "total": 2845.0, "input_share_pct": 88.4, "output_share_pct": 11.6,
        "caching_active": False, "by_model": {},
    }
    plan = build_optimization_plan(llm, {}, days=30, bedrock_split=bedrock_split)

    caching = [l for l in plan["levers"] if l["category"] == "prompt_caching"]
    assert len(caching) == 1
    lever = caching[0]
    # A real, conservative number now appears (30% of input at ~90% off).
    assert lever["monthly_savings_usd"] == round(2514.0 * 0.30 * 0.9, 2)
    assert lever["confidence"] == "medium"
    assert plan["addressable_savings_monthly_usd"] >= 600.0
    # Spend shape should call out the input-heavy uncached driver.
    assert plan["spend_shape"]["primary_driver"] == "input_tokens_uncached"
    assert plan["spend_shape"]["bedrock_input_cost_share_pct"] == 88.4
    # The caching lever (with a dollar value) outranks the no-dollar audit lever.
    assert plan["levers"][0]["category"] == "prompt_caching"


def test_bedrock_caching_skipped_when_already_caching():
    llm = {"total_usd": 1000.0, "by_provider": {"bedrock": 1000.0},
           "by_model": {"Claude Sonnet 4.5": 1000.0}, "daily": [],
           "model_efficiency": [], "top_spenders": [], "recommendations": []}
    bedrock_split = {"input_cost": 700.0, "output_cost": 100.0, "cache_read_cost": 200.0,
                     "cache_write_cost": 0.0, "other_cost": 0.0, "total": 1000.0,
                     "input_share_pct": 70.0, "output_share_pct": 10.0,
                     "caching_active": True, "by_model": {}}
    plan = build_optimization_plan(llm, {}, days=30, bedrock_split=bedrock_split)
    caching = [l for l in plan["levers"] if l["category"] == "prompt_caching"]
    # Already caching: no invented dollar figure, low-confidence widen-coverage note only.
    assert len(caching) == 1
    assert caching[0]["monthly_savings_usd"] is None


def test_no_spend_returns_connect_prompt():
    plan = build_optimization_plan({"total_usd": 0.0, "by_model": {}}, {}, days=30)
    assert plan["addressable_savings_monthly_usd"] == 0.0
    assert plan["levers"] == []
    assert plan["routing_table"] == []
    assert any("Connect a provider" in n for n in plan["notes"])
