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


def test_plan_ranks_levers_and_totals_only_grounded_numbers():
    plan = build_optimization_plan(_llm(), _kpi(), days=30)

    cats = [l["category"] for l in plan["levers"]]
    assert "model_routing" in cats
    assert "prompt_caching" in cats
    assert "error_reduction" in cats

    # Routing is the biggest lever (538/mo) and must sort first.
    assert plan["levers"][0]["category"] == "model_routing"
    assert plan["levers"][0]["monthly_savings_usd"] == 538.0

    # Levers with a dollar value come before the null-savings governance lever.
    savings = [l.get("monthly_savings_usd") for l in plan["levers"]]
    numeric = [s for s in savings if s is not None]
    assert savings[: len(numeric)] == numeric  # all numerics first
    assert numeric == sorted(numeric, reverse=True)

    # Addressable total only sums grounded levers and is >= the routing+error floor.
    assert plan["addressable_savings_monthly_usd"] >= 538.0 + 50.0
    # Sprawl lever has no number, so it cannot be in the total.
    assert plan["addressable_savings_monthly_usd"] == round(sum(numeric), 2)


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


def test_no_spend_returns_connect_prompt():
    plan = build_optimization_plan({"total_usd": 0.0, "by_model": {}}, {}, days=30)
    assert plan["addressable_savings_monthly_usd"] == 0.0
    assert plan["levers"] == []
    assert plan["routing_table"] == []
    assert any("Connect a provider" in n for n in plan["notes"])
