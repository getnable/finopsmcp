"""
Anthropic API cost and usage connector.

Tracks spend across Claude models via:
  1. Anthropic Usage API (beta) — /v1/organizations/{org}/usage
  2. Estimated from token counts × published prices (fallback)

Env vars:
  ANTHROPIC_API_KEY          — standard key
  ANTHROPIC_ADMIN_KEY        — org-level key (preferred for usage data)
  ANTHROPIC_ORGANIZATION_ID  — required for org-level usage endpoint

Published pricing (May 2026): https://www.anthropic.com/pricing
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

log = logging.getLogger(__name__)

# Per 1M tokens (USD)
_MODEL_PRICING: dict[str, dict[str, float]] = {
    # Claude 3.7 / 3.5 family
    "claude-3-7-sonnet-20250219":      {"input": 3.00,  "output": 15.00},
    "claude-3-5-sonnet-20241022":      {"input": 3.00,  "output": 15.00},
    "claude-3-5-sonnet-20240620":      {"input": 3.00,  "output": 15.00},
    "claude-3-5-haiku-20241022":       {"input": 0.80,  "output": 4.00},
    # Claude 3 family
    "claude-3-opus-20240229":          {"input": 15.00, "output": 75.00},
    "claude-3-sonnet-20240229":        {"input": 3.00,  "output": 15.00},
    "claude-3-haiku-20240307":         {"input": 0.25,  "output": 1.25},
    # Claude 2
    "claude-2.1":                      {"input": 8.00,  "output": 24.00},
    "claude-2.0":                      {"input": 8.00,  "output": 24.00},
    # Shorthand aliases
    "claude-3-5-sonnet-latest":        {"input": 3.00,  "output": 15.00},
    "claude-3-5-haiku-latest":         {"input": 0.80,  "output": 4.00},
    "claude-3-opus-latest":            {"input": 15.00, "output": 75.00},
}

_API_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "anthropic-beta": "usage-1",
        "content-type": "application/json",
    }


def get_costs(
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """
    Fetch Anthropic usage costs for the given date range.
    Falls back to estimated costs if the org-level API is unavailable.
    """
    from ...security.env import get_env
    api_key = get_env("ANTHROPIC_ADMIN_KEY") or get_env("ANTHROPIC_API_KEY")
    org_id  = get_env("ANTHROPIC_ORGANIZATION_ID") or None

    if not api_key:
        return _empty("not_configured")

    # Try org-level usage endpoint first
    if org_id:
        result = _fetch_org_usage(api_key, org_id, start_date, end_date)
        if result.get("source") == "api":
            return result

    # Fall back to workspace-level token usage
    return _fetch_workspace_usage(api_key, start_date, end_date)


def _fetch_org_usage(
    api_key: str,
    org_id: str,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    try:
        import httpx
    except ImportError:
        return _empty("httpx_missing")

    try:
        resp = httpx.get(
            f"{_API_BASE}/v1/organizations/{org_id}/usage",
            params={
                "start_date": start_date.isoformat(),
                "end_date":   end_date.isoformat(),
            },
            headers=_headers(api_key),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.debug("Anthropic org usage API unavailable: %s", e)
        return _empty("org_api_unavailable")

    return _parse_usage(data, source="api")


def _fetch_workspace_usage(
    api_key: str,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """Workspace-level usage — available to all API keys."""
    try:
        import httpx
    except ImportError:
        return _empty("httpx_missing")

    try:
        resp = httpx.get(
            f"{_API_BASE}/v1/usage",
            params={
                "start_date": start_date.isoformat(),
                "end_date":   end_date.isoformat(),
            },
            headers=_headers(api_key),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.debug("Anthropic workspace usage API unavailable: %s", e)
        return _empty("api_error")

    return _parse_usage(data, source="estimated")


def _parse_usage(data: dict, source: str) -> dict[str, Any]:
    total = 0.0
    by_model: dict[str, float] = {}
    by_model_tokens: dict[str, dict[str, int]] = {}
    daily: list[dict] = []

    for entry in data.get("data", data.get("usage", [])):
        model      = entry.get("model") or entry.get("model_id") or "unknown"
        input_tok  = entry.get("input_tokens", 0)
        output_tok = entry.get("output_tokens", 0)
        day        = entry.get("date") or entry.get("timestamp", "")[:10]

        # If actual cost is in the response, use it
        cost = float(entry.get("cost_usd", 0.0))
        if cost == 0.0:
            pricing = _MODEL_PRICING.get(model, {"input": 0.0, "output": 0.0})
            cost = (input_tok / 1_000_000 * pricing["input"] +
                    output_tok / 1_000_000 * pricing["output"])

        total += cost
        by_model[model] = by_model.get(model, 0.0) + cost
        if model not in by_model_tokens:
            by_model_tokens[model] = {"input": 0, "output": 0}
        by_model_tokens[model]["input"]  += input_tok
        by_model_tokens[model]["output"] += output_tok

        # Accumulate daily
        existing = next((d for d in daily if d["date"] == day), None)
        if existing:
            existing["total_usd"] = round(existing["total_usd"] + cost, 4)
            existing["by_model"][model] = round(
                existing["by_model"].get(model, 0.0) + cost, 4)
        else:
            daily.append({"date": day, "total_usd": round(cost, 4),
                          "by_model": {model: round(cost, 4)}})

    return {
        "total_usd":      round(total, 4),
        "by_model":       {k: round(v, 4) for k, v in
                           sorted(by_model.items(), key=lambda x: x[1], reverse=True)},
        "by_model_tokens": by_model_tokens,
        "daily":          sorted(daily, key=lambda d: d["date"]),
        "source":         source,
        **({"note": "Costs estimated from token counts × published prices."} if source == "estimated" else {}),
    }


def _empty(reason: str) -> dict[str, Any]:
    return {"total_usd": 0.0, "by_model": {}, "by_model_tokens": {},
            "daily": [], "source": "none", "reason": reason}


async def is_configured() -> bool:
    from ...security.env import get_env
    return bool(get_env("ANTHROPIC_API_KEY") or get_env("ANTHROPIC_ADMIN_KEY"))
