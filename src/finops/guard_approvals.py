# SPDX-License-Identifier: Apache-2.0
"""One-time approvals for agent harnesses that cannot ask.

Codex CLI, Gemini CLI, Cline and GitHub Copilot's cloud agent have no way to
pause a tool call for a person, so every "ask" the guard gives them becomes a
deny (guard_adapters). Before this, the only way past one was for a person
to copy the command into their own shell. Now the deny carries an approval
id and the command that grants it:

    nable guard approve 3f9c2a1b

run by a person in their own terminal. The identical call (the same command,
or MCP tool and arguments, digested; the same working directory; the same
harness) is then allowed once within the approval's window, and the ledger
records it as approved_out_of_band, with who approved.

What it never lifts: a deny. `on_budget_breach: deny`, a freeze in deny mode,
a pack's deny rule and a call outside the policy's allowlist are the org's
policy, not a question waiting for a person, and their reasons say so. Nor a
change to the guard itself (its budgets, its hook, its packs, the org model,
its own files): a person makes those changes themselves.

Who may approve: a person. approve() takes the HumanDecision only a CLI with
a terminal, or with --as naming someone, makes (org.cli._who), and the
guard's self rules ask about, or in these harnesses deny, `nable guard
approve` from an agent on every entry point: a shell command, a command
line in an MCP call's arguments, and code that calls approve(). The store
(guard-approvals.json beside the decision ledger) is one of the guard's
protected files, written atomically, 0600, and pruned of expired entries on
every write.

Standard library only: the hook reads this module on a deny-only harness's
ask, and on nothing else.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

STORE_NAME = "guard-approvals.json"
EXPIRY_MINUTES = 15
# Harnesses whose hooks cannot ask a person (guard_adapters): their asks are
# denies. Copilot is one only under its cloud agent.
DENY_ONLY = ("codex", "gemini", "cline")
_LOCK_WAIT_S = 0.2
_VERSION = 1


class ApprovalError(Exception):
    """An approval that cannot be given: no such id, expired, already used."""


def store_path() -> Path:
    """Beside the decision ledger, in nable's data directory."""
    from .guard_ledger import ledger_path
    return ledger_path().with_name(STORE_NAME)


def deny_only(harness: str | None) -> bool:
    """Whether `harness` turns an ask into a deny."""
    if harness in DENY_ONLY:
        return True
    if harness == "copilot":
        from .guard_adapters import _copilot_cloud
        return _copilot_cloud()
    return False


def call_digest(kind: str, text: str) -> str:
    """sha256 over what the call is: "shell" and its command line, or "mcp"
    and the tool name with its arguments as canonical JSON."""
    return hashlib.sha256(f"{kind}\n{text}".encode()).hexdigest()


def mcp_text(tool_name: str, arguments: Any) -> str:
    return f"{tool_name}\n" + json.dumps(arguments, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False, default=str)


def _cwd(cwd: str | None) -> str:
    return os.path.normpath(cwd) if cwd else ""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _at(text: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ── the store ─────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _locked(d: Path) -> Iterator[bool]:
    """The store's directory locked (flock on its fd, so no lock file lands
    beside it), waiting at most _LOCK_WAIT_S. Yields False when the lock is
    held elsewhere: the caller gives up, and a deny stays a deny."""
    d.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:            # no flock (Windows): best effort
        yield True
        return
    fd = os.open(d, os.O_RDONLY)
    try:
        deadline = time.monotonic() + _LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.01)
            except OSError:        # a filesystem without flock
                break
        yield True
    finally:
        os.close(fd)


def _read(p: Path) -> dict[str, dict[str, Any]]:
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = doc.get("approvals") if isinstance(doc, dict) else None
    if not isinstance(rows, dict):
        return {}
    return {str(k): v for k, v in rows.items() if isinstance(v, dict)}


def _pruned(rows: dict[str, dict[str, Any]], now: datetime) -> dict[str, dict[str, Any]]:
    out = {}
    for k, r in rows.items():
        end = _at(r.get("expires_at"))
        if end is not None and end > now:
            out[k] = r
    return out


def _write(p: Path, rows: dict[str, dict[str, Any]]) -> None:
    """Temp file in the same directory (mkstemp makes it 0600), fsync,
    os.replace: a crash leaves the old store, never half a new one."""
    body = json.dumps({"v": _VERSION, "approvals": rows}, indent=1, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _matches(r: dict[str, Any], digest: str, cwd: str, harness: str) -> bool:
    return (r.get("digest") == digest and r.get("cwd") == cwd
            and r.get("harness") == harness)


# ── the hook's side ───────────────────────────────────────────────────────────

def take(kind: str, text: str, *, cwd: str | None, harness: str) -> dict[str, Any] | None:
    """The approval a person gave for exactly this call, used up: the same
    digest, working directory and harness, approved, unused and unexpired.
    None when there is none (or the store is locked elsewhere)."""
    p = store_path()
    digest, where, now = call_digest(kind, text), _cwd(cwd), _now()
    if not p.exists():
        return None
    with _locked(p.parent) as ok:
        if not ok:
            return None
        rows = _read(p)
        live = _pruned(rows, now)
        hit = None
        for r in live.values():
            if _matches(r, digest, where, harness) and r.get("approved_by") \
                    and not r.get("used_at"):
                hit = r
                break
        if hit is None:
            if len(live) != len(rows):
                _write(p, live)
            return None
        hit["used_at"] = _iso(now)
        _write(p, live)
        return dict(hit)


def pending(kind: str, text: str, *, cwd: str | None, harness: str, session: str | None,
            tool: str | None, verdict: dict[str, Any]) -> str | None:
    """The id of the pending approval for this call, made (or the one
    already waiting for it, so a retried call does not pile up ids). None
    when the store is locked elsewhere."""
    from .guard_ledger import redact
    p = store_path()
    digest, where, now = call_digest(kind, text), _cwd(cwd), _now()
    with _locked(p.parent) as ok:
        if not ok:
            return None
        live = _pruned(_read(p), now)
        for r in live.values():
            if _matches(r, digest, where, harness) and not r.get("approved_by"):
                return str(r["id"])
        aid = secrets.token_hex(4)
        while aid in live:
            aid = secrets.token_hex(4)
        est = verdict.get("estimate") or {}
        live[aid] = {
            "id": aid, "kind": kind, "digest": digest, "cwd": where, "harness": harness,
            "session": redact(session, limit=128) if isinstance(session, str) and session
            else None,
            "tool": tool, "call": redact(text, limit=400),
            "why": redact(verdict.get("reason"), limit=600),
            "action_type": verdict.get("action_type"),
            "monthly_usd": est.get("monthly_usd"),
            "created_at": _iso(now),
            "expires_at": _iso(now + timedelta(minutes=EXPIRY_MINUTES)),
        }
        _write(p, live)
        return aid


# ── the person's side ─────────────────────────────────────────────────────────

def waiting() -> list[dict[str, Any]]:
    """Unexpired approvals, the newest first (what `nable guard approve`
    lists with no id)."""
    rows = _pruned(_read(store_path()), _now())
    return sorted(rows.values(), key=lambda r: str(r.get("created_at")), reverse=True)


def approve(approval_id: str, by: Any) -> dict[str, Any]:
    """A person approves one pending call, once. `by` is the HumanDecision
    the CLI made (a terminal, or --as): a name is not enough. Raises
    ApprovalError when the id names nothing waiting."""
    from .org.store import HumanDecision
    if not isinstance(by, HumanDecision):
        raise ApprovalError("approving a call the guard stopped is a person's decision: run "
                            "`nable guard approve ID` in your own terminal (or with --as WHO)")
    aid = (approval_id or "").strip().lower()
    p = store_path()
    now = _now()
    with _locked(p.parent) as ok:
        if not ok:
            raise ApprovalError("the approvals file is locked by another process; try again")
        rows = _read(p)
        live = _pruned(rows, now)
        r = live.get(aid)
        if r is None:
            if aid in rows:
                raise ApprovalError(f"approval {aid} has expired (they last "
                                    f"{EXPIRY_MINUTES} minutes); have the agent run the "
                                    "command again for a new id")
            raise ApprovalError(f"no approval {aid!r} is waiting (`nable guard approve` lists "
                                "them)")
        if r.get("used_at"):
            raise ApprovalError(f"approval {aid} was already used at {r['used_at']}")
        if r.get("approved_by"):
            raise ApprovalError(f"approval {aid} was already given by {r['approved_by']}")
        r.update(approved_by=by.who, approved_how=by.how, approved_at=_iso(now))
        _write(p, live)
        return dict(r)
