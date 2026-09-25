"""
OpenAI cost and usage connector.

Uses the OpenAI Organization API to fetch:
  - Daily cost by line item and project (via /v1/organization/costs)
  - Token usage breakdown by model (via /v1/organization/usage/completions)
  - Cost by project, API key or user (get_cost_attribution)

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


_ORG_API = "https://api.openai.com/v1/organization"


def _list_all(httpx: Any, url: str, headers: dict[str, str],
              params: dict[str, Any] | None = None) -> list[dict]:
    """Every object of a cursor-paged admin list (data / has_more / last_id,
    continued with ?after=), the shape of /organization/projects, /users and
    /projects/{id}/api_keys."""
    params = {"limit": 100, **(params or {})}
    items: list[dict] = []
    for _ in range(_MAX_PAGES):
        resp = httpx.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        page = data.get("data", [])
        items.extend(page)
        last = data.get("last_id") or (page[-1].get("id") if page else None)
        if not data.get("has_more") or not last:
            return items
        params["after"] = last
    raise RuntimeError(f"{url} still had more pages after {_MAX_PAGES}")


def get_projects(api_key: str, org_id: str | None = None) -> dict[str, str]:
    """
    Fetch the list of OpenAI projects and return an id→name mapping.

    Calls GET /v1/organization/projects (requires Admin API key), every page,
    archived projects included: last month's spend can sit on a project that
    has been archived since. Returns an empty dict gracefully on any error.
    """
    try:
        import httpx
    except ImportError:
        return {}

    try:
        projects = _list_all(httpx, f"{_ORG_API}/projects", _headers(api_key, org_id),
                             {"include_archived": True})
    except Exception as e:
        log.debug("OpenAI projects API unavailable: %s", e)
        return {}

    id_to_name: dict[str, str] = {}
    for proj in projects:
        pid  = proj.get("id", "")
        name = proj.get("name") or pid
        if pid:
            id_to_name[pid] = name
    return id_to_name


# API keys are listed per project, one call each. Past this many projects the
# rest stay as raw key ids rather than fan out into hundreds of calls.
_KEY_NAME_PROJECT_CAP = 50


def _api_key_names(api_key: str, org_id: str | None, project_ids: list[str]) -> dict[str, str]:
    """key id -> key name, from GET /organization/projects/{id}/api_keys.
    Best effort: a project that cannot be listed leaves its keys unnamed."""
    try:
        import httpx
    except ImportError:
        return {}
    names: dict[str, str] = {}
    for pid in project_ids[:_KEY_NAME_PROJECT_CAP]:
        try:
            keys = _list_all(httpx, f"{_ORG_API}/projects/{pid}/api_keys",
                             _headers(api_key, org_id))
        except Exception as e:
            log.debug("OpenAI api_keys list failed for %s: %s", pid, e)
            continue
        for k in keys:
            if k.get("id") and k.get("name"):
                names[k["id"]] = k["name"]
    return names


def _user_names(api_key: str, org_id: str | None) -> dict[str, str]:
    """user id -> email (or name), from GET /organization/users. Best effort."""
    try:
        import httpx
        users = _list_all(httpx, f"{_ORG_API}/users", _headers(api_key, org_id))
    except Exception as e:
        log.debug("OpenAI users list unavailable: %s", e)
        return {}
    return {u["id"]: (u.get("email") or u.get("name") or u["id"])
            for u in users if u.get("id")}


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
        "limit": _COSTS_PAGE_LIMIT,
    }
    # The costs endpoint groups by project_id, line_item and api_key_id only.
    # It used to be asked for "model", which is not in that list; the model
    # is the first part of each line item ("gpt-4o-2024-08-06, input").
    params["group_by"] = group_by or ["project_id", "line_item"]

    try:
        data = {"data": _get_all_buckets(
            httpx, "https://api.openai.com/v1/organization/costs",
            params, _headers(api_key, org_id),
        )}
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


def _line_item_model(line_item: Any) -> str | None:
    """The model a costs line item bills for: "gpt-4o-2024-08-06, input" ->
    "gpt-4o-2024-08-06". A line item that is not per model ("web search tool
    calls") has no comma and comes back whole, so it still gets a label."""
    if not line_item or not isinstance(line_item, str):
        return None
    return line_item.split(",", 1)[0].strip() or None


def _row_model(result: dict) -> str:
    """UsageCompletionsResult names the model `model`; `model_id` is kept for
    older payloads and the fixtures that still use it."""
    return result.get("model") or result.get("model_id") or "unknown"


def _usage_row_cost(result: dict) -> float | None:
    """List-price USD for one usage/completions row, or None when the model has
    no known price (the caller lists it; pricing it at $0 would hide it).
    input_tokens includes the cached subset, which bills at the cached rate."""
    price = price_for(_row_model(result))
    if price is None:
        return None
    input_tok = int(result.get("input_tokens", 0) or 0)
    cached = min(int(result.get("input_cached_tokens", 0) or 0), input_tok)
    return price.cost(input_tokens=input_tok - cached, cache_read_tokens=cached,
                      output_tokens=int(result.get("output_tokens", 0) or 0))


def _note_unpriced(unpriced: dict[str, dict[str, int]], result: dict) -> None:
    u = unpriced.setdefault(_row_model(result), {"input_tokens": 0, "output_tokens": 0})
    u["input_tokens"] += int(result.get("input_tokens", 0) or 0)
    u["output_tokens"] += int(result.get("output_tokens", 0) or 0)


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
            model  = (result.get("model_id") or _line_item_model(result.get("line_item"))
                      or "unknown")
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
    unpriced: dict[str, dict[str, int]] = {}
    daily: list[dict] = []

    for bucket in buckets:
        bucket_total = 0.0
        bucket_by_model: dict[str, float] = {}

        for result in bucket.get("results", []):
            model       = _row_model(result)
            # Same usage rows already carry the token counts the KPI engine needs.
            _accumulate_tokens(result, by_model_tokens)
            cost = _usage_row_cost(result)
            if cost is None:
                # No published price for this model id. Pricing it at $0 made
                # its spend vanish from the estimate; list it instead.
                _note_unpriced(unpriced, result)
                continue
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


# ── Cost attribution: spend by project, API key or user ──────────────────────
# dimension -> (the org API field that carries it, whether the billed costs
# endpoint can group by it). Costs group by project_id, line_item and
# api_key_id; user_id exists on usage rows only, so user spend is an estimate.
_ATTRIBUTION_FIELDS: dict[str, tuple[str, bool]] = {
    "project": ("project_id", True),
    "api_key": ("api_key_id", True),
    "user":    ("user_id", False),
}
_UNASSIGNED = {"project": "(no project)", "api_key": "(no API key)", "user": "(no user)"}
_NOT_AVAILABLE = {
    "workspace": "OpenAI has projects, not workspaces. Use dimension='project'.",
    "team": ("OpenAI has no team field. Projects are the usual stand-in for a team or "
             "product area: use dimension='project'."),
    "tag": "OpenAI's cost and usage APIs carry no request tags or metadata.",
    "session": "OpenAI's cost and usage APIs carry no session id.",
}
_ESTIMATE_NOTE = ("Estimated from completions token usage at list price. Does not reflect "
                  "discounts or credits, and embeddings, images, audio and tool fees are "
                  "not included.")


def _unix(d: date) -> int:
    from datetime import datetime, timezone
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _amount_usd(result: dict) -> float | None:
    """USD of one costs result, or None when its amount cannot be read. An
    unreadable amount is not a $0 line item; the caller counts it."""
    amount = result.get("amount")
    value = amount.get("value") if isinstance(amount, dict) else None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_cost_attribution(dimension: str, start_date: date, end_date: date) -> dict[str, Any]:
    """
    OpenAI spend for [start_date, end_date] split by project, API key or user.

    project and api_key read billed dollars from /v1/organization/costs
    (group_by=project_id or api_key_id, source="cost_api"). If that endpoint
    fails they fall back to /v1/organization/usage/completions priced at list
    price (source="estimated"). user is always that estimate: costs carry no
    user_id. Names come from /organization/projects, /projects/{id}/api_keys
    and /organization/users when the key can read them, raw ids otherwise.

    Returns the shared attribution shape (see _attribution.py): groups on a
    read, source="unsupported" for a dimension OpenAI does not have, and an
    unread result, never an empty $0, when nothing could be read.
    """
    from ._attribution import groups_result, unread, unsupported

    from ...security.env import get_env
    api_key = get_env("OPENAI_ADMIN_KEY") or get_env("OPENAI_API_KEY")
    org_id = get_env("OPENAI_ORG_ID") or None
    # Not connected comes first: "OpenAI has no team field" is only worth
    # saying to someone who has OpenAI connected.
    if not api_key:
        return unread("not_configured")
    if dimension in _NOT_AVAILABLE:
        return unsupported(_NOT_AVAILABLE[dimension])
    if dimension not in _ATTRIBUTION_FIELDS:
        return unsupported(f"OpenAI cannot attribute cost by '{dimension}'.")
    try:
        import httpx
    except ImportError:
        return unread("httpx_missing")

    field, billed = _ATTRIBUTION_FIELDS[dimension]
    headers = _headers(api_key, org_id)
    # end_time is exclusive, so the day after end_date keeps end_date whole.
    window = {"start_time": _unix(start_date),
              "end_time": _unix(end_date + timedelta(days=1)),
              "bucket_width": "1d"}

    sums: dict[str | None, float] | None = None
    source, note = "cost_api", None
    unpriced: dict[str, dict[str, int]] = {}
    unreadable = 0
    if billed:
        try:
            buckets = _get_all_buckets(
                httpx, f"{_ORG_API}/costs",
                {**window, "group_by": [field], "limit": _COSTS_PAGE_LIMIT}, headers)
            sums = {}
            for bucket in buckets:
                for result in bucket.get("results", []):
                    usd = _amount_usd(result)
                    if usd is None:
                        unreadable += 1
                        continue
                    gid = result.get(field)
                    sums[gid] = sums.get(gid, 0.0) + usd
        except Exception as e:
            log.info("OpenAI costs by %s failed (%s), estimating from usage", field, e)

    if sums is None:
        try:
            buckets = _get_all_buckets(
                httpx, f"{_ORG_API}/usage/completions",
                {**window, "group_by": [field, "model"], "limit": _USAGE_1D_PAGE_LIMIT},
                headers)
        except Exception as e:
            if _is_auth_error(e):
                return unread(
                    "credential_invalid",
                    "OpenAI refused this key on the organization cost and usage APIs, "
                    "which need an Admin key: set OPENAI_ADMIN_KEY (sk-admin-...). "
                    f"({e})", source="error")
            return unread("api_error", str(e))
        sums, source, note = {}, "estimated", _ESTIMATE_NOTE
        for bucket in buckets:
            for result in bucket.get("results", []):
                cost = _usage_row_cost(result)
                if cost is None:
                    _note_unpriced(unpriced, result)
                    continue
                gid = result.get(field)
                sums[gid] = sums.get(gid, 0.0) + cost

    if dimension == "project":
        names = get_projects(api_key, org_id)
    elif dimension == "api_key":
        names = _api_key_names(api_key, org_id, list(get_projects(api_key, org_id)))
    else:
        names = _user_names(api_key, org_id)

    out = groups_result(sums, names, source=source, unassigned=_UNASSIGNED[dimension],
                        note=note)
    if unpriced:
        out["unpriced_models"] = unpriced
        out["note"] = (f"{out.get('note', '')} {len(unpriced)} model(s) have no known price "
                       f"and are excluded: {', '.join(sorted(unpriced))}.").strip()
    if unreadable:
        out["unreadable_rows"] = unreadable
        out["note"] = (f"{out.get('note', '')} {unreadable} cost row(s) had no readable "
                       f"amount and are excluded.").strip()
    return out


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
