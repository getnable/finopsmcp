"""
AI cost attribution: who and what the AI bill is for.

get_llm_costs answers "how much, on which model". This answers "for which
project, workspace, API key, team, user, tag or session", from the providers
that record one:

  openai     project and API key: billed costs (/v1/organization/costs);
             user: estimated from usage rows
  anthropic  workspace: billed costs (Cost API);
             API key and user: estimated from the Messages Usage API
  litellm    team, virtual key, user and request tag: proxy-logged spend
  langfuse   trace tag, user and session: Langfuse-calculated spend

Every provider's answer keeps its own source label. A provider that cannot
split by the asked dimension says so under not_available; one that could and
was not read is listed under failed_providers and contributes nothing, never
a $0 row.

Bedrock, Vertex and OpenRouter spend is not attributed here: it is in
get_llm_costs, by model.
"""
from __future__ import annotations

import concurrent.futures
import importlib
import logging
from datetime import date, timedelta
from typing import Any

from .saas._attribution import DIMENSIONS

log = logging.getLogger(__name__)

# Order is the display order when two groups cost the same.
PROVIDERS: tuple[str, ...] = ("openai", "anthropic", "litellm", "langfuse")
_MODULES = {
    "openai": "finops.connectors.saas.openai_usage",
    "anthropic": "finops.connectors.saas.anthropic_usage",
    "litellm": "finops.connectors.saas.litellm",
    "langfuse": "finops.connectors.saas.langfuse",
}
# Providers that bill, and sources that observe calls a biller also bills: a
# LiteLLM proxy in front of OpenAI logs the same request OpenAI invoices.
_BILLERS = frozenset({"openai", "anthropic"})
_OBSERVERS = frozenset({"litellm", "langfuse"})

# What people call the thing they want to split by, mapped to the field a
# provider actually records. No provider has a customer, feature or agent
# field; those arrive as request tags (LiteLLM) or trace tags (Langfuse).
_ALIASES = {
    "projects": "project", "workspaces": "workspace", "key": "api_key",
    "keys": "api_key", "api-key": "api_key", "apikey": "api_key", "api_keys": "api_key",  # pragma: allowlist secret
    "virtual_key": "api_key", "teams": "team", "users": "user", "tags": "tag",
    "label": "tag", "labels": "tag", "sessions": "session",
    "customer": "tag", "customers": "tag", "feature": "tag", "features": "tag",
    "agent": "tag", "agents": "tag",
}
_TAG_STANDIN_NOTE = (
    "No AI provider records a {asked} field, so {asked} spend comes from request "
    "tags: LiteLLM request tags or Langfuse trace tags such as '{asked}:acme'. "
    "Groups below are tags; untagged spend is not in them.")

NOT_COVERED = ("Bedrock, Vertex AI and OpenRouter spend is not split here; "
               "get_llm_costs has it by model.")


def normalize_dimension(raw: str | None) -> tuple[str | None, str | None]:
    """(dimension, note) for what the caller typed; dimension None if unknown."""
    asked = (raw or "").strip().lower().replace(" ", "_")
    if asked in DIMENSIONS:
        return asked, None
    dim = _ALIASES.get(asked)
    if dim is None:
        return None, None
    note = _TAG_STANDIN_NOTE.format(asked=asked.rstrip("s")) if dim == "tag" and asked not in (
        "tags", "label", "labels") else None
    return dim, note


def _fetch(provider: str, dimension: str, start_date: date, end_date: date) -> dict[str, Any]:
    # Resolved at call time so a test (or a plugin) can replace one provider.
    module = importlib.import_module(_MODULES[provider])
    return module.get_cost_attribution(dimension, start_date, end_date)


def get_ai_cost_attribution(
    dimension: str,
    provider: str | None = None,
    days: int = 30,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    """AI spend per group for one dimension, from every provider that has it.

    Returns:
      {
        "dimension": str, "period": str,
        "groups": [{"group", "id", "provider", "source", "cost_usd"}, ...],
        "by_provider": {provider: {"source", "total_usd", "group_count", ...}},
        "total_usd": float | None,   # None when adding providers would double count
        "not_available": {provider: reason},   # the provider has no such field
        "failed_providers": {provider: detail}, # could answer, was not read
        "not_connected": [provider, ...],
        "partial": bool,
      }
    """
    dim, dim_note = normalize_dimension(dimension)
    if dim is None:
        return {"error": f"Unknown dimension '{dimension}'.",
                "valid_dimensions": list(DIMENSIONS)}
    if provider:
        p = provider.strip().lower()
        if p not in PROVIDERS:
            return {"error": f"Unknown provider '{provider}'.",
                    "valid_providers": list(PROVIDERS)}
        providers: tuple[str, ...] = (p,)
    else:
        providers = PROVIDERS

    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=days)

    answers: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(providers)) as pool:
        futs = {p: pool.submit(_fetch, p, dim, start_date, end_date) for p in providers}
        for p, fut in futs.items():
            try:
                answers[p] = fut.result()
            except Exception as e:
                log.warning("%s cost attribution failed: %s", p, e)
                answers[p] = {"source": "none", "reason": "exception",
                              "error": f"{type(e).__name__}: {e}"}

    read: dict[str, dict[str, Any]] = {}
    not_available: dict[str, str] = {}
    failed: dict[str, str] = {}
    not_connected: list[str] = []
    for p in providers:
        a = answers[p]
        src = a.get("source")
        if src == "unsupported":
            not_available[p] = a.get("reason") or f"{p} cannot split cost by {dim}."
        elif src in ("none", "error", None):
            if a.get("reason") == "not_configured":
                not_connected.append(p)
            else:
                failed[p] = a.get("error") or str(a.get("reason") or "unknown")
        else:
            read[p] = a

    groups: list[dict[str, Any]] = []
    by_provider: dict[str, dict[str, Any]] = {}
    for p, a in read.items():
        for g in a.get("groups", []):
            groups.append({**g, "provider": p, "source": a["source"]})
        summary = {"source": a["source"], "total_usd": a.get("total_usd"),
                   "group_count": len(a.get("groups", []))}
        for k in ("note", "groups_overlap", "unpriced_models", "unreadable_rows", "truncated"):
            if a.get(k):
                summary[k] = a[k]
        by_provider[p] = summary
    rank = {p: i for i, p in enumerate(PROVIDERS)}
    groups.sort(key=lambda g: (-g["cost_usd"], rank[g["provider"]]))

    out: dict[str, Any] = {
        "dimension": dim,
        "period": f"{start_date} to {end_date}",
        "groups": groups,
        "by_provider": by_provider,
    }
    if dim_note:
        out["dimension_note"] = dim_note

    notes: list[str] = []
    overlap_across = bool(set(read) & _BILLERS and set(read) & _OBSERVERS)
    overlap_within = any(a.get("groups_overlap") for a in read.values())
    if overlap_across:
        notes.append(
            "LiteLLM and Langfuse record calls that OpenAI and Anthropic also bill. Read "
            "each provider's groups on their own: adding them across providers can count "
            "the same call twice.")
    if read and not overlap_across and not overlap_within:
        out["total_usd"] = round(sum(a.get("total_usd") or 0.0 for a in read.values()), 4)
    else:
        out["total_usd"] = None
    if not_available:
        out["not_available"] = {p: f"Not available from {p}: {r}" for p, r in not_available.items()}
    if failed:
        out["failed_providers"] = failed
        notes.append(f"Not read: {', '.join(sorted(failed))}. Their spend is missing "
                     f"from these groups, not zero.")
    if not_connected:
        out["not_connected"] = not_connected
    if failed or any(a.get("unpriced_models") or a.get("unreadable_rows") or a.get("truncated")
                     for a in read.values()):
        out["partial"] = True
    if notes:
        out["note"] = " ".join(notes)
    out["not_covered"] = NOT_COVERED

    if not read:
        if failed:
            out["error"] = f"No connected AI provider could be read for dimension '{dim}'."
        elif not_available:
            out["error"] = (f"None of the connected AI providers can split cost by '{dim}'. "
                            f"See not_available for what each one records instead.")
        else:
            out["error"] = (
                "No AI provider is connected, so there is no AI spend to attribute. "
                "Project and workspace spend needs an admin key: OPENAI_ADMIN_KEY "
                "(sk-admin-...) or ANTHROPIC_ADMIN_KEY. Team, key and tag spend comes from "
                "a LiteLLM proxy (LITELLM_PROXY_URL + LITELLM_MASTER_KEY); tag, user and "
                "session spend from Langfuse. Run `nable openai`, `nable anthropic`, "
                "`nable litellm` or `nable langfuse` to connect.")
    return out
