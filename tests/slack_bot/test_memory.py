"""Tests for thread conversation memory: window trim, TTL expiry, DM keys."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from finops.slack_bot import memory


def test_round_trip(tmp_db):
    assert memory.load_history("C1", "111.0") == []
    memory.save_turn("C1", "111.0", "what did we spend?", "$12,946 last 30 days")
    history = memory.load_history("C1", "111.0")
    assert history == [
        {"role": "user", "content": "what did we spend?"},
        {"role": "assistant", "content": "$12,946 last 30 days"},
    ]


def test_threads_are_isolated(tmp_db):
    memory.save_turn("C1", "111.0", "q1", "a1")
    memory.save_turn("C1", "222.0", "q2", "a2")
    assert memory.load_history("C1", "111.0")[0]["content"] == "q1"
    assert memory.load_history("C1", "222.0")[0]["content"] == "q2"


def test_dm_key_uses_channel(tmp_db):
    memory.save_turn("D42", None, "hello", "hi")
    assert memory.load_history("D42", None) != []
    assert memory.thread_key("D42", None) == "D42"
    assert memory.thread_key("C1", "9.9") == "C1:9.9"


def test_window_trims_to_max_turns(tmp_db):
    for i in range(memory.MAX_TURNS):  # 2 messages per turn, far exceeds window
        memory.save_turn("C1", "t", f"q{i}", f"a{i}")
    history = memory.load_history("C1", "t")
    assert len(history) == memory.MAX_TURNS
    # Oldest messages dropped, newest kept
    assert history[-1]["content"] == f"a{memory.MAX_TURNS - 1}"
    assert history[0]["content"] != "q0"


def test_expired_thread_returns_empty(tmp_db):
    from finops.storage.db import get_engine, slack_threads

    stale = datetime.utcnow() - timedelta(hours=memory.THREAD_TTL_HOURS + 1)
    with get_engine().begin() as conn:
        conn.execute(
            slack_threads.insert().values(
                thread_key="C9:old",
                channel="C9",
                messages=json.dumps([{"role": "user", "content": "ancient"}]),
                updated_at=stale,
            )
        )
    assert memory.load_history("C9", "old") == []

    # A write elsewhere prunes the stale row entirely
    memory.save_turn("C1", "fresh", "q", "a")
    from sqlalchemy import select

    with get_engine().connect() as conn:
        row = conn.execute(
            select(slack_threads).where(slack_threads.c.thread_key == "C9:old")
        ).fetchone()
    assert row is None
