# SPDX-License-Identifier: Apache-2.0
"""Code-bearing plugins: found and listed, never loaded here.

Connectors, org-context adapters and action sinks are the only pack types
that carry code. They ship as Python packages declaring typed entry points:

    [project.entry-points."nable.connectors"]
    kubecost = "nable_k8s.kubecost:main"

This module lists what is installed in those groups and, where it can, the
pack manifest that declares each one (an installed pack whose [provides]
names the same entry, or a nable-pack.toml shipped in the distribution). It
reads metadata and TOML only: no entry point is imported, so nothing a code
pack contains runs. Running code is broker.py's job, out of process, with
only the secrets, data scopes and hosts the manifest declares, and only for
entries an installed pack declares. Code that lives in a distribution rather
than in the pack is not covered by the pack's signature, so the broker runs it
only for a pack the org allowlists in packs.allow_unsigned_code.

The first-party `finops.plugins` register(mcp) hook (plugins.py) is separate
and unchanged.
"""
from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any

log = logging.getLogger("finops.packs")

CODE_GROUPS: dict[str, str] = {
    "nable.connectors": "connectors",
    "nable.adapters": "adapters",
    "nable.sinks": "sinks",
}


def _declared_in_index(kind: str, value: str) -> str | None:
    from . import store
    from .errors import PackError
    try:
        idx = store.read_index()
    except PackError:
        return None
    for pid, e in sorted(idx["packs"].items()):
        for c in e.get("code") or []:
            if c.get("kind") == kind and c.get("entry") == value:
                return pid
    return None


def _declared_in_dist(ep: Any, kind: str) -> str | None:
    from .errors import PackError
    from .manifest import MANIFEST_NAME, parse_manifest
    dist = getattr(ep, "dist", None)
    if dist is None:
        return None
    try:
        for f in dist.files or []:
            if f.name != MANIFEST_NAME:
                continue
            text = f.read_text(encoding="utf-8")
            try:
                m = parse_manifest(text)
            except PackError:
                continue
            if any(c.kind == kind and c.entry == ep.value for c in m.code):
                return m.id
    except (OSError, UnicodeDecodeError, ValueError):
        # Odd distribution metadata reads as "no manifest", never an error.
        return None
    return None


def code_plugins() -> list[dict[str, Any]]:
    """Every entry point in the nable.* code groups, with its declaring pack
    when one is found. `loaded` is always False in this part of the SDK."""
    out: list[dict[str, Any]] = []
    for group, kind in CODE_GROUPS.items():
        try:
            eps = entry_points(group=group)
        except Exception as e:  # noqa: BLE001 - broken metadata must not break a listing
            log.warning("finops.packs: could not read entry points for %s (%s)", group, e)
            continue
        for ep in sorted(eps, key=lambda e: e.name):
            dist = getattr(ep, "dist", None)
            declared = _declared_in_index(kind, ep.value) or _declared_in_dist(ep, kind)
            out.append({
                "group": group, "kind": kind, "name": ep.name, "entry": ep.value,
                "distribution": getattr(dist, "name", None) if dist else None,
                "version": getattr(dist, "version", None) if dist else None,
                "declared_by": declared, "loaded": False,
            })
    return out
