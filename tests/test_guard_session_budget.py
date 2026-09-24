"""The guard measures a per-session cap against the session making the call.

Claude Code's PreToolUse payload carries session_id. The guard passes it to the
budget gate, so "this task may spend at most $40" stops the task that spent it,
not whichever session happened to write its transcript last. Driven through the
real hook body against real transcript files: nothing about ai_budget is stubbed.
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timezone

import pytest

import finops.ai_budget as ab
import finops.guard as guard


def _write(claude_dir, session, output_tokens):
    # Opus 5.5 output is $20 per million.
    ts = datetime.fromtimestamp(time.time() - 60, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    f = claude_dir / "projects" / "-Users-x-proj" / f"{session}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({
        "timestamp": ts, "sessionId": session, "requestId": f"req-{session}",
        "message": {"id": f"msg-{session}", "model": "claude-opus-5-5", "usage": {
            "input_tokens": 0, "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
    }) + "\n")
    return f


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("FINOPS_GUARD_STOP_ON_BUDGET", raising=False)
    claude = tmp_path / "claude"
    fine = _write(claude, "sess-fine", 1_000_000)   # $20, no cap of its own
    _write(claude, "sess-over", 1_000_000)          # $20 against a $10 cap, written last
    old = time.time() - 600
    os.utime(fine, (old, old))
    ab.set_budget(session_cap=10, session_id="sess-over")
    yield


def _hook(session_id):
    out = io.StringIO()
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls -la"},
               "hook_event_name": "PreToolUse", "session_id": session_id}
    assert guard.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def test_the_session_over_its_cap_is_asked():
    v = _hook("sess-over")["hookSpecificOutput"]
    assert v["permissionDecision"] == "ask"
    assert "~$20.00 estimated this session, 200% of its $10.00 session cap" in (
        v["permissionDecisionReason"])


def test_another_session_is_not_stopped_by_that_cap():
    # sess-over was written last, so a guard that guessed "latest session" would
    # stop sess-fine for sess-over's spend. The payload says which one is calling.
    assert _hook("sess-fine") is None


def test_the_hard_stop_applies_to_a_session_cap(monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    assert _hook("sess-over")["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_gate_passes_the_id_through_and_keeps_the_no_id_call(monkeypatch):
    seen = []

    def fake_status(**kw):
        seen.append(kw)
        return {"verdict": ab.BUDGET_OK}

    monkeypatch.setattr(ab, "status", fake_status)
    guard.gate_command("ls -la", session_id="sess-x")
    guard.gate_command("ls -la")
    assert seen == [{"session_id": "sess-x"}, {}]
