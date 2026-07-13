"""
Context memory: the operating model nable learns from a human answering once.

The loop is: nable flags something, a person says "that's fine, and here's why"
one time, and nable (a) never re-flags that thing, and (b) can generalize the
reason so a whole class stops nagging ("DR standbys are intentional", "spot on
prod is always wrong for us"). Each answer is one annotation. Over months of the
always-on loop this becomes a queryable memory of how THIS org actually runs, the
thing a fresh tool or a new hire doesn't have.

This module is propose-shaping, not acting: it only decides which findings surface.
It reads and writes one local table (context_annotations) and touches no cloud.

Scopes, narrowest to broadest:
  - resource       exact resource_id            "this box (i-0abc) is fine"
  - resource_type  all of a type                "all NAT gateways are load-bearing"
  - bucket         an environment_bucket        "anything in the dr bucket is fine"
  - source         a finding type               "ignore spot recs"
  - provider       a whole provider             "ignore snowflake findings"
A broad scope can be narrowed to one org boundary with provider/account_id.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from ..storage.db import context_annotations, get_engine

VALID_SCOPES = {"resource", "resource_type", "bucket", "source", "provider"}
VALID_VERDICTS = {"intentional"}

# rec dict field that each scope matches against (see savings_tracker.list_recommendations)
_SCOPE_FIELD = {
    "resource": "resource_id",
    "resource_type": "resource_type",
    "bucket": "environment_bucket",
    "source": "source",
    "provider": "provider",
}


def remember(
    scope: str,
    match_value: str,
    reason: str,
    *,
    verdict: str = "intentional",
    provider: str | None = None,
    account_id: str | None = None,
    created_by: str = "",
    source_rec_id: int | None = None,
) -> dict[str, Any]:
    """Record one learned exception. Returns the stored annotation as a dict.

    Idempotent on (scope, match_value, provider, account_id, verdict): re-answering
    the same thing refreshes the reason instead of stacking duplicates.
    """
    scope = (scope or "").strip().lower()
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {sorted(VALID_SCOPES)}, got {scope!r}")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(VALID_VERDICTS)}, got {verdict!r}")
    match_value = (match_value or "").strip()
    if not match_value:
        raise ValueError("match_value is required")
    reason = (reason or "").strip()
    provider = (provider or None)
    account_id = (account_id or None)
    now = datetime.now(timezone.utc)

    engine = get_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            select(context_annotations.c.id).where(
                context_annotations.c.active.is_(True),
                context_annotations.c.scope == scope,
                context_annotations.c.match_value == match_value,
                context_annotations.c.verdict == verdict,
                context_annotations.c.provider.is_(None) if provider is None
                else context_annotations.c.provider == provider,
                context_annotations.c.account_id.is_(None) if account_id is None
                else context_annotations.c.account_id == account_id,
            )
        ).fetchone()
        if existing:
            conn.execute(
                update(context_annotations)
                .where(context_annotations.c.id == existing.id)
                .values(reason=reason, created_by=created_by or "",
                        source_rec_id=source_rec_id, created_at=now)
            )
            ann_id = existing.id
        else:
            r = conn.execute(context_annotations.insert().values(
                scope=scope, match_value=match_value, provider=provider,
                account_id=account_id, verdict=verdict, reason=reason,
                created_by=created_by or "", source_rec_id=source_rec_id,
                created_at=now, active=True,
            ))
            ann_id = int(r.inserted_primary_key[0])

    return {
        "id": ann_id, "scope": scope, "match_value": match_value,
        "provider": provider, "account_id": account_id, "verdict": verdict,
        "reason": reason, "created_by": created_by or "",
        "source_rec_id": source_rec_id, "created_at": now.isoformat(),
    }


def forget(annotation_id: int) -> bool:
    """Soft-delete a learned exception (keeps the audit trail). Returns True if one changed."""
    engine = get_engine()
    with engine.begin() as conn:
        r = conn.execute(
            update(context_annotations)
            .where(context_annotations.c.id == annotation_id,
                   context_annotations.c.active.is_(True))
            .values(active=False)
        )
        return r.rowcount > 0


def list_context(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    """Return the learned operating model, newest first."""
    engine = get_engine()
    q = select(context_annotations).order_by(context_annotations.c.created_at.desc())
    if not include_inactive:
        q = q.where(context_annotations.c.active.is_(True))
    with engine.connect() as conn:
        rows = conn.execute(q).fetchall()
    return [{
        "id": r.id, "scope": r.scope, "match_value": r.match_value,
        "provider": r.provider, "account_id": r.account_id, "verdict": r.verdict,
        "reason": r.reason, "created_by": r.created_by,
        "source_rec_id": r.source_rec_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "active": bool(r.active),
    } for r in rows]


def _matches(ann: dict[str, Any], rec: dict[str, Any]) -> bool:
    """Does one annotation apply to one recommendation?"""
    field = _SCOPE_FIELD.get(ann["scope"])
    if field is None:
        return False
    if str(rec.get(field) or "") != str(ann["match_value"]):
        return False
    if ann.get("provider") and str(rec.get("provider") or "") != str(ann["provider"]):
        return False
    if ann.get("account_id") and str(rec.get("account_id") or "") != str(ann["account_id"]):
        return False
    return True


def match(rec: dict[str, Any], annotations: list[dict] | None = None) -> dict | None:
    """Return the narrowest matching 'intentional' annotation for a rec, or None.

    Narrowest-wins so a per-resource "actually flag this one" future verdict can
    override a broad rule; today all verdicts are 'intentional' so it just picks
    the most specific reason to show.
    """
    anns = annotations if annotations is not None else list_context()
    hits = [a for a in anns if a.get("verdict") == "intentional" and _matches(a, rec)]
    if not hits:
        return None
    order = {"resource": 0, "resource_type": 1, "bucket": 2, "source": 3, "provider": 4}
    hits.sort(key=lambda a: order.get(a["scope"], 9))
    return hits[0]


def partition(recs: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Split recs into (visible, suppressed_by_context). Never mutates inputs.

    A suppressed rec is copied with a `context` block naming the reason and the
    annotation that silenced it, so a UI can show "hidden because: <why>".
    """
    anns = list_context()
    if not anns:
        return list(recs), []
    visible: list[dict] = []
    suppressed: list[dict] = []
    for rec in recs:
        hit = match(rec, anns)
        if hit is None:
            visible.append(rec)
        else:
            annotated = dict(rec)
            annotated["context"] = {
                "suppressed": True,
                "reason": hit["reason"],
                "scope": hit["scope"],
                "match_value": hit["match_value"],
                "annotation_id": hit["id"],
            }
            suppressed.append(annotated)
    return visible, suppressed
