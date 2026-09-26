"""Refresh a file the guard reads, in a detached process, so the hook never waits.

The guard hook answers every agent tool call in about 100 ms, so it reads only
what is already on disk. Two of those files go stale unless something else
rewrites them:

  cursor  harness_usage's Cursor Admin API cache, good for an hour. A Cursor-only
          user who never runs `nable ai-budget` would otherwise never have one,
          and the guard would count no Cursor usage at all. On whenever
          CURSOR_ADMIN_API_KEY is set.
  budget  budget/summary.py's cloud spend summary, trusted for 48 hours. Opt-in:
          FINOPS_GUARD_AUTO_REFRESH_BUDGET=1 (see _refresh_budget for why it is
          off by default).

When the guard finds one missing or past its age it calls maybe_start(), which
starts `python -m finops.background_refresh <job>` detached and returns at once.
The hook answers from what is on disk and says a refresh is under way; the
child does one refresh and exits.

  single flight  an O_EXCL lock file beside the job's file: parallel hooks start
                 at most one refresh. The child removes it when it is done. A
                 lock older than LOCK_STALE_AFTER belongs to a child that died,
                 and is taken over.
  backoff        a refresh that failed is not started again before its retry
                 time: the Cursor cache's own failed_at (harness_usage), or this
                 module's state file for the budget.
  kill switch    FINOPS_GUARD_BACKGROUND_REFRESH=0 turns every job off. Under
                 pytest (PYTEST_CURRENT_TEST set) they are off unless it is 1.

Stdlib only on the hook's side: subprocess is imported when a child is started,
and a job's own modules only in the child.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

CURSOR = "cursor"
BUDGET = "budget"
JOBS = (CURSOR, BUDGET)

# maybe_start's answers other than None (not started, and none running).
STARTED = "started"
RUNNING = "running"

LOCK_STALE_AFTER = 300
# How long a failed budget refresh waits before the next is started. The
# Cursor job keeps its own (harness_usage._CURSOR_RETRY_AFTER, the same 10 min).
RETRY_AFTER = 600
# How long the budget job waits after finding no cost data newer than the
# summary's: nothing will change until the cost history is synced.
NOTHING_NEW_WAIT = 3600

_ON = ("1", "true", "yes", "on")
_OFF = ("0", "false", "no", "off")


def enabled() -> bool:
    """Whether the guard may start background refreshes at all."""
    raw = os.getenv("FINOPS_GUARD_BACKGROUND_REFRESH", "").strip().lower()
    if raw in _OFF:
        return False
    # A test suite must never start a real process by accident; a test that
    # means to sets the variable to 1 (and stubs _spawn).
    return not os.getenv("PYTEST_CURRENT_TEST") or raw in _ON


def budget_auto_enabled() -> bool:
    """FINOPS_GUARD_AUTO_REFRESH_BUDGET=1, and background refresh not switched off."""
    raw = os.getenv("FINOPS_GUARD_AUTO_REFRESH_BUDGET", "").strip().lower()
    return raw in _ON and enabled()


def _job_dir(job: str) -> Path:
    """Where the job's file lives: its lock and state go beside it."""
    if job == CURSOR:
        from . import harness_usage
        return harness_usage._cursor_cache_path().parent
    if job == BUDGET:
        from .budget.summary import summary_path
        return summary_path().parent
    raise ValueError(f"unknown refresh job {job!r}")


def _lock_path(job: str) -> Path:
    return _job_dir(job) / f".refresh-{job}.lock"


def _state_path(job: str) -> Path:
    return _job_dir(job) / f"refresh-{job}.json"


def read_state(job: str) -> dict[str, Any]:
    """The job's last recorded outcome: {"at", "retry_at", "error" or "note"},
    or {} when its last run succeeded or none has run."""
    try:
        doc = json.loads(_state_path(job).read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _backing_off(job: str, now: float) -> bool:
    retry_at = read_state(job).get("retry_at")
    return isinstance(retry_at, (int, float)) and now < retry_at


def running(job: str, now: float | None = None) -> bool:
    """Whether a refresh holds the job's lock (and is not a dead one)."""
    try:
        age = (now or time.time()) - _lock_path(job).stat().st_mtime
    except (OSError, ValueError):
        return False
    return age < LOCK_STALE_AFTER


def _take_lock(lock: Path) -> bool:
    """Create the lock, or False when a live refresh holds it."""
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue                  # released in between: try again
            if age < LOCK_STALE_AFTER:
                return False
            # Left by a child that died. Two hooks taking it over at the same
            # moment can each start one refresh; both write atomically, so the
            # cost is a duplicate read, once, after a crash.
            with contextlib.suppress(FileNotFoundError):
                lock.unlink()
            continue
        with os.fdopen(fd, "w") as fh:
            json.dump({"pid": os.getpid(), "at": time.time()}, fh)
        return True
    return False


def _spawn(job: str, cwd: Path) -> None:
    """Start the child, detached from the hook: its own session (process
    group on Windows), no inherited handles, nothing to wait for. Separate so
    tests replace it and never start a process."""
    import subprocess

    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL, "close_fds": True,
                              # Not the agent's working directory: `python -m`
                              # puts the cwd first on sys.path.
                              "cwd": str(cwd)}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "finops.background_refresh", job],
                     **kwargs)


def maybe_start(job: str) -> str | None:
    """Start the job's refresh in the background unless one is running, the
    last one failed recently, or background refresh is off. The caller has
    already decided its file needs one.

    STARTED: this call started it. RUNNING: another one is under way. None:
    nothing is refreshing. Never raises and never waits."""
    if not enabled():
        return None
    try:
        now = time.time()
        if _backing_off(job, now):
            return None
        lock = _lock_path(job)
        if not _take_lock(lock):
            return RUNNING
        try:
            _spawn(job, lock.parent)
        except Exception:  # noqa: BLE001 - no process, no lock: the next call tries again
            with contextlib.suppress(OSError):
                lock.unlink()
            return None
        return STARTED
    except Exception:  # noqa: BLE001 - a refresh is a nicety, never a hook failure
        return None


def status(*, start: bool = False) -> dict[str, Any]:
    """For `nable guard doctor`: whether background refresh is on, how old the
    Cursor cache is, and the budget job's setting and last outcome.

    start: also start the Cursor refresh when its cache is old or missing and
    no failed read is backing off, as the guard would (never waits)."""
    from . import harness_usage
    now = time.time()
    cursor = harness_usage.cursor_cache_info(now)
    if cursor["enabled"]:
        if start and cursor["stale"] and "retry_at" not in cursor:
            maybe_start(CURSOR)
        cursor["refreshing"] = running(CURSOR, now)
    raw = os.getenv("FINOPS_GUARD_AUTO_REFRESH_BUDGET", "").strip().lower()
    budget: dict[str, Any] = {"auto": budget_auto_enabled(), "requested": raw in _ON,
                              "refreshing": running(BUDGET, now)}
    last = read_state(BUDGET)
    if isinstance(last.get("retry_at"), (int, float)) and now < last["retry_at"]:
        budget.update({k: last[k] for k in ("error", "note", "retry_at") if k in last})
    switch = os.getenv("FINOPS_GUARD_BACKGROUND_REFRESH", "").strip().lower()
    return {"enabled": enabled(), "switched_off": switch in _OFF, "cursor": cursor,
            "budget": budget}


# ── The child ─────────────────────────────────────────────────────────────────

def _refresh_cursor() -> dict[str, Any] | None:
    """One Cursor Admin API read over the span the guard asks for, through
    harness_usage's own read and cache write. A failure is written down in
    that cache (failed_at), which is the backoff the guard checks."""
    from . import ai_budget, harness_usage
    if harness_usage.cursor_enabled():
        month = ai_budget._month_start_epoch()
        harness_usage.cursor_responses(month, month_start=month, allow_network=True)
    return None


def _refresh_budget() -> dict[str, Any] | None:
    """`nable budget refresh`: recompute every budget from the local cost
    history and rewrite the summary the guard reads.

    Off by default (FINOPS_GUARD_AUTO_REFRESH_BUDGET=1 turns it on). It reads
    the cost history already on this machine and never syncs it: it runs as
    unattended work (billing_access.unattended_context), where a billed Cost
    Explorer request is refused. So it only helps where something else keeps
    that history current, and it costs a process that loads the database on
    the first priced change after the summary goes stale.

    It rewrites the summary only when the history has a day newer than the
    summary's spend_through (or there is no summary). Restamping the same old
    spend as current would let the guard trust a figure it rightly refused as
    stale."""
    from .billing_access import unattended_context
    from .budget import enforcer
    from .budget.summary import read_summary
    with unattended_context():
        old = read_summary()
        through = old.get("spend_through") if old else None
        if old is not None and through:
            from .storage.db import get_engine
            with get_engine().connect() as conn:
                newest = enforcer._spend_through(conn)
            if not newest or str(newest) <= str(through):
                return {"note": f"no cost data newer than {through} on this machine",
                        "wait": NOTHING_NEW_WAIT}
        got = enforcer.refresh_summary()
    if not got.get("ok"):
        return {"error": str(got.get("error") or "the refresh failed")[:200]}
    return None


_RUNNERS = {CURSOR: _refresh_cursor, BUDGET: _refresh_budget}


def run(job: str) -> int:
    """Do the job's refresh once, record how it went, release the lock. The
    exit status is 0 whatever happened: nobody reads it."""
    now = time.time()
    try:
        outcome = _RUNNERS[job]()
    except Exception as e:  # noqa: BLE001 - recorded, and backed off
        outcome = {"error": f"{type(e).__name__}: {e}"[:200]}
    try:
        path = _state_path(job)
        if outcome:
            wait = outcome.pop("wait", RETRY_AFTER)
            from .harness_usage import write_json_atomic
            write_json_atomic(path, {**outcome, "at": now, "retry_at": now + wait})
        else:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
    except (OSError, ValueError):
        pass
    finally:
        # Released after the state is written, so a hook that finds no lock
        # also finds the backoff.
        with contextlib.suppress(OSError, ValueError):
            _lock_path(job).unlink()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in JOBS:
        print(f"usage: python -m finops.background_refresh {{{','.join(JOBS)}}}",
              file=sys.stderr)
        return 2
    return run(args[0])


if __name__ == "__main__":
    sys.exit(main())
