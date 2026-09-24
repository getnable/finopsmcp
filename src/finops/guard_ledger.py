"""Append-only, hash-chained record of every guard verdict.

The guard's value is the moment before money is spent, and a moment leaves no
trace. Without a record nobody can answer "what did our agents try to do this
month, what did the guard stop, and how many dollars were in play", and a
guard that cannot show its work is hard to keep installed. This is that
record, kept locally: nothing here leaves the machine.

Format: one JSON object per line in <data_dir>/guard-ledger.jsonl, created
0600 and opened O_APPEND. Every record carries `prev`, the sha256 of the
previous line's bytes (64 zeros for the first), so editing, deleting or
reordering any record breaks the chain at the next line, which `nable guard
verify-log` reports. What a chain cannot show on its own is the tail being cut
off; verify-log prints the head hash so it can be anchored somewhere else (a
ticket, a commit, a log shipper) and compared later.

Cost to the hook: one open, one lock, one bounded read of the last line (a
seek from the end, never a scan), one write. append() never raises. A ledger
that cannot be written is a missing line, not a blocked agent.

Commands are stored as a redacted summary. An agent's shell line can carry a
credential (`AWS_SECRET_ACCESS_KEY=... terraform apply`, a bearer token in a
curl, a password flag), and an audit log is exactly the file that gets shared
with the people who should never see one. redact() errs toward removing too
much: a lost detail in a summary costs nothing, a leaked key costs a rotation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LEDGER_NAME = "guard-ledger.jsonl"
GENESIS = "0" * 64
SCHEMA = 1
DECISIONS = ("allow", "warn", "ask", "deny", "fail_open")

# Tests point this at a throwaway file (tests/conftest.py); nothing else sets it.
_path_override: Path | None = None

_SUMMARY_MAX = 400
_TAIL_CHUNK = 64 * 1024
# The most recent() reads on the hook path: a few thousand records, a few ms.
_RECENT_MAX_BYTES = 4 * 1024 * 1024


# ── where ─────────────────────────────────────────────────────────────────────

def _data_dir() -> Path:
    """storage.db.data_dir(), without importing SQLAlchemy into the hook.

    That import costs ~240ms, on every agent tool call, to compute a path. The
    rule is copied instead (tests pin that the two agree) and the real function
    is used whenever something else already paid for the import."""
    db = sys.modules.get("finops.storage.db")
    if db is not None:
        return db.data_dir()
    profile = os.environ.get("FINOPS_PROFILE", "").strip()
    if profile:
        d = Path.home() / ".finops" / "profiles" / profile
    else:
        raw = os.environ.get("FINOPS_DATA_DIR", "")
        d = Path(raw).expanduser() if raw else Path.home() / ".finops"
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def ledger_path() -> Path:
    return _path_override if _path_override is not None else _data_dir() / LEDGER_NAME


# ── redaction ─────────────────────────────────────────────────────────────────

_SECRET_WORD = r"(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|AUTH)"
_REDACTIONS: list[tuple[re.Pattern[str], Any]] = [
    # PEM private keys, whole block (or to the end if the block is cut off).
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
                re.DOTALL), "[REDACTED-PRIVATE-KEY]"),
    # NAME=value where NAME mentions a key, secret, token or password:
    # AWS_SECRET_ACCESS_KEY=..., GITHUB_TOKEN="...", db_password='...'.
    (re.compile(rf"\b([A-Za-z_][A-Za-z0-9_]*{_SECRET_WORD}[A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S+)",
                re.IGNORECASE), r"\1=[REDACTED]"),
    # --password x, --master-user-password=x, --api-key x, --auth-token x.
    (re.compile(r"(--[A-Za-z0-9-]*(?:password|passwd|secret|token|key)[A-Za-z0-9-]*)(=|\s+)"
                r"(\"[^\"]*\"|'[^']*'|\S+)", re.IGNORECASE), r"\1\2[REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b"),
     "[REDACTED-AWS-KEY-ID]"),
    (re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), r"\1 [REDACTED]"),
    # A password embedded in a URL, before the @.
    (re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:)[^@\s]+@"),  # pragma: allowlist secret
     r"\1[REDACTED]@"),
]
# Long high-entropy runs: base64 or url-safe tokens (AWS secret keys, GitHub
# and Slack tokens, JWT segments). A run counts when it mixes upper case, lower
# case and digits, which a hex digest, a path or a resource name rarely does.
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_-]{32,}={0,2}")


def _long_token(m: re.Match[str]) -> str:
    t = m.group(0)
    if t.startswith("/"):
        return t                       # an absolute path is the audit detail, not a secret
    if re.search(r"[A-Z]", t) and re.search(r"[a-z]", t) and re.search(r"[0-9]", t):
        return "[REDACTED]"
    return t


def redact(text: Any, limit: int = _SUMMARY_MAX) -> str:
    """A summary of `text` safe to keep in an audit log."""
    s = " ".join(str(text or "").split())
    for pattern, repl in _REDACTIONS:
        s = pattern.sub(repl, s)
    s = _LONG_TOKEN_RE.sub(_long_token, s)
    return s if len(s) <= limit else s[: limit - 3] + "..."


# ── write ─────────────────────────────────────────────────────────────────────

def _sha(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def _last_line(fd: int) -> bytes | None:
    """The file's last line without its newline, or None when it is empty."""
    size = os.fstat(fd).st_size
    if size == 0:
        return None
    chunk = min(size, _TAIL_CHUNK)
    while True:
        os.lseek(fd, size - chunk, os.SEEK_SET)
        tail = os.read(fd, chunk)
        body = tail[:-1] if tail.endswith(b"\n") else tail
        cut = body.rfind(b"\n")
        if cut >= 0 or chunk >= size:
            return body[cut + 1:]
        chunk = min(size, chunk * 2)


def append(entry: dict[str, Any]) -> bool:
    """Append one record, chained to the one before it. Never raises."""
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            try:
                if os.fstat(fd).st_mode & 0o077:
                    os.fchmod(fd, 0o600)   # a pre-existing file keeps no group/world bits
            except (AttributeError, OSError):
                pass
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass                       # no flock (Windows): single-writer best effort
            last = _last_line(fd)
            rec = {"v": SCHEMA,
                   "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                   **entry,
                   "prev": _sha(last) if last is not None else GENESIS}
            line = json.dumps(rec, separators=(",", ":"), sort_keys=True,
                              default=str).encode("utf-8")
            # A torn previous write left no newline; never glue onto it.
            lead = b"\n" if last is not None and not _ends_with_newline(fd) else b""
            os.write(fd, lead + line + b"\n")
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def _ends_with_newline(fd: int) -> bool:
    size = os.fstat(fd).st_size
    if size == 0:
        return True
    os.lseek(fd, size - 1, os.SEEK_SET)
    return os.read(fd, 1) == b"\n"


# ── read ──────────────────────────────────────────────────────────────────────

def verify(path: Path | None = None) -> dict[str, Any]:
    """Walk the chain. ok is False at the first line that does not parse or
    whose `prev` is not the hash of the line before it."""
    path = path or ledger_path()
    out: dict[str, Any] = {"ok": True, "records": 0, "head": GENESIS, "path": str(path)}
    if not path.exists():
        return out
    prev = GENESIS
    with path.open("rb") as fh:
        for n, raw in enumerate(fh, start=1):
            line = raw.rstrip(b"\n")
            try:
                rec = json.loads(line)
                claimed = rec.get("prev") if isinstance(rec, dict) else None
            except ValueError:
                claimed = None
                rec = None
            if rec is None:
                out.update(ok=False, broken_at=n, problem="line is not a ledger record")
                return out
            if claimed != prev:
                out.update(ok=False, broken_at=n,
                           problem="prev does not match the line before it: a record "
                                   "was edited, removed or reordered")
                return out
            prev = _sha(line)
            out["records"] = n
    out["head"] = prev
    return out


def recent(minutes: float, *, path: Path | None = None, now: datetime | None = None,
           max_bytes: int = _RECENT_MAX_BYTES) -> list[dict[str, Any]]:
    """Records from the last `minutes`, oldest first, read from the END of the file.

    This is the hook's read (the velocity cap and loop detection in guard.py),
    so it never scans the file: it reads backwards in chunks and stops at the
    first record older than the window. Records are appended in time order
    under a lock, so everything before that one is older too. `max_bytes`
    bounds the read whatever the window holds; a window that does not fit is
    answered from its newest part. Unreadable lines are skipped. Raises on an
    unreadable file: the caller decides what a failed read means."""
    path = path or ledger_path()
    since = (now or datetime.now(UTC)) - timedelta(minutes=minutes)
    try:
        fh = path.open("rb")
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    with fh:
        pos = fh.seek(0, os.SEEK_END)
        carry = b""
        read_total = 0
        while pos > 0 and read_total < max_bytes:
            step = min(_TAIL_CHUNK, pos, max_bytes - read_total)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + carry
            read_total += step
            lines = buf.split(b"\n")
            # The first piece may be the tail of a line that starts further
            # back; keep it for the next chunk unless this is the file's start.
            carry = lines.pop(0) if pos > 0 else b""
            for raw in reversed(lines):
                rec = _parse_recent(raw)
                if rec is None:
                    continue
                if rec[0] < since:
                    out.reverse()
                    return out
                out.append(rec[1])
    # A line cut off by max_bytes is left unread rather than guessed at.
    out.reverse()
    return out


def _parse_recent(raw: bytes) -> tuple[datetime, dict[str, Any]] | None:
    if not raw.strip():
        return None
    try:
        rec = json.loads(raw)
        ts = datetime.fromisoformat(rec["ts"])
    except (ValueError, KeyError, TypeError):
        return None
    return (ts, rec) if ts.tzinfo is not None else None


def read(days: float | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    """Records newer than `days` (all when None), skipping unreadable lines."""
    path = path or ledger_path()
    if not path.exists():
        return []
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    out = []
    with path.open("rb") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
                ts = datetime.fromisoformat(rec["ts"])
            except (ValueError, KeyError, TypeError):
                continue
            if since is None or ts >= since:
                out.append(rec)
    return out


def summarize(days: float = 30, path: Path | None = None) -> dict[str, Any]:
    """What `nable guard report` prints: counts, dollars at stake, the biggest."""
    recs = read(days, path)
    by_decision = {d: 0 for d in DECISIONS}
    by_harness: dict[str, int] = {}
    by_action: dict[str, int] = {}
    stake = allowed = committed = 0.0
    errors: dict[str, int] = {}
    for r in recs:
        d = r.get("decision")
        by_decision[d] = by_decision.get(d, 0) + 1
        h = r.get("harness") or "unknown"
        by_harness[h] = by_harness.get(h, 0) + 1
        if r.get("action_type"):
            by_action[r["action_type"]] = by_action.get(r["action_type"], 0) + 1
        usd = r.get("monthly_usd")
        if isinstance(usd, (int, float)):
            if d in ("ask", "deny"):
                stake += usd
            elif d in ("allow", "warn"):
                allowed += usd
        if isinstance(r.get("total_usd"), (int, float)) and d in ("ask", "deny"):
            committed += r["total_usd"]
        if d == "fail_open":
            e = r.get("error") or "unknown"
            errors[e] = errors.get(e, 0) + 1
    priced = [r for r in recs if r.get("decision") in ("ask", "deny")
              and isinstance(r.get("monthly_usd"), (int, float))]
    top = sorted(priced, key=lambda r: -abs(r["monthly_usd"]))[:5]
    return {
        "days": days,
        "records": len(recs),
        "by_decision": by_decision,
        "by_harness": by_harness,
        "by_action_type": dict(sorted(by_action.items(), key=lambda kv: -kv[1])),
        "usd_per_month_escalated_or_blocked": round(stake, 2),
        "usd_per_month_allowed_with_a_figure": round(allowed, 2),
        "usd_order_ceilings_escalated_or_blocked": round(committed, 2),
        "fail_open_errors": errors,
        "largest": [{k: r.get(k) for k in ("ts", "harness", "tool", "decision",
                                           "action_type", "monthly_usd", "basis",
                                           "command")} for r in top],
        "path": str(path or ledger_path()),
    }
