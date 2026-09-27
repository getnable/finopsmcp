# SPDX-License-Identifier: Apache-2.0
"""Render an installed pack's report template over nable's own data.

A report template is text with ${dotted.key} placeholders (content.render:
dict lookups only, nothing evaluated). A placeholder under a data scope's
name reads that scope: `${ledger.guard.counts.approved}` reads the guard
ledger. The core builds the scope's values itself, in process, and only for
a scope the pack declares in `[capabilities].read_data`: a template that
reads a scope its pack did not declare is refused, so what a report can show
is what was approved at install.

    SOURCES       scope -> the core function that builds its values
    render()      one report, or one per item of a list (`each`), as text,
                  with the values it was rendered from (the JSON export)

`sets` fills plain placeholders a template leaves to the person
(`--set year=2027`); they can never stand in for a data scope.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from .errors import PackError, PolicyRefusal

_SET_KEY = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
MAX_SET_VALUE = 200


def _ledger_guard(opts: dict[str, Any]) -> dict[str, Any]:
    from .. import change_evidence
    return change_evidence.build(opts.get("since"), until=opts.get("until"))


# Data scopes a report template may read, by the read_data scope it needs.
SOURCES: dict[str, Any] = {"ledger.guard": _ledger_guard}


def scopes_in(text: str) -> list[str]:
    """The data scopes a template's placeholders read."""
    from .content import placeholders
    out: list[str] = []
    for ph in placeholders(text):
        for scope in SOURCES:
            if (ph == scope or ph.startswith(scope + ".")) and scope not in out:
                out.append(scope)
    return out


def find(pack_id: str, name: str) -> Any:
    """The installed, loaded pack's report template called `name`: its path
    in the pack (reports/cc8.1-evidence.md), its file name or its stem."""
    from . import runtime
    if pack_id not in runtime.loaded_packs():
        why = [p for p in runtime.load_problems() if p.startswith(f"{pack_id} ")]
        raise PackError(f"{pack_id} is not installed and loaded"
                        + (f": {why[0]}" if why else ""))
    mine = [r for r in runtime.active("reports") if r.pack == pack_id]
    for r in mine:
        p = PurePosixPath(r.path)
        stem = p.name.split(".", 1)[0]
        if name in (r.path, p.name, stem, p.stem):
            return r
    known = ", ".join(sorted(PurePosixPath(r.path).name for r in mine)) or "none"
    raise PackError(f"{pack_id} has no report {name!r} (its reports: {known})")


def _declared(pack_id: str) -> tuple[str, ...]:
    from . import store
    e = store.read_index()["packs"].get(pack_id) or {}
    return tuple((e.get("capabilities") or {}).get("read_data") or ())


def _set_values(sets: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    roots = {s.split(".", 1)[0] for s in SOURCES}
    for k, v in (sets or {}).items():
        if not _SET_KEY.match(k):
            raise PackError(f"--set {k!r}: a name is lowercase letters, digits and _")
        if k in roots:
            raise PackError(f"--set {k}: {k} is a data scope, which only nable fills")
        v = str(v)
        if len(v) > MAX_SET_VALUE or any(ord(c) < 32 for c in v):
            raise PackError(f"--set {k}: the value must be one line of at most "
                            f"{MAX_SET_VALUE} characters")
        out[k] = v
    return out


def _nest(values: dict[str, Any], scope: str, data: dict[str, Any]) -> None:
    cur = values
    parts = scope.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = data


def _lookup(values: dict[str, Any], path: str) -> Any:
    cur: Any = values
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def render(pack_id: str, name: str, *, since: datetime | None = None,
           until: datetime | None = None, sets: dict[str, str] | None = None,
           each: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """{"pack", "report", "scopes", "text" (str, or a list with `each`),
    "values"}. Raises PolicyRefusal for a scope the pack did not declare."""
    tmpl = find(pack_id, name)
    scopes = scopes_in(tmpl.text + (f" ${{{each}}}" if each else ""))
    declared = _declared(pack_id)
    for s in scopes:
        if s not in declared:
            raise PolicyRefusal(f"{pack_id} report {tmpl.path} reads {s}, which the pack "
                                "does not declare in [capabilities].read_data")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    values: dict[str, Any] = {
        "generated_at": now.isoformat(timespec="seconds"),
        "since": since.isoformat(timespec="seconds") if since else "the first record",
        "until": (until or now).isoformat(timespec="seconds"),
        "pack": pack_id, "report": tmpl.path,
        **_set_values(sets),
    }
    for s in scopes:
        _nest(values, s, SOURCES[s]({"since": since, "until": until}))
    if each is None:
        text: Any = tmpl.render(values)
    else:
        items = _lookup(values, each)
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise PackError(f"--each {each}: that is not a list of records in the values "
                            "this report reads")
        text = [tmpl.render({**values, "item": item}) for item in items]
    return {"pack": pack_id, "report": tmpl.path, "scopes": scopes, "text": text,
            "values": values}
