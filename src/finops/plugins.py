# SPDX-License-Identifier: Apache-2.0
"""Enterprise plugin seam.

The open finops-mcp core discovers optional tool providers through the
``finops.plugins`` entry-point group. When a provider package (the proprietary
``nable-enterprise``, or anything else) is installed, its registered callable is
invoked with the live ``FastMCP`` instance so it can add tools on top of the
core set. Because it registers through the same ``mcp.tool`` the core uses, the
extra tools pick up telemetry and advertisement filtering for free.

When no provider is installed the loop is empty and the open product behaves
exactly as before. A broken or failing plugin is logged and skipped, it must
never crash the core server.

A provider declares itself in its own packaging, e.g.::

    [project.entry-points."finops.plugins"]
    enterprise = "nable_enterprise.plugin:register"

where ``register(mcp)`` adds tools via ``@mcp.tool()``.
"""
from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any

log = logging.getLogger("finops.plugins")

_PLUGIN_GROUP = "finops.plugins"
_loaded: list[str] = []


def load_plugins(mcp: Any) -> list[str]:
    """Discover and register every ``finops.plugins`` entry point against ``mcp``.

    Each entry point resolves to a callable ``register(mcp) -> None`` that adds
    tools to the server. Idempotent: a plugin already loaded in this process is
    skipped, so calling this twice does not double-register. Returns the names
    loaded on this call (not the cumulative set).
    """
    try:
        eps = entry_points(group=_PLUGIN_GROUP)
    except TypeError:
        # Python <3.10 selectable API: entry_points() returns a group->list dict.
        eps = entry_points().get(_PLUGIN_GROUP, [])  # type: ignore[attr-defined]

    newly: list[str] = []
    for ep in eps:
        if ep.name in _loaded:
            continue
        try:
            register = ep.load()
            register(mcp)
        except Exception as exc:  # a plugin must never take down the core server
            log.warning("finops.plugins: skipped %r (%s)", ep.name, exc)
            continue
        _loaded.append(ep.name)
        newly.append(ep.name)
        log.info("finops.plugins: loaded %r", ep.name)
    return newly


def loaded_plugins() -> list[str]:
    """Names of enterprise plugins loaded so far this process."""
    return list(_loaded)
