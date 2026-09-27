# SPDX-License-Identifier: Apache-2.0
"""nable packs: extensions that are mostly data, with declared capabilities.

A pack is a directory with a `nable-pack.toml` manifest and the content it
provides. Six content types are data (policies, guard rules, playbooks, price
books, report templates, skills): the core validates and loads them, and
nothing in them can execute. Three are code (connectors, adapters, sinks):
declared in the manifest, never imported by the core, and run out of process
by the broker (broker.py), which hands them only the secrets, data scopes and
network hosts their manifest declares. Code runs only from a pack signed by a
trusted key (signing.py) or one the org allowlists by name.

    from finops import packs
    packs.active("policies")              # validated rules from installed packs
    packs.price_override("aws", "p4d.24xlarge")
    packs.guard_rules()                   # for the guard; may only tighten
    packs.org_adapters()                  # pack adapters for `nable org init`

The pack API is versioned separately from nable (API_VERSION); a manifest's
`nable_api` range must include it. This module stays light: everything is
imported on first use, so importing finops.packs costs nothing on a hot path.
"""
from __future__ import annotations

from datetime import date
from typing import Any

API_VERSION = "1.0"
MANIFEST_NAME = "nable-pack.toml"
DEFAULT_REGISTRY_URL = "https://raw.githubusercontent.com/getnable/registry/main/index.json"

__all__ = ["API_VERSION", "DEFAULT_REGISTRY_URL", "MANIFEST_NAME", "active", "guard_rules",
           "installed", "load_problems", "org_adapters", "price_override", "summary"]


def org_adapters() -> list[Any]:
    """Org-context adapters from installed, runnable packs, as callables with
    the finops.org ADAPTERS signature: adapter(model) -> iterable of Facts,
    every one a proposal whose source starts with the pack id. See
    broker.org_adapters for how `nable org init` should call them."""
    from .broker import org_adapters as _oa
    return _oa()


def active(kind: str) -> list[Any]:
    """Validated content of `kind` from every installed, intact, in-policy pack."""
    from .runtime import active as _active
    return _active(kind)


def price_override(provider: str, sku: str, *, on: date | None = None) -> dict[str, Any] | None:
    """An installed price book's effective rate for `sku`, or None."""
    from .runtime import price_override as _po
    return _po(provider, sku, on=on)


def guard_rules() -> list[Any]:
    """Validated guard rules from installed packs. They may only tighten a
    verdict (combine with finops.packs.content.tighten), never loosen one."""
    from .runtime import guard_rules as _gr
    return _gr()


def installed() -> list[dict[str, Any]]:
    """Index entries of installed packs (without per-file hashes)."""
    from .install import list_installed
    return list_installed()


def load_problems() -> list[str]:
    """Why any installed pack is not loaded right now."""
    from .runtime import load_problems as _lp
    return _lp()


def summary() -> dict[str, Any]:
    """Installed packs for the list_installed_packs MCP tool."""
    from .runtime import summary as _s
    return _s()
