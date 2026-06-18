"""
Pinned views: persistence for agent-built cost cards.

A pinned view stores the CardSpec (which carries the SliceSpec that regenerates
its data), not a materialized result, so the card re-runs live on load. Owner is a
local identity or "instance" for shared team pins. Single-tenant / local-first:
owner is never a multi-tenant tenant id. These functions touch only the local
dashboard_views table, never the cloud.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update

from ..storage.db import dashboard_views, get_engine

log = logging.getLogger(__name__)

VALID_SCOPES = {"me", "instance"}


def _row_to_dict(r) -> dict:
    return {
        "id": r.id,
        "owner": r.owner,
        "scope": r.scope,
        "title": r.title,
        "template": r.template,
        "slice": json.loads(r.slice_spec or "{}"),
        "card": json.loads(r.card_spec or "{}"),
        "position": r.position,
        "refresh_secs": r.refresh_secs,
    }


def pin_view(card: dict, owner: str = "instance", scope: str = "instance",
             created_by: str = "") -> int:
    """Persist a CardSpec as a pinned view. Returns the new view id.

    `card` is the CardSpec dict produced by slice_costs (it embeds `slice`, the
    SliceSpec that regenerates the data). Pins append to the end of the owner's list.
    """
    if scope not in VALID_SCOPES:
        scope = "instance"
    slice_spec = card.get("slice") or {}
    title = (card.get("title") or "Cost view")[:256]
    template = card.get("template") or "bar"
    now = datetime.now(timezone.utc)
    engine = get_engine()
    with engine.begin() as conn:
        max_pos = conn.execute(
            select(func.max(dashboard_views.c.position)).where(dashboard_views.c.owner == owner)
        ).scalar()
        pos = (max_pos or 0) + 1
        res = conn.execute(dashboard_views.insert().values(
            owner=owner, scope=scope, title=title, template=template,
            slice_spec=json.dumps(slice_spec), card_spec=json.dumps(card),
            position=pos, refresh_secs=int(card.get("refresh_secs") or 43200),
            created_at=now, updated_at=now, created_by=created_by,
        ))
        return int(res.inserted_primary_key[0])


def list_pinned_views(owner: str = "instance", include_instance: bool = True) -> list[dict]:
    """Pinned views for `owner`, plus shared 'instance' pins, ordered by position."""
    engine = get_engine()
    with engine.begin() as conn:
        cond = dashboard_views.c.owner == owner
        if include_instance and owner != "instance":
            cond = (dashboard_views.c.owner == owner) | (dashboard_views.c.scope == "instance")
        rows = conn.execute(
            select(dashboard_views).where(cond).order_by(dashboard_views.c.position)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_pinned_view(view_id: int, owner: str = "instance") -> dict | None:
    """One pinned view (the stored spec), or None. An 'instance' pin is visible to all."""
    engine = get_engine()
    with engine.begin() as conn:
        r = conn.execute(
            select(dashboard_views).where(dashboard_views.c.id == view_id)
        ).first()
    if r is None:
        return None
    if r.owner != owner and r.scope != "instance":
        return None
    return _row_to_dict(r)


def unpin_view(view_id: int, owner: str = "instance") -> bool:
    """Remove a pinned view the owner controls. Returns True if a row was deleted."""
    engine = get_engine()
    with engine.begin() as conn:
        # Only the owner can unpin; an 'instance' pin can be unpinned by anyone on the box.
        r = conn.execute(select(dashboard_views.c.owner, dashboard_views.c.scope)
                         .where(dashboard_views.c.id == view_id)).first()
        if r is None or (r.owner != owner and r.scope != "instance"):
            return False
        conn.execute(delete(dashboard_views).where(dashboard_views.c.id == view_id))
        return True


def reorder_views(ordered_ids: list[int], owner: str = "instance") -> None:
    """Set position to match the given id order (only rows the owner can see)."""
    visible = {v["id"] for v in list_pinned_views(owner)}
    engine = get_engine()
    with engine.begin() as conn:
        for pos, vid in enumerate(ordered_ids):
            if vid in visible:
                conn.execute(update(dashboard_views)
                             .where(dashboard_views.c.id == int(vid))
                             .values(position=pos, updated_at=datetime.now(timezone.utc)))
