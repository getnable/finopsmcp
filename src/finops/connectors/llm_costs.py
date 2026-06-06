"""
LLM cost aggregator — normalizes spend across all AI providers.

Sources:
  - OpenAI (GPT-4o, o3, embeddings, DALL-E, Whisper)
  - Anthropic (Claude 3.x / 3.5 / 3.7)
  - AWS Bedrock (Claude, Llama, Titan, Mistral, Nova via Cost Explorer)
  - Azure OpenAI (via Azure Cost Management)
  - Google Vertex AI (via Cloud Billing)
  - Together AI, Cohere, Mistral AI (via API usage endpoints)

All costs normalised to USD. Token counts normalised to per-1M-token rates
for cross-model efficiency comparison.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

# Bedrock model pricing per 1M tokens (on-demand, us-east-1, May 2026)
_BEDROCK_PRICING: dict[str, dict[str, float]] = {
    # Anthropic on Bedrock
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 3.00,  "output": 15.00},
    "anthropic.claude-3-5-haiku-20241022-v1:0":  {"input": 0.80,  "output": 4.00},
    "anthropic.claude-3-opus-20240229-v1:0":     {"input": 15.00, "output": 75.00},
    "anthropic.claude-3-sonnet-20240229-v1:0":   {"input": 3.00,  "output": 15.00},
    "anthropic.claude-3-haiku-20240307-v1:0":    {"input": 0.25,  "output": 1.25},
    # Meta Llama
    "meta.llama3-70b-instruct-v1:0":             {"input": 0.99,  "output": 0.99},
    "meta.llama3-8b-instruct-v1:0":              {"input": 0.22,  "output": 0.22},
    "meta.llama3-1-405b-instruct-v1:0":          {"input": 5.32,  "output": 16.00},
    # Amazon Nova
    "amazon.nova-pro-v1:0":                      {"input": 0.80,  "output": 3.20},
    "amazon.nova-lite-v1:0":                     {"input": 0.06,  "output": 0.24},
    "amazon.nova-micro-v1:0":                    {"input": 0.035, "output": 0.14},
    # Mistral
    "mistral.mistral-large-2402-v1:0":           {"input": 4.00,  "output": 12.00},
    "mistral.mistral-7b-instruct-v0:2":          {"input": 0.15,  "output": 0.20},
    # Amazon Titan
    "amazon.titan-text-premier-v1:0":            {"input": 0.50,  "output": 1.50},
    "amazon.titan-text-lite-v1":                 {"input": 0.15,  "output": 0.20},
    "amazon.titan-embed-text-v2:0":              {"input": 0.02,  "output": 0.00},
}


def get_bedrock_costs(start_date: date, end_date: date) -> dict[str, Any]:
    """Fetch Bedrock costs from Cost Explorer, broken down by model."""
    try:
        import boto3
    except ImportError:
        return _empty("boto3_missing")

    try:
        ce = boto3.client("ce", region_name="us-east-1")
        period = {"Start": start_date.isoformat(), "End": end_date.isoformat()}
        # AWS labels Bedrock spend under service names that vary and change over
        # time: plain "Amazon Bedrock" plus per-model SKUs like
        # "Claude Sonnet 4.5 (Amazon Bedrock Edition)". Cost Explorer's SERVICE
        # filter is exact-match only (no contains), and ce:GetDimensionValues is
        # not in nable's minimum read-only policy. So discover the names with a
        # SERVICE-grouped GetCostAndUsage (the same permission cost queries use),
        # then pull daily model detail filtered to just those services.
        discover = ce.get_cost_and_usage(
            TimePeriod=period,
            Granularity="MONTHLY",
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            Metrics=["UnblendedCost"],
        )
        bedrock_services = sorted({
            g["Keys"][0]
            for r in discover.get("ResultsByTime", [])
            for g in r.get("Groups", [])
            if "bedrock" in g["Keys"][0].lower()
        })
        if not bedrock_services:
            return _empty("no_bedrock_services")
        resp = ce.get_cost_and_usage(
            TimePeriod=period,
            Granularity="DAILY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": bedrock_services}},
            GroupBy=[
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ],
            Metrics=["UnblendedCost"],
        )
    except Exception as e:
        log.warning("Bedrock cost fetch failed: %s", e)
        return _empty("ce_error")

    total = 0.0
    by_model: dict[str, float] = {}
    daily: list[dict] = []

    for result in resp.get("ResultsByTime", []):
        day = result["TimePeriod"]["Start"]
        day_total = 0.0
        day_by_model: dict[str, float] = {}

        for group in result.get("Groups", []):
            service, usage_type = group["Keys"][0], group["Keys"][1]
            if "bedrock" not in service.lower():
                continue  # belt-and-suspenders; filter should already exclude these
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            day_total += amount
            # Prefer the model id encoded in the usage type
            # (e.g. "USE1-anthropic.claude-3-5-sonnet..."). Real Bedrock model
            # ids always contain a vendor dot ("anthropic.", "meta.", "amazon.").
            # When AWS bills the model as its own service SKU, the usage type is
            # generic, so fall back to the service name (suffix stripped).
            model_id = _extract_model_from_usage_type(usage_type)
            if "." not in model_id:
                model_id = service.replace(" (Amazon Bedrock Edition)", "").strip() or service
            day_by_model[model_id] = day_by_model.get(model_id, 0.0) + amount
            by_model[model_id] = by_model.get(model_id, 0.0) + amount

        total += day_total
        daily.append({"date": day, "total_usd": round(day_total, 4),
                      "by_model": {k: round(v, 4) for k, v in day_by_model.items()}})

    return {
        "total_usd": round(total, 4),
        "by_model":  {k: round(v, 4) for k, v in
                      sorted(by_model.items(), key=lambda x: x[1], reverse=True)},
        "daily":     daily,
        "source":    "cost_explorer",
    }


def _extract_model_from_usage_type(usage_type: str) -> str:
    """
    Bedrock usage types look like:
      USE1-anthropic.claude-3-5-sonnet-20241022-v2:0-input-tokens
    Extract the model ID portion.
    """
    # Strip region prefix (USE1-, EUW1-, etc.)
    parts = usage_type.split("-", 1)
    if len(parts) == 2 and len(parts[0]) <= 5:
        usage_type = parts[1]
    # Strip suffix (-input-tokens, -output-tokens, etc.)
    for suffix in ["-input-tokens", "-output-tokens", "-invocations"]:
        if usage_type.endswith(suffix):
            usage_type = usage_type[:-len(suffix)]
    return usage_type


def get_all_llm_costs(
    start_date: date | None = None,
    end_date: date | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """
    Aggregate LLM costs across all configured providers.

    Returns:
      {
        "total_usd": float,
        "by_provider": {"openai": float, "anthropic": float, "bedrock": float, ...},
        "by_model": {"gpt-4o": float, "claude-3-5-sonnet...": float, ...},
        "daily": [...],
        "model_efficiency": [  # sorted by cost per 1M tokens
            {"model": str, "provider": str, "cost_usd": float,
             "input_price_per_1m": float, "output_price_per_1m": float},
            ...
        ],
        "top_spenders": [...],   # models ranked by total spend
        "recommendations": [...],
      }
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=days)

    from .saas.openai_usage import get_costs as openai_costs, is_configured as openai_configured
    from .saas.anthropic_usage import get_costs as anthropic_costs, is_configured as anthropic_configured
    from .saas.vertex_costs import get_vertex_costs, is_configured as vertex_configured

    results: dict[str, dict] = {}

    # Use asyncio.run() safely — avoid deprecated get_event_loop on Python 3.10+
    import asyncio

    def _run(coro):  # type: ignore[return]
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Already inside an async context (e.g. called from an MCP tool handler)
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, coro).result()
            return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)

    # OpenAI
    try:
        if _run(openai_configured()):
            results["openai"] = openai_costs(start_date, end_date)
    except Exception as e:
        log.debug("OpenAI cost fetch error: %s", e)

    # Anthropic
    try:
        if _run(anthropic_configured()):
            results["anthropic"] = anthropic_costs(start_date, end_date)
    except Exception as e:
        log.debug("Anthropic cost fetch error: %s", e)

    # Bedrock
    try:
        import boto3
        boto3.client("sts").get_caller_identity()  # quick auth check
        results["bedrock"] = get_bedrock_costs(start_date, end_date)
    except Exception as e:
        log.debug("Bedrock cost fetch skipped: %s", e)

    # Vertex AI
    try:
        if _run(vertex_configured()):
            v = get_vertex_costs(start_date, end_date)
            if v.get("source") != "none":
                results["vertex"] = v
    except Exception as e:
        log.debug("Vertex AI cost fetch skipped: %s", e)

    # Aggregate
    total = 0.0
    by_provider: dict[str, float] = {}
    by_model: dict[str, float] = {}
    daily_map: dict[str, float] = {}

    for provider, data in results.items():
        amt = data.get("total_usd", 0.0)
        by_provider[provider] = round(amt, 4)
        total += amt

        for model, cost in data.get("by_model", {}).items():
            full_key = f"{model}"  # keep model names as-is; provider is implicit
            by_model[full_key] = by_model.get(full_key, 0.0) + cost

        for day_entry in data.get("daily", []):
            d = day_entry.get("date", "")
            daily_map[d] = daily_map.get(d, 0.0) + day_entry.get("total_usd", 0.0)

    daily = [{"date": d, "total_usd": round(v, 4)}
             for d, v in sorted(daily_map.items())]

    top_spenders = sorted(
        [{"model": k, "cost_usd": round(v, 4)} for k, v in by_model.items()],
        key=lambda x: x["cost_usd"], reverse=True
    )[:10]

    recommendations = _generate_recommendations(by_model, results)

    return {
        "period":       f"{start_date} → {end_date}",
        "total_usd":    round(total, 4),
        "by_provider":  by_provider,
        "by_model":     {k: round(v, 4) for k, v in
                         sorted(by_model.items(), key=lambda x: x[1], reverse=True)},
        "daily":        daily,
        "top_spenders": top_spenders,
        "recommendations": recommendations,
        "sources":      {k: v.get("source", "none") for k, v in results.items()},
    }


def _generate_recommendations(
    by_model: dict[str, float],
    results: dict[str, dict],
) -> list[dict[str, Any]]:
    """Surface cost-saving opportunities across model choices."""
    recs: list[dict[str, Any]] = []

    from .saas.openai_usage import _MODEL_PRICING as OAI_PRICING
    from .saas.anthropic_usage import _MODEL_PRICING as ANT_PRICING

    all_pricing = {**OAI_PRICING, **ANT_PRICING, **_BEDROCK_PRICING}

    # Expensive model -> cheaper sibling for lower-complexity tasks. The savings
    # percentage is NOT hardcoded; it's computed from the real pricing tables
    # (blended input+output), so it tracks price changes and never asserts a
    # made-up number. It's an estimate that assumes a balanced input/output token
    # mix; the true figure depends on the workload's ratio.
    downgrades = {
        "gpt-4o":                   "gpt-4o-mini",
        "gpt-4-turbo":              "gpt-4o",
        "claude-3-opus-20240229":   "claude-3-5-sonnet-20241022",
        "claude-opus-4-20250514":   "claude-sonnet-4-5-20250929",
        "claude-opus-4-1-20250805": "claude-sonnet-4-5-20250929",
        "o1":                       "o3-mini",
    }

    def _blended(price: dict) -> float:
        return float(price.get("input", 0.0)) + float(price.get("output", 0.0))

    for model, spend in by_model.items():
        if spend < 5.0:  # ignore noise
            continue
        # Normalize bedrock/vendor-prefixed ids ("bedrock/anthropic.claude-...")
        # so the match works regardless of provider routing prefix.
        clean = model.split("/")[-1].replace("anthropic.", "").replace("meta.", "").replace("amazon.", "")
        for expensive, cheaper in downgrades.items():
            if expensive not in clean:
                continue
            cur_price, new_price = all_pricing.get(expensive), all_pricing.get(cheaper)
            if not cur_price or not new_price:
                continue
            cur_blended, new_blended = _blended(cur_price), _blended(new_price)
            if cur_blended <= 0:
                continue
            savings_frac = max(0.0, 1.0 - new_blended / cur_blended)
            recs.append({
                "model":        model,
                "current_spend": round(spend, 2),
                "recommendation": f"Consider {cheaper} for lower-complexity tasks",
                "estimated_savings_pct": f"{savings_frac * 100:.0f}%",
                "estimated_savings_usd": round(spend * savings_frac, 2),
                "basis": "price-ratio estimate assuming a balanced input/output token mix; actual savings depend on your ratio",
            })
            break

    return sorted(recs, key=lambda r: r["estimated_savings_usd"], reverse=True)


def _empty(reason: str) -> dict[str, Any]:
    return {"total_usd": 0.0, "by_model": {}, "daily": [],
            "source": "none", "reason": reason}
