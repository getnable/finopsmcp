"""Regression tests from an audit of the MCP tool surface.

Each test is one reproduced tool call: the input a client sent, and the answer
that was wrong. Calls go through the real MCP dispatch (the in-memory client),
so the instrumentation wrapper, argument validation and output serialisation
are all on the path, exactly as for Claude or any other MCP client.
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _call(name: str, args: dict | None = None):
    """One tool call through the in-memory MCP client. JSON answers come back
    decoded, text answers as text."""
    from mcp.shared.memory import create_connected_server_and_client_session

    from finops import server

    async def run():
        async with create_connected_server_and_client_session(server.mcp._mcp_server) as c:
            res = await c.call_tool(name, args or {})
            text = "".join(getattr(b, "text", "") for b in res.content)
            assert not res.isError, text
            try:
                return json.loads(text)
            except ValueError:
                return text
    return asyncio.run(run())


@pytest.fixture
def db(monkeypatch, tmp_path):
    """A fresh local database and data dir for the tools that keep state."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    yield db_mod
    db_mod._ENGINE = None


# ── check_budget_status: no budget checked is not "all on track" ──────────────

def test_budget_status_with_no_budgets_is_not_all_on_track(db):
    """check_budget_status() on a machine with no budget answered
    "✅ All budgets on track." about zero budgets."""
    out = _call("check_budget_status")

    assert "on track" not in out["alert"], out
    assert out["summary"]["total_budgets"] == 0
    assert out.get("no_budgets") is True
    assert "set_budget" in out["alert"]


def test_budget_status_for_a_name_that_matches_nothing_says_so(db):
    """check_budget_status(budget_name="platform") with only a "data" budget
    answered "✅ All budgets on track." for a budget that does not exist."""
    _call("set_budget", {"name": "data", "limit_usd": 500.0})
    out = _call("check_budget_status", {"budget_name": "platform"})

    assert "on track" not in out.get("alert", ""), out
    assert "platform" in out["error"]
    assert out["budgets_available"] == ["data"]
