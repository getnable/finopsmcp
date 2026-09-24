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

from ...llm_prices import price_for

log = logging.getLogger(__name__)

# Page sizes. /organization/costs takes 1-180 daily buckets per page; the
# /organization/usage/* endpoints cap bucket_width=1d at 31 per page and reject
# anything larger, which silently emptied every token fetch that asked for 180.
_COSTS_PAGE_LIMIT = 180
_USAGE_1D_PAGE_LIMIT = 31
_MAX_PAGES = 100


def _get_all_buckets(httpx: Any, url: str, params: dict[str, Any],
                     headers: dict[str, str]) -> list[dict]:
    """Every bucket of a paged Organization API call (has_more / next_page)."""
    params = dict(params)
    buckets: list[dict] = []
    for _ in range(_MAX_PAGES):
        resp = httpx.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        buckets.extend(data.get("data", []))
        nxt = data.get("next_page")
        if not data.get("has_more") or not nxt:
            return buckets
        params["page"] = nxt
    raise RuntimeError(f"{url} still had more pages after {_MAX_PAGES}")


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
        "limit": _COSTS_PAGE_LIMIT,
    }
    if group_by:
        params["group_by"] = group_by
    else:
        params["group_by"] = ["model", "project_id"]

    try:
        data = {"data": _get_all_buckets(
            httpx, "https://api.openai.com/v1/organization/costs",
            params, _headers(api_key, org_id),
        )}
    except Exception as e:
        log.warning("OpenAI costs API failed: %s — falling back to usage estimate", e)
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

    buckets = _get_all_buckets(
        httpx, "https://api.openai.com/v1/organization/usage/completions",
        {
            "start_time": start_ts,
            "end_time":   end_ts,
            "bucket_width": "1d",
            "group_by": ["model"],
            "limit": _USAGE_1D_PAGE_LIMIT,
        },
        _headers(api_key, org_id),
    )

    by_model_tokens: dict[str, dict[str, int]] = {}
    for bucket in buckets:
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
    Fallback: fetch token usage and multiply by published prices (llm_prices).
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
        buckets = _get_all_buckets(
            httpx, "https://api.openai.com/v1/organization/usage/completions",
            {
                "start_time": start_ts,
                "end_time":   end_ts,
                "bucket_width": "1d",
                "group_by": ["model"],
                "limit": _USAGE_1D_PAGE_LIMIT,
            },
            _headers(api_key, org_id),
        )
    except Exception as e:
        log.warning("OpenAI usage API also failed: %s", e)
        return _empty_result("api_error")

    total = 0.0
    by_model: dict[str, float] = {}
    by_model_tokens: dict[str, dict[str, int]] = {}
    unpriced: dict[str, dict[str, int]] = {}
    daily: list[dict] = []

    for bucket in buckets:
        bucket_total = 0.0
        bucket_by_model: dict[str, float] = {}

        for result in bucket.get("results", []):
            model       = result.get("model_id") or "unknown"
            input_tok   = result.get("input_tokens", 0)
            output_tok  = result.get("output_tokens", 0)
            # Same usage rows already carry the token counts the KPI engine needs.
            _accumulate_tokens(result, by_model_tokens)
            price       = price_for(model)
            if price is None:
                # No published price for this model id. Pricing it at $0 made
                # its spend vanish from the estimate; list it instead.
                u = unpriced.setdefault(model, {"input_tokens": 0, "output_tokens": 0})
                u["input_tokens"] += int(input_tok or 0)
                u["output_tokens"] += int(output_tok or 0)
                continue
            # input_tokens includes the cached subset, which bills at the cached
            # rate rather than full input.
            cached = min(int(result.get("input_cached_tokens", 0) or 0), int(input_tok or 0))
            cost = price.cost(input_tokens=int(input_tok or 0) - cached,
                              cache_read_tokens=cached,
                              output_tokens=int(output_tok or 0))
            bucket_total += cost
            bucket_by_model[model] = bucket_by_model.get(model, 0.0) + cost
            by_model[model]        = by_model.get(model, 0.0) + cost

        total += bucket_total
        ts = bucket.get("start_time", 0)
        from datetime import datetime, timezone
        day_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""
        daily.append({"date": day_str, "total_usd": round(bucket_total, 4),
                      "by_model": {k: round(v, 4) for k, v in bucket_by_model.items()}})

    out = {
        "total_usd":  round(total, 4),
        "by_model":   {k: round(v, 4) for k, v in
                       sorted(by_model.items(), key=lambda x: x[1], reverse=True)},
        "by_project": {},
        "by_model_tokens": by_model_tokens,
        "daily":      daily,
        "source":     "estimated",
        "note":       "Costs estimated from token counts × published prices. Does not reflect discounts or credits.",
    }
    if unpriced:
        out["unpriced_models"] = unpriced
        out["note"] += (f" {len(unpriced)} model(s) have no known price and are "
                        f"excluded from total_usd: {', '.join(sorted(unpriced))}.")
    return out


def _empty_result(reason: str) -> dict[str, Any]:
    return {"total_usd": 0.0, "by_model": {}, "by_project": {}, "by_model_tokens": {},
            "daily": [], "source": "none", "reason": reason}


async def is_configured() -> bool:
    from ...security.env import get_env
    return bool(get_env("OPENAI_API_KEY") or get_env("OPENAI_ADMIN_KEY"))
