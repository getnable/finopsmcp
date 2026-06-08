"""
AI cost optimizer.

Fuses the LLM cost breakdown (``get_all_llm_costs``) and the AI KPI report
(``full_kpi_report``) into one ranked, dollar-quantified optimization plan, and
decomposes the bill into its real driver: model choice, token size (especially
output), or request volume.

This is OpenRouter-style intelligence (the cheapest way to get the same output)
delivered as read-only recommendations, not a runtime proxy. nable never sits in
the request path.

Dollar discipline: a lever only carries ``monthly_savings_usd`` when it has a
grounded basis. The routing numbers come from real price ratios, error spend is
measured, and the cache estimate is computed from actual token counts. Governance
levers (model sprawl, context bloat) are listed for action but never inflate the
headline number. Output-trim savings are skipped for any model that already has a
routing recommendation, so nothing is counted twice.

All functions here are pure. The server tool fetches the data and calls
``build_optimization_plan``.
"""
from __future__ import annotations

from typing import Any

# Clearly top-tier (expensive) models. Mid-tier models like gpt-4o and Sonnet are
# left out on purpose so the premium share is not overstated; routing recs already
# handle Sonnet-to-Haiku style swaps where they apply.
_PREMIUM_KEYWORDS = (
    "opus", "gpt-4-turbo", "gpt-4.5", "o1", "o3-pro",
    "gemini-1.5-pro", "gemini-2.0-pro", "gemini-2.5-pro",
)

# Models that typically have a materially cheaper sibling for simpler tasks
# (Sonnet to Haiku, Opus to Sonnet, gpt-4o to gpt-4o-mini). Used to flag the
# model_choice driver when a routing opportunity exists.
_DOWNGRADEABLE_KEYWORDS = _PREMIUM_KEYWORDS + (
    "sonnet", "gpt-4o", "gpt-4.1",
)

# Anthropic cache reads cost about 10% of the normal input price, so a cache hit
# saves roughly 90% on the cached portion.
_CACHE_SAVINGS_FRACTION = 0.9
# Conservative share of fresh input assumed to be static, repeated, cacheable text.
_CACHEABLE_FRACTION = 0.5
# Conservative output reduction from adding max_tokens limits and concise instructions.
_OUTPUT_TRIM_FRACTION = 0.25

_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def _monthly(value: float, days: int) -> float:
    """Normalize a period total to a 30-day month."""
    if not value or days <= 0:
        return 0.0
    return value * 30.0 / days


def _is_premium(model: str) -> bool:
    m = model.lower()
    return any(k in m for k in _PREMIUM_KEYWORDS)


def _is_downgradeable(model: str) -> bool:
    m = model.lower()
    return any(k in m for k in _DOWNGRADEABLE_KEYWORDS)


def _anthropic_input_price(model_efficiency: list[dict[str, Any]]) -> float | None:
    """Input price per 1M tokens of the costliest configured Anthropic model."""
    anthropic = [
        e for e in model_efficiency
        if (e.get("provider") == "anthropic" or "claude" in str(e.get("model", "")).lower())
        and e.get("input_price_per_1m")
    ]
    if not anthropic:
        return None
    top = max(anthropic, key=lambda e: e.get("cost_usd", 0.0))
    return float(top["input_price_per_1m"])


def _output_price(model: str, model_efficiency: list[dict[str, Any]]) -> float | None:
    for e in model_efficiency:
        if e.get("model") == model and e.get("output_price_per_1m"):
            return float(e["output_price_per_1m"])
    return None


def _target_model(rec_text: str) -> str | None:
    """Best-effort pull of the suggested model out of a recommendation string
    like 'Consider gpt-4o-mini for lower-complexity tasks'."""
    if "Consider " not in rec_text:
        return None
    after = rec_text.split("Consider ", 1)[1]
    return after.split(" for ", 1)[0].strip() or None


def _routing_lever(llm: dict[str, Any], days: int) -> tuple[dict | None, list[dict]]:
    """Build the model-routing lever and a per-model routing table from the
    already dollar-quantified recommendations in the cost breakdown."""
    recs = llm.get("recommendations") or []
    eff = llm.get("model_efficiency") or []
    provider_of = {e.get("model"): e.get("provider") for e in eff}

    table: list[dict] = []
    period_savings = 0.0
    for r in recs:
        save = float(r.get("estimated_savings_usd") or 0.0)
        if save <= 0:
            continue
        period_savings += save
        table.append({
            "model": r.get("model"),
            "provider": provider_of.get(r.get("model")),
            "current_monthly_usd": round(_monthly(float(r.get("current_spend") or 0.0), days), 2),
            "recommended_model": _target_model(r.get("recommendation", "")),
            "monthly_savings_usd": round(_monthly(save, days), 2),
            "quality_risk": "low",
        })

    if not table:
        return None, []

    table.sort(key=lambda t: t["monthly_savings_usd"], reverse=True)
    monthly = round(_monthly(period_savings, days), 2)
    swaps = ", ".join(
        f"{t['model']} to {t['recommended_model']}"
        for t in table[:3] if t.get("recommended_model")
    )
    lever = {
        "category": "model_routing",
        "title": f"Route {len(table)} workload(s) to cheaper models",
        "monthly_savings_usd": monthly,
        "confidence": "high",
        "action": f"Move lower-complexity calls off premium models: {swaps}." if swaps
                  else "Move lower-complexity calls to the cheaper sibling model in each pair.",
        "basis": "Blended input+output price ratios between the current and cheaper model. "
                 "Actual savings track your input/output mix.",
    }
    return lever, table


def _caching_lever(kpi: dict[str, Any], llm: dict[str, Any], days: int) -> dict | None:
    cache = kpi.get("cache_hit_rate") or {}
    fresh = cache.get("fresh_input_tokens")
    hit = cache.get("hit_rate_pct")
    if not fresh or hit is None or hit >= 70:
        return None
    price = _anthropic_input_price(llm.get("model_efficiency") or [])
    savings = None
    if price:
        cacheable = fresh * _CACHEABLE_FRACTION
        period = cacheable / 1_000_000 * price * _CACHE_SAVINGS_FRACTION
        savings = round(_monthly(period, days), 2)
    return {
        "category": "prompt_caching",
        "title": f"Raise prompt cache hit rate (currently {hit:.0f}%)",
        "monthly_savings_usd": savings,
        "confidence": "medium",
        "action": "Add cache_control to your large static system prompts and keep them "
                  "byte-stable so repeated input bills at ~10% of the input price.",
        "basis": "Assumes about half of fresh input is static and cacheable. Anthropic cache "
                 "reads cost ~10% of input price." if savings is not None
                 else "Hit rate is low; connect Anthropic pricing to quantify the savings.",
    }


def _output_lever(kpi: dict[str, Any], llm: dict[str, Any], days: int,
                  routed_models: set[str]) -> dict | None:
    pe = (kpi.get("prompt_efficiency") or {}).get("by_model") or {}
    eff = llm.get("model_efficiency") or []
    verbose = []
    period_savings = 0.0
    have_price = False
    for model, d in pe.items():
        if d.get("signal") != "verbose":
            continue
        if model in routed_models:
            # Routing this model supersedes trimming its output; do not double count.
            continue
        verbose.append(model)
        out_price = _output_price(model, eff)
        out_tokens = d.get("output_tokens") or 0
        if out_price and out_tokens:
            have_price = True
            out_spend = out_tokens / 1_000_000 * out_price
            period_savings += out_spend * _OUTPUT_TRIM_FRACTION
    if not verbose:
        return None
    return {
        "category": "output_reduction",
        "title": f"Trim verbose output on {len(verbose)} model(s)",
        "monthly_savings_usd": round(_monthly(period_savings, days), 2) if have_price else None,
        "confidence": "medium" if have_price else "low",
        "action": "Set max_tokens limits and instruct concise responses on: "
                  + ", ".join(verbose[:4]) + ". Output tokens are the pricier side of the bill.",
        "basis": f"Output-to-input ratio above 3x; assumes a {int(_OUTPUT_TRIM_FRACTION*100)}% "
                 "output reduction." if have_price
                 else "Output-to-input ratio above 3x. Connect model pricing to quantify.",
    }


def _error_lever(kpi: dict[str, Any], days: int) -> dict | None:
    err = kpi.get("error_spend") or {}
    wasted = err.get("estimated_wasted_usd")
    rate = err.get("error_rate_pct")
    if not wasted or wasted <= 1.0:
        return None
    return {
        "category": "error_reduction",
        "title": f"Cut spend on failed requests ({rate:.0f}% error rate)" if rate is not None
                 else "Cut spend on failed requests",
        "monthly_savings_usd": round(_monthly(float(wasted), days), 2),
        "confidence": "medium",
        "action": "Add retry with exponential backoff and validate inputs before the call so "
                  "failed requests stop burning tokens.",
        "basis": "Measured error rate applied to spend over the period.",
    }


def _sprawl_lever(kpi: dict[str, Any]) -> dict | None:
    sprawl = kpi.get("model_sprawl") or {}
    flags = sprawl.get("flags") or []
    if not flags:
        return None
    return {
        "category": "model_consolidation",
        "title": f"Consolidate model sprawl ({sprawl.get('model_count', 0)} models in use)",
        "monthly_savings_usd": None,
        "confidence": "low",
        "action": "Standardize on three tiers: a small model for classification/routing, a "
                  "mid-tier for generation, a frontier model for reasoning. " + flags[0],
        "basis": "Governance and indirect cost; not directly quantified.",
    }


def _bedrock_audit_lever(llm: dict[str, Any], routed_models: set) -> dict | None:
    """Bedrock Cost Explorer reports model SKU display names (e.g. 'Claude Sonnet
    4.5') with no token detail, so the price-ratio routing recs never match. When
    Bedrock Claude Sonnet/Opus spend is present and uncovered, flag the routing
    opportunity and point at the per-function analyzer for the quantified number.
    We do not invent a savings figure here."""
    by_provider = llm.get("by_provider", {}) or {}
    if not by_provider.get("bedrock"):
        return None
    by_model = llm.get("by_model", {}) or {}
    candidates = [
        m for m, v in by_model.items()
        if v > 0 and m not in routed_models
        and ("sonnet" in m.lower() or "opus" in m.lower())
    ]
    if not candidates:
        return None
    top = max(candidates, key=lambda m: by_model[m])
    return {
        "category": "model_routing",
        "title": f"Audit Bedrock Claude workloads for cheaper-model routing (top: {top})",
        "monthly_savings_usd": None,
        "confidence": "medium",
        "action": "Classification, extraction, and short-context calls rarely need Sonnet and "
                  "run on Haiku at a fraction of the price (Opus calls can drop to Sonnet). Run "
                  "recommend_bedrock_model_routing for the per-function breakdown and the "
                  "quantified monthly savings.",
        "basis": "Bedrock Cost Explorer reports SKU names without token-level data. The "
                 "per-function analyzer adds CloudWatch invocation metrics to size the eligible "
                 "share, so the dollar figure comes from that tool, not this estimate.",
    }


def _spend_shape(llm: dict[str, Any], kpi: dict[str, Any]) -> dict[str, Any]:
    by_model = llm.get("by_model", {}) or {}
    total = sum(by_model.values()) or float(llm.get("total_usd", 0.0) or 0.0)
    premium = sum(v for m, v in by_model.items() if _is_premium(m))
    premium_pct = round(premium / total * 100, 1) if total else 0.0
    downgradeable = sum(v for m, v in by_model.items() if _is_downgradeable(m))
    downgradeable_pct = round(downgradeable / total * 100, 1) if total else 0.0

    pe = (kpi.get("prompt_efficiency") or {}).get("by_model") or {}
    in_tok = sum(d.get("input_tokens", 0) for d in pe.values())
    out_tok = sum(d.get("output_tokens", 0) for d in pe.values())
    tok_total = in_tok + out_tok
    out_pct = round(out_tok / tok_total * 100, 1) if tok_total else None

    hit = (kpi.get("cache_hit_rate") or {}).get("hit_rate_pct")

    if premium_pct >= 55:
        driver = "model_choice"
        headline = (f"{premium_pct:.0f}% of the bill is premium-tier models. The biggest lever "
                    "is routing simpler tasks to cheaper models.")
    elif out_pct is not None and out_pct >= 55:
        driver = "output_tokens"
        headline = (f"Output is {out_pct:.0f}% of token volume and the pricier side. The biggest "
                    "lever is tightening response length.")
    elif hit is not None and hit < 50 and tok_total:
        driver = "input_tokens_uncached"
        headline = (f"Input dominates and cache hit rate is {hit:.0f}%. The biggest lever is "
                    "prompt caching.")
    elif downgradeable_pct >= 55:
        driver = "model_choice"
        headline = (f"{downgradeable_pct:.0f}% of spend runs on models with a cheaper sibling "
                    "(Sonnet to Haiku, Opus to Sonnet). Routing simpler calls is the biggest lever.")
    else:
        driver = "mixed"
        headline = "No single driver dominates. Apply the ranked levers below in order."

    shape = {
        "primary_driver": driver,
        "headline": headline,
        "premium_model_share_pct": premium_pct,
        "downgradeable_tier_share_pct": downgradeable_pct,
        "output_token_share_pct": out_pct,
    }
    if out_pct is None:
        shape["note"] = ("Token-level split is unavailable. Connect an Anthropic or OpenAI admin "
                         "key for input/output and cache analysis.")
    return shape


def build_optimization_plan(
    llm_costs_result: dict[str, Any],
    kpi_report: dict[str, Any] | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Build the ranked AI cost optimization plan.

    Parameters
    ----------
    llm_costs_result:
        Output of ``get_all_llm_costs``.
    kpi_report:
        Output of ``full_kpi_report``. Optional; routing still works without it.
    days:
        Lookback window the inputs cover, used to normalize savings to a month.
    """
    kpi_report = kpi_report or {}
    total = float(llm_costs_result.get("total_usd", 0.0) or 0.0)
    monthly_total = round(_monthly(total, days), 2)

    if total <= 0 and not (llm_costs_result.get("by_model")):
        return {
            "period_days": days,
            "ai_spend_usd": 0.0,
            "ai_spend_monthly_usd": 0.0,
            "addressable_savings_monthly_usd": 0.0,
            "addressable_savings_pct": 0.0,
            "spend_shape": {"primary_driver": "none", "headline": "No AI spend detected."},
            "levers": [],
            "routing_table": [],
            "top_3": [],
            "notes": [
                "No AI/LLM spend found. Connect a provider (OpenAI, Anthropic, AWS Bedrock, "
                "Azure OpenAI, or Vertex) to get an optimization plan.",
            ],
        }

    routing_lever, routing_table = _routing_lever(llm_costs_result, days)
    routed_models = {r.get("model") for r in (llm_costs_result.get("recommendations") or [])}

    levers: list[dict] = []
    if routing_lever:
        levers.append(routing_lever)
    for lever in (
        _bedrock_audit_lever(llm_costs_result, routed_models),
        _caching_lever(kpi_report, llm_costs_result, days),
        _output_lever(kpi_report, llm_costs_result, days, routed_models),
        _error_lever(kpi_report, days),
        _sprawl_lever(kpi_report),
    ):
        if lever:
            levers.append(lever)

    def _sort_key(l: dict):
        s = l.get("monthly_savings_usd")
        if s is None:
            return (1, _CONFIDENCE_RANK.get(l.get("confidence", "low"), 3), 0.0)
        return (0, 0, -s)

    levers.sort(key=_sort_key)

    addressable = round(sum(
        l["monthly_savings_usd"] for l in levers
        if l.get("monthly_savings_usd") is not None
    ), 2)

    notes = [
        "Savings are independent estimates. Caching and routing can overlap on the same "
        "model, so treat the total as a directional ceiling, not a guarantee.",
    ]

    return {
        "period_days": days,
        "ai_spend_usd": round(total, 2),
        "ai_spend_monthly_usd": monthly_total,
        "addressable_savings_monthly_usd": addressable,
        "addressable_savings_pct": round(addressable / monthly_total * 100, 1) if monthly_total else 0.0,
        "spend_shape": _spend_shape(llm_costs_result, kpi_report),
        "levers": levers,
        "routing_table": routing_table,
        "top_3": [l["title"] for l in levers[:3]],
        "notes": notes,
    }
