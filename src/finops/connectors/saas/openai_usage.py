"""
OpenAI cost and usage connector.

Uses the OpenAI Organization API to fetch:
  - Daily cost by model (via /v1/organization/costs)
  - Token usage breakdown by model (via /v1/organization/usage/completions)

Requires an Admin API key (sk-admin-...) or an org-level key with
  "Read billing" and "Read usage" scopes.

Env vars:
  OPENAI_API_KEY      — standard key (limited usage data)
  OPENAI_ADMIN_KEY    — admin/org key (full cost + usage breakdown)
  OPENAI_ORG_ID       — optional, scopes to a specific org
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

# Current OpenAI pricing per 1M tokens (USD) — updated May 2026
# Source: https://openai.com/pricing
_MODEL_PRICING: dict[str, dict[str, float]] = {
    # GPT-4o family
    "gpt-4o":               {"input": 5.00,   "output": 15.00},
    "gpt-4o-2024-11-20":    {"input": 2.50,   "output": 10.00},
    "gpt-4o-mini":          {"input": 0.15,   "output": 0.60},
    "gpt-4o-mini-2024-07-18": {"input": 0.15, "output": 0.60},
    # o-series reasoning
    "o1":                   {"input": 15.00,  "output": 60.00},
    "o1-mini":              {"input": 3.00,   "output": 12.00},
    "o3":                   {"input": 10.00,  "output": 40.00},
    "o3-mini":              {"input": 1.10,   "output": 4.40},
    "o4-mini":              {"input": 1.10,   "output": 4.40},
    # GPT-4 Turbo
    "gpt-4-turbo":          {"input": 10.00,  "output": 30.00},
    "gpt-4-turbo-preview":  {"input": 10.00,  "output": 30.00},
    # GPT-3.5
    "gpt-3.5-turbo":        {"input": 0.50,   "output": 1.50},
    # Embeddings
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "text-embedding-3-large": {"input": 0.13, "output": 0.0},
    "text-embedding-ada-002": {"input": 0.10, "output": 0.0},
    # Image (per image, stored as input cost, output=0)
    "dall-e-3":             {"input": 0.04,   "output": 0.0},  # per image (1024x1024)
    "dall-e-2":             {"input": 0.02,   "output": 0.0},
    # Audio / TTS
    "whisper-1":            {"input": 0.006,  "output": 0.0},  # per minute
    "tts-1":                {"input": 0.015,  "output": 0.0},  # per 1k chars
    "tts-1-hd":             {"input": 0.030,  "output": 0.0},
}


def _headers(api_key: str, org_id: str | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if org_id:
        h["OpenAI-Organization"] = org_id
    return h


def get_costs(
    start_date: date,
    end_date: date,
    group_by: list[str] | None = None,
) -> dict[str, Any]:
    """
    Fetch actual billed costs from OpenAI's /v1/organization/costs endpoint.
    Requires an Admin API key.

    Returns normalised result:
      {
        "total_usd": float,
        "by_model": {"gpt-4o": float, ...},
        "by_project": {"proj_abc": float, ...},
        "daily": [{"date": "YYYY-MM-DD", "total_usd": float, "by_model": {...}}, ...],
        "source": "api" | "estimated",
      }
    """
    try:
        import httpx
    except ImportError:
        log.warning("httpx not installed — pip install httpx")
        return _empty_result("httpx_missing")

    from ...security.env import get_env
    api_key = get_env("OPENAI_ADMIN_KEY") or get_env("OPENAI_API_KEY")
    org_id  = get_env("OPENAI_ORG_ID") or None

    if not api_key:
        return _empty_result("not_configured")

    # OpenAI costs API uses unix timestamps
    import time
    from datetime import datetime, timezone
    start_ts = int(datetime(start_date.year, start_date.month, start_date.day,
                            tzinfo=timezone.utc).timestamp())
    end_ts   = int(datetime(end_date.year, end_date.month, end_date.day,
                            tzinfo=timezone.utc).timestamp())

    params: dict[str, Any] = {
        "start_time": start_ts,
        "end_time":   end_ts,
        "bucket_width": "1d",
        "limit": 180,
    }
    if group_by:
        params["group_by"] = group_by
    else:
        params["group_by"] = ["model", "project_id"]

    try:
        resp = httpx.get(
            "https://api.openai.com/v1/organization/costs",
            params=params,
            headers=_headers(api_key, org_id),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("OpenAI costs API failed: %s — falling back to usage estimate", e)
        return _estimate_from_usage(start_date, end_date, api_key, org_id)

    return _parse_costs_response(data)


def _parse_costs_response(data: dict) -> dict[str, Any]:
    total = 0.0
    by_model: dict[str, float] = {}
    by_project: dict[str, float] = {}
    daily: list[dict] = []

    for bucket in data.get("data", []):
        bucket_total = 0.0
        bucket_by_model: dict[str, float] = {}

        for result in bucket.get("results", []):
            amount = result.get("amount", {}).get("value", 0.0)
            model  = result.get("model_id") or "unknown"
            proj   = result.get("project_id") or "default"
            bucket_total += amount
            bucket_by_model[model] = bucket_by_model.get(model, 0.0) + amount
            by_model[model]        = by_model.get(model, 0.0) + amount
            by_project[proj]       = by_project.get(proj, 0.0) + amount

        total += bucket_total
        ts = bucket.get("start_time", 0)
        from datetime import datetime, timezone
        day_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""
        daily.append({"date": day_str, "total_usd": round(bucket_total, 4),
                      "by_model": {k: round(v, 4) for k, v in bucket_by_model.items()}})

    return {
        "total_usd":  round(total, 4),
        "by_model":   {k: round(v, 4) for k, v in
                       sorted(by_model.items(), key=lambda x: x[1], reverse=True)},
        "by_project": {k: round(v, 4) for k, v in
                       sorted(by_project.items(), key=lambda x: x[1], reverse=True)},
        "daily":      daily,
        "source":     "api",
    }


def _estimate_from_usage(
    start_date: date,
    end_date: date,
    api_key: str,
    org_id: str | None,
) -> dict[str, Any]:
    """
    Fallback: fetch token usage and multiply by published prices.
    Less accurate (doesn't include discounts/credits) but works with standard keys.
    """
    try:
        import httpx
    except ImportError:
        return _empty_result("httpx_missing")

    import time
    from datetime import datetime, timezone
    start_ts = int(datetime(start_date.year, start_date.month, start_date.day,
                            tzinfo=timezone.utc).timestamp())
    end_ts   = int(datetime(end_date.year, end_date.month, end_date.day,
                            tzinfo=timezone.utc).timestamp())

    try:
        resp = httpx.get(
            "https://api.openai.com/v1/organization/usage/completions",
            params={
                "start_time": start_ts,
                "end_time":   end_ts,
                "bucket_width": "1d",
                "group_by": ["model"],
                "limit": 180,
            },
            headers=_headers(api_key, org_id),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("OpenAI usage API also failed: %s", e)
        return _empty_result("api_error")

    total = 0.0
    by_model: dict[str, float] = {}
    daily: list[dict] = []

    for bucket in data.get("data", []):
        bucket_total = 0.0
        bucket_by_model: dict[str, float] = {}

        for result in bucket.get("results", []):
            model       = result.get("model_id") or "unknown"
            input_tok   = result.get("input_tokens", 0)
            output_tok  = result.get("output_tokens", 0)
            pricing     = _MODEL_PRICING.get(model, {"input": 0.0, "output": 0.0})
            cost = (input_tok / 1_000_000 * pricing["input"] +
                    output_tok / 1_000_000 * pricing["output"])
            bucket_total += cost
            bucket_by_model[model] = bucket_by_model.get(model, 0.0) + cost
            by_model[model]        = by_model.get(model, 0.0) + cost

        total += bucket_total
        ts = bucket.get("start_time", 0)
        from datetime import datetime, timezone
        day_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""
        daily.append({"date": day_str, "total_usd": round(bucket_total, 4),
                      "by_model": {k: round(v, 4) for k, v in bucket_by_model.items()}})

    return {
        "total_usd":  round(total, 4),
        "by_model":   {k: round(v, 4) for k, v in
                       sorted(by_model.items(), key=lambda x: x[1], reverse=True)},
        "by_project": {},
        "daily":      daily,
        "source":     "estimated",
        "note":       "Costs estimated from token counts × published prices. Does not reflect discounts or credits.",
    }


def _empty_result(reason: str) -> dict[str, Any]:
    return {"total_usd": 0.0, "by_model": {}, "by_project": {}, "daily": [],
            "source": "none", "reason": reason}


async def is_configured() -> bool:
    from ...security.env import get_env
    return bool(get_env("OPENAI_API_KEY") or get_env("OPENAI_ADMIN_KEY"))
