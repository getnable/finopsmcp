"""
OpenRouter cost and usage connector.

OpenRouter is the aggregation gateway a large share of early AI startups route
through (one key, 300+ models, 60+ providers). The spend lives on OpenRouter's
dashboard, never on a cloud bill, and OpenRouter carries the per-call, per-model
token data the direct provider invoices withhold. That makes it a first-class
source for nable's AI cost view.

Two data paths, tried in order:
  1. /api/v1/activity  — per-day, per-model usage (cost + tokens + requests).
     Requires a provisioning key (or a key with activity scope). This is the
     good path: real model/token detail that feeds the KPI engine.
  2. /api/v1/credits   — lifetime credits + usage only. Used to confirm the key
     works and surface remaining balance when activity is unavailable.

Credits are denominated in USD (1 credit = $1), so usage values are dollars.

Env vars:
  OPENROUTER_API_KEY            — standard or provisioning key
  OPENROUTER_PROVISIONING_KEY   — provisioning key (preferred for activity data)
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

log = logging.getLogger(__name__)

_API_BASE = "https://openrouter.ai/api/v1"


def _key() -> str | None:
    from ...security.env import get_env
    return get_env("OPENROUTER_PROVISIONING_KEY") or get_env("OPENROUTER_API_KEY")


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def get_costs(start_date: date, end_date: date) -> dict[str, Any]:
    """
    Fetch OpenRouter spend and token usage for the date range.

    Returns the normalised LLM-connector shape:
      {
        "total_usd": float,
        "by_model": {model: usd, ...},
        "by_model_tokens": {model: {input_tokens, output_tokens,
                            cache_read_input_tokens, cache_creation_input_tokens,
                            request_count}, ...},
        "daily": [{"date": "YYYY-MM-DD", "total_usd": float, "by_model": {...}}, ...],
        "source": "api" | "limited" | "none",
      }
    """
    api_key = _key()
    if not api_key:
        return _empty("not_configured")

    activity = _fetch_activity(api_key, start_date, end_date)
    if activity is not None:
        return activity

    # No activity access (standard key). Confirm the key and surface balance,
    # but do NOT inject lifetime usage into a range total — that would corrupt
    # the aggregate. Return 0 with an honest note instead.
    return _fetch_credits_summary(api_key)


def _fetch_activity(api_key: str, start_date: date, end_date: date) -> dict[str, Any] | None:
    """
    Pull per-day, per-model activity. Returns None when the endpoint is not
    accessible (e.g. standard key without activity scope) so the caller can
    fall back. Returns a normalised result dict on success.
    """
    try:
        import httpx
    except ImportError:
        return None

    try:
        resp = httpx.get(
            f"{_API_BASE}/activity",
            headers=_headers(api_key),
            timeout=30,
        )
        if resp.status_code in (401, 403, 404):
            # No activity scope / not a provisioning key — let caller fall back.
            log.debug("OpenRouter activity unavailable (status %s)", resp.status_code)
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.debug("OpenRouter activity fetch failed: %s", e)
        return None

    rows = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return None

    start_s, end_s = start_date.isoformat(), end_date.isoformat()
    total = 0.0
    by_model: dict[str, float] = {}
    by_model_tokens: dict[str, dict[str, int]] = {}
    daily_map: dict[str, dict[str, Any]] = {}

    for row in rows:
        day = str(row.get("date") or "")[:10]
        # Inclusive window [start_date, end_date]. The callers (get_all_llm_costs,
        # get_llm_costs) default end_date to today and intend it inclusive, like the
        # sibling connectors. An end-EXCLUSIVE check here dropped today's spend, and
        # made a single-day query (start == end == today) return an empty result.
        if not day or day < start_s or day > end_s:
            continue
        model = row.get("model") or row.get("model_permaslug") or "unknown"
        cost = _float(row.get("usage"))
        total += cost
        by_model[model] = by_model.get(model, 0.0) + cost

        bucket = by_model_tokens.setdefault(model, {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "request_count": 0,
        })
        bucket["input_tokens"]  += _int(row.get("prompt_tokens"))
        bucket["output_tokens"] += _int(row.get("completion_tokens"))
        bucket["request_count"] += _int(row.get("requests"))

        d = daily_map.setdefault(day, {"date": day, "total_usd": 0.0, "by_model": {}})
        d["total_usd"] = round(d["total_usd"] + cost, 6)
        d["by_model"][model] = round(d["by_model"].get(model, 0.0) + cost, 6)

    return {
        "total_usd": round(total, 4),
        "by_model": {k: round(v, 4) for k, v in
                     sorted(by_model.items(), key=lambda x: x[1], reverse=True)},
        "by_model_tokens": by_model_tokens,
        "daily": [daily_map[d] for d in sorted(daily_map)],
        "source": "api",
    }


def _fetch_credits_summary(api_key: str) -> dict[str, Any]:
    """Confirm the key works and surface remaining balance; range total is 0."""
    try:
        import httpx
    except ImportError:
        return _empty("httpx_missing")

    try:
        resp = httpx.get(f"{_API_BASE}/credits", headers=_headers(api_key), timeout=30)
        resp.raise_for_status()
        data = resp.json().get("data") or {}
    except Exception as e:
        log.debug("OpenRouter credits fetch failed: %s", e)
        return _empty("api_error")

    # A 200 with a null or non-dict "data" field (malformed/off-nominal response)
    # must degrade gracefully, not crash get_costs on the .get below.
    if not isinstance(data, dict):
        data = {}
    total_credits = _float(data.get("total_credits"))
    total_usage   = _float(data.get("total_usage"))
    return {
        "total_usd": 0.0,
        "by_model": {},
        "by_model_tokens": {},
        "daily": [],
        "source": "limited",
        "lifetime_usage_usd": round(total_usage, 4),
        "credits_remaining_usd": round(total_credits - total_usage, 4),
        "note": (
            "Per-model, range-scoped usage needs an OpenRouter provisioning key. "
            "Add OPENROUTER_PROVISIONING_KEY to break spend down by model and day."
        ),
    }


def _float(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _empty(reason: str) -> dict[str, Any]:
    return {"total_usd": 0.0, "by_model": {}, "by_model_tokens": {},
            "daily": [], "source": "none", "reason": reason}


async def is_configured() -> bool:
    return bool(_key())
