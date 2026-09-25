"""Token usage from the agent harnesses other than Claude Code.

ai_budget.py tallies per-response records. Claude Code's come from its own
transcripts; this module produces the same records for the other harnesses the
guard hooks (guard_adapters.py), so one budget sees every agent's spend:

  Codex CLI  local session rollouts, read from disk, nothing leaves the machine
  Cursor     Cursor keeps usage server-side, so only its team Admin API has it:
             read when CURSOR_ADMIN_API_KEY is set, never otherwise

Each record is a dict with the keys ai_budget._tally reads: ts, model, input,
output, cache_write, cache_write_1h, cache_read, fast, us_only, session, cwd,
plus harness. `input` is fresh (uncached) input, as in Claude's usage block, so
billable tokens mean the same thing for every harness.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HARNESS_CLAUDE = "claude-code"
HARNESS_CODEX = "codex"
HARNESS_CURSOR = "cursor"


def _epoch(ts: Any) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return None


def _int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


# ── Codex CLI ─────────────────────────────────────────────────────────────────
#
# The rollout format, read from https://github.com/openai/codex (codex-rs/):
#
#   rollout/src/recorder.rs     sessions/YYYY/MM/DD/rollout-<local time>-<thread id>.jsonl
#                               under CODEX_HOME; each line {"timestamp": "...Z",
#                               "type": ..., "payload": ...}, stamped when written
#   rollout/src/lib.rs          SESSIONS_SUBDIR "sessions", ARCHIVED_SESSIONS_SUBDIR
#                               "archived_sessions" (an archived thread moves there)
#   history/src/rollout_payload.rs   the line types: session_meta, turn_context,
#                               token_usage_record, event_msg, ...
#   protocol/src/protocol.rs    SessionMeta (id, session_id, forked_from_id, cwd),
#                               TurnContextItem (turn_id, cwd, model), TokenUsage,
#                               TokenUsageInfo, TokenCountEvent, TokenUsageRecord
#   rollout/src/policy.rs       token_usage_record, turn_context, session_meta and
#                               the token_count event are all persisted
#
# Usage is recorded two ways, and they must not both be counted:
#
#   token_usage_record (current Codex): one per model response, keyed by
#     response_id, with that response's own usage. core/src/session/mod.rs
#     (record_observed_response_completed) writes it; a forked thread does not
#     inherit it (core/src/agent/control/spawn.rs). Exact, counted as is.
#
#   event_msg token_count (every version): info.total_token_usage is the
#     thread's running total and info.last_token_usage the latest response.
#     last_token_usage is NOT safe to sum: the same event is re-sent with an
#     unchanged info whenever rate limits update (update_rate_limits), and
#     recompute_token_usage rewrites last_token_usage to an estimate. So a
#     rollout without records is counted by the growth of total_token_usage
#     between consecutive events. The total carries across a resume (the
#     session seeds it from the last token_count, session/mod.rs), and
#     set_token_usage_full resets its classes to zero, which reads as a restart.
#
# A forked thread's rollout starts with a copy of its parent's events, token
# counts included, and continues the parent's running total. Those copies are
# recognised by value: a running total already seen in the parent's rollout (or
# in any rollout read earlier) is a baseline, not new usage.
#
# TokenUsage follows the Responses API (codex-api/src/sse/responses.rs):
# input_tokens includes cached_input_tokens and cache_write_input_tokens,
# output_tokens includes reasoning_output_tokens.
#
# Rollouts compressed to .jsonl.zst (the local_thread_store_compression feature,
# off by default, for rollouts untouched for 7 days) are not read; the count of
# skipped files in the window is reported.

_CODEX_ROLLOUT = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)\.jsonl$")
_CODEX_LINE_TYPES = ('"session_meta"', '"turn_context"', '"token_usage_record"', '"token_count"')
_USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                 "output_tokens", "reasoning_output_tokens", "total_tokens")


def codex_home() -> Path:
    # Codex's find_codex_home: CODEX_HOME when set and non-empty, else ~/.codex.
    env = os.getenv("CODEX_HOME", "")
    return Path(env) if env else Path.home() / ".codex"


def _codex_dirs() -> list[Path]:
    home = codex_home()
    return [d for d in (home / "sessions", home / "archived_sessions") if d.is_dir()]


def codex_present() -> bool:
    return bool(_codex_dirs())


def _thread_of(path: Path) -> str | None:
    """The thread id in a rollout's file name (a reverted thread's name adds
    _<rollout id> after it)."""
    m = _CODEX_ROLLOUT.match(path.name)
    return m.group(1).split("_", 1)[0] if m else None


def _codex_files() -> list[Path]:
    files: list[Path] = []
    for d in _codex_dirs():
        try:
            files.extend(p for p in d.rglob("rollout-*.jsonl") if _CODEX_ROLLOUT.match(p.name))
        except OSError:
            continue
    # The file name starts with the thread's creation time, so a parent is read
    # before the forks that copied its events.
    return sorted(files, key=lambda p: p.name)


def _compressed_in_window(since_epoch: float) -> int:
    n = 0
    for d in _codex_dirs():
        try:
            for p in d.rglob("rollout-*.jsonl.zst"):
                try:
                    if p.stat().st_mtime >= since_epoch - 1:
                        n += 1
                except OSError:
                    continue
        except OSError:
            continue
    return n


def _meta(path: Path) -> dict[str, Any] | None:
    """The rollout's own session_meta payload: its first line."""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            first = fh.readline()
        rec = json.loads(first)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("type") != "session_meta":
        return None
    payload = rec.get("payload")
    return payload if isinstance(payload, dict) else None


def _meta_session(meta: dict[str, Any] | None, path: Path) -> str:
    # session_id is the root thread's id, so a subagent's rollout belongs to the
    # session that spawned it, and it is what the PreToolUse hook sends as
    # session_id (core/src/hook_runtime.rs). Older rollouts have only id.
    meta = meta or {}
    return str(meta.get("session_id") or meta.get("id") or _thread_of(path) or path.stem)


def _totals(info: Any) -> tuple[int, ...] | None:
    if not isinstance(info, dict):
        return None
    total = info.get("total_token_usage")
    if not isinstance(total, dict):
        return None
    return tuple(_int(total.get(f)) for f in _USAGE_FIELDS)


def _record(ts: float, model: str, usage: dict[str, int], session: str,
            cwd: Any) -> dict[str, Any] | None:
    cached = usage["cached_input_tokens"]
    written = usage["cache_write_input_tokens"]
    fresh = max(0, usage["input_tokens"] - cached - written)
    out = usage["output_tokens"]
    if fresh == out == cached == written == 0:
        return None
    return {"ts": ts, "model": model or "unknown", "input": fresh, "output": out,
            "cache_write": written, "cache_write_1h": 0, "cache_read": cached,
            "fast": False, "us_only": False, "session": session,
            "cwd": cwd if isinstance(cwd, str) else None, "harness": HARNESS_CODEX}


def _read_totals(path: Path) -> set[tuple[int, ...]]:
    """Every running total a rollout's token_count events carry."""
    seen: set[tuple[int, ...]] = set()
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"token_count"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                payload = rec.get("payload") if isinstance(rec, dict) else None
                if isinstance(payload, dict) and payload.get("type") == "token_count":
                    t = _totals(payload.get("info"))
                    if t and any(t):
                        seen.add(t)
    except OSError:
        pass
    return seen


def _codex_file(path: Path, since_epoch: float, meta: dict[str, Any] | None,
                seen: set[tuple[int, ...]], responses: dict[Any, dict[str, Any]]) -> None:
    """Add one rollout's responses in the window to `responses`, keyed so a
    response is counted once however many files or lines carry it."""
    session = _meta_session(meta, path)
    cwd = (meta or {}).get("cwd")
    model = "unknown"
    model_by_turn: dict[str, str] = {}
    prev: tuple[int, ...] | None = None
    has_records = False
    own_totals: set[tuple[int, ...]] = set()
    try:
        fh = path.open("r", encoding="utf-8", errors="ignore")
    except OSError:
        return
    with fh:
        for lineno, line in enumerate(fh):
            if not any(t in line for t in _CODEX_LINE_TYPES):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind, payload = rec.get("type"), rec.get("payload")
            if not isinstance(payload, dict):
                continue
            if kind == "turn_context":
                if isinstance(payload.get("model"), str) and payload["model"]:
                    model = payload["model"]
                    if payload.get("turn_id"):
                        model_by_turn[str(payload["turn_id"])] = model
                if isinstance(payload.get("cwd"), str):
                    cwd = payload["cwd"]
                continue
            if kind == "token_usage_record":
                has_records = True
                ts = _epoch(rec.get("timestamp"))
                usage = payload.get("usage")
                if ts is None or ts < since_epoch or not isinstance(usage, dict):
                    continue
                u = {f: _int(usage.get(f)) for f in _USAGE_FIELDS}
                r = _record(ts, model_by_turn.get(str(payload.get("turn_id")), model), u,
                            str(payload.get("session_id") or session), cwd)
                rid = payload.get("response_id")
                if r:
                    responses[(HARNESS_CODEX, rid) if rid else (str(path), lineno)] = r
                continue
            if kind != "event_msg" or payload.get("type") != "token_count":
                continue
            cur = _totals(payload.get("info"))
            if cur is None:
                continue                  # a rate-limit-only update
            before = prev
            prev = cur
            if cur in own_totals:
                continue                  # re-sent unchanged (rate limits)
            own_totals.add(cur)
            if not any(cur) or cur in seen or has_records:
                # A copy of another rollout's total (a fork's inherited prefix)
                # or a file whose records already count each response.
                continue
            if before is None or any(c < p for c, p in zip(cur, before)):
                before = (0,) * len(cur)  # the first total, or a reset
            delta = dict(zip(_USAGE_FIELDS, (c - p for c, p in zip(cur, before))))
            ts = _epoch(rec.get("timestamp"))
            if ts is None or ts < since_epoch:
                continue
            r = _record(ts, model, delta, session, cwd)
            if r:
                responses[(str(path), lineno)] = r
    seen.update(own_totals)


def codex_responses(since_epoch: float, session_id: str | None = None) -> list[dict[str, Any]]:
    """Codex CLI responses since `since_epoch`, one record each. With a
    session_id, only that session's (the root thread and every thread it
    spawned)."""
    files = _codex_files()
    by_thread = {t: p for p in files if (t := _thread_of(p))}
    seen: set[tuple[int, ...]] = set()
    responses: dict[Any, dict[str, Any]] = {}
    floor = since_epoch
    if session_id:
        root = by_thread.get(session_id)
        if root is None:
            return []
        # A session's threads are all created after its root, so nothing
        # written before the root's first line can hold any of them.
        root_meta = _meta(root)
        start = _epoch((root_meta or {}).get("timestamp"))
        floor = max(since_epoch, (start or 0) - 60)
    for path in files:
        try:
            if path.stat().st_mtime < floor - 1:
                continue                  # whole file is older than the window
        except OSError:
            continue
        meta = _meta(path)
        if session_id and _meta_session(meta, path) != session_id:
            continue
        parent = (meta or {}).get("forked_from_id")
        if parent and str(parent) in by_thread:
            seen |= _read_totals(by_thread[str(parent)])
        _codex_file(path, since_epoch, meta, seen, responses)
    out = list(responses.values())
    if session_id:
        out = [r for r in out if r["session"] == session_id]
    return out


def codex_latest_session() -> tuple[float, str] | None:
    """(mtime, session id) of the most recently written rollout."""
    latest: tuple[float, Path] | None = None
    for path in _codex_files():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if latest is None or mtime > latest[0]:
            latest = (mtime, path)
    if latest is None:
        return None
    return latest[0], _meta_session(_meta(latest[1]), latest[1])


def codex_compressed_skipped(since_epoch: float) -> int:
    return _compressed_in_window(since_epoch)


# ── Cursor ────────────────────────────────────────────────────────────────────
#
# Cursor has no local usage log to read. Its token usage lives server-side, and
# the documented way to read it is the team Admin API:
#
#   https://cursor.com/docs/account/teams/admin-api  "Get Usage Events Data"
#   POST https://api.cursor.com/teams/filtered-usage-events
#   Basic auth, the API key as the user name and an empty password.
#   Body: startDate / endDate (epoch ms, at most 30 days apart), email (filter
#   to one user), page, pageSize (default 100, max 1000).
#   Response: usageEvents[] with timestamp (epoch ms, a string), model,
#   userEmail, conversationId (the agent session; omitted when there is none),
#   isTokenBasedCall, and tokenUsage {inputTokens, outputTokens,
#   cacheWriteTokens, cacheReadTokens, totalCents}; pagination {hasNextPage}.
#   Rate limited to 60 requests a minute per team; data is aggregated hourly.
#
# cursor.com is not reachable from where this was written, so the shape above was
# read through the search index of that page, not the page itself. Re-read it on
# the next change. Only the fields listed are used, and each is optional.
#
# Off unless CURSOR_ADMIN_API_KEY is set: with it unset nothing is requested. It
# is a team key, so CURSOR_ADMIN_USER_EMAIL narrows it to one person's usage;
# without that the whole team's usage counts, and status says so. Results are
# cached for an hour in the data dir. The guard, which asks on every tool call,
# reads only that cache and never the network (allow_network=False): a fetch
# there put up to one 5 s timeout per page on a tool call. A failed read is
# written down too, and the next attempt waits _CURSOR_RETRY_AFTER rather than
# retrying on every call.

CURSOR_API = "https://api.cursor.com/teams/filtered-usage-events"
_CURSOR_TTL = 3600
_CURSOR_RETRY_AFTER = 600
_CURSOR_PAGE = 1000
_CURSOR_MAX_PAGES = 20
_CURSOR_SPAN_MS = 30 * 86400 * 1000
_CURSOR_TIMEOUT = 5.0


def cursor_enabled() -> bool:
    return bool(os.getenv("CURSOR_ADMIN_API_KEY", "").strip())


def cursor_email() -> str:
    return os.getenv("CURSOR_ADMIN_USER_EMAIL", "").strip()


def _cursor_cache_path() -> Path:
    d = Path(os.getenv("FINOPS_DATA_DIR") or (Path.home() / ".nable"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "cursor-usage.json"


def write_json_atomic(path: Path, data: Any) -> None:
    """Write `data` as JSON to `path`, owner-only, all or nothing: a reader
    never sees half a file, and a failed write leaves the old one."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _cursor_post(body: dict[str, Any]) -> dict[str, Any]:
    """One Admin API call. Separate so tests replace it and never reach the network."""
    import httpx

    resp = httpx.post(CURSOR_API, json=body, timeout=_CURSOR_TIMEOUT,
                      auth=(os.getenv("CURSOR_ADMIN_API_KEY", "").strip(), ""))
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _cursor_event(ev: Any) -> dict[str, Any] | None:
    if not isinstance(ev, dict):
        return None
    tu = ev.get("tokenUsage")
    if not isinstance(tu, dict):
        return None               # a request-priced call carries no tokens to count
    try:
        ts = int(str(ev.get("timestamp"))) / 1000
    except (TypeError, ValueError):
        return None
    r = {"ts": ts, "model": str(ev.get("model") or "unknown"),
         "input": _int(tu.get("inputTokens")), "output": _int(tu.get("outputTokens")),
         "cache_write": _int(tu.get("cacheWriteTokens")), "cache_write_1h": 0,
         "cache_read": _int(tu.get("cacheReadTokens")),
         "fast": False, "us_only": False,
         "session": str(ev.get("conversationId") or "cursor"), "cwd": None,
         "harness": HARNESS_CURSOR}
    cents = tu.get("totalCents")
    if isinstance(cents, (int, float)) and not isinstance(cents, bool):
        # Cursor's own figure for what the request cost at the model's rate.
        r["usd"] = float(cents) / 100
    if r["input"] == r["output"] == r["cache_write"] == r["cache_read"] == 0 and "usd" not in r:
        return None
    return r


def _cursor_fetch(start_ms: int, end_ms: int) -> tuple[list[dict[str, Any]], bool]:
    """(events, truncated). truncated: some 30-day span still had pages left
    after _CURSOR_MAX_PAGES, so the events undercount it."""
    events: list[dict[str, Any]] = []
    truncated = False
    email = cursor_email()
    lo = start_ms
    while lo < end_ms:
        hi = min(end_ms, lo + _CURSOR_SPAN_MS)
        for page in range(1, _CURSOR_MAX_PAGES + 1):
            body: dict[str, Any] = {"startDate": lo, "endDate": hi, "page": page,
                                    "pageSize": _CURSOR_PAGE}
            if email:
                body["email"] = email
            data = _cursor_post(body)
            batch = data.get("usageEvents")
            for ev in batch if isinstance(batch, list) else []:
                r = _cursor_event(ev)
                if r:
                    events.append(r)
            pag = data.get("pagination")
            if not (isinstance(pag, dict) and pag.get("hasNextPage")):
                break
        else:
            truncated = True
        lo = hi
    return events, truncated


_cursor_status: dict[str, Any] = {}

# What cursor_status() says when the guard found no Cursor read to use.
CURSOR_NOT_READ_BY_GATE = ("the guard reads Cursor usage only from the last Admin API "
                           "read, and there is none yet; `nable ai-budget` reads it")


def cursor_status() -> dict[str, Any]:
    """How the last Cursor read went: enabled, scope, fetched_at, and when it
    applies: error, not_read (why no Cursor usage is counted), stale (an old
    read served without refreshing), retry_at (a failed read's backoff), and
    truncated (the Admin API had more pages than were read: a lower bound)."""
    return dict(_cursor_status)


def _cursor_write(cache: dict[str, Any]) -> None:
    try:
        write_json_atomic(_cursor_cache_path(), cache)
    except OSError:
        pass


def cursor_responses(since_epoch: float, session_id: str | None = None,
                     month_start: float | None = None, *,
                     allow_network: bool = True) -> list[dict[str, Any]]:
    """Cursor responses since `since_epoch` from the Admin API, or none when no
    key is set. With a session_id, only that conversation's.

    allow_network=False (the guard) reads only the cache: an old read is used
    as it is, and with none cursor_status() says Cursor was not read."""
    _cursor_status.clear()
    if not cursor_enabled():
        return []
    email = cursor_email()
    now = time.time()
    # One read serves the window, the month and a session: from a day before
    # the month's start, so a window that crosses into the month is covered too.
    # Never further back: every 30 days is another request, and a session's
    # usage before this month is not what a budget measures.
    start = (month_start if month_start is not None else since_epoch) - 86400
    start_ms = int(start * 1000)
    _cursor_status.update({"enabled": True, "scope": email or "team"})
    cache: dict[str, Any] = {}
    try:
        cache = json.loads(_cursor_cache_path().read_text())
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    usable = cache.get("email") == email and isinstance(cache.get("events"), list)
    fresh = (usable
             and isinstance(cache.get("start_ms"), int) and cache["start_ms"] <= start_ms
             and isinstance(cache.get("fetched_at"), (int, float))
             and now - cache["fetched_at"] < _CURSOR_TTL)
    failed_at = cache.get("failed_at") if cache.get("email") == email else None
    backing_off = (isinstance(failed_at, (int, float))
                   and now - failed_at < _CURSOR_RETRY_AFTER)
    if not fresh:
        if not allow_network or backing_off:
            if backing_off:
                _cursor_status["error"] = str(cache.get("error") or "the last read failed")
                _cursor_status["retry_at"] = failed_at + _CURSOR_RETRY_AFTER
            if not usable:
                _cursor_status["not_read"] = (_cursor_status.get("error")
                                              or CURSOR_NOT_READ_BY_GATE)
                return []
            _cursor_status["stale"] = True
        else:
            try:
                events, truncated = _cursor_fetch(start_ms, int(now * 1000))
                cache = {"email": email, "start_ms": start_ms, "fetched_at": now,
                         "events": events, "truncated": truncated}
                _cursor_write(cache)
            except Exception as e:  # noqa: BLE001 - network, auth, a changed response: never fatal
                err = f"{type(e).__name__}: {e}"[:200]
                _cursor_status["error"] = err
                _cursor_status["retry_at"] = now + _CURSOR_RETRY_AFTER
                # Written down so the next call backs off instead of retrying;
                # the last good read's events (this scope's only) are kept.
                kept = cache if cache.get("email") == email else {}
                _cursor_write({**kept, "email": email, "failed_at": now, "error": err})
                if not usable:
                    _cursor_status["not_read"] = err
                    return []
                # Keep what the last good read had; stale beats nothing.
                _cursor_status["stale"] = True
    _cursor_status["fetched_at"] = cache.get("fetched_at")
    if cache.get("truncated"):
        _cursor_status["truncated"] = True
    out = [r for r in cache["events"] if isinstance(r, dict)
           and isinstance(r.get("ts"), (int, float)) and r["ts"] >= since_epoch]
    if session_id:
        out = [r for r in out if r.get("session") == session_id]
    return out
