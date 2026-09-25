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
that cannot be written is a missing line, not a blocked agent. The lock is
waited on for at most 200 ms: a lock held longer (by anything, the agent
included) costs the record, which is counted in guard-ledger.unrecorded.jsonl
for `nable guard doctor`, and never the verdict.

Commands are stored as a redacted summary. An agent's shell line can carry a
credential (`AWS_SECRET_ACCESS_KEY=... terraform apply`, a bearer token in a
curl, a password flag), and an audit log is exactly the file that gets shared
with the people who should never see one. redact() errs toward removing too
much: a lost detail in a summary costs nothing, a leaked key costs a rotation.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LEDGER_NAME = "guard-ledger.jsonl"
# Verdicts answered but not recorded (the ledger was locked or not a file).
UNRECORDED_NAME = "guard-ledger.unrecorded.jsonl"
# The longest append() waits for another process's lock.
_LOCK_WAIT_S = 0.2
GENESIS = "0" * 64
SCHEMA = 1
DECISIONS = ("allow", "warn", "ask", "deny", "fail_open")

# Tests point this at a throwaway file (tests/conftest.py); nothing else sets it.
_path_override: Path | None = None

_SUMMARY_MAX = 400
# What redact() reads of its input, at most. Ten times the summary, so the
# summary is always complete, and small enough that no pattern can be slow.
_REDACT_INPUT_MAX = 4096
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

# PASS and PW catch the short spellings (db_pass=, admin_pw=) and, yes, also
# `bypass=`: a lost detail in a summary costs nothing, a leaked password does.
_SECRET_WORD = (r"(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|PASS|PW|CREDENTIAL|AUTH"
                r"|SIGNATURE)")
_REDACTIONS: list[tuple[re.Pattern[str], Any]] = [
    # PEM private keys, whole block (or to the end if the block is cut off).
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
                re.DOTALL), "[REDACTED-PRIVATE-KEY]"),
    # NAME=value where NAME mentions a key, secret, token or password:
    # AWS_SECRET_ACCESS_KEY=..., GITHUB_TOKEN="...", db_password='...'.
    # The name may be the secret word alone (`password=...`, `--set
    # db.password=...`): the lookbehind anchors it without spending a character.
    (re.compile(rf"(?<![A-Za-z0-9_])([A-Za-z0-9_]*{_SECRET_WORD}[A-Za-z0-9_]*)="
                r"(\"[^\"]*\"|'[^']*'|\S+)", re.IGNORECASE), r"\1=[REDACTED]"),
    # --password x, --master-user-password=x, --api-key x, --auth-token x.
    (re.compile(r"(?<![A-Za-z0-9-])(--[A-Za-z0-9-]*(?:password|passwd|pass|pw|secret|token|key"
                r"|credential|signature)[A-Za-z0-9-]*)(=|\s+)"
                r"(\"[^\"]*\"|'[^']*'|\S+)", re.IGNORECASE), r"\1\2[REDACTED]"),
    # A signed URL's query: ?sig=... (Azure SAS), X-Amz-Signature=..., and the
    # like. Names with a secret word in them are already caught above.
    (re.compile(r"([?&](?:sig|x-amz-signature|x-goog-signature|code)=)[^&\s]+", re.IGNORECASE),
     r"\1[REDACTED]"),
    # The attached password of the MySQL clients: mysql -uroot -phunter2.
    (re.compile(r"(\b(?:mysql|mysqldump|mysqladmin|mysqlsh|mariadb)\b[^|;&]*?(?<!\S)-p)"
                r"(?=\S)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1[REDACTED]"),
    # -p <password> on a registry or cloud login: az login -u x -p y,
    # docker login -p y (the long --password form is caught above).
    (re.compile(r"(\b(?:az|docker|podman|oras|skopeo|registry)\s+login\b[^|;&]*?(?<!\S)-p)"
                r"(\s+|=)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1\2[REDACTED]"),
    # user:password handed to a client: curl -u admin:hunter2, --user a:b.
    (re.compile(r"((?<!\S)(?:-u|--user|--proxy-user)(?:\s+|=)[^:\s]+:)(\S+)"),
     r"\1[REDACTED]"),
    # Tokens with a published prefix, whatever their length or mix.
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{6,}"), "[REDACTED-SLACK-TOKEN]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})"),
     "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"), "[REDACTED-API-KEY]"),
    (re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{12,}"), "[REDACTED-API-KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[REDACTED-API-KEY]"),
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b"),
     "[REDACTED-AWS-KEY-ID]"),
    (re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), r"\1 [REDACTED]"),
    # A password embedded in a URL, before the @.
    # Anchored where a scheme can start, so a long run of letters is one start,
    # not one per letter (redact must stay linear: see _REDACT_INPUT_MAX).
    (re.compile(r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:)[^@\s]+@"),  # pragma: allowlist secret
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
    """A summary of `text` safe to keep in an audit log.

    Only the first _REDACT_INPUT_MAX characters are looked at: the summary is
    a few hundred characters anyway, and an agent's command can be padded to
    megabytes to make the hook run past its timeout. A token cut in two at
    that boundary is dropped rather than kept half-redacted."""
    raw = str(text or "")
    cut = len(raw) > _REDACT_INPUT_MAX
    s = " ".join(raw[:_REDACT_INPUT_MAX].split())
    if cut:
        s = s.rsplit(" ", 1)[0] + " ..." if " " in s else "..."
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


def _lock(fd: int) -> bool:
    """Take the ledger's exclusive lock, waiting at most _LOCK_WAIT_S.

    A blocking lock let anything that could hold one (`flock -x
    ~/.finops/guard-ledger.jsonl sleep 999 &`, run by the agent itself) hang
    every recorded verdict until the harness timed the hook out, which fails
    open. False means the lock is held elsewhere; the caller gives up on the
    record, never on the verdict."""
    try:
        import fcntl
    except ImportError:
        return True                        # no flock (Windows): single-writer best effort
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        except OSError:
            return True                    # a filesystem without flock: best effort, as before


def _note_unrecorded(entry: dict[str, Any], why: str) -> None:
    """Count a verdict that was answered but could not be recorded.

    One short line in a file beside the ledger (no command, so nothing to
    redact), which `nable guard doctor` reads. Never blocks and never raises:
    opened non-blocking, and only a regular file is written."""
    with contextlib.suppress(Exception):
        path = ledger_path().with_name(UNRECORDED_NAME)
        flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(path, flags, 0o600)
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                line = json.dumps({"ts": datetime.now(UTC).isoformat(timespec="seconds"),
                                   "why": why, "decision": entry.get("decision"),
                                   "action_type": entry.get("action_type")})
                os.write(fd, line.encode() + b"\n")
        finally:
            os.close(fd)


def unrecorded(path: Path | None = None) -> dict[str, Any]:
    """{count, last, why} for verdicts answered but never recorded."""
    path = path or ledger_path().with_name(UNRECORDED_NAME)
    out: dict[str, Any] = {"count": 0, "last": None, "why": {}, "path": str(path)}
    try:
        with path.open("rb") as fh:
            for raw in fh:
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                out["count"] += 1
                out["last"] = rec.get("ts")
                why = str(rec.get("why"))
                out["why"][why] = out["why"].get(why, 0) + 1
    except OSError:
        pass
    return out


def append(entry: dict[str, Any]) -> bool:
    """Append one record, chained to the one before it. Never raises, and
    never waits more than _LOCK_WAIT_S on another process: a record that
    cannot be written in time is counted (unrecorded()) and dropped."""
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = (os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(path, flags, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                # A FIFO or a device in the ledger's place would block the
                # first read forever.
                _note_unrecorded(entry, "not_a_file")
                return False
            try:
                if st.st_mode & 0o077:
                    os.fchmod(fd, 0o600)   # a pre-existing file keeps no group/world bits
            except (AttributeError, OSError):
                pass
            if not _lock(fd):
                _note_unrecorded(entry, "locked")
                return False
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

def verify(path: Path | None = None, *, at_line: int | None = None) -> dict[str, Any]:
    """Walk the chain. ok is False at the first line that does not parse or
    whose `prev` is not the hash of the line before it. With `at_line`, the
    chain hash after that line is returned as hash_at (see check())."""
    path = path or ledger_path()
    out: dict[str, Any] = {"ok": True, "records": 0, "head": GENESIS, "path": str(path)}
    if not path.exists():
        return out
    prev = GENESIS
    with path.open("rb") as fh:
        for n, raw in enumerate(fh, start=1):
            if n - 1 == at_line:
                out["hash_at"] = prev
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
    if out["records"] == at_line:
        out["hash_at"] = prev
    out["head"] = prev
    return out


# ── anchor ────────────────────────────────────────────────────────────────────
# A chain shows an edit in the middle, but not the end being cut off: delete
# the last ten records, or the whole file, and what is left still verifies.
# The anchor is the record count and head hash the last clean check saw,
# kept beside the ledger. A later check that finds fewer records, or a
# different hash at that record, says so. The anchor sits in the same data
# directory, so whatever can rewrite the ledger can rewrite the anchor too; it
# catches a careless or partial edit, and for more, copy the head somewhere
# the agent cannot write (a ticket, a commit, a log shipper).

ANCHOR_NAME = "guard-ledger.anchor.json"


def anchor_path() -> Path:
    return ledger_path().with_name(ANCHOR_NAME)


def read_anchor() -> dict[str, Any] | None:
    try:
        a = json.loads(anchor_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(a, dict) or not isinstance(a.get("records"), int):
        return None
    return a


def save_anchor(result: dict[str, Any]) -> None:
    """Remember what a check saw: records, head, when. Never raises."""
    with contextlib.suppress(Exception):
        path = anchor_path()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"records": result["records"], "head": result["head"],
                                   "seen_at": datetime.now(UTC).isoformat(timespec="seconds"),
                                   "ledger": result.get("path")}) + "\n")
        tmp.chmod(0o600)
        os.replace(tmp, path)


def check(path: Path | None = None) -> dict[str, Any]:
    """verify(), plus what has changed since the anchor: `warnings` lists
    records removed from the end (or the file deleted or emptied) and history
    rewritten before the anchored record. `clean` is True only when the
    chain verifies and there is no warning."""
    anchor = read_anchor()
    at = anchor["records"] if anchor else None
    res = verify(path, at_line=at)
    warnings: list[str] = []
    if anchor and at:
        seen = anchor.get("seen_at") or "the last check"
        if res["records"] < at:
            gone = "the file is empty or gone" if res["records"] == 0 else (
                f"{at - res['records']} record(s) are gone from the end")
            warnings.append(f"the ledger had {at} record(s) at {seen} and has "
                            f"{res['records']} now: {gone}")
        elif res.get("hash_at") != anchor.get("head"):
            warnings.append(f"record {at} is not the one seen at {seen}: the history "
                            "up to it was rewritten")
    res["anchor"] = anchor
    res["warnings"] = warnings
    res["clean"] = bool(res["ok"] and not warnings)
    return res


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


# Two identical commands from the same session this close together are one
# decision asked twice (an agent retrying after a "no"), not twice the money.
_REPEAT_WINDOW = timedelta(minutes=10)


def _repeats(recs: list[dict[str, Any]]) -> set[int]:
    """Indexes of records that repeat an identical command, from the same
    session and with the same kind of verdict, within _REPEAT_WINDOW of the
    last one. Their dollars are not summed again."""
    seen: dict[tuple[Any, ...], datetime] = {}
    out: set[int] = set()
    for i, r in enumerate(recs):
        if not r.get("command"):
            continue
        bucket = "stake" if r.get("decision") in ("ask", "deny") else r.get("decision")
        key = (r["command"], r.get("session"), bucket)
        try:
            ts = datetime.fromisoformat(r["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        last = seen.get(key)
        if last is not None and timedelta(0) <= ts - last <= _REPEAT_WINDOW:
            out.add(i)
        seen[key] = ts
    return out


def _positive(x: Any) -> float:
    """A cost increase, or 0: a destroy plan's -$1,328/mo is not money at
    stake, and summing it would hide the launch next to it."""
    return float(x) if isinstance(x, (int, float)) and x > 0 else 0.0


def summarize(days: float = 30, path: Path | None = None, *,
              session: str | None = None) -> dict[str, Any]:
    """What `nable guard report` prints: counts, dollars at stake, the biggest,
    and the same dollars per agent session (by_session, largest first).

    `session` limits everything to one session's records. Dollar sums count
    cost increases only, once per decision: a repeat of the same command from
    the same session within ten minutes is counted in the decisions but not
    summed again (repeats_not_summed)."""
    recs = read(days, path)
    if session is not None:
        recs = [r for r in recs if r.get("session") == session]
    repeats = _repeats(recs)
    by_decision = {d: 0 for d in DECISIONS}
    by_harness: dict[str, int] = {}
    by_action: dict[str, int] = {}
    by_session: dict[str, dict[str, Any]] = {}
    stake = allowed = committed = 0.0
    errors: dict[str, int] = {}
    for i, r in enumerate(recs):
        d = r.get("decision")
        by_decision[d] = by_decision.get(d, 0) + 1
        h = r.get("harness") or "unknown"
        by_harness[h] = by_harness.get(h, 0) + 1
        if r.get("action_type"):
            by_action[r["action_type"]] = by_action.get(r["action_type"], 0) + 1
        if d == "fail_open":
            e = r.get("error") or "unknown"
            errors[e] = errors.get(e, 0) + 1
        sess = by_session.setdefault(r.get("session") or "unknown", {
            "records": 0, "asked_or_blocked": 0, "usd_per_month_escalated_or_blocked": 0.0,
            "usd_per_month_allowed_with_a_figure": 0.0, "first": r.get("ts"),
            "harness": r.get("harness")})
        sess["records"] += 1
        sess["last"] = r.get("ts")
        if d in ("ask", "deny"):
            sess["asked_or_blocked"] += 1
        if i in repeats:
            continue
        usd = _positive(r.get("monthly_usd"))
        if d in ("ask", "deny"):
            stake += usd
            committed += _positive(r.get("total_usd"))
            sess["usd_per_month_escalated_or_blocked"] += usd
        elif d in ("allow", "warn"):
            allowed += usd
            sess["usd_per_month_allowed_with_a_figure"] += usd
    for sess in by_session.values():
        for k in ("usd_per_month_escalated_or_blocked", "usd_per_month_allowed_with_a_figure"):
            sess[k] = round(sess[k], 2)
    priced = [r for i, r in enumerate(recs) if r.get("decision") in ("ask", "deny")
              and i not in repeats and _positive(r.get("monthly_usd"))]
    top = sorted(priced, key=lambda r: -r["monthly_usd"])[:5]
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
        "repeats_not_summed": len(repeats),
        "session": session,
        "by_session": dict(sorted(
            by_session.items(),
            key=lambda kv: -(kv[1]["usd_per_month_escalated_or_blocked"]
                             + kv[1]["usd_per_month_allowed_with_a_figure"]))),
        "largest": [{k: r.get(k) for k in ("ts", "harness", "session", "tool", "decision",
                                           "action_type", "monthly_usd", "basis",
                                           "command")} for r in top],
        "path": str(path or ledger_path()),
    }
