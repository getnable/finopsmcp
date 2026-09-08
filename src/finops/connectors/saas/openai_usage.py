"""
OpenAI cost and usage connector.

Uses the OpenAI Organization API to fetch:
  - Daily cost by model (via /v1/organization/costs)
  - Token usage breakdown by model (via /v1/organization/usage/completions)

Requires an Admin API key (sk-admin-...) or an org-level key with
  "Read billing" and "Read usage" scopes.

Env vars:
  OPENAI_API_KEY:   standard key (limited usage data)
  OPENAI_ADMIN_KEY: admin/org key (full cost + usage breakdown)
  OPENAI_ORG_ID:    optional, scopes to a specific org

A rejected key (401/403) is not a zero, but /v1/organization/costs is
admin-only, so a standard OPENAI_API_KEY 401s there even when it is
perfectly valid: OPENAI_ADMIN_KEY is optional by design. So a costs-endpoint
auth failure is not proof of a bad key. get_costs() always falls through to
the usage-based estimate on any costs failure, same as before this module
tried to distinguish bad keys at all. The auth check that actually
distinguishes a bad key from a real zero (_is_auth_error) lives at that
fallback endpoint instead, the one any valid key, standard or admin, is
entitled to reach. A 401/403 there returns a typed _credential_error_result
(source="error"), so a caller can tell "nothing was spent" from "nable
cannot see what was spent" apart.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

# Current OpenAI pricing per 1M tokens (USD), updated May 2026
# Source: https://openai.com/pricing
_MODEL_PRICING: dict[str, dict[str, float]] = {
    # GPT-4o family
    "gpt-4o":               {"input": 2.50,   "output": 10.00},
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


def get_projects(api_key: str, org_id: str | None = None) -> dict[str, str]:
    """
    Fetch the list of OpenAI projects and return an id→name mapping.

    Calls GET /v1/organization/projects (requires Admin API key).
    Returns an empty dict gracefully on any error.
    """
    try:
        import httpx
    except ImportError:
        return {}

    try:
        resp = httpx.get(
            "https://api.openai.com/v1/organization/projects",
            params={"limit": 100},
            headers=_headers(api_key, org_id),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.debug("OpenAI projects API unavailable: %s", e)
        return {}

    id_to_name: dict[str, str] = {}
    for proj in data.get("data", []):
        pid  = proj.get("id", "")
        name = proj.get("name") or pid
        if pid:
            id_to_name[pid] = name
    return id_to_name


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
        "by_model_tokens": {"gpt-4o": {"input_tokens": int, "output_tokens": int,
                            "cache_read_input_tokens": int,
                            "cache_creation_input_tokens": int,
                            "request_count": int}, ...},
        "daily": [{"date": "YYYY-MM-DD", "total_usd": float, "by_model": {...}}, ...],
        "source": "api" | "estimated",
      }

    The ``by_model_tokens`` block matches the Anthropic connector shape so the
    AI KPI engine (cache hit rate, context-window utilisation, prompt
    efficiency) works for OpenAI accounts, not just Anthropic ones.
    """
    try:
        import httpx
    except ImportError:
        log.warning("httpx not installed: pip install httpx")
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
        # /v1/organization/costs is admin-only. A standard OPENAI_API_KEY
        # (no OPENAI_ADMIN_KEY set, an intended and documented setup) always
        # 401s here, not because the key is bad, but because a standard key
        # was never entitled to call this endpoint in the first place. So an
        # auth failure here is not evidence of a bad key and must not
        # short-circuit into a credential error. Every failure of this
        # endpoint, auth or otherwise, falls through to the usage-based
        # estimate instead. If the key really is bad, _estimate_from_usage
        # will find out for certain: that endpoint is the one any valid key,
        # standard or admin, is entitled to reach.
        log.warning("OpenAI costs API failed: %s, falling back to usage estimate", e)
        return _estimate_from_usage(start_date, end_date, api_key, org_id)

    # Resolve project IDs to names when using an admin key
    project_names: dict[str, str] = {}
    admin_key = get_env("OPENAI_ADMIN_KEY")
    if admin_key:
        project_names = get_projects(admin_key, org_id)

    parsed = _parse_costs_response(data, project_names=project_names)

    # The costs endpoint returns dollars only, no token counts. Pull per-model
    # tokens from the usage endpoint so the KPI engine has real input/output/
    # cache/request data for OpenAI. A token-fetch failure must not break the
    # cost result, so it's best-effort.
    try:
        toks = _fetch_usage_tokens(start_date, end_date, api_key, org_id)
        parsed["by_model_tokens"] = toks
    except Exception as e:
        log.debug("OpenAI token usage fetch skipped: %s", e)
        parsed.setdefault("by_model_tokens", {})

    return parsed


def _parse_costs_response(
    data: dict,
    project_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    total = 0.0
    by_model: dict[str, float] = {}
    by_project: dict[str, float] = {}
    by_project_named: dict[str, float] = {}
    daily: list[dict] = []

    project_names = project_names or {}

    for bucket in data.get("data", []):
        bucket_total = 0.0
        bucket_by_model: dict[str, float] = {}

        for result in bucket.get("results", []):
            amount = result.get("amount", {}).get("value", 0.0)
            model  = result.get("model_id") or "unknown"
            proj   = result.get("project_id") or "default"
            proj_name = project_names.get(proj, proj)

            bucket_total += amount
            bucket_by_model[model] = bucket_by_model.get(model, 0.0) + amount
            by_model[model]        = by_model.get(model, 0.0) + amount
            by_project[proj]       = by_project.get(proj, 0.0) + amount
            by_project_named[proj_name] = by_project_named.get(proj_name, 0.0) + amount

        total += bucket_total
        ts = bucket.get("start_time", 0)
        from datetime import datetime, timezone
        day_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""
        daily.append({"date": day_str, "total_usd": round(bucket_total, 4),
                      "by_model": {k: round(v, 4) for k, v in bucket_by_model.items()}})

    result: dict[str, Any] = {
        "total_usd":  round(total, 4),
        "by_model":   {k: round(v, 4) for k, v in
                       sorted(by_model.items(), key=lambda x: x[1], reverse=True)},
        "by_project": {k: round(v, 4) for k, v in
                       sorted(by_project.items(), key=lambda x: x[1], reverse=True)},
        "daily":      daily,
        "source":     "api",
    }
    # Only include named breakdown when we actually resolved any names
    if project_names:
        result["by_project_named"] = {
            k: round(v, 4)
            for k, v in sorted(by_project_named.items(), key=lambda x: x[1], reverse=True)
        }
    return result


def _accumulate_tokens(result: dict, by_model_tokens: dict[str, dict[str, int]]) -> None:
    """
    Fold one OpenAI usage/completions result row into a by_model_tokens map
    shaped like the Anthropic connector's (input_tokens / output_tokens /
    cache_read_input_tokens / cache_creation_input_tokens) plus request_count.

    OpenAI reports ``input_tokens`` as the TOTAL input (cached included) and
    ``input_cached_tokens`` as the cached subset. We store fresh input
    (total minus cached) so cache hit-rate math matches Anthropic semantics,
    where ``input_tokens`` means uncached input. OpenAI has no separate
    cache-creation charge, so cache_creation_input_tokens stays 0.
    """
    model = result.get("model_id") or result.get("model") or "unknown"
    total_input  = int(result.get("input_tokens", 0) or 0)
    cached_input = int(result.get("input_cached_tokens", 0) or 0)
    fresh_input  = max(0, total_input - cached_input)
    bucket = by_model_tokens.setdefault(model, {
        "input_tokens":                0,
        "output_tokens":               0,
        "cache_read_input_tokens":     0,
        "cache_creation_input_tokens": 0,
        "request_count":               0,
    })
    bucket["input_tokens"]            += fresh_input
    bucket["output_tokens"]           += int(result.get("output_tokens", 0) or 0)
    bucket["cache_read_input_tokens"] += cached_input
    bucket["request_count"]           += int(result.get("num_model_requests", 0) or 0)


def _fetch_usage_tokens(
    start_date: date,
    end_date: date,
    api_key: str,
    org_id: str | None,
) -> dict[str, dict[str, int]]:
    """
    Fetch per-model token counts from /v1/organization/usage/completions and
    return a by_model_tokens map. Used to enrich the costs-API result, which
    carries dollars but no tokens. Returns {} gracefully on any error.
    """
    try:
        import httpx
    except ImportError:
        return {}

    from datetime import datetime, timezone
    start_ts = int(datetime(start_date.year, start_date.month, start_date.day,
                            tzinfo=timezone.utc).timestamp())
    end_ts   = int(datetime(end_date.year, end_date.month, end_date.day,
                            tzinfo=timezone.utc).timestamp())

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

    by_model_tokens: dict[str, dict[str, int]] = {}
    for bucket in data.get("data", []):
        for result in bucket.get("results", []):
            _accumulate_tokens(result, by_model_tokens)
    return by_model_tokens


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
        if _is_auth_error(e):
            # Unlike the costs endpoint, this one is reachable by a standard
            # key too, so a 401/403 here is the real signal: OpenAI itself
            # rejected this credential, standard or admin.
            log.warning("OpenAI rejected the key on the usage API: %s", e)
            return _credential_error_result(str(e)[:300])
        log.warning("OpenAI usage API also failed: %s", e)
        return _empty_result("api_error")

    total = 0.0
    by_model: dict[str, float] = {}
    by_model_tokens: dict[str, dict[str, int]] = {}
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
            # Same usage rows already carry the token counts the KPI engine needs.
            _accumulate_tokens(result, by_model_tokens)

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
        "by_model_tokens": by_model_tokens,
        "daily":      daily,
        "source":     "estimated",
        "note":       "Costs estimated from token counts × published prices. Does not reflect discounts or credits.",
    }


def _empty_result(reason: str) -> dict[str, Any]:
    return {"total_usd": 0.0, "by_model": {}, "by_project": {}, "by_model_tokens": {},
            "daily": [], "source": "none", "reason": reason}


def _is_auth_error(exc: Exception) -> bool:
    """True when OpenAI itself rejected the credential (401/403), not when
    the request merely failed to complete (network blip, OpenAI down, httpx
    missing). Same distinction the OpenRouter connector already draws with
    its own status-code check: a rejected key is not the same failure as a
    call that never got an answer, and must not be handled the same way.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status in (401, 403)


def _credential_error_result(detail: str) -> dict[str, Any]:
    """A typed result for a credential OpenAI itself rejected.

    Every other failure in this module falls through to _empty_result or the
    token-estimate fallback, which is right for "we could not tell" but wrong
    for "the key is bad": both would otherwise report total_usd=0.0 with
    source="none", indistinguishable from a genuine zero-spend account.
    source="error" is the one carve-out, so a caller can surface "this
    credential needs attention" instead of a silent $0. No sibling saas
    connector has a shared error type to reuse (checked anthropic_usage,
    openrouter, datadog, snowflake): openrouter.py comes closest, with an
    inline 401/403/404 status check, but returns None to fall back rather
    than a typed result. This is deliberately still a plain dict, matching
    every other result this module returns, not a new exception type nothing
    downstream would know to catch.
    """
    out = _empty_result("credential_invalid")
    out["source"] = "error"
    out["error"] = detail
    return out


async def is_configured() -> bool:
    from ...security.env import get_env
    return bool(get_env("OPENAI_API_KEY") or get_env("OPENAI_ADMIN_KEY"))
