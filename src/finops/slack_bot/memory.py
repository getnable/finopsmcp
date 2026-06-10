"""
Thread conversation memory for the Slack bot.

Each Slack thread (or DM channel) gets a rolling window of text turns so
follow-ups like "what's driving that?" work. Only plain text turns are stored,
never tool_use blocks: tool results can be tens of KB and re-sending them
every turn would burn tokens for no gain. Claude re-fetches data when needed.

Retention:
  - Window: last MAX_TURNS messages per thread.
  - TTL: threads idle longer than THREAD_TTL_HOURS get pruned opportunistically
    on write, so there is no background job to babysit.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import delete, select

log = logging.getLogger(__name__)

MAX_TURNS = 16          # messages kept per thread (8 user/assistant pairs)
THREAD_TTL_HOURS = 48

# Slack Bolt dispatches listeners on a thread pool, so two quick @nable mentions
# in the same thread can run save_turn() concurrently. The read-modify-write of
# the message blob would then lose a turn (second write clobbers the first). The
# bot is a single Socket Mode process, so a per-thread in-process lock fully
# serializes those writes. Keyed by thread_key so unrelated threads never block.
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = _thread_locks[key] = threading.Lock()
        return lock


def thread_key(channel: str, thread_ts: str | None) -> str:
    """Stable key for a conversation. DMs have no thread, so the channel is the key."""
    return f"{channel}:{thread_ts}" if thread_ts else channel


def load_history(channel: str, thread_ts: str | None) -> list[dict]:
    """Return prior text turns for this thread, oldest first. Empty list if none."""
    from ..storage.db import get_engine, slack_threads

    key = thread_key(channel, thread_ts)
    try:
        with get_engine().connect() as conn:
            row = conn.execute(
                select(slack_threads.c.messages, slack_threads.c.updated_at).where(
                    slack_threads.c.thread_key == key
                )
            ).fetchone()
        if not row:
            return []
        if row.updated_at and row.updated_at < datetime.utcnow() - timedelta(hours=THREAD_TTL_HOURS):
            return []
        messages = json.loads(row.messages or "[]")
        return messages if isinstance(messages, list) else []
    except Exception as e:  # noqa: BLE001 — memory is best-effort, never block a reply
        log.warning("Failed to load thread history for %s: %s", key, e)
        return []


def save_turn(channel: str, thread_ts: str | None, user_text: str, assistant_text: str) -> None:
    """Append one user/assistant exchange to the thread, trimming to the window."""
    from ..storage.db import get_engine, slack_threads

    key = thread_key(channel, thread_ts)
    now = datetime.utcnow()
    try:
        # Serialize the whole read-modify-write per thread so a concurrent
        # save_turn on the same thread reads our just-written turn instead of
        # the pre-write blob (which would drop one exchange).
        with _lock_for(key):
            history = load_history(channel, thread_ts)
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": assistant_text})
            history = history[-MAX_TURNS:]
            payload = json.dumps(history, default=str)

            engine = get_engine()
            with engine.begin() as conn:
                updated = conn.execute(
                    slack_threads.update()
                    .where(slack_threads.c.thread_key == key)
                    .values(messages=payload, updated_at=now)
                )
                if updated.rowcount == 0:
                    conn.execute(
                        slack_threads.insert().values(
                            thread_key=key, channel=channel, messages=payload, updated_at=now
                        )
                    )
        _prune_expired()
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to save thread history for %s: %s", key, e)


def _prune_expired() -> None:
    """Drop threads idle past the TTL. Called opportunistically after writes."""
    from ..storage.db import get_engine, slack_threads

    cutoff = datetime.utcnow() - timedelta(hours=THREAD_TTL_HOURS)
    try:
        with get_engine().begin() as conn:
            conn.execute(delete(slack_threads).where(slack_threads.c.updated_at < cutoff))
    except Exception as e:  # noqa: BLE001
        log.debug("Thread prune failed: %s", e)
