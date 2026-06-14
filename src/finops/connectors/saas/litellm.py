"""
LiteLLM proxy cost and usage connector.

LiteLLM is the open-source gateway early AI startups self-host in production
once spend justifies it (MIT, 100+ providers, one OpenAI-compatible endpoint).
When run as a proxy with a database it records per-request spend and tokens,
which nable reads from the admin API and normalises into the LLM cost view.

Reads GET {proxy}/spend/logs (returns per-request rows with model, spend, and
token counts) and aggregates by model and day. No data leaves the user's
network: the proxy URL is their own host.

Env vars:
  LITELLM_PROXY_URL   — base URL of the proxy, e.g. http://localhost:4000
  LITELLM_MASTER_KEY  — admin/master key (sk-...); also accepts LITELLM_API_KEY
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

log = logging.getLogger(__name__)


def _base_url() -> str | None:
    from ...security.env import get_env
    url = get_env("LITELLM_PROXY_URL") or get_env("LITELLM_BASE_URL")
    return url.rstrip("/") if url else None


def _api_key() -> str | None:
    from ...security.env import get_env
    return get_env("LITELLM_MASTER_KEY") or get_env("LITELLM_API_KEY")


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def get_costs(start_date: date, end_date: date) -> dict[str, Any]:
    """
    Fetch LiteLLM proxy spend + tokens for the date range, normalised to the
    LLM-connector shape (total_usd / by_model / by_model_tokens / daily).
    """
    base = _base_url()
    api_key = _api_key()
    if not base or not api_key:
        return _empty("not_configured")

    logs = _fetch_spend_logs(base, api_key, start_date, end_date)
    if logs is None:
        return _empty("api_error")

    total = 0.0
    by_model: dict[str, float] = {}
    by_model_tokens: dict[str, dict[str, int]] = {}
    daily_map: dict[str, dict[str, Any]] = {}

    for row in logs:
        if not isinstance(row, dict):
            continue
        model = row.get("model") or "unknown"
        cost = _float(row.get("spend"))
        day = str(row.get("startTime") or row.get("start_time") or "")[:10]
        total += cost
        by_model[model] = by_model.get(model, 0.0) + cost

        bucket = by_model_tokens.setdefault(model, {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "request_count": 0,
        })
        bucket["input_tokens"]            += _int(row.get("prompt_tokens"))
        bucket["output_tokens"]           += _int(row.get("completion_tokens"))
        bucket["cache_read_input_tokens"] += _int(row.get("cache_read_input_tokens"))
        bucket["request_count"]           += 1

        if day:
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


def _fetch_spend_logs(
    base: str, api_key: str, start_date: date, end_date: date
) -> list[dict] | None:
    """GET /spend/logs for the range. Returns a list, or None on error."""
    try:
        import httpx
    except ImportError:
        return None
    try:
        resp = httpx.get(
            f"{base}/spend/logs",
            params={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            headers=_headers(api_key),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.debug("LiteLLM spend/logs fetch failed: %s", e)
        return None
    # Some versions return a bare list, others wrap in {"data": [...]}.
    if isinstance(data, dict):
        return data.get("data", [])
    return data if isinstance(data, list) else []


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
    return bool(_base_url() and _api_key())
