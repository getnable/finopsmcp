"""Per-session budgets: "this task may spend at most $40".

A monthly cap tells you after the fact that the month went badly. A session cap
is the one a person can set before a task starts and the agent can read before
it spends, because a Claude Code session is the unit of work: one task, with the
subagents it spawns. These tests hold the three things that makes true: the
session is found and counted whole, the cap is keyed to the session it was set
for, and the headroom is on the MCP tools the agent actually calls.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone

import pytest

from finops import ai_budget as ab

_PROJ = "-Users-x-proj"


def _rec(ts, session, msg_id, tin=0, tout=0, model="claude-opus-5-5", cwd="/Users/x/proj"):
    return {
        "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "sessionId": session, "requestId": f"req_{msg_id}", "cwd": cwd,
        "message": {"id": msg_id, "model": model, "usage": {
            "input_tokens": tin, "output_tokens": tout,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
    }


def _write(claude_dir, session, records, subagent=None, project=_PROJ):
    """Lay files out the way Claude Code does: <project>/<session>.jsonl, and a
    subagent's under <project>/<session>/subagents/."""
    proj = claude_dir / "projects" / project
    f = (proj / session / "subagents" / f"{subagent}.jsonl" if subagent
         else proj / f"{session}.jsonl")
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return f


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    # Claude Code sets this for every process it starts, the test runner included.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    yield


@pytest.fixture
def claude(tmp_path):
    return tmp_path / "claude"


# ── counting a session ───────────────────────────────────────────────────────

def test_the_month_splits_by_session_costliest_first(claude):
    now = time.time()
    # sess-a: 1M in + 1M out on Opus 5.5 = $24. sess-b: the same on Haiku 4.5 = $6.
    _write(claude, "sess-b", [_rec(now - 50, "sess-b", "m2", 1_000_000, 1_000_000,
                                   model="claude-haiku-4-5", cwd="/Users/x/other")])
    _write(claude, "sess-a", [_rec(now - 90, "sess-a", "m1", 1_000_000, 1_000_000)])
    u = ab.read_agent_usage(now - 3600)
    assert list(u["by_session"]) == ["sess-a", "sess-b"]
    a = u["by_session"]["sess-a"]
    assert (a["usd_equivalent"], a["messages"], a["project"]) == (24.0, 1, "proj")
    assert u["by_session"]["sess-b"]["project"] == "other"
    assert u["session_count"] == 2


def test_a_session_counts_its_subagents_and_nothing_else(claude):
    now = time.time()
    _write(claude, "sess-a", [_rec(now - 90, "sess-a", "m1", 1_000_000)])
    _write(claude, "sess-a", [_rec(now - 60, "sess-a", "m2", 0, 1_000_000)],
           subagent="agent-1")
    _write(claude, "sess-b", [_rec(now - 30, "sess-b", "m3", 5_000_000)])
    u = ab.read_session_usage("sess-a")
    assert u["messages"] == 2
    assert u["usd_equivalent"] == 24.0            # $4 input + $20 output, no sess-b


def test_a_session_is_named_for_the_project_it_started_in(claude):
    # A subagent working in a worktree later on is still the same task.
    now = time.time()
    _write(claude, "sess-a", [_rec(now - 90, "sess-a", "m1", 10)])
    _write(claude, "sess-a", [_rec(now - 30, "sess-a", "m2", 10,
                                   cwd="/Users/x/proj/.claude/worktrees/agent-1")],
           subagent="agent-1")
    assert ab.read_agent_usage(now - 3600)["by_session"]["sess-a"]["project"] == "proj"


def test_a_session_is_counted_from_its_start_not_from_the_window(claude):
    # A long task that began before the 5h window, or last month, is one task.
    now = time.time()
    _write(claude, "sess-a", [_rec(now - 40 * 86400, "sess-a", "m1", 1_000_000),
                              _rec(now - 60, "sess-a", "m2", 1_000_000)])
    assert ab.read_session_usage("sess-a")["usd_equivalent"] == 8.0


def test_an_id_unsafe_for_a_glob_matches_nothing(claude):
    now = time.time()
    _write(claude, "sess-a", [_rec(now - 60, "sess-a", "m1", 1_000_000)])
    assert ab.read_session_usage("*")["messages"] == 0
    assert ab.read_session_usage("../sess-a")["messages"] == 0


# ── which session is "this" one ──────────────────────────────────────────────

def test_resolve_session_prefers_argument_then_env_then_latest_activity(claude, monkeypatch):
    now = time.time()
    older = _write(claude, "sess-old", [_rec(now - 600, "sess-old", "m1", 10)])
    _write(claude, "sess-new", [_rec(now - 60, "sess-new", "m2", 10)])
    os.utime(older, (now - 600, now - 600))
    assert ab.resolve_session() == ("sess-new", "latest_activity")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-old")
    assert ab.resolve_session() == ("sess-old", "env")
    assert ab.resolve_session("sess-x") == ("sess-x", "argument")


def test_no_transcripts_means_no_session(claude):
    assert ab.resolve_session() == (None, None)
    assert ab.status()["session"] is None


# ── the cap ──────────────────────────────────────────────────────────────────

def test_this_task_may_spend_at_most_40(claude):
    now = time.time()
    _write(claude, "sess-a", [_rec(now - 90, "sess-a", "m1", 1_000_000, 1_000_000)])  # $24
    ab.set_budget(session_cap=40, session_id="sess-a")

    st = ab.status(session_id="sess-a")
    assert st["verdict"] == ab.BUDGET_OK and st["verdict_basis"] == "session"
    s = st["session"]
    assert (s["usd_equivalent"], s["cap_usd"], s["remaining_usd"]) == (24.0, 40.0, 16.0)
    assert s["cap_scope"] == "this_session"
    assert st["headroom"]["session_usd"] == 16.0

    _write(claude, "sess-a", [_rec(now - 30, "sess-a", "m2", 0, 400_000)])            # +$8 = $32
    st = ab.status(session_id="sess-a")
    assert st["verdict"] == ab.BUDGET_WARN
    assert st["pct_of_budget"] == 0.8

    _write(claude, "sess-a", [_rec(now - 10, "sess-a", "m3", 0, 1_000_000)])          # +$20 = $52
    st = ab.status(session_id="sess-a")
    assert st["verdict"] == ab.BUDGET_OVER
    assert st["headroom"]["session_usd"] == 0.0
    assert "this session's $40.00 cap" in st["summary"]
    g = ab.check(session_id="sess-a")
    assert g["verdict"] == ab.BUDGET_OVER
    assert "past its $40.00 cap" in g["recommendation"]
    assert g["headroom"]["session_usd"] == 0.0


def test_a_cap_set_for_one_session_does_not_follow_you_to_the_next(claude):
    now = time.time()
    _write(claude, "sess-a", [_rec(now - 90, "sess-a", "m1", 0, 1_000_000)])  # $20
    _write(claude, "sess-b", [_rec(now - 60, "sess-b", "m2", 0, 1_000_000)])  # $20
    ab.set_budget(session_cap=10, session_id="sess-a")
    assert ab.status(session_id="sess-a")["verdict"] == ab.BUDGET_OVER
    b = ab.status(session_id="sess-b")
    assert b["verdict"] == ab.BUDGET_OK
    assert b["session"]["cap_usd"] is None and b["headroom"]["session_usd"] is None


def test_a_cap_for_every_session_and_a_per_session_override(claude):
    now = time.time()
    _write(claude, "sess-a", [_rec(now - 90, "sess-a", "m1", 0, 1_000_000)])  # $20
    _write(claude, "sess-b", [_rec(now - 60, "sess-b", "m2", 0, 1_000_000)])  # $20
    ab.set_budget(session_cap=15)                                  # every session
    ab.set_budget(session_cap=100, session_id="sess-a")            # this one gets more
    assert ab.status(session_id="sess-b")["verdict"] == ab.BUDGET_OVER
    assert ab.status(session_id="sess-b")["session"]["cap_scope"] == "every_session"
    assert ab.status(session_id="sess-a")["verdict"] == ab.BUDGET_OK
    ab.set_budget(session_cap=0, session_id="sess-a")              # drop the override
    assert ab.status(session_id="sess-a")["verdict"] == ab.BUDGET_OVER


def test_the_worse_of_the_monthly_and_session_verdicts_wins(claude):
    now = time.time()
    _write(claude, "sess-a", [_rec(now - 90, "sess-a", "m1", 0, 1_000_000)])  # $20, 1M tokens
    ab.set_budget(monthly_tokens=100_000_000, session_cap=10, session_id="sess-a")
    st = ab.status(session_id="sess-a")
    assert (st["verdict"], st["verdict_basis"]) == (ab.BUDGET_OVER, "session")
    assert st["headroom"]["month_tokens"] == 99_000_000

    ab.set_budget(monthly_tokens=500_000, session_cap=1000, session_id="sess-a")
    st = ab.status(session_id="sess-a")
    assert (st["verdict"], st["verdict_basis"]) == (ab.BUDGET_OVER, "tokens")
    assert st["session"]["verdict"] == ab.BUDGET_OK


def test_a_flat_plan_with_no_session_cap_is_unchanged(claude):
    now = time.time()
    _write(claude, "sess-a", [_rec(now - 90, "sess-a", "m1", 1_000_000, 5_000_000)])
    ab.set_budget(mode="flat", plan_cost=20)
    st = ab.status(session_id="sess-a")
    assert (st["verdict"], st["verdict_basis"]) == (ab.BUDGET_OK, "none")
    assert st["session"]["usd_equivalent"] == 104.0   # shown, never gated on


def test_the_saved_list_of_session_caps_stays_bounded(claude):
    for i in range(ab._SESSION_CAPS_KEPT + 5):
        ab.set_budget(session_cap=1, session_id=f"sess-{i}")
    caps = ab.get_budget()["session_caps"]
    assert len(caps) == ab._SESSION_CAPS_KEPT
    assert "sess-0" not in caps and f"sess-{ab._SESSION_CAPS_KEPT + 4}" in caps


# ── `nable ai-budget` ────────────────────────────────────────────────────────

def _cli(capsys, **flags):
    import argparse

    from finops import cli_ai_budget as cli

    ns = {"plan_cost": None, "spend_cap": None, "tokens": None, "session_cap": None,
          "month": False, "reset": False, "json": False}
    ns.update(flags)
    assert cli.run(argparse.Namespace(**ns)) == 0
    return capsys.readouterr().out


def test_the_cli_shows_cost_by_model_and_by_session(claude, capsys):
    now = time.time()
    _write(claude, "sess-aaaa1111", [
        _rec(now - 90, "sess-aaaa1111", "m1", 1_000_000, 1_000_000),                  # $24
        _rec(now - 80, "sess-aaaa1111", "m2", 1_000_000, 1_000_000, model="claude-nova-9"),
    ])
    _write(claude, "sess-bbbb2222", [_rec(now - 60, "sess-bbbb2222", "m3", 1_000_000, 1_000_000,
                                          model="claude-haiku-4-5", cwd="/Users/x/other")])
    out = _cli(capsys)
    model_part = out.split("by model, last 5h")[1].split("by session")[0]
    lines = [ln.split() for ln in model_part.strip().splitlines()]
    assert lines[0][:3] == ["claude-opus-5-5", "~$24.00", "50%"]
    assert lines[1][:2] == ["claude-nova-9", "~$18.00"]
    assert "unpriced, at the fallback rate" in model_part.splitlines()[2]
    assert lines[2][:2] == ["claude-haiku-4-5", "~$6.00"]

    session_part = out.split("by session, last 5h")[1]
    rows = [ln.split() for ln in session_part.strip().splitlines()[:2]]
    assert rows[0][:3] == ["~$42.00", "proj", "sess-aaa"]
    assert rows[1][:3] == ["~$6.00", "other", "sess-bbb"]


def test_the_cli_session_cap_flag_and_this_sessions_row(claude, capsys, monkeypatch):
    now = time.time()
    _write(claude, "sess-a", [_rec(now - 90, "sess-a", "m1", 1_000_000, 1_000_000)])  # $24
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-a")
    out = _cli(capsys, session_cap=40)
    assert ab.get_budget()["session_cap"] == 40.0
    line = next(ln for ln in out.splitlines() if "this session" in ln and "cap" in ln)
    assert "~$24.00 of $40.00 cap" in line and "OK (60%)" in line and "~$16.00 left" in line
    assert "(this session)" in out.split("by session")[1]


def test_the_cli_month_flag_splits_the_month(claude, capsys, monkeypatch):
    now = time.time()
    monkeypatch.setattr(ab, "_month_start_epoch", lambda: now - 30 * 86400)
    _write(claude, "sess-a", [_rec(now - 3 * 86400, "sess-a", "m1", 1_000_000)])   # outside 5h
    assert "by model" not in _cli(capsys)
    out = _cli(capsys, month=True)
    assert "by model, month to date" in out and "~$4.00" in out


# ── the MCP tools the agent calls ────────────────────────────────────────────

def test_the_agent_sets_and_reads_its_own_session_cap(claude, monkeypatch):
    from finops import server as srv

    now = time.time()
    _write(claude, "sess-a", [_rec(now - 90, "sess-a", "m1", 1_000_000, 1_000_000)])  # $24
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-a")

    out = asyncio.run(srv.set_ai_budget(session_cap=40))
    assert out["session_cap_applies_to"] == {"session_id": "sess-a", "id_source": "env"}

    chk = asyncio.run(srv.check_ai_budget())
    assert chk["verdict"] == ab.BUDGET_OK
    assert chk["headroom"]["session_usd"] == 16.0
    assert chk["session"]["id"] == "sess-a"
    assert "$16.00 of this session's $40.00 cap left" in chk["recommendation"]

    st = asyncio.run(srv.get_ai_budget_status())
    assert st["session"]["remaining_usd"] == 16.0
    assert st["month_to_date"]["cost_by_model"] == {"claude-opus-5-5": 24.0}


def test_every_session_from_the_tool(claude):
    from finops import server as srv

    out = asyncio.run(srv.set_ai_budget(session_cap=25, every_session=True))
    assert out["session_cap_applies_to"] == "every_session"
    assert ab.get_budget()["session_cap"] == 25.0


# ── a guessed session is never presented as a known one ──────────────────────

def test_a_guessed_session_is_named_as_a_guess(claude):
    """No id passed and none in the environment: "this session" is the one whose
    transcript was written last. That is a guess, and the text says so."""
    now = time.time()
    _write(claude, "sess-a", [_rec(now - 90, "sess-a", "m1", 1_000_000, 1_000_000)])  # $24
    ab.set_budget(session_cap=20)
    st = ab.status()
    assert st["session"]["id_source"] == "latest_activity"
    assert "session guessed from the most recently active transcript" in st["summary"]
    assert "this session" not in st["summary"]
    chk = ab.check()
    assert "session guessed from the most recently active transcript" in chk["reason"]
    assert "session guessed from the most recently active transcript" in chk["recommendation"]


def test_a_known_session_reads_as_this_session(claude, monkeypatch):
    now = time.time()
    _write(claude, "sess-a", [_rec(now - 90, "sess-a", "m1", 1_000_000, 1_000_000)])
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-a")
    ab.set_budget(session_cap=20)
    chk = ab.check()
    assert "this session's $20.00 cap" in chk["reason"]
    assert "guessed" not in chk["reason"] + chk["recommendation"]


def test_the_tool_will_not_cap_a_guessed_session(claude):
    """Capping "this session" by a guess would cap whichever agent wrote last.
    Refused, and nothing in the call is saved."""
    from finops import server as srv

    now = time.time()
    _write(claude, "sess-a", [_rec(now - 90, "sess-a", "m1", 1_000_000)])
    out = asyncio.run(srv.set_ai_budget(session_cap=40, spend_cap=100))
    assert "error" in out and "session_id" in out["error"]
    b = ab.get_budget()
    assert (b["session_cap"], b["session_caps"], b["spend_cap"]) == (0.0, {}, 0.0)

    out = asyncio.run(srv.set_ai_budget(session_cap=40, session_id="sess-a"))
    assert out["session_cap_applies_to"] == {"session_id": "sess-a", "id_source": "argument"}


def test_the_tool_will_not_cap_every_session_when_it_meant_one(claude):
    """With no transcript at all the call used to fall through to capping every
    session, which is not what was asked."""
    from finops import server as srv

    out = asyncio.run(srv.set_ai_budget(session_cap=40))
    assert "error" in out
    assert ab.get_budget()["session_cap"] == 0.0
