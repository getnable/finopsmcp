"""
Infer standing cost policies from what a team keeps rejecting.

context_memory learns exceptions a human types in one at a time. This closes the
loop the other way: when a team dismisses the same CLASS of finding for the same
business reason over and over ("spot on prod, no", "idle in dr, that's the standby"),
nable notices the pattern and proposes the rule back. Three rejections become one
durable policy the human confirms with a click, instead of nable nagging forever.

It only proposes. Confirming a candidate calls context_memory.remember(), so the
human is always the one who turns a pattern into a rule (propose-only, never auto).
The axes it groups on are exactly the scopes context_memory can express: source,
bucket, provider, resource_type.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select

from ...storage.db import get_engine, savings_recommendations
from ..context_memory import list_context

# A dismissal only counts toward a policy when it's a BUSINESS choice ("keep it,
# and here's why"), not a quality miss ("your estimate is wrong") or a deferral
# ("we'll do it next sprint"). Mirrors learning.signal._BUSINESS_DISMISS_REASONS.
BUSINESS_REASONS = frozenset({"reserved_for_peak", "sla_sensitive", "not_our_resource"})
ACTED_STATUSES = frozenset({"acted_on", "verified"})

MIN_SUPPORT = 3        # need at least this many business dismissals before proposing
MIN_CONSISTENCY = 0.8  # dismissed-as-intentional / (dismissed + acted) must be this high

# Which rec field each proposable scope groups on (matches context_memory._SCOPE_FIELD).
_SCOPE_FIELD = {
    "source": "source",
    "bucket": "environment_bucket",
    "provider": "provider",
    "resource_type": "resource_type",
}
_REASON_PHRASE = {
    "reserved_for_peak": "reserved for peak or burst capacity",
    "sla_sensitive": "SLA-sensitive, not worth the risk",
    "not_our_resource": "owned by another team",
}


def _load_decided() -> list[dict]:
    """Recs the team has actually decided on (acted or dismissed). Open/expired don't
    carry a signal about intent, so they're left out."""
    sr = savings_recommendations
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(
                sr.c.source, sr.c.environment_bucket, sr.c.provider, sr.c.resource_type,
                sr.c.status, sr.c.dismiss_reason_category, sr.c.dismiss_reason,
                sr.c.resource_id,
            ).where(sr.c.status.in_(list(ACTED_STATUSES) + ["dismissed"]))
        ).fetchall()
    return [{
        "source": r.source, "environment_bucket": r.environment_bucket,
        "provider": r.provider, "resource_type": r.resource_type, "status": r.status,
        "category": getattr(r, "dismiss_reason_category", None),
        "reason": getattr(r, "dismiss_reason", None), "resource_id": r.resource_id,
    } for r in rows]


def infer_policies(
    *, min_support: int = MIN_SUPPORT, min_consistency: float = MIN_CONSISTENCY,
    recs: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """Propose standing cost policies from the dismissal history. Never writes anything.

    Returns a list of candidate rules, strongest first, each with the evidence behind
    it and the exact remember_cost_context() call that would enact it. Candidates
    already covered by an active context_memory rule are skipped.
    """
    decided = recs if recs is not None else _load_decided()
    existing = {(a["scope"], str(a["match_value"])) for a in list_context()}

    candidates: list[dict] = []
    for scope, field in _SCOPE_FIELD.items():
        groups: dict[str, dict] = defaultdict(
            lambda: {"acted": 0, "business": 0, "cats": defaultdict(int),
                     "reasons": [], "resources": [], "sources": set()})
        for rec in decided:
            val = rec.get(field)
            if not val or str(val).startswith("unknown"):
                continue
            g = groups[str(val)]
            if rec["status"] in ACTED_STATUSES:
                g["acted"] += 1
            elif rec["status"] == "dismissed" and rec["category"] in BUSINESS_REASONS:
                g["business"] += 1
                g["cats"][rec["category"]] += 1
                g["sources"].add(rec.get("source"))
                if rec.get("reason") and len(g["reasons"]) < 3:
                    g["reasons"].append(rec["reason"])
                if rec.get("resource_id") and len(g["resources"]) < 3:
                    g["resources"].append(rec["resource_id"])

        for val, g in groups.items():
            support = g["business"]
            denom = support + g["acted"]
            if support < min_support or denom == 0:
                continue
            consistency = support / denom
            if consistency < min_consistency:
                continue
            if (scope, val) in existing:
                continue
            # Over-suppression guard: a broad rule (provider / bucket / resource_type)
            # is only justified when the rejections span MULTIPLE finding types. If they
            # all came from one source, that's a source rule, not "ignore all of aws".
            if scope != "source" and len({s for s in g["sources"] if s}) < 2:
                continue
            dominant = max(g["cats"], key=g["cats"].get)
            phrase = _REASON_PHRASE.get(dominant, "intentional for this environment")
            candidates.append({
                "scope": scope,
                "match_value": val,
                "support": support,
                "acted": g["acted"],
                "consistency": round(consistency, 2),
                "dominant_reason": dominant,
                "suggested_reason": phrase,
                "sample_reasons": g["reasons"],
                "sample_resources": g["resources"],
                "evidence": (
                    f"You marked {support} {scope}={val} finding(s) intentional "
                    f"({phrase}) and acted on {g['acted']}. nable can stop surfacing "
                    f"this whole class instead of flagging each one."
                ),
                "confirm": (
                    f'remember_cost_context(scope="{scope}", '
                    f'match_value="{val}", reason="{phrase}")'
                ),
            })

    # Strongest evidence first: most rejections, then most consistent.
    candidates.sort(key=lambda c: (c["support"], c["consistency"]), reverse=True)
    return candidates


def policy_for_rec(rec: dict[str, Any]) -> dict[str, Any] | None:
    """Cheap check used by the dismiss nudge: does the just-dismissed rec now push
    one of its own axes over the threshold? Returns the matching candidate or None."""
    try:
        cands = infer_policies()
    except Exception:
        return None
    for c in cands:
        field = _SCOPE_FIELD[c["scope"]]
        if str(rec.get(field) or "") == str(c["match_value"]):
            return c
    return None
