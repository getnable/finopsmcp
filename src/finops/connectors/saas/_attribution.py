"""
Shared result shape for per-provider AI cost attribution.

Each LLM connector that can split its spend by something other than model
(project, workspace, API key, team, user, tag, session) answers
``get_cost_attribution(dimension, start_date, end_date)`` with one of three
shapes, all plain dicts like every other connector result:

  read:        {"source": "cost_api" | "estimated" | "api",
                "groups": [{"group": label, "id": raw_id, "cost_usd": float}, ...],
                "total_usd": float, "note"?: str, "groups_overlap"?: bool, ...}
  unsupported: {"source": "unsupported", "reason": "<why this provider cannot>"}
  unread:      {"source": "none" | "error", "reason": "<code>", "error"?: str}

"unsupported" means the provider's API has no such field, so no key or setting
would change the answer. "unread" means the provider could answer and did not
(no key, a rejected key, a network failure), which is never a $0.
"""
from __future__ import annotations

from typing import Any

# Every dimension get_ai_cost_attribution accepts. Session is Langfuse-only but
# listed here so every provider can say plainly that it has none.
DIMENSIONS: tuple[str, ...] = ("project", "workspace", "api_key", "team", "user", "tag", "session")


def groups_result(
    sums: dict[str | None, float],
    names: dict[str, str] | None = None,
    *,
    source: str,
    unassigned: str,
    note: str | None = None,
    groups_overlap: bool = False,
) -> dict[str, Any]:
    """Normalise {raw_id: usd} into the read shape, labelled and sorted.

    A None or empty id is spend the provider could not tie to any group (the
    default workspace, a request with no user), labelled ``unassigned`` rather
    than dropped, so the groups still add up to the provider's total. Exact
    zeros are dropped: the APIs return empty groups and they are only noise.
    """
    names = names or {}
    merged: dict[str, dict[str, Any]] = {}
    for gid, usd in sums.items():
        if not usd:
            continue
        key = gid or ""
        label = (names.get(gid) or gid) if gid else unassigned
        row = merged.setdefault(key, {"group": label, "id": gid or None, "cost_usd": 0.0})
        row["cost_usd"] += usd
    groups = sorted(merged.values(), key=lambda r: r["cost_usd"], reverse=True)
    for row in groups:
        row["cost_usd"] = round(row["cost_usd"], 4)
    out: dict[str, Any] = {
        "source": source,
        "groups": groups,
        "total_usd": round(sum(sums.values()), 4),
    }
    if note:
        out["note"] = note
    if groups_overlap:
        out["groups_overlap"] = True
    return out


def unsupported(reason: str) -> dict[str, Any]:
    return {"source": "unsupported", "reason": reason}


def unread(reason: str, detail: str | None = None, *, source: str = "none") -> dict[str, Any]:
    out: dict[str, Any] = {"source": source, "reason": reason}
    if detail:
        out["error"] = detail[:300]
    return out
