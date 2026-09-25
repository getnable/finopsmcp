"""
LiteLLM proxy cost and usage connector.

LiteLLM is the open-source gateway early AI startups self-host in production
once spend justifies it (MIT, 100+ providers, one OpenAI-compatible endpoint).
When run as a proxy with a database it records per-request spend and tokens,
which nable reads from the admin API and normalises into the LLM cost view.

Reads GET {proxy}/spend/logs (returns per-request rows with model, spend, and
token counts) and aggregates by model and day. get_cost_attribution reads the
daily team, user and tag spend endpoints for spend by team, virtual key, user
and request tag. No data leaves the user's network: the proxy URL is their
own host.

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


# ── Cost attribution: spend by team, virtual key, user or request tag ────────
# The proxy's daily spend tables answer these directly: /team/daily/activity,
# /user/daily/activity and /tag/daily/activity each return one paginated
# SpendAnalyticsPaginatedResponse, whose breakdown carries per-entity (team,
# user or tag) and per-key spend with the team alias, user email and key alias
# as metadata. The per-request /spend/logs rows are not needed, and would mean
# paging through every request of the month.
_ACTIVITY: dict[str, tuple[str, str]] = {
    # dimension -> (endpoint, which breakdown map holds the groups)
    "team":    ("/team/daily/activity", "entities"),
    "user":    ("/user/daily/activity", "entities"),
    "api_key": ("/user/daily/activity", "api_keys"),
    "tag":     ("/tag/daily/activity", "entities"),
}
_LABEL_FIELDS = {"team": ("team_alias",), "user": ("user_email", "user_alias"),
                 "api_key": ("key_alias",), "tag": ()}
_UNASSIGNED = {"team": "(no team)", "user": "(no user)", "api_key": "(no key)",
               "tag": "(untagged)"}
_NOT_AVAILABLE = {
    "project": ("LiteLLM groups spend by team, key, user and tag, not project. Use "
                "dimension='team', or tag requests with the project and use dimension='tag'."),
    "workspace": ("LiteLLM groups spend by team, key, user and tag, not workspace. Use "
                  "dimension='team'."),
    "session": "LiteLLM's daily spend endpoints do not aggregate by session.",
}
_ACTIVITY_PAGE_SIZE = 1000
_ACTIVITY_PAGE_CAP = 100


def get_cost_attribution(dimension: str, start_date: date, end_date: date) -> dict[str, Any]:
    """
    LiteLLM proxy spend for [start_date, end_date] split by team, virtual key,
    user or request tag, in the shared attribution shape (_attribution.py).

    Spend is what the proxy logged per request (source="api"): LiteLLM's own
    price for each call, not the provider's invoice. Tag spend is counted once
    per tag, so a request with two tags appears under both (groups_overlap).
    """
    from ._attribution import groups_result, unread, unsupported

    if dimension in _NOT_AVAILABLE:
        return unsupported(_NOT_AVAILABLE[dimension])
    if dimension not in _ACTIVITY:
        return unsupported(f"LiteLLM cannot attribute cost by '{dimension}'.")
    base, api_key = _base_url(), _api_key()
    if not base or not api_key:
        return unread("not_configured")
    try:
        import httpx
    except ImportError:
        return unread("httpx_missing")

    path, section = _ACTIVITY[dimension]
    sums: dict[str | None, float] = {}
    names: dict[str, str] = {}
    try:
        for page in range(1, _ACTIVITY_PAGE_CAP + 1):
            resp = httpx.get(
                f"{base}{path}",
                params={"start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                        "page": page, "page_size": _ACTIVITY_PAGE_SIZE},
                headers=_headers(api_key), timeout=30,
            )
            resp.raise_for_status()
            data = resp.json() or {}
            for day in data.get("results", []):
                groups = ((day.get("breakdown") or {}).get(section)) or {}
                for gid, entry in groups.items():
                    spend = _float(((entry or {}).get("metrics") or {}).get("spend"))
                    # LiteLLM files spend with no team or user under "Unassigned".
                    key = None if gid in ("", "Unassigned") else gid
                    sums[key] = sums.get(key, 0.0) + spend
                    meta = (entry or {}).get("metadata") or {}
                    label = next((meta[f] for f in _LABEL_FIELDS[dimension] if meta.get(f)), None)
                    if key and label:
                        names[key] = label
                    elif key and dimension == "api_key":
                        names.setdefault(key, f"key {key[:12]}")
            if not (data.get("metadata") or {}).get("has_more"):
                break
        else:
            return unread("truncated", f"{path} still had pages left after "
                                       f"{_ACTIVITY_PAGE_CAP} of {_ACTIVITY_PAGE_SIZE} rows")
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 403):
            return unread("credential_invalid",
                          f"LiteLLM refused the key on {path}; it needs an admin or master "
                          f"key ({e}).", source="error")
        if status == 404:
            return unread("endpoint_missing",
                          f"This LiteLLM proxy has no {path} endpoint (an older release, or "
                          f"no database connected).")
        return unread("api_error", f"{path}: {e}")

    note = "Spend as logged by the LiteLLM proxy at its own model prices, not the provider invoice."
    overlap = dimension == "tag"
    if overlap:
        note += (" A request with several tags counts under each tag, and untagged requests "
                 "are not in any tag, so tag rows do not add up to total spend.")
    out = groups_result(sums, names, source="api", unassigned=_UNASSIGNED[dimension],
                        note=note, groups_overlap=overlap)
    if overlap:
        out["total_usd"] = None
    return out


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
