# SPDX-License-Identifier: Apache-2.0
"""Installed data packs, loaded read-only into the running product.

active(kind) is what the rest of nable calls. It reads the index, and for each
installed pack re-hashes every file against what was approved, re-checks the
org's packs: policy, re-validates the manifest and the content, and only then
hands the typed items out. A pack that fails any of that is skipped (and named
in load_problems()), never half-loaded: a file edited after approval does not
reach the pricing code or the guard.

The result is cached per process and rebuilt when the index or the policy
file changes, or after CACHE_TTL_S, so calling active() on a hot path costs a
couple of stat() calls. An edit to an installed file inside the TTL is caught
at the next rebuild and by `nable pack audit` at any time.
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from .content import KINDS as _KINDS
from .content import GuardRule, PriceRate

log = logging.getLogger("finops.packs")

CACHE_TTL_S = 60.0
_CACHE: dict[str, Any] = {}


def invalidate() -> None:
    _CACHE.clear()


def _policy_stamp() -> tuple[str, int, int] | None:
    try:
        from ..policy import policy_file_path
        p = policy_file_path()
        st = p.stat()
        return (str(p), st.st_mtime_ns, st.st_size)
    except (OSError, ValueError):
        return None


def _load() -> dict[str, Any]:
    from ..policy import pack_policy
    from . import store
    from .content import load_content
    from .errors import PackError
    from .install import check_installed
    from .manifest import load_manifest

    items: dict[str, list[Any]] = {k: [] for k in _KINDS}
    loaded: list[str] = []
    problems: list[str] = []
    try:
        idx = store.read_index()
    except PackError as e:
        return {"items": items, "loaded": loaded, "problems": [str(e)]}
    pp = pack_policy()
    for pid, e in sorted(idx["packs"].items()):
        try:
            root, _, why = check_installed(pid, e, pp)
            if why:
                problems.append(f"{pid} is not loaded: " + "; ".join(why))
                continue
            manifest = load_manifest(root)
            content = load_content(root, manifest.provides)
            if content.problems:
                problems.append(f"{pid} is not loaded: "
                                + "; ".join(str(p) for p in content.problems[:3]))
                continue
        except Exception as err:  # noqa: BLE001 - one bad pack never drops the others
            # PackError, OSError and KeyError are the expected ones; anything
            # else is a bug or a hostile pack, and still only this pack's problem.
            why = str(err) if isinstance(err, (PackError, OSError, KeyError)) \
                else f"{type(err).__name__}: {err}"[:300]
            problems.append(f"{pid} is not loaded: {why}")
            continue
        for kind, objs in content.items.items():
            items[kind].extend(replace(o, pack=pid) for o in objs)
        loaded.append(pid)
    for p in problems:
        log.warning("finops.packs: %s", p)
    return {"items": items, "loaded": loaded, "problems": problems}


def _state() -> dict[str, Any]:
    from . import store
    key = (str(store.packs_root()), store.index_stamp(), _policy_stamp())
    now = time.monotonic()
    if _CACHE.get("key") == key and now - _CACHE.get("at", 0.0) < CACHE_TTL_S:
        return _CACHE["state"]
    state = _load()
    _CACHE.update(key=key, at=now, state=state)
    return state


def active(kind: str) -> list[Any]:
    """Validated items of `kind` from every installed, intact, in-policy pack,
    each tagged with its pack id. kind is one of content.KINDS."""
    if kind not in _KINDS:
        raise ValueError(f"{kind!r} is not a pack content type; known: {', '.join(_KINDS)}")
    return list(_state()["items"][kind])


def loaded_packs() -> list[str]:
    return list(_state()["loaded"])


def load_problems() -> list[str]:
    return list(_state()["problems"])


def price_override(provider: str, sku: str, *, on: date | None = None) -> dict[str, Any] | None:
    """An installed price book's rate for `sku` on `provider`, in effect on
    `on` (default today), or None. When several apply, the one that took
    effect most recently wins; a tie goes to the first pack by id."""
    day = on or datetime.now().astimezone().date()  # the local calendar day
    p, s = (provider or "").strip().lower(), (sku or "").strip().casefold()
    best: PriceRate | None = None
    for r in active("price_books"):
        if r.provider != p or r.sku.casefold() != s or not r.in_effect(day):
            continue
        if best is None or r.effective_from > best.effective_from:
            best = r
    return best.to_dict() if best else None


def guard_rules() -> list[GuardRule]:
    """Validated guard rules from installed packs, for the guard to consult.

    Packs may only tighten: every rule's verdict is ask or deny, and the
    guard must combine them with content.tighten(), which returns the stricter
    of its own verdict and theirs and never a looser one. A pack can never
    allow what the user's or the org's policy asks about, and never turn the
    guard off."""
    return active("guard_rules")


def summary() -> dict[str, Any]:
    """What list_installed_packs returns: installed packs, whether each is
    loaded, and what it provides and may do."""
    from . import API_VERSION, store
    from .errors import PackError
    try:
        idx = store.read_index()
    except PackError as e:
        return {"packs": [], "count": 0, "problems": [str(e)], "api_version": API_VERSION}
    state = _state()
    rows = []
    for pid, e in sorted(idx["packs"].items()):
        rows.append({
            "id": pid, "version": e.get("version"), "tier": e.get("tier"),
            "description": e.get("description"), "loaded": pid in state["loaded"],
            "capabilities": e.get("capabilities") or {},
            "provides": sorted((e.get("provides") or {}).keys()),
            "code": [c.get("id") for c in e.get("code") or []],
            "source": (e.get("source") or {}).get("spec"),
            "approved_by": e.get("approved_by"), "approved_at": e.get("approved_at"),
        })
    return {"packs": rows, "count": len(rows), "problems": list(state["problems"]),
            "api_version": API_VERSION,
            "note": ("Most packs are data: nable validates them and nothing in them runs. "
                     "Connectors, adapters and sinks run only out of process through the "
                     "broker, with the secrets, data scopes and hosts they declared, and only "
                     "when signed by a trusted key or allowlisted by the org. "
                     "`nable pack audit` re-hashes every file against what was approved.")}
