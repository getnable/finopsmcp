"""The AI budget counts every agent the guard hooks, not only Claude Code.

Codex CLI writes each session to a rollout under $CODEX_HOME/sessions, and the
fixtures here are laid out the way codex-rs writes them (rollout/src/recorder.rs,
history/src/rollout_payload.rs, protocol/src/protocol.rs): session_meta first,
turn_context per turn, and usage as token_usage_record lines (current Codex) or
event_msg token_count events carrying a running total (every version). Cursor
keeps usage server-side; its Admin API is read only with a key, and never here:
the one function that would reach the network is replaced in every test.
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import UTC, datetime

import pytest

from finops import ai_budget as ab
from finops import guard_adapters, harness_usage

ROOT = "0195cda5-433d-7f9a-9d7b-a9f15b60c2e2"
OTHER = "0195cda5-5555-7f9a-9d7b-a9f15b60c2e2"
CHILD = "0195cda5-7777-7f9a-9d7b-a9f15b60c2e2"


def _iso(ts: float) -> str:
    return (datetime.fromtimestamp(ts, tz=UTC)
            .strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ts * 1000) % 1000:03d}Z")


def _usage(inp=0, cached=0, out=0, reasoning=0, cache_write=0):
    return {"input_tokens": inp, "cached_input_tokens": cached,
            "cache_write_input_tokens": cache_write, "output_tokens": out,
            "reasoning_output_tokens": reasoning, "total_tokens": inp + out}


def _meta(ts, thread, session=None, forked_from=None, cwd="/Users/x/proj"):
    payload = {"id": thread, "timestamp": _iso(ts), "cwd": cwd,
               "originator": "codex_cli_rs", "cli_version": "0.99.0", "source": "cli",
               "model_provider": "openai"}
    if session is not None:
        payload["session_id"] = session
    if forked_from:
        payload["forked_from_id"] = forked_from
    return {"timestamp": _iso(ts), "type": "session_meta", "payload": payload}


def _turn(ts, model, turn_id="turn-1", cwd="/Users/x/proj"):
    return {"timestamp": _iso(ts), "type": "turn_context",
            "payload": {"turn_id": turn_id, "cwd": cwd, "approval_policy": "never",
                        "sandbox_policy": {"type": "danger-full-access"},
                        "model": model, "summary": "auto"}}


def _count(ts, total, last, rate_limits=None):
    return {"timestamp": _iso(ts), "type": "event_msg",
            "payload": {"type": "token_count",
                        "info": {"total_token_usage": total, "last_token_usage": last,
                                 "model_context_window": 272000},
                        "rate_limits": rate_limits}}


def _rate_limits_only(ts):
    # rollout/src/tests.rs writes exactly this shape: info null.
    return {"timestamp": _iso(ts), "type": "event_msg",
            "payload": {"type": "token_count", "info": None,
                        "rate_limits": {"limit_id": None, "limit_name": None,
                                        "primary": {"used_percent": 0.0, "window_minutes": 60,
                                                    "resets_at": 1800000000},
                                        "secondary": None, "credits": None,
                                        "individual_limit": None,
                                        "spend_control_reached": None, "plan_type": None,
                                        "rate_limit_reached_type": None}}}


def _record(ts, response_id, usage, session=ROOT, thread=ROOT, turn_id="turn-1"):
    return {"timestamp": _iso(ts), "type": "token_usage_record",
            "payload": {"thread_id": thread, "turn_id": turn_id, "session_id": session,
                        "root_turn_id": turn_id, "response_id": response_id,
                        "usage": usage, "turn_token_usage": usage,
                        "thread_token_usage": usage}}


def _rollout(codex_home, thread, lines, created="2026-09-24T10-00-00"):
    day = created[:10].replace("-", "/")
    f = codex_home / "sessions" / day / f"rollout-{created}-{thread}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")
    return f


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("FINOPS_GUARD_STOP_ON_BUDGET", raising=False)

    def no_network(body):
        raise AssertionError("the Cursor Admin API was called")

    monkeypatch.setattr(harness_usage, "_cursor_post", no_network)
    yield


@pytest.fixture
def codex(tmp_path):
    return tmp_path / "codex"


# ── Codex: counting each response once ───────────────────────────────────────

def test_a_legacy_rollout_is_counted_by_its_running_total(codex):
    """token_count carries total_token_usage (the thread's running total) and
    last_token_usage (the latest response). Codex re-sends the same event when
    rate limits update, so summing last_token_usage counts a response twice."""
    now = time.time()
    r1 = _usage(inp=1_000_000, out=100_000)
    r2 = _usage(inp=1_000_000, cached=800_000, out=100_000)
    t1 = r1
    t2 = _usage(inp=2_000_000, cached=800_000, out=200_000)
    _rollout(codex, ROOT, [
        _meta(now - 300, ROOT, session=ROOT),
        _turn(now - 290, "gpt-4o"),
        _rate_limits_only(now - 285),
        _count(now - 280, t1, r1),
        _count(now - 200, t2, r2),
        _count(now - 190, t2, r2),        # rate limits changed; same usage re-sent
        _count(now - 180, t2, r2),
    ])
    u = ab.read_agent_usage(now - 3600)
    # gpt-4o: fresh input 1.2M x $2.50 + cached 0.8M x $1.25 + output 0.2M x $10.
    assert u["usd_equivalent"] == 6.0
    assert u["messages"] == 2
    assert (u["input_tokens"], u["cache_read_tokens"], u["output_tokens"]) == (
        1_200_000, 800_000, 200_000)
    assert u["cost_by_model"] == {"gpt-4o": 6.0}
    assert u["by_session"][ROOT]["harness"] == "codex"
    assert u["by_session"][ROOT]["project"] == "proj"


def test_records_count_each_response_once_and_the_token_counts_are_not_added(codex):
    """Current Codex writes a token_usage_record per response AND the token_count
    events. Both describe the same responses; the records are the ones counted,
    and a compaction's copy of the latest record is not a response."""
    now = time.time()
    r1 = _usage(inp=1_000_000, out=100_000)
    r2 = _usage(inp=1_000_000, out=100_000)
    _rollout(codex, ROOT, [
        _meta(now - 300, ROOT, session=ROOT),
        _turn(now - 290, "o3"),
        _record(now - 280, "resp-1", r1),
        _count(now - 279, r1, r1),
        _record(now - 200, "resp-2", r2),
        _count(now - 199, _usage(inp=2_000_000, out=200_000), r2),
        {"timestamp": _iso(now - 150), "type": "compacted",
         "payload": {"message": "summary", "compaction_response_id": None,
                     "latest_token_usage_record": _record(now - 200, "resp-2", r2)["payload"]}},
    ])
    u = ab.read_agent_usage(now - 3600)
    # o3: 2M input x $2 + 0.2M output x $8.
    assert (u["messages"], u["usd_equivalent"]) == (2, 5.6)


def test_cache_writes_and_reasoning_are_not_counted_twice(codex):
    """Responses API usage: input_tokens includes the cached and cache-write
    tokens, output_tokens includes reasoning (codex-api/src/sse/responses.rs)."""
    now = time.time()
    _rollout(codex, ROOT, [
        _meta(now - 300, ROOT, session=ROOT),
        _turn(now - 290, "gpt-4o"),
        _record(now - 280, "resp-1", _usage(inp=100, cached=40, cache_write=60,
                                           out=10, reasoning=5)),
    ])
    u = ab.read_agent_usage(now - 3600)
    assert (u["input_tokens"], u["cache_read_tokens"], u["cache_creation_tokens"],
            u["output_tokens"]) == (0, 40, 60, 10)


def test_a_fork_does_not_count_the_parents_usage_again(codex):
    """A forked thread's rollout starts with a copy of the parent's events and
    carries the parent's running total on. Only its own growth is new."""
    now = time.time()
    p1 = _usage(inp=1_000_000, out=100_000)
    _rollout(codex, ROOT, [
        _meta(now - 600, ROOT, session=ROOT),
        _turn(now - 590, "gpt-4o"),
        _count(now - 580, p1, p1),
    ], created="2026-09-24T10-00-00")
    own = _usage(inp=400_000, out=0)
    _rollout(codex, CHILD, [
        _meta(now - 300, CHILD, session=ROOT, forked_from=ROOT),
        _meta(now - 300, ROOT, session=ROOT),            # the copied prefix
        _turn(now - 300, "gpt-4o"),
        _count(now - 300, p1, p1),
        _turn(now - 200, "gpt-4o", turn_id="turn-2"),
        _count(now - 100, _usage(inp=1_400_000, out=100_000), own),
    ], created="2026-09-24T10-05-00")
    u = ab.read_agent_usage(now - 3600)
    # Parent: 1M x $2.50 + 0.1M x $10 = $3.50. Child's own: 0.4M x $2.50 = $1.00.
    assert u["usd_equivalent"] == 4.5
    assert u["messages"] == 2
    # And the same when the window leaves the parent's own file out.
    os.utime(codex / "sessions/2026/09/24/rollout-2026-09-24T10-00-00-"
             f"{ROOT}.jsonl", (now - 7200, now - 7200))
    assert ab.read_agent_usage(now - 3600)["usd_equivalent"] == 1.0


def test_usage_before_the_window_is_a_baseline_not_a_spend(codex):
    now = time.time()
    t1 = _usage(inp=1_000_000)
    _rollout(codex, ROOT, [
        _meta(now - 9000, ROOT, session=ROOT),
        _turn(now - 9000, "gpt-4o"),
        _count(now - 8000, t1, t1),                       # outside a 1h window
        _count(now - 60, _usage(inp=1_200_000), _usage(inp=200_000)),   # resumed
    ])
    assert ab.read_agent_usage(now - 3600)["usd_equivalent"] == 0.5


def test_a_model_llm_prices_does_not_know_is_reported_unpriced(codex):
    now = time.time()
    _rollout(codex, ROOT, [
        _meta(now - 300, ROOT, session=ROOT),
        _turn(now - 290, "gpt-5-codex"),
        _record(now - 280, "resp-1", _usage(inp=1_000_000, out=0)),
    ])
    u = ab.read_agent_usage(now - 3600)
    assert u["unpriced_models"] == {"gpt-5-codex": 1_000_000}
    assert "gpt-5-codex" in u["unpriced_note"]
    assert u["usd_equivalent"] > 0          # at the fallback rate, and said so


def test_the_model_is_the_one_the_responses_turn_ran(codex):
    now = time.time()
    _rollout(codex, ROOT, [
        _meta(now - 300, ROOT, session=ROOT),
        _turn(now - 290, "gpt-4o", turn_id="turn-1"),
        _turn(now - 250, "o3", turn_id="turn-2"),
        _record(now - 240, "resp-1", _usage(inp=1_000_000), turn_id="turn-1"),
        _record(now - 200, "resp-2", _usage(inp=1_000_000), turn_id="turn-2"),
    ])
    assert ab.read_agent_usage(now - 3600)["cost_by_model"] == {"gpt-4o": 2.5, "o3": 2.0}


# ── the harness split ────────────────────────────────────────────────────────

def _claude(tmp_path, ts, session, output_tokens):
    f = tmp_path / "claude" / "projects" / "-Users-x-proj" / f"{session}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({
        "timestamp": _iso(ts), "sessionId": session, "requestId": f"req-{session}",
        "message": {"id": f"msg-{session}", "model": "claude-opus-5-5", "usage": {
            "input_tokens": 0, "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
    }) + "\n")


def test_status_splits_cost_by_harness(tmp_path, codex, capsys):
    now = time.time()
    _claude(tmp_path, now - 100, "sess-claude", 1_000_000)          # $20
    _rollout(codex, ROOT, [
        _meta(now - 300, ROOT, session=ROOT), _turn(now - 290, "o3"),
        _record(now - 280, "resp-1", _usage(inp=1_000_000)),         # $2
    ])
    st = ab.status()
    for lens in (st["window"], st["month_to_date"]):
        assert lens["cost_by_harness"] == {"claude-code": 20.0, "codex": 2.0}
        assert lens["billable_tokens_by_harness"] == {"claude-code": 1_000_000,
                                                      "codex": 1_000_000}
    assert st["window"]["sources"]["codex"] is True
    assert st["window"]["sources"]["cursor"] is False

    import argparse

    from finops import cli_ai_budget as cli
    assert cli.run(argparse.Namespace(plan_cost=None, spend_cap=None, tokens=None,
                                      session_cap=None, month=False, reset=False,
                                      json=False)) == 0
    out = capsys.readouterr().out
    agents = out.split("by agent, last 5h")[1].split("by model")[0]
    rows = [ln.split() for ln in agents.strip().splitlines()]
    assert rows[0][:3] == ["Claude", "Code", "~$20.00"]
    assert rows[1][:3] == ["Codex", "CLI", "~$2.00"]
    assert "Codex CLI" in out.split("by session")[1]


# ── per-session caps for a Codex session ─────────────────────────────────────

def _codex_session(codex, thread, session, usd_of_o3_output, minutes_ago=5,
                   created="2026-09-24T10-00-00"):
    now = time.time()
    tokens = int(usd_of_o3_output / 8 * 1_000_000)                  # o3 output $8/M
    return _rollout(codex, thread, [
        _meta(now - minutes_ago * 60, thread, session=session), _turn(now - 280, "o3"),
        _record(now - 60, f"resp-{thread}", _usage(out=tokens), session=session,
                thread=thread),
    ], created=created)


def test_a_codex_session_is_counted_whole_with_its_subagents(codex):
    _codex_session(codex, ROOT, ROOT, 16)
    _codex_session(codex, CHILD, ROOT, 8, created="2026-09-24T10-01-00")
    _codex_session(codex, OTHER, OTHER, 40, created="2026-09-24T10-02-00")
    u = ab.read_session_usage(ROOT)
    assert (u["usd_equivalent"], u["messages"]) == (24.0, 2)
    assert ab.read_session_usage(OTHER)["usd_equivalent"] == 40.0
    assert ab.read_session_usage("0195cda5-0000-7f9a-9d7b-a9f15b60c2e2")["messages"] == 0


def test_codex_session_id_from_its_environment(codex, monkeypatch):
    _codex_session(codex, ROOT, ROOT, 16)
    monkeypatch.setenv("CODEX_SESSION_ID", ROOT)
    ab.set_budget(session_cap=20, session_id=ROOT)
    st = ab.status()
    assert (st["session"]["id"], st["session"]["id_source"]) == (ROOT, "env")
    assert st["session"]["cost_by_harness"] == {"codex": 16.0}
    assert (st["verdict"], st["verdict_basis"]) == (ab.BUDGET_WARN, "session")


def _codex_hook(session_id):
    out = io.StringIO()
    payload = {"session_id": session_id, "turn_id": "turn-9",
               "transcript_path": None, "cwd": "/Users/x/proj",
               "hook_event_name": "PreToolUse", "model": "o3",
               "permission_mode": "default", "tool_name": "Bash",
               "tool_input": {"command": "ls -la"}, "tool_use_id": "call-1"}
    assert guard_adapters.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=out,
                                   stderr=io.StringIO()) == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def test_the_codex_hook_measures_the_session_that_is_calling(codex, monkeypatch):
    _codex_session(codex, ROOT, ROOT, 16, created="2026-09-24T10-00-00")
    # Written last, so a guard that guessed "latest session" would pick it.
    _codex_session(codex, OTHER, OTHER, 1, created="2026-09-24T10-02-00")
    ab.set_budget(session_cap=10, session_id=ROOT)

    msg = _codex_hook(ROOT)["systemMessage"]      # notify: Codex cannot pause
    assert "~$16.00 estimated this session, 160% of its $10.00 session cap" in msg
    assert _codex_hook(OTHER) is None

    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    out = _codex_hook(ROOT)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"


# ── Cursor ───────────────────────────────────────────────────────────────────

def _cursor_events(now):
    return {"totalUsageEventsCount": 2,
            "pagination": {"numPages": 1, "currentPage": 1, "pageSize": 1000,
                           "hasNextPage": False, "hasPreviousPage": False},
            "usageEvents": [
                {"timestamp": str(int((now - 120) * 1000)), "model": "claude-4.5-sonnet",
                 "kind": "Usage-based", "maxMode": True, "isTokenBasedCall": True,
                 "userEmail": "dev@example.com", "conversationId": "conv-1",
                 "tokenUsage": {"inputTokens": 126, "outputTokens": 450,
                                "cacheWriteTokens": 6112, "cacheReadTokens": 11964,
                                "totalCents": 1200.0}},
                {"timestamp": str(int((now - 60) * 1000)), "model": "auto",
                 "kind": "Included in Business", "isTokenBasedCall": False,
                 "userEmail": "dev@example.com"},
            ]}


def test_cursor_is_not_read_and_nothing_is_requested_without_a_key():
    u = ab.read_agent_usage(time.time() - 3600)      # the fixture fails any request
    assert u["cost_by_harness"] == {} and u["sources"]["cursor"] is False


def test_cursor_usage_from_the_admin_api(monkeypatch):
    now = time.time()
    calls = []

    def fake_post(body):
        calls.append(body)
        return _cursor_events(now)

    monkeypatch.setattr(harness_usage, "_cursor_post", fake_post)
    monkeypatch.setenv("CURSOR_ADMIN_API_KEY", "key_test")
    monkeypatch.setenv("CURSOR_ADMIN_USER_EMAIL", "dev@example.com")
    u = ab.read_agent_usage(now - 3600)
    assert u["cost_by_harness"] == {"cursor": 12.0}      # Cursor's own totalCents
    assert u["unpriced_models"] == {}
    assert (u["input_tokens"], u["cache_creation_tokens"], u["cache_read_tokens"],
            u["output_tokens"]) == (126, 6112, 11964, 450)
    assert u["messages"] == 1                  # a request-priced call has no tokens
    assert calls[0]["email"] == "dev@example.com"
    assert calls[0]["endDate"] - calls[0]["startDate"] <= 30 * 86400 * 1000
    assert ab.read_session_usage("conv-1")["usd_equivalent"] == 12.0
    assert len(calls) == 1                      # cached: the guard asks every call
    assert u["sources"]["cursor"]["scope"] == "dev@example.com"


def test_a_failed_cursor_read_counts_nothing_and_says_why(monkeypatch):
    def down(body):
        raise OSError("unreachable")

    monkeypatch.setattr(harness_usage, "_cursor_post", down)
    monkeypatch.setenv("CURSOR_ADMIN_API_KEY", "key_test")
    u = ab.read_agent_usage(time.time() - 3600)
    assert u["messages"] == 0
    assert "unreachable" in u["sources"]["cursor"]["error"]


def _cursor_hook(conversation_id):
    out = io.StringIO()
    payload = {"conversation_id": conversation_id, "generation_id": "gen-1",
               "hook_event_name": "beforeShellExecution", "command": "ls -la",
               "cwd": "/Users/x/proj", "workspace_roots": ["/Users/x/proj"]}
    assert guard_adapters.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=out,
                                   stderr=io.StringIO()) == 0
    return json.loads(out.getvalue())


def test_the_cursor_hook_measures_its_conversation_when_usage_is_readable(monkeypatch):
    now = time.time()
    monkeypatch.setattr(harness_usage, "_cursor_post", lambda body: _cursor_events(now))
    monkeypatch.setenv("CURSOR_ADMIN_API_KEY", "key_test")
    ab.set_budget(session_cap=10, session_id="conv-1")
    v = _cursor_hook("conv-1")
    assert v["permission"] == "ask"
    assert "~$12.00 estimated this session" in v["user_message"]
    assert _cursor_hook("conv-2") == {"permission": "allow"}


def test_without_its_usage_cursor_is_never_judged_by_another_agents_session(tmp_path):
    """No Admin API key: Cursor's conversation cannot be measured. Guessing the
    latest session would stop Cursor for what a Claude Code session spent, and
    call it "this session"."""
    _claude(tmp_path, time.time() - 60, "sess-claude", 1_000_000)   # $20
    ab.set_budget(session_cap=5)                                      # every session
    assert _cursor_hook("conv-1") == {"permission": "allow"}


def test_a_codex_payload_without_a_session_id_is_not_given_a_guessed_one(tmp_path):
    _claude(tmp_path, time.time() - 60, "sess-claude", 1_000_000)   # $20
    ab.set_budget(session_cap=5)
    assert _codex_hook("") is None


def test_no_session_still_keeps_the_monthly_budget(tmp_path):
    _claude(tmp_path, time.time() - 60, "sess-claude", 1_000_000)   # $20
    ab.set_budget(spend_cap=10)
    v = _cursor_hook("conv-1")
    assert v["permission"] == "ask" and "estimated this month" in v["user_message"]
    st = ab.status(session_id=ab.NO_SESSION)
    assert st["session"] is None and st["verdict_basis"] == "spend"
