# SPDX-License-Identifier: Apache-2.0
"""The workspace's standing on the authority ladder, kept on the laptop for
the guard (D173, D176).

authority.yaml says, per class that moves no money (tag, ticket and
offhours_schedule), how far an agent may go unasked. A paid workspace also
keeps the evidence: every reversal (`nable guard reverse`, or one it infers
from the ledgers people send) demotes its class at once on the box, to
approve_each, until a signed-in admin lifts it. The box says so in the
status answer the laptop already reads, as an `authority` block (contract 1,
below). No endpoint and no request is added. `nable cloud status` and `nable
cloud connect --send` keep it here, as authority-standing.json (0600) beside
the guard ledger, only when it comes from the box and the workspace this
machine last sent to (cloud_standing.bound(), the rule cloud_rates keeps);
otherwise it is shown and not kept, with why.

Contract 1, as the box writes it (at most BLOCK_MAX_BYTES):

    {"v": 1, "read": true, "workspace_ref": "ws_<32 hex>",
     "as_of": "<ISO 8601 UTC>",
     "classes": {"<class>": {"rung_in_force": "off" | "recommend" | "approve_each"
                                              | "bounded" | null,
                             "demoted": bool, "since": "YYYY-MM-DD" | null,
                             "line": "<sha256 of the reversal's ledger line>" | null}}}

or, when the box's ladder pass did not run on the last upload, {"v": 1,
"read": false, "reason", "workspace_ref", "as_of"}. A class this version does
not know is left out. A block that names the workspace this machine sends to
and is out of shape anywhere else is kept as not read, never as wider.

What the guard does with it (guard_authority, D176): the stricter of the
repo's authority.yaml and the box. A demoted class is at approve_each at
most; a rung in force on the box lower than the file's is the one that
applies; a standing that is not read holds every class the file has at
bounded at approve_each. Nothing here can raise a rung: a block that says
bounded where the file says approve_each still asks.

On the hook path the file is read through guard_paths.read_regular, without
waiting on a FIFO, at most MAX_BYTES, and never the network: standard
library only, and no connection is opened here.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import cloud_standing

FILE_NAME = "authority-standing.json"
FORMAT = 1
BLOCK_MAX_BYTES = 4096
# The block, and the few fields this module adds around it.
MAX_BYTES = BLOCK_MAX_BYTES + 1024
# finops.org.model.NO_MONEY_CLASSES and CLASS_RUNGS, copied: the org package
# is not light, and this module is read on the hook path.
CLASSES = ("tag", "ticket", "offhours_schedule")
RUNGS = ("off", "recommend", "approve_each", "bounded")
CLASS_KEYS = frozenset({"rung_in_force", "demoted", "since", "line"})
READ_KEYS = frozenset({"v", "read", "workspace_ref", "as_of", "classes"})
NOT_READ_KEYS = frozenset({"v", "read", "reason", "workspace_ref", "as_of"})
_CLASSES_MAX = 32
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class StandingError(ValueError):
    """A block that is not contract 1."""


def path() -> Path:
    """authority-standing.json, beside the guard ledger."""
    from . import guard_ledger
    return guard_ledger.ledger_path().parent / FILE_NAME


def _line(v: Any, name: str, most: int = 300) -> str:
    if not isinstance(v, str) or not v or len(v) > most or any(
            ord(c) < 0x20 or ord(c) == 0x7f for c in v):
        raise StandingError(f"{name} is not one line of text of at most {most} characters")
    return v


def _as_of(v: Any) -> str:
    if not isinstance(v, str) or len(v) > 64:
        raise StandingError("as_of is not a time")
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        raise StandingError("as_of is not a time") from None
    if t.tzinfo is None or t.utcoffset() is None:
        raise StandingError("as_of carries no UTC offset")
    return v


def _class(v: Any, name: str) -> dict[str, Any]:
    if not isinstance(v, dict) or set(v) != CLASS_KEYS:
        raise StandingError(f"classes.{name} is not {{rung_in_force, demoted, since, line}}")
    if v["rung_in_force"] is not None and v["rung_in_force"] not in RUNGS:
        raise StandingError(f"classes.{name}.rung_in_force is not a rung")
    if not isinstance(v["demoted"], bool):
        raise StandingError(f"classes.{name}.demoted is not true or false")
    since = v["since"]
    if since is not None:
        if not isinstance(since, str) or not _DAY_RE.match(since):
            raise StandingError(f"classes.{name}.since is not a day")
        try:
            date.fromisoformat(since)
        except ValueError:
            raise StandingError(f"classes.{name}.since is not a day") from None
    if v["demoted"] and since is None:
        raise StandingError(f"classes.{name} is demoted with no day it was")
    if v["line"] is not None and (not isinstance(v["line"], str)
                                  or not _SHA_RE.match(v["line"])):
        raise StandingError(f"classes.{name}.line is not a sha256")
    return {k: v[k] for k in ("rung_in_force", "demoted", "since", "line")}


def check(block: Any, *, workspace_ref: str | None) -> dict[str, Any]:
    """`block` as kept: contract 1, naming `workspace_ref` (its status's
    own), the classes this version knows. Raises StandingError."""
    if not isinstance(block, dict):
        raise StandingError("the authority block is not an object")
    size = len(json.dumps(block, sort_keys=True, separators=(",", ":"),
                          default=str).encode("utf-8"))
    if size > BLOCK_MAX_BYTES:
        raise StandingError(f"the authority block is {size:,} bytes, over {BLOCK_MAX_BYTES:,}")
    if block.get("v") != FORMAT or not isinstance(block.get("read"), bool):
        raise StandingError("the authority block is not version 1")
    ref = cloud_standing.workspace_ref(block.get("workspace_ref"))
    if ref is None or ref != workspace_ref:
        raise StandingError("the authority block names another workspace than the status it "
                            "came in")
    if not block["read"]:
        if not set(block) <= NOT_READ_KEYS | {"classes"} or "reason" not in block:
            raise StandingError("the authority block's keys are not contract 1's")
        out: dict[str, Any] = {"v": FORMAT, "read": False, "workspace_ref": ref,
                               "reason": _line(block["reason"], "reason")}
        if block.get("as_of") is not None:
            out["as_of"] = _as_of(block["as_of"])
        return out
    if set(block) != READ_KEYS:
        raise StandingError("the authority block's keys are not contract 1's")
    classes = block["classes"]
    if not isinstance(classes, dict) or len(classes) > _CLASSES_MAX:
        raise StandingError(f"classes is not an object of at most {_CLASSES_MAX}")
    kept: dict[str, Any] = {}
    for name, v in classes.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise StandingError("classes has a key that is not a class name")
        if name in CLASSES:
            kept[name] = _class(v, name)
    return {"v": FORMAT, "read": True, "workspace_ref": ref, "as_of": _as_of(block["as_of"]),
            "classes": kept}


# ── kept ─────────────────────────────────────────────────────────────────────

def _write(doc: dict[str, Any]) -> Path:
    """Write `doc` as the file, 0600, all or nothing: a temp file beside it,
    then os.replace. `nable cloud status` and `nable cloud connect --send`
    call it; the guard never writes it."""
    data = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise StandingError(f"the standing is {len(data):,} bytes, over the {MAX_BYTES:,} the "
                            "guard reads")
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return target


def _bound_to(box: str, workspace: str | None) -> str | None:
    """None when `box` and `workspace` are the ones this machine last sent
    to, else why not."""
    sent, why = cloud_standing.bound()
    if why:
        return why
    host = box.split("://", 1)[-1]
    if sent is None:
        return ("this machine has sent nothing to a box yet, so nothing says this standing is "
                f"its workspace's; `nable cloud connect --box {box} --send` binds one")
    if sent["box"] != box:
        return (f"this machine last sent to {sent['box'].split('://', 1)[-1]}, not {host}, so "
                "this is not the standing of the workspace it sends to")
    if sent["workspace"] is None or sent["workspace"] != workspace:
        return (f"this standing is another workspace's on {host} than the one this machine "
                f"last sent to; send again with `nable cloud connect --box {box} --send`")
    return None


def _not_kept(why: str) -> dict[str, Any]:
    return {"kept": False, "why": why, "file": None, "read": None, "classes": 0}


def keep(block: Any, *, box: str, workspace_ref: str | None,
         fetched_at: datetime) -> dict[str, Any]:
    """Keep a status answer's authority block for the guard, when it may:
    {"kept", "why", "file", "read", "classes"}. Never raises. A block for
    another box or workspace is not kept, with why; one for this machine's
    that is out of shape is kept as not read, which only narrows."""
    try:
        return _keep(block, box=box, workspace_ref=workspace_ref, fetched_at=fetched_at)
    except Exception as exc:  # noqa: BLE001 - said, never raised
        return _not_kept(f"not kept ({type(exc).__name__})")


def _keep(block: Any, *, box: str, workspace_ref: str | None,
          fetched_at: datetime) -> dict[str, Any]:
    if block is None:
        # An older box. A standing kept from another box or workspace than
        # this one is no longer this machine's, and nothing replaces it.
        if _bound_to(box, workspace_ref) is None:
            doc, _why = read()
            if doc is not None and (doc.get("box"), doc.get("workspace_ref")) != (
                    box, workspace_ref):
                with contextlib.suppress(OSError):
                    path().unlink()
        return _not_kept("the box's status carries no authority block (an older box), so "
                         "authority.yaml alone answers here")
    named = block.get("workspace_ref") if isinstance(block, dict) else None
    if workspace_ref is None or named != workspace_ref:
        return _not_kept("the authority block was refused whole: it names another workspace "
                         "than the status it came in")
    not_bound = _bound_to(box, workspace_ref)
    if not_bound:
        return _not_kept(not_bound)
    try:
        kept = check(block, workspace_ref=workspace_ref)
        why = None if kept["read"] else (
            f"the workspace's ladder pass was not read: {kept['reason']}")
    except StandingError as exc:
        kept = {"v": FORMAT, "read": False, "workspace_ref": workspace_ref,
                "reason": f"the box's authority block was refused: {exc}"}
        why = kept["reason"]
    doc = {"v": FORMAT, "box": box, "host": box.split("://", 1)[-1],
           "workspace_ref": workspace_ref,
           "fetched_at": fetched_at.astimezone(UTC).isoformat(timespec="seconds"),
           "block": kept}
    try:
        target = _write(doc)
    except (StandingError, OSError) as exc:
        return _not_kept(f"not kept ({type(exc).__name__}): {exc}")
    return {"kept": True, "why": why, "file": str(target), "read": kept["read"],
            "classes": len(kept.get("classes") or {})}


def read() -> tuple[dict[str, Any] | None, str | None]:
    """(the kept standing, None), (None, None) when none is kept, or (None,
    why it was not read). Through guard_paths.read_regular: a FIFO or a
    device in its place is not read, and never waited on."""
    from . import guard_paths
    p = path()
    try:
        raw = guard_paths.read_regular(p, MAX_BYTES + 1)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"{FILE_NAME} could not be read ({exc.strerror or type(exc).__name__})"
    if len(raw) > MAX_BYTES:
        return None, f"{FILE_NAME} is over the {MAX_BYTES:,} bytes the guard reads"
    try:
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("v") != FORMAT:
            raise StandingError("not this format")
        box = doc.get("box")
        if not isinstance(box, str) or not box.startswith("https://"):
            raise StandingError("it names no https box")
        ref = cloud_standing.workspace_ref(doc.get("workspace_ref"))
        block = doc.get("block")
        if ref is None or not isinstance(block, dict):
            raise StandingError("it names no workspace")
        if block.get("read") is False:
            # Kept as not read: by this module, from a refused block too.
            _line(block.get("reason"), "reason", 500)
            if block.get("workspace_ref") != ref:
                raise StandingError("its block names another workspace")
        else:
            block = check(block, workspace_ref=ref)
    except (UnicodeDecodeError, ValueError, AttributeError, TypeError) as exc:
        return None, f"{FILE_NAME} is not a standing this version reads ({exc})"
    return {"box": box, "host": box.split("://", 1)[-1], "workspace_ref": ref,
            "fetched_at": doc.get("fetched_at"), "block": block}, None


def for_guard() -> dict[str, Any] | None:
    """What the guard applies, or None when no standing is kept (the file
    alone answers): {"read", "why", "classes", "as_of", "host",
    "workspace_ref"}. Not read (read False, with why) when the file cannot
    be read, is another box's or workspace's than this machine last sent
    to, or says the box's ladder pass did not run. Never raises."""
    try:
        doc, why = read()
        if why:
            return {"read": False, "why": why, "classes": {}, "as_of": None, "host": None,
                    "workspace_ref": None}
        if doc is None:
            return None
        base = {"as_of": doc["block"].get("as_of"), "host": doc["host"],
                "workspace_ref": doc["workspace_ref"]}
        not_bound = _bound_to(doc["box"], doc["workspace_ref"])
        if not_bound:
            return {"read": False, "why": f"the standing kept here is not read: {not_bound}",
                    "classes": {}, **base}
        block = doc["block"]
        if not block["read"]:
            return {"read": False, "why": str(block["reason"]), "classes": {}, **base}
        return {"read": True, "why": None, "classes": dict(block["classes"]), **base}
    except Exception as exc:  # noqa: BLE001 - not read, never a crash in the hook
        return {"read": False, "why": f"{FILE_NAME} was not read ({type(exc).__name__})",
                "classes": {}, "as_of": None, "host": None, "workspace_ref": None}


def text(got: dict[str, Any], block: Any) -> str:
    """What `nable cloud status` and `nable cloud connect --send` say of the
    authority block: kept for the guard, or shown and not kept, and why."""
    head = "Authority ladder: "
    if isinstance(block, dict) and block.get("read") is True and isinstance(
            block.get("classes"), dict):
        parts = []
        for name in CLASSES:
            c = block["classes"].get(name)
            if not isinstance(c, dict):
                continue
            if c.get("demoted"):
                parts.append(f"{name} demoted on {c.get('since')} after a reversal, at "
                             "approve_each until an admin lifts it")
            elif c.get("rung_in_force"):
                parts.append(f"{name} at {c['rung_in_force']} on the workspace")
        head += "; ".join(parts) if parts else "no class named on the workspace"
    elif isinstance(block, dict) and block.get("read") is False:
        head += "not read on the workspace"
    else:
        head += "none served"
    if got.get("kept"):
        if got.get("read") is False:
            return (f"{head}. Kept in {got['file']} as not read ({got.get('why')}): the guard "
                    "holds every class authority.yaml has at bounded at approve_each.")
        return (f"{head}. Kept in {got['file']} for the guard, which applies the stricter of "
                "this and authority.yaml; the workspace can only narrow.")
    return f"{head}. Not kept for the guard, which reads authority.yaml alone: {got.get('why')}."
