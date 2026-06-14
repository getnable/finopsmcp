"""
AI cost KPIs — the metrics that matter beyond raw spend.

Surfaces optimisation opportunities with estimated monthly savings for each,
framed for engineering and finance audiences.
"""
from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger(__name__)

# Max context window sizes (tokens) per model — used for utilisation analysis
_CONTEXT_WINDOWS: dict[str, int] = {
    # OpenAI
    "gpt-4o":                    128_000,
    "gpt-4o-2024-11-20":         128_000,
    "gpt-4o-mini":               128_000,
    "gpt-4o-mini-2024-07-18":    128_000,
    "o1":                        128_000,
    "o1-mini":                   128_000,
    "o3":                        128_000,
    "o3-mini":                   128_000,
    "o4-mini":                   128_000,
    "gpt-4-turbo":                128_000,
    "gpt-4-turbo-preview":       128_000,
    "gpt-3.5-turbo":              16_385,
    # Anthropic
    "claude-3-7-sonnet-20250219": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-5-sonnet-20240620": 200_000,
    "claude-3-5-haiku-20241022":  200_000,
    "claude-3-opus-20240229":     200_000,
    "claude-3-sonnet-20240229":   200_000,
    "claude-3-haiku-20240307":    200_000,
    "claude-2.1":                 200_000,
    "claude-2.0":                 100_000,
    "claude-3-5-sonnet-latest":   200_000,
    "claude-3-5-haiku-latest":    200_000,
    "claude-3-opus-latest":       200_000,
    # Vertex AI
    "gemini-1.5-pro":           1_000_000,
    "gemini-1.5-pro-long":      1_000_000,
    "gemini-1.5-flash":         1_000_000,
    "gemini-1.5-flash-long":    1_000_000,
    "gemini-1.0-pro":              32_768,
    "gemini-2.0-flash":         1_000_000,
    "gemini-2.0-pro":           1_000_000,
    # Default fallback
    "_default":                  128_000,
}

# Anthropic cache pricing: cache reads cost ~10% of normal input
_CACHE_READ_DISCOUNT = 0.10


# ---------------------------------------------------------------------------
# 1. Cache hit rate
# ---------------------------------------------------------------------------

def cache_hit_rate(anthropic_data: dict[str, Any]) -> dict[str, Any]:
    """
    Compute Anthropic prompt-cache effectiveness.

    Anthropic returns ``cache_read_input_tokens`` and
    ``cache_creation_input_tokens`` in the usage response.  This function
    accumulates those across all model entries in ``by_model_tokens`` and
    computes:

      hit_rate = cache_reads / (cache_reads + fresh_input_tokens)

    Also estimates savings: cache reads cost 10% of normal input price.

    Parameters
    ----------
    anthropic_data:
        Dict returned by ``anthropic_usage.get_costs()``.

    Returns
    -------
    {
        cache_reads:          int
        cache_creations:      int
        fresh_input_tokens:   int
        hit_rate_pct:         float   (0–100)
        estimated_savings_usd: float
        grade:                "A" | "B" | "C" | "D" | "F"
        recommendation:       str
    }
    """
    tokens = anthropic_data.get("by_model_tokens", {})

    total_cache_reads  = 0
    total_cache_create = 0
    total_fresh_input  = 0
    savings_usd        = 0.0

    for model, tok in tokens.items():
        reads   = tok.get("cache_read_input_tokens", 0)
        creates = tok.get("cache_creation_input_tokens", 0)
        fresh   = tok.get("input_tokens", 0)

        total_cache_reads  += reads
        total_cache_create += creates
        total_fresh_input  += fresh

        # Estimate savings from cache reads
        try:
            from ..connectors.saas.anthropic_usage import _MODEL_PRICING
            pricing     = _MODEL_PRICING.get(model, {"input": 3.00, "output": 15.00})
            input_price = pricing["input"]  # per 1M tokens
        except Exception:
            input_price = 3.00

        # Full price would have been: reads * input_price / 1M
        # We paid:                    reads * input_price * 0.10 / 1M
        savings_usd += reads / 1_000_000 * input_price * (1 - _CACHE_READ_DISCOUNT)

    denominator = total_cache_reads + total_fresh_input
    hit_rate    = (total_cache_reads / denominator * 100) if denominator > 0 else 0.0

    if hit_rate >= 60:
        grade = "A"
        rec   = "Excellent cache utilisation. Keep system prompts stable and use cache_control."
    elif hit_rate >= 40:
        grade = "B"
        rec   = "Good caching. Consider adding cache_control breakpoints to more prompts."
    elif hit_rate >= 20:
        grade = "C"
        rec   = (
            "Moderate caching. Use the 'cache_control' parameter on large system prompts "
            "and shared context to increase hit rate."
        )
    elif hit_rate > 0:
        grade = "D"
        rec   = (
            "Low cache utilisation. Add cache_control to static system prompts. "
            "Ensure conversation context is structured to maximise cache hits."
        )
    else:
        grade = "F"
        rec   = (
            "No cache hits detected. Enable prompt caching via 'cache_control' on "
            "system prompts and long context blocks. Potential savings: up to 90% on "
            "cached tokens."
        )

    return {
        "cache_reads":           total_cache_reads,
        "cache_creations":       total_cache_create,
        "fresh_input_tokens":    total_fresh_input,
        "hit_rate_pct":          round(hit_rate, 2),
        "estimated_savings_usd": round(savings_usd, 4),
        "grade":                 grade,
        "recommendation":        rec,
    }


# ---------------------------------------------------------------------------
# 2. Context window utilisation
# ---------------------------------------------------------------------------

def context_window_utilization(usage_data: dict[str, Any]) -> dict[str, Any]:
    """
    Compute average context window utilisation per model.

    Flags models where the average input size is less than 10% of the
    model's maximum context window — a sign you're paying for premium
    context capacity that you're not using.

    Parameters
    ----------
    usage_data:
        Dict with a ``by_model_tokens`` key (same shape as returned by
        ``anthropic_usage.get_costs()`` or aggregated across providers).

    Returns
    -------
    {
        by_model: {
            "model-name": {
                avg_input_tokens:  int,
                max_context:       int,
                utilization_pct:   float,
                flag:              bool,
                recommendation:    str,
            }
        },
        low_utilization_models: list[str],
    }
    """
    tokens_map = usage_data.get("by_model_tokens", {})
    # Also accept flat by_model dict (from aggregated data — tokens estimated)
    by_model_result: dict[str, dict[str, Any]] = {}
    low_util_models: list[str] = []

    for model, tok in tokens_map.items():
        if not isinstance(tok, dict):
            # tok is a cost float (aggregated data without token detail)
            continue
        input_tokens = tok.get("input_tokens", 0)
        request_count = tok.get("request_count", 0)

        if request_count <= 0:
            # No per-request data. The Anthropic Usage API returns token totals, not
            # request counts, so dividing the whole-period token sum by 1 would report
            # a meaningless thousands-of-percent figure and falsely label it "healthy".
            # Surface that it is unavailable instead of guessing. OpenAI carries
            # num_model_requests, so its models still compute a real average.
            by_model_result[model] = {
                "note": "per-request data unavailable; context-window utilisation not computed",
            }
            continue

        avg_input   = input_tokens / request_count
        max_context = _CONTEXT_WINDOWS.get(model, _CONTEXT_WINDOWS["_default"])
        util_pct    = avg_input / max_context * 100

        flag = util_pct < 10.0
        if flag:
            low_util_models.append(model)

        if flag:
            # Find a smaller-context model recommendation
            if max_context >= 1_000_000:
                rec = (
                    f"You're using ~{util_pct:.1f}% of {model}'s 1M-token context. "
                    f"Consider gemini-1.0-pro or a smaller model for tasks with short context."
                )
            elif max_context >= 200_000:
                rec = (
                    f"You're using ~{util_pct:.1f}% of {model}'s 200K context. "
                    f"Haiku or GPT-4o-mini may handle these requests at a fraction of the cost."
                )
            else:
                rec = (
                    f"Low context utilisation ({util_pct:.1f}%) — consider a cheaper or "
                    f"smaller-context model."
                )
        else:
            rec = f"Context utilisation is healthy ({util_pct:.1f}%)."

        by_model_result[model] = {
            "avg_input_tokens":  round(avg_input),
            "max_context":       max_context,
            "utilization_pct":   round(util_pct, 2),
            "flag":              flag,
            "recommendation":    rec,
        }

    return {
        "by_model":               by_model_result,
        "low_utilization_models": low_util_models,
    }


# ---------------------------------------------------------------------------
# 3. Model sprawl score
# ---------------------------------------------------------------------------

def model_sprawl_score(by_model: dict[str, float]) -> dict[str, Any]:
    """
    Quantify AI model proliferation and concentration risk.

    Computes:
      - Number of distinct models in use
      - Herfindahl-Hirschman Index (HHI) of cost concentration
        (HHI = sum of squared market shares; 10000 = monopoly, <1500 = competitive)
      - Flags if >5 models are in use (governance / security risk)
      - Flags if a cheap model dominates requests but an expensive one handles the rest

    Parameters
    ----------
    by_model:
        Dict of {model_name: total_cost_usd}.

    Returns
    -------
    {
        model_count:     int,
        hhi:             float   (0–10000),
        concentration:   "high" | "medium" | "low",
        flags:           list[str],
        recommendations: list[str],
        model_shares:    {model: share_pct},
    }
    """
    if not by_model:
        return {
            "model_count":   0,
            "hhi":           0.0,
            "concentration": "none",
            "flags":         [],
            "recommendations": [],
            "model_shares":  {},
        }

    total = sum(by_model.values())
    if total == 0:
        return {
            "model_count":   len(by_model),
            "hhi":           0.0,
            "concentration": "none",
            "flags":         [],
            "recommendations": [],
            "model_shares":  {},
        }

    shares    = {m: v / total * 100 for m, v in by_model.items()}
    hhi       = sum((s / 100) ** 2 * 10_000 for s in shares.values())
    model_count = len(by_model)

    flags: list[str] = []
    recs:  list[str] = []

    if model_count > 5:
        flags.append(
            f"{model_count} distinct models in use — high governance complexity. "
            f"Consider standardising on 2–3 tiers (fast/cheap, balanced, premium)."
        )
        recs.append(
            "Audit each model use-case. Many teams can consolidate to: "
            "a frontier model for reasoning, a mid-tier for generation, "
            "and a small model for classification/routing."
        )

    # Detect cheap model dominance + expensive model presence
    expensive_models = [m for m in by_model if any(
        k in m for k in ["opus", "gpt-4-turbo", "o1", "o3", "gemini-1.5-pro", "gemini-2.0-pro"]
    )]
    cheap_models = [m for m in by_model if any(
        k in m for k in ["haiku", "gpt-4o-mini", "o3-mini", "o4-mini", "gemini-1.5-flash",
                          "gemini-2.0-flash", "gpt-3.5", "text-bison", "chat-bison"]
    )]
    # The expensive keys are bare ("o1", "o3") and substring-match cheap reasoning
    # models ("o3" in "o3-mini", "o1" in "o1-mini"), so those got counted as BOTH,
    # inflating expensive_cost and firing a false "expensive models still cost $X"
    # flag. A model classified cheap is cheap; remove it from expensive.
    expensive_models = [m for m in expensive_models if m not in cheap_models]

    if cheap_models and expensive_models:
        cheap_share  = sum(shares.get(m, 0) for m in cheap_models)
        exp_share    = sum(shares.get(m, 0) for m in expensive_models)
        expensive_cost = sum(by_model.get(m, 0) for m in expensive_models)

        if cheap_share > 80 and exp_share > 0:
            flags.append(
                f"Cheap models handle {cheap_share:.0f}% of spend but expensive models "
                f"still cost ${expensive_cost:.2f}. Verify expensive model tasks can't "
                f"be handled by the cheaper tier."
            )
            recs.append(
                f"Review tasks routed to {', '.join(expensive_models[:3])}. "
                f"If they're simple enough for the cheap tier, route them there."
            )

    if hhi > 8_000:
        concentration = "high"
    elif hhi > 2_500:
        concentration = "medium"
    else:
        concentration = "low"

    return {
        "model_count":     model_count,
        "hhi":             round(hhi, 1),
        "concentration":   concentration,
        "flags":           flags,
        "recommendations": recs,
        "model_shares":    {k: round(v, 2) for k, v in
                            sorted(shares.items(), key=lambda x: x[1], reverse=True)},
    }


# ---------------------------------------------------------------------------
# 4. Peak usage analysis
# ---------------------------------------------------------------------------

def peak_usage_analysis(daily_data: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Identify temporal cost patterns from daily (or hourly) AI spend data.

    Parameters
    ----------
    daily_data:
        List of dicts: ``{"date": "YYYY-MM-DD", "total_usd": float}``.
        Hourly data should use ``{"date": "YYYY-MM-DD HH:MM:SS", "total_usd": float}``.

    Returns
    -------
    {
        highest_cost_day:    {"date": str, "total_usd": float},
        lowest_cost_day:     {"date": str, "total_usd": float},
        weekday_avg_usd:     float,
        weekend_avg_usd:     float,
        weekend_ratio:       float   (weekend / weekday spend ratio),
        day_of_week_avg:     {"Monday": float, ...},
        patterns:            list[str],
    }
    """
    from datetime import datetime

    if not daily_data:
        return {"error": "no_data"}

    parsed: list[tuple[datetime, float]] = []
    for entry in daily_data:
        raw  = entry.get("date", "")
        cost = float(entry.get("total_usd", 0.0))
        try:
            dt = datetime.fromisoformat(raw[:10])
            parsed.append((dt, cost))
        except ValueError:
            continue

    if not parsed:
        return {"error": "unparseable_dates"}

    parsed.sort(key=lambda x: x[0])

    # Highest / lowest cost days
    highest = max(parsed, key=lambda x: x[1])
    lowest  = min(parsed, key=lambda x: x[1])

    # Day-of-week averages (0=Monday, 6=Sunday)
    dow_totals: dict[int, list[float]] = {i: [] for i in range(7)}
    for dt, cost in parsed:
        dow_totals[dt.weekday()].append(cost)

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    dow_avg   = {
        day_names[dow]: round(sum(vals) / len(vals), 4) if vals else 0.0
        for dow, vals in dow_totals.items()
    }

    weekday_vals = [c for dt, c in parsed if dt.weekday() < 5]
    weekend_vals = [c for dt, c in parsed if dt.weekday() >= 5]

    weekday_avg = sum(weekday_vals) / len(weekday_vals) if weekday_vals else 0.0
    weekend_avg = sum(weekend_vals) / len(weekend_vals) if weekend_vals else 0.0
    weekend_ratio = weekend_avg / weekday_avg if weekday_avg > 0 else 0.0

    patterns: list[str] = []

    if weekend_ratio > 0.8:
        patterns.append(
            f"Weekend spend is {weekend_ratio:.0%} of weekday spend — "
            f"AI usage is driven by end-users or automated jobs (not just employees)."
        )
    elif weekend_ratio < 0.3:
        patterns.append(
            f"Weekend spend is only {weekend_ratio:.0%} of weekday spend — "
            f"predominantly employee-driven usage. Good for scheduled batch optimisation."
        )

    # Find the most expensive day of week
    busiest_day = max(dow_avg, key=lambda d: dow_avg[d])
    quietest_day = min(dow_avg, key=lambda d: dow_avg[d])
    if dow_avg[busiest_day] > 0:
        multiplier = dow_avg[busiest_day] / max(dow_avg[quietest_day], 0.0001)
        if multiplier > 2:
            patterns.append(
                f"{busiest_day} is the most expensive day ({multiplier:.1f}x {quietest_day}). "
                f"Consider scheduling batch AI workloads on {quietest_day} to spread cost."
            )

    return {
        "highest_cost_day":  {"date": highest[0].date().isoformat(), "total_usd": round(highest[1], 4)},
        "lowest_cost_day":   {"date": lowest[0].date().isoformat(),  "total_usd": round(lowest[1], 4)},
        "weekday_avg_usd":   round(weekday_avg, 4),
        "weekend_avg_usd":   round(weekend_avg, 4),
        "weekend_ratio":     round(weekend_ratio, 4),
        "day_of_week_avg":   dow_avg,
        "patterns":          patterns,
    }


# ---------------------------------------------------------------------------
# 5. Prompt efficiency score
# ---------------------------------------------------------------------------

def prompt_efficiency_score(by_model_tokens: dict[str, dict[str, int]]) -> dict[str, Any]:
    """
    Compute the output-to-input token ratio per model.

    High ratio (>3×): responses are very verbose — add ``max_tokens`` constraints
    or instruct the model to be concise.

    Low ratio (<0.1) on expensive models: short answers from premium models —
    wrong model choice. Route to a smaller model.

    Parameters
    ----------
    by_model_tokens:
        Dict of ``{model: {"input_tokens": int, "output_tokens": int}}``.

    Returns
    -------
    {
        by_model: {
            "model": {
                ratio:           float,
                input_tokens:    int,
                output_tokens:   int,
                signal:          "verbose" | "terse_expensive" | "balanced",
                recommendation:  str,
            }
        },
        overall_recommendations: list[str],
    }
    """
    by_model_result: dict[str, dict[str, Any]] = {}
    overall_recs: list[str] = []

    expensive_tier = {"gpt-4o", "o1", "o3", "claude-3-opus-20240229",
                      "gemini-1.5-pro", "gemini-2.0-pro"}

    for model, tok in by_model_tokens.items():
        if not isinstance(tok, dict):
            continue
        inp = tok.get("input_tokens", 0)
        out = tok.get("output_tokens", 0)

        if inp == 0:
            continue

        ratio = out / inp

        if ratio > 3.0:
            signal = "verbose"
            rec    = (
                f"{model}: output/input ratio is {ratio:.1f}× — responses are very long. "
                f"Add 'max_tokens' constraints or prompt for concise output. "
                f"This can reduce cost by {min(int((ratio - 1) / ratio * 100), 66):.0f}%+ "
                f"on output tokens."
            )
        elif ratio < 0.1 and any(ex in model for ex in expensive_tier):
            signal = "terse_expensive"
            rec    = (
                f"{model}: output/input ratio is {ratio:.2f}× — very short answers "
                f"from an expensive model. Consider routing to gpt-4o-mini, claude-haiku, "
                f"or gemini-flash for short-form responses."
            )
        else:
            signal = "balanced"
            rec    = f"{model}: ratio is {ratio:.2f}× — looks balanced."

        if signal != "balanced":
            overall_recs.append(rec)

        by_model_result[model] = {
            "ratio":           round(ratio, 4),
            "input_tokens":    inp,
            "output_tokens":   out,
            "signal":          signal,
            "recommendation":  rec,
        }

    return {
        "by_model":               by_model_result,
        "overall_recommendations": overall_recs,
    }


# ---------------------------------------------------------------------------
# 6. Error spend estimate
# ---------------------------------------------------------------------------

def error_spend_estimate(usage_data: dict[str, Any]) -> dict[str, Any]:
    """
    Estimate spend wasted on failed requests.

    If the usage data includes ``error_rate`` or ``error_requests``, compute
    the fraction of total cost that was consumed by failed calls.
    Otherwise, surfaces a recommendation to enable error-rate tracking.

    Parameters
    ----------
    usage_data:
        Dict with optional keys: ``total_usd``, ``error_rate`` (0–1),
        ``error_requests``, ``total_requests``.

    Returns
    -------
    {
        error_rate_pct:       float | None,
        estimated_wasted_usd: float | None,
        recommendation:       str,
    }
    """
    total_usd     = usage_data.get("total_usd", 0.0)
    error_rate    = usage_data.get("error_rate")       # 0.0–1.0
    error_reqs    = usage_data.get("error_requests")
    total_reqs    = usage_data.get("total_requests")

    if error_rate is not None:
        er_pct   = error_rate * 100
        wasted   = total_usd * error_rate
        return {
            "error_rate_pct":       round(er_pct, 2),
            "estimated_wasted_usd": round(wasted, 4),
            "recommendation": (
                f"Error rate is {er_pct:.1f}% — estimated ${wasted:.2f} wasted on failed requests. "
                f"Implement retry with exponential backoff and stream error detection to "
                f"reduce re-attempted token spend."
            ) if wasted > 1.0 else (
                f"Error rate is {er_pct:.1f}% — cost impact is minimal (${wasted:.4f})."
            ),
        }

    if error_reqs and total_reqs and total_reqs > 0:
        er      = error_reqs / total_reqs
        wasted  = total_usd * er
        return {
            "error_rate_pct":       round(er * 100, 2),
            "estimated_wasted_usd": round(wasted, 4),
            "recommendation": (
                f"{error_reqs:,} of {total_reqs:,} requests failed "
                f"({er*100:.1f}%) — estimated ${wasted:.2f} wasted."
            ),
        }

    return {
        "error_rate_pct":       None,
        "estimated_wasted_usd": None,
        "recommendation": (
            "Error rate data not available. Enable request-level logging or integrate "
            "with your observability platform (Datadog, Sentry, etc.) to track failed "
            "AI calls. Even a 2% error rate can waste hundreds of dollars per month on "
            "high-volume workloads."
        ),
    }


# ---------------------------------------------------------------------------
# 7. AI vs infra ratio
# ---------------------------------------------------------------------------

def ai_vs_infra_ratio(ai_total: float, infra_total: float) -> dict[str, Any]:
    """
    Compute AI spend as a percentage of total cloud/infrastructure spend.

    Benchmarks:
      - < 5%  — AI is a minor cost centre (room to invest)
      - 5–15% — healthy SaaS range
      - 15–30% — notable; review growth trajectory
      - > 30% — margin risk territory

    Parameters
    ----------
    ai_total:    Total AI/LLM spend in USD.
    infra_total: Total cloud infrastructure spend in USD.

    Returns
    -------
    {
        ai_total_usd:    float,
        infra_total_usd: float,
        ai_pct_of_infra: float,
        benchmark:       str,
        health:          "low" | "healthy" | "watch" | "risk",
    }
    """
    if infra_total <= 0:
        return {
            "ai_total_usd":    round(ai_total, 4),
            "infra_total_usd": round(infra_total, 4),
            "ai_pct_of_infra": None,
            "benchmark":       "Cannot compute — infra_total must be > 0",
            "health":          "unknown",
        }

    pct = ai_total / infra_total * 100

    if pct < 5:
        health    = "low"
        benchmark = (
            f"AI is {pct:.1f}% of your infrastructure spend — well below the 5–15% "
            f"healthy SaaS range. You likely have headroom to invest more in AI features."
        )
    elif pct <= 15:
        health    = "healthy"
        benchmark = (
            f"AI is {pct:.1f}% of infrastructure spend — within the healthy 5–15% SaaS range."
        )
    elif pct <= 30:
        health    = "watch"
        benchmark = (
            f"AI is {pct:.1f}% of infrastructure spend — above the 15% healthy threshold. "
            f"Monitor growth trajectory and review model efficiency."
        )
    else:
        health    = "risk"
        benchmark = (
            f"AI is {pct:.1f}% of infrastructure spend — above 30% is a margin risk. "
            f"Prioritise caching, model downgrades, and prompt optimisation immediately."
        )

    return {
        "ai_total_usd":    round(ai_total, 4),
        "infra_total_usd": round(infra_total, 4),
        "ai_pct_of_infra": round(pct, 2),
        "benchmark":       benchmark,
        "health":          health,
    }


# ---------------------------------------------------------------------------
# 8. Gross margin impact
# ---------------------------------------------------------------------------

def gross_margin_impact(
    ai_cost_per_customer: float,
    arpu: float,
) -> dict[str, Any]:
    """
    Compute AI cost as a fraction of per-customer revenue (ARPU).

    Flags customers / cohorts where AI cost exceeds 20% of ARPU, as this
    significantly compresses gross margin.

    Parameters
    ----------
    ai_cost_per_customer: Average AI cost per paying customer in USD.
    arpu:                 Average Revenue Per User in USD (same period).

    Returns
    -------
    {
        ai_cost_per_customer: float,
        arpu:                 float,
        ai_pct_of_arpu:       float,
        gross_margin_impact:  str,
        flag:                 bool,
        recommendation:       str,
    }
    """
    if arpu <= 0:
        return {
            "ai_cost_per_customer": round(ai_cost_per_customer, 4),
            "arpu":                 arpu,
            "ai_pct_of_arpu":       None,
            "gross_margin_impact":  "Cannot compute — ARPU must be > 0",
            "flag":                 False,
            "recommendation":       "Provide a valid ARPU to compute gross margin impact.",
        }

    pct  = ai_cost_per_customer / arpu * 100
    flag = pct > 20

    if pct < 5:
        impact = f"AI cost is {pct:.1f}% of ARPU — minimal gross margin impact."
        rec    = "AI costs are well-controlled relative to revenue. No action needed."
    elif pct < 10:
        impact = f"AI cost is {pct:.1f}% of ARPU — manageable gross margin impact."
        rec    = (
            "AI is within an acceptable range. Monitor as you scale — costs can grow "
            "faster than revenue if usage patterns shift."
        )
    elif pct < 20:
        impact = f"AI cost is {pct:.1f}% of ARPU — notable gross margin drag."
        rec    = (
            f"At ${ai_cost_per_customer:.2f} AI cost per customer vs ${arpu:.2f} ARPU, "
            f"AI consumes {pct:.1f}% of revenue. Implement caching and model tiering "
            f"to bring this below 10%."
        )
    else:
        impact = (
            f"AI cost is {pct:.1f}% of ARPU — significant gross margin compression. "
            f"At scale this is a margin risk."
        )
        rec    = (
            f"CRITICAL: ${ai_cost_per_customer:.2f} AI cost vs ${arpu:.2f} ARPU means "
            f"AI alone consumes {pct:.1f}% of revenue per customer. "
            f"Immediate actions: (1) Add prompt caching, (2) Downgrade to cheaper models "
            f"for low-value paths, (3) Rate-limit AI features per customer tier, "
            f"(4) Review pricing — consider AI usage-based add-ons."
        )

    return {
        "ai_cost_per_customer": round(ai_cost_per_customer, 4),
        "arpu":                 round(arpu, 4),
        "ai_pct_of_arpu":       round(pct, 2),
        "gross_margin_impact":  impact,
        "flag":                 flag,
        "recommendation":       rec,
    }


# ---------------------------------------------------------------------------
# Full KPI report
# ---------------------------------------------------------------------------

def full_kpi_report(
    llm_costs_result: dict[str, Any],
    anthropic_data: dict[str, Any] | None = None,
    infra_total_usd: float | None = None,
) -> dict[str, Any]:
    """
    Generate a complete AI KPI dashboard.

    Runs all individual KPI functions against the aggregated LLM cost data
    and returns a single structured report with total estimated savings.

    Parameters
    ----------
    llm_costs_result:
        Dict from ``get_all_llm_costs()``.
    anthropic_data:
        Raw Anthropic connector result (for cache hit analysis).
    infra_total_usd:
        Total cloud infrastructure spend to compute AI vs infra ratio.

    Returns
    -------
    Combined report dict with keys for each KPI plus ``total_estimated_savings_usd``.
    """
    by_model       = llm_costs_result.get("by_model", {})
    daily_data     = llm_costs_result.get("daily", [])
    total_ai_usd   = llm_costs_result.get("total_usd", 0.0)

    report: dict[str, Any] = {
        "period":        llm_costs_result.get("period", "unknown"),
        "total_ai_usd":  total_ai_usd,
        "by_provider":   llm_costs_result.get("by_provider", {}),
    }

    # Collect all by_model_tokens across providers (best effort). The aggregated
    # llm_costs_result now carries merged per-model tokens from every provider
    # that reports them (OpenAI, Anthropic, gateways), so it is the primary
    # source. anthropic_data is still accepted for direct callers that pass it
    # alongside a token-less aggregate (e.g. legacy tests); its models are only
    # merged when the aggregate did not already account for them, so anthropic
    # tokens are never double-counted when both are present.
    combined_tokens: dict[str, dict[str, int]] = {}

    def _merge_tokens(src: dict[str, Any] | None) -> None:
        for model, tok in ((src or {}).get("by_model_tokens") or {}).items():
            bucket = combined_tokens.setdefault(model, {})
            for k, v in tok.items():
                try:
                    bucket[k] = bucket.get(k, 0) + int(v)
                except (TypeError, ValueError):
                    continue

    _merge_tokens(llm_costs_result)
    if anthropic_data:
        for model, tok in (anthropic_data.get("by_model_tokens") or {}).items():
            if model in combined_tokens:
                continue  # already represented by the aggregate; avoid double count
            bucket = combined_tokens.setdefault(model, {})
            for k, v in tok.items():
                try:
                    bucket[k] = bucket.get(k, 0) + int(v)
                except (TypeError, ValueError):
                    continue

    # 1. Cache hit rate. Anthropic reports cache tokens directly; OpenAI reports
    # input_cached_tokens (mapped to cache_read_input_tokens by the connector),
    # so cache analysis now works for either. Prefer the richer Anthropic payload
    # when present, else compute from the combined token map if any provider
    # carries cache-read data.
    if anthropic_data:
        report["cache_hit_rate"] = cache_hit_rate(anthropic_data)
    elif any(t.get("cache_read_input_tokens", 0) or t.get("input_tokens", 0)
             for t in combined_tokens.values()):
        report["cache_hit_rate"] = cache_hit_rate({"by_model_tokens": combined_tokens})
    else:
        report["cache_hit_rate"] = {
            "note": "No token-level data — connect Anthropic or OpenAI with an admin key for cache analysis."
        }

    # 2. Context window utilisation
    if combined_tokens:
        report["context_window_utilization"] = context_window_utilization(
            {"by_model_tokens": combined_tokens}
        )
    else:
        report["context_window_utilization"] = {
            "note": "Token-level data not available — connect Anthropic or OpenAI with admin key."
        }

    # 3. Model sprawl
    report["model_sprawl"] = model_sprawl_score(by_model)

    # 4. Peak usage
    report["peak_usage"] = peak_usage_analysis(daily_data)

    # 5. Prompt efficiency
    if combined_tokens:
        report["prompt_efficiency"] = prompt_efficiency_score(combined_tokens)
    else:
        report["prompt_efficiency"] = {
            "note": "Token-level data not available for prompt efficiency analysis."
        }

    # 6. Error spend
    report["error_spend"] = error_spend_estimate(llm_costs_result)

    # 7. AI vs infra
    if infra_total_usd and infra_total_usd > 0:
        report["ai_vs_infra"] = ai_vs_infra_ratio(total_ai_usd, infra_total_usd)
    else:
        report["ai_vs_infra"] = {
            "ai_total_usd":  total_ai_usd,
            "note": (
                "Pass infra_total_usd to compare AI spend against total cloud bill. "
                "Healthy SaaS target: 5–15%."
            ),
        }

    # Aggregate estimated savings opportunities
    savings = 0.0
    if isinstance(report.get("cache_hit_rate"), dict):
        savings += report["cache_hit_rate"].get("estimated_savings_usd", 0.0)

    report["total_estimated_savings_usd"] = round(savings, 4)

    return report
