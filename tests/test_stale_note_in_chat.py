"""Stale-build nudge must reach the human IN CHAT, not just stderr.

PostHog (2026-07-09) showed ~97% of weekly machines running pre-fix builds. The
staleness self-check existed but only logged to stderr, which no human sees
inside an editor, so the rescue channel was silent. These lock in that the note
now rides a tool response, exactly once per session, and only when a newer build
exists.
"""
from __future__ import annotations

import asyncio

import pytest

from finops import server


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _demo(monkeypatch):
    monkeypatch.setenv("FINOPS_DEMO_MODE", "1")
    # Reset the per-session flags each test.
    server._stale_note = None
    server._stale_note_shown = False
    yield
    server._stale_note = None
    server._stale_note_shown = False


def test_stale_note_surfaces_in_tool_result():
    server._stale_note = "nable 0.9.0 is out (you are on 0.8.0); run: finops upgrade"
    r = _run(server.get_cost_summary())
    assert "_update" in r
    assert "0.9.0" in r["_update"]
    # It tells an editor user the crucial second step.
    assert "restart" in r["_update"].lower()


def test_stale_note_is_once_per_session():
    server._stale_note = "nable 0.9.0 is out; run: finops upgrade"
    first = _run(server.get_cost_summary())
    second = _run(server.get_cost_summary())
    assert "_update" in first
    assert "_update" not in second


def test_no_note_when_current():
    # No stashed note (up to date) -> nothing injected.
    server._stale_note = None
    r = _run(server.get_cost_summary())
    assert "_update" not in r
