"""
Anthropic API cost and usage connector.

Tracks spend across Claude models via:
  1. Anthropic Cost API — /v1/organizations/cost_report (actual USD; needs an Admin key)
  2. Anthropic Usage API (beta) — /v1/organizations/{org}/usage (token counts)
  3. Estimated from token counts × published prices (fallback), per model from
     finops.llm_prices
  4. Cost by workspace (Cost API), API key or user (Messages Usage API), via
     get_cost_attribution

Env vars:
  ANTHROPIC_API_KEY          — standard key
  ANTHROPIC_ADMIN_KEY        — org-level key (preferred for usage data)
  ANTHROPIC_ORGANIZATION_ID  — required for org-level usage endpoint
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from ...llm_prices import price_for

log = logging.getLogger(__name__)

_API_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
# Cost API pagination safety cap. 31 daily buckets per page, so 60 pages covers
# ~5 years; beyond that we fall back rather than report a truncated total.
_COST_PAGE_CAP = 60


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "anthropic-beta": "usage-1",
        "content-type": "application/json",
    }


def _admin_headers(admin_key: str) -> dict[str, str]:
    return {
        "x-api-key": admin_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


# Admin list endpoints page with after_id and allow up to 1000 per page.
_LIST_PAGE_CAP = 20


def _admin_list(httpx: Any, path: str, admin_key: str,
                params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every object of a cursor-paged Admin API list (data / has_more /
    last_id, continued with ?after_id=): workspaces, api_keys, users."""
    params = {"limit": 1000, **(params or {})}
    items: list[dict[str, Any]] = []
    for _ in range(_LIST_PAGE_CAP):
        resp = httpx.get(f"{_API_BASE}{path}", params=params,
                         headers=_admin_headers(admin_key), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        page = data.get("data", [])
        items.extend(page)
        last = data.get("last_id") or (page[-1].get("id") if page else None)
        if not data.get("has_more") or not last:
            return items
        params["after_id"] = last
    raise RuntimeError(f"{path} still had more pages after {_LIST_PAGE_CAP}")


def get_workspaces(api_key: str, org_id: str | None = None) -> list[dict[str, Any]]:
    """
    List all workspaces in an Anthropic organization, archived ones included.

    Calls GET /v1/organizations/workspaces with an Admin key. org_id is kept
    for callers of the old signature and is not needed: the Admin key names
    the organization. Returns a list of workspace dicts with at least
    {"id", "name"}, or an empty list gracefully when it cannot be read.
    """
    try:
        import httpx
        return _admin_list(httpx, "/v1/organizations/workspaces", api_key,
                           {"include_archived": True})
    except Exception as e:
        log.debug("Anthropic workspaces list unavailable: %s", e)
        return []


def _names(items: list[dict[str, Any]], *fields: str) -> dict[str, str]:
    """id -> the first of ``fields`` that is set, for labelling groups."""
    out: dict[str, str] = {}
    for item in items:
        iid = item.get("id")
        label = next((item[f] for f in fields if item.get(f)), None)
        if iid and label:
            out[iid] = label
    return out


def _list_names(admin_key: str, path: str, *fields: str) -> dict[str, str]:
    try:
        import httpx
        return _names(_admin_list(httpx, path, admin_key), *fields)
    except Exception as e:
        log.debug("Anthropic %s list unavailable: %s", path, e)
        return {}


class _Truncated(RuntimeError):
    """A report still had pages left at the cap: its sum would undercount."""


def _report_buckets(httpx: Any, path: str, admin_key: str,
                    params: dict[str, Any]) -> list[dict[str, Any]]:
    """Every bucket of a paged cost/usage report (has_more / next_page)."""
    params = dict(params)
    buckets: list[dict[str, Any]] = []
    for _ in range(_COST_PAGE_CAP):
        resp = httpx.get(f"{_API_BASE}{path}", params=params,
                         headers=_admin_headers(admin_key), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        buckets.extend(data.get("data", []))
        if data.get("has_more") and data.get("next_page"):
            params["page"] = data["next_page"]
        else:
            return buckets
    raise _Truncated(f"{path} still had more pages after {_COST_PAGE_CAP}")


def _report_window(start_date: date, end_date: date) -> dict[str, str]:
    # ending_at is exclusive (only buckets that END BEFORE it are returned), so
    # add a day to include the full final day.
    return {"starting_at": f"{start_date.isoformat()}T00:00:00Z",
            "ending_at": f"{(end_date + timedelta(days=1)).isoformat()}T00:00:00Z",
            "bucket_width": "1d"}


def _cents_to_usd(amount: Any) -> float | None:
    """Cost report ``amount`` is a decimal string in cents."""
    try:
        return float(amount) / 100.0
    except (TypeError, ValueError):
        return None


def get_costs(
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """
    Fetch Anthropic usage costs for the given date range.
    Falls back to estimated costs if the org-level API is unavailable.

    When the Cost API answers (Admin key), also returns ``by_workspace``
    mapping workspace names to their billed costs, read from the same Cost
    API grouped by workspace_id.
    """
    from ...security.env import get_env
    admin_key = get_env("ANTHROPIC_ADMIN_KEY")
    api_key   = admin_key or get_env("ANTHROPIC_API_KEY")
    org_id    = get_env("ANTHROPIC_ORGANIZATION_ID") or None

    if not api_key:
        return _empty("not_configured")

    # Prefer the org Cost API: actual billed USD, not estimated. Requires an
    # Admin key (sk-ant-admin...). When it works, the costs are authoritative.
    if admin_key:
        cost = get_cost_report(admin_key, start_date, end_date)
        if cost.get("source") == "cost_api":
            # The Cost API reports dollars, not token counts. Best-effort enrich
            # by_model_tokens from the Usage API so the AI-KPI layer (cache hit
            # rate, context-window utilisation) still has data; the dollar figures
            # stay authoritative from the Cost API.
            if org_id:
                usage = _fetch_org_usage(admin_key, org_id, start_date, end_date)
                if usage.get("by_model_tokens"):
                    cost["by_model_tokens"] = usage["by_model_tokens"]
            by_workspace = _fetch_by_workspace(admin_key, start_date, end_date)
            if by_workspace:
                cost["by_workspace"] = by_workspace
            return cost
        # Cost API unavailable (not enterprise, missing permission, network):
        # fall through to the usage/estimate path below.

    # Org-level usage endpoint (estimated costs)
    if org_id:
        result = _fetch_org_usage(api_key, org_id, start_date, end_date)
        if result.get("source") == "api":
            return result

    # Fall back to workspace-level token usage
    return _fetch_workspace_usage(api_key, start_date, end_date)


def get_cost_report(
    admin_key: str,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """
    Fetch ACTUAL Anthropic costs (USD) from the organization Cost API:
        GET /v1/organizations/cost_report

    Returns the same shape as get_costs() (total_usd, by_model, daily) with
    source="cost_api". Requires an Admin API key (sk-ant-admin...). The API
    reports ``amount`` in the lowest currency unit (cents) as a decimal string,
    so we divide by 100 for dollars. ``by_model_tokens`` is left empty here: the
    Cost API reports dollars, not token counts (the Usage API path fills those).
    Any HTTP/permission error returns an _empty() result so get_costs() can fall
    back to the usage/estimate path.
    """
    try:
        import httpx
    except ImportError:
        return _empty("httpx_missing")

    total = 0.0
    by_model: dict[str, float] = {}
    daily: list[dict] = []

    try:
        buckets = _report_buckets(
            httpx, "/v1/organizations/cost_report", admin_key,
            {**_report_window(start_date, end_date),
             "group_by[]": "description",  # per-model + token-type breakdown
             "limit": 31})
    except _Truncated:
        # Never report a known-incomplete sum as authoritative billed dollars.
        # Fall back to the usage/estimate path instead of undercounting silently.
        log.warning(
            "Anthropic Cost API pagination hit the %d-page cap for %s..%s; "
            "falling back rather than reporting a truncated total.",
            _COST_PAGE_CAP, start_date, end_date,
        )
        return _empty("cost_api_truncated")
    except Exception as e:
        log.debug("Anthropic Cost API unavailable, falling back: %s", e)
        return _empty("cost_api_unavailable")

    for bucket in buckets:
        day = (bucket.get("starting_at") or "")[:10]
        if not day:
            # Undated bucket (not expected from the real API): skip it so
            # the daily breakdown and total_usd cannot diverge.
            continue
        for item in bucket.get("results", []):
            usd = _cents_to_usd(item.get("amount"))
            if not usd:
                continue
            # model is null for non-token costs (web_search, etc.); fall
            # back to the cost_type so those still get a line item.
            label = item.get("model") or item.get("cost_type") or "other"
            total += usd
            by_model[label] = by_model.get(label, 0.0) + usd
            existing = next((d for d in daily if d["date"] == day), None)
            if existing:
                existing["total_usd"] = round(existing["total_usd"] + usd, 4)
                existing["by_model"][label] = round(
                    existing["by_model"].get(label, 0.0) + usd, 4)
            else:
                daily.append({"date": day, "total_usd": round(usd, 4),
                              "by_model": {label: round(usd, 4)}})

    return {
        "total_usd":       round(total, 4),
        "by_model":        {k: round(v, 4) for k, v in
                            sorted(by_model.items(), key=lambda x: x[1], reverse=True)},
        "by_model_tokens": {},
        "daily":           sorted(daily, key=lambda d: d["date"]),
        "source":          "cost_api",
    }


def _fetch_by_workspace(
    admin_key: str,
    start_date: date,
    end_date: date,
) -> dict[str, float]:
    """
    Billed cost per workspace name, from the Cost API grouped by workspace_id.
    Returns {} when it cannot be read: by_workspace only enriches get_costs(),
    and get_cost_attribution() is where a failed read is reported as one.
    """
    res = get_cost_attribution("workspace", start_date, end_date, admin_key=admin_key)
    return {g["group"]: g["cost_usd"] for g in res.get("groups", [])}


# ── Cost attribution: spend by workspace, API key or user ────────────────────
# The Cost API groups by description and workspace_id only, so workspace is
# billed dollars. The Messages Usage API groups by api_key_id and account_id
# (plus model), so API key and user spend is priced from tokens: an estimate.
_USAGE_GROUP = {"api_key": "api_key_id", "user": "account_id"}  # pragma: allowlist secret
_UNASSIGNED = {"workspace": "Default workspace", "api_key": "(no API key)",
               "user": "(no user account)"}
_NOT_AVAILABLE = {
    "project": "Anthropic has workspaces, not projects. Use dimension='workspace'.",
    "team": ("Anthropic has no team field. Workspaces are the usual stand-in for a team: "
             "use dimension='workspace'."),
    "tag": "Anthropic's cost and usage reports carry no request tags or metadata.",
    "session": "Anthropic's cost and usage reports carry no session id.",
}
_ESTIMATE_NOTE = ("Estimated from Messages API token usage at standard list price. Does "
                  "not reflect discounts, batch, priority or regional pricing, and web "
                  "search and other tool fees are not included.")


def _usage_row_cost(row: dict[str, Any]) -> float | None:
    """List-price USD for one usage_report/messages row, None when unpriced."""
    price = price_for(row.get("model") or "")
    if price is None:
        return None
    split = row.get("cache_creation") if isinstance(row.get("cache_creation"), dict) else {}
    return price.cost(
        input_tokens=_int(row.get("uncached_input_tokens")),
        output_tokens=_int(row.get("output_tokens")),
        cache_read_tokens=_int(row.get("cache_read_input_tokens")),
        cache_write_5m_tokens=_int(split.get("ephemeral_5m_input_tokens")),
        cache_write_1h_tokens=_int(split.get("ephemeral_1h_input_tokens")),
    )


def get_cost_attribution(
    dimension: str,
    start_date: date,
    end_date: date,
    admin_key: str | None = None,
) -> dict[str, Any]:
    """
    Anthropic spend for [start_date, end_date] split by workspace, API key or
    user, in the shared attribution shape (see _attribution.py).

    workspace: GET /v1/organizations/cost_report?group_by[]=workspace_id,
    billed dollars (source="cost_api"); null workspace_id is the default
    workspace. api_key / user: GET /v1/organizations/usage_report/messages
    grouped by api_key_id or account_id plus model, priced at list price
    (source="estimated"). Names come from the Admin API workspace, API key
    and user lists. Every one of these needs an Admin key.
    """
    from ._attribution import groups_result, unread, unsupported

    if admin_key is None:
        from ...security.env import get_env
        admin_key = get_env("ANTHROPIC_ADMIN_KEY")
        # Not connected comes first, as for every provider.
        if not admin_key and not get_env("ANTHROPIC_API_KEY"):
            return unread("not_configured")
    if dimension in _NOT_AVAILABLE:
        return unsupported(_NOT_AVAILABLE[dimension])
    if dimension not in _UNASSIGNED:
        return unsupported(f"Anthropic cannot attribute cost by '{dimension}'.")
    if not admin_key:
        return unread("admin_key_required",
                      "Anthropic's cost and usage reports need an Admin key: set "
                      "ANTHROPIC_ADMIN_KEY (sk-ant-admin...). A regular API key "
                      "cannot read them.")
    try:
        import httpx
    except ImportError:
        return unread("httpx_missing")

    window = {**_report_window(start_date, end_date), "limit": 31}
    sums: dict[str | None, float] = {}
    unpriced: dict[str, dict[str, int]] = {}
    try:
        if dimension == "workspace":
            buckets = _report_buckets(httpx, "/v1/organizations/cost_report", admin_key,
                                      {**window, "group_by[]": ["workspace_id"]})
            for bucket in buckets:
                for item in bucket.get("results", []):
                    usd = _cents_to_usd(item.get("amount"))
                    if usd:
                        gid = item.get("workspace_id")
                        sums[gid] = sums.get(gid, 0.0) + usd
        else:
            field = _USAGE_GROUP[dimension]
            buckets = _report_buckets(httpx, "/v1/organizations/usage_report/messages",
                                      admin_key, {**window, "group_by[]": [field, "model"]})
            for bucket in buckets:
                for row in bucket.get("results", []):
                    cost = _usage_row_cost(row)
                    if cost is None:
                        u = unpriced.setdefault(row.get("model") or "unknown",
                                                {"input_tokens": 0, "output_tokens": 0})
                        u["input_tokens"] += _int(row.get("uncached_input_tokens"))
                        u["output_tokens"] += _int(row.get("output_tokens"))
                        continue
                    gid = row.get(field)
                    sums[gid] = sums.get(gid, 0.0) + cost
    except _Truncated as e:
        return unread("truncated", str(e))
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 403):
            return unread("credential_invalid",
                          f"Anthropic refused ANTHROPIC_ADMIN_KEY ({e}).", source="error")
        return unread("api_error", str(e))

    if dimension == "workspace":
        names = _names(get_workspaces(admin_key), "name")
        return groups_result(sums, names, source="cost_api", unassigned=_UNASSIGNED[dimension])
    if dimension == "api_key":
        names = _list_names(admin_key, "/v1/organizations/api_keys", "name", "partial_key_hint")
    else:
        names = _list_names(admin_key, "/v1/organizations/users", "email", "name")
    out = groups_result(sums, names, source="estimated", unassigned=_UNASSIGNED[dimension],
                        note=_ESTIMATE_NOTE)
    if unpriced:
        out["unpriced_models"] = unpriced
        out["note"] += (f" {len(unpriced)} model(s) have no known price and are excluded: "
                        f"{', '.join(sorted(unpriced))}.")
    return out


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


def _int(value: Any) -> int:
    """Coerce an API token/count field to int, tolerating None and strings."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_usage(data: dict, source: str) -> dict[str, Any]:
    total = 0.0
    by_model: dict[str, float] = {}
    by_model_tokens: dict[str, dict[str, int]] = {}
    daily: list[dict] = []

    # Best-effort request/error tallies. The Usage API returns token counts, not
    # request/error counts, so these usually stay 0 and the keys are omitted from
    # the result, keeping error_spend_estimate in its graceful "not available"
    # path. They populate only if a future/enterprise response carries them.
    total_requests = 0
    error_requests = 0
    unpriced: dict[str, dict[str, int]] = {}

    for entry in data.get("data", data.get("usage", [])):
        model      = entry.get("model") or entry.get("model_id") or "unknown"
        # Fresh (uncached) input. The workspace report names this `input_tokens`;
        # the org usage report names it `uncached_input_tokens`. Accept either.
        input_tok  = _int(entry.get("input_tokens", entry.get("uncached_input_tokens", 0)))
        output_tok = _int(entry.get("output_tokens", 0))
        # Prompt-cache token counts, billed separately by Anthropic. The KPI layer
        # reads these to compute cache hit rate and cache-read savings.
        cache_read     = _int(entry.get("cache_read_input_tokens", 0))
        cache_creation = _int(entry.get("cache_creation_input_tokens", 0))
        day        = entry.get("date") or entry.get("timestamp", "")[:10]

        req = _int(entry.get("request_count", entry.get("num_requests", 0)))
        total_requests += req
        error_requests += _int(entry.get("error_count", entry.get("num_errors", 0)))

        # If actual cost is in the response, use it. Otherwise price every token
        # class at the model's own rate: cache reads and writes are billed too,
        # and on a cache-heavy workload they are most of the input bill.
        cost: float | None = float(entry.get("cost_usd", 0.0) or 0.0)
        if cost == 0.0:
            price = price_for(model)
            if price is None:
                # No confirmed price. Pricing it at $0 made its spend vanish
                # from the estimate; list it instead. input_tokens here is all
                # input, cached included, the shape openai_usage reports.
                u = unpriced.setdefault(model, {"input_tokens": 0, "output_tokens": 0})
                u["input_tokens"] += input_tok + cache_read + cache_creation
                u["output_tokens"] += output_tok
                cost = None
            else:
                split = entry.get("cache_creation")
                write_1h = (min(cache_creation, _int(split.get("ephemeral_1h_input_tokens", 0)))
                            if isinstance(split, dict) else 0)
                cost = price.cost(input_tokens=input_tok, output_tokens=output_tok,
                                  cache_read_tokens=cache_read,
                                  cache_write_5m_tokens=cache_creation - write_1h,
                                  cache_write_1h_tokens=write_1h)
        # Sub-keys match what ai_kpis.py reads: input_tokens / output_tokens /
        # cache_read_input_tokens / cache_creation_input_tokens.
        bucket = by_model_tokens.setdefault(model, {
            "input_tokens":                0,
            "output_tokens":               0,
            "cache_read_input_tokens":     0,
            "cache_creation_input_tokens": 0,
            "request_count":               0,
        })
        bucket["input_tokens"]                += input_tok
        bucket["output_tokens"]               += output_tok
        bucket["cache_read_input_tokens"]     += cache_read
        bucket["cache_creation_input_tokens"] += cache_creation
        # Per-model request count when the org/enterprise Usage API carries it; lets
        # context-window utilisation compute a real per-request average. Usually 0 on
        # the token-only Usage API, in which case the KPI marks it unavailable.
        bucket["request_count"]               += req

        if cost is None:
            continue
        total += cost
        by_model[model] = by_model.get(model, 0.0) + cost

        # Accumulate daily
        existing = next((d for d in daily if d["date"] == day), None)
        if existing:
            existing["total_usd"] = round(existing["total_usd"] + cost, 4)
            existing["by_model"][model] = round(
                existing["by_model"].get(model, 0.0) + cost, 4)
        else:
            daily.append({"date": day, "total_usd": round(cost, 4),
                          "by_model": {model: round(cost, 4)}})

    result: dict[str, Any] = {
        "total_usd":      round(total, 4),
        "by_model":       {k: round(v, 4) for k, v in
                           sorted(by_model.items(), key=lambda x: x[1], reverse=True)},
        "by_model_tokens": by_model_tokens,
        "daily":          sorted(daily, key=lambda d: d["date"]),
        "source":         source,
        **({"note": "Costs estimated from token counts × published prices."} if source == "estimated" else {}),
    }
    # Surface request/error counts only when the API actually returned them, so
    # error_spend_estimate reports a real rate rather than a fabricated 0%.
    if total_requests > 0:
        result["total_requests"] = total_requests
        result["error_requests"] = error_requests
    if unpriced:
        result["unpriced_models"] = unpriced
        result["note"] = (result.get("note", "") + f" {len(unpriced)} model(s) have no known "
                          f"price and are excluded from total_usd: "
                          f"{', '.join(sorted(unpriced))}.").strip()
    return result


def _empty(reason: str) -> dict[str, Any]:
    return {"total_usd": 0.0, "by_model": {}, "by_model_tokens": {},
            "daily": [], "source": "none", "reason": reason}


async def is_configured() -> bool:
    from ...security.env import get_env
    return bool(get_env("ANTHROPIC_API_KEY") or get_env("ANTHROPIC_ADMIN_KEY"))
