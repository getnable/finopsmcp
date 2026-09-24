"""The agent guard in Cursor and Codex CLI, not just Claude Code.

The same gate (guard.gate_command) answers every harness; what differs is the
wire format and the config file. Invariants under test:

  - each harness's payload is recognised from the payload alone, and one that
    is not recognised is treated as Claude Code, which is what `guard hook`
    always was
  - allow / ask / deny round-trip into each harness's own response format,
    and Codex never receives a value its parser rejects
  - every failure allows, says why on stderr, and keeps stdout to the
    harness's JSON and nothing else

Payload fixtures are copied from the sources guard_adapters.py cites.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import finops.guard as g
import finops.guard_adapters as ga

SRC = Path(__file__).resolve().parents[1] / "src"

# Cursor, https://cursor.com/docs/hooks: the fields every hook receives, plus
# beforeShellExecution's own (command, cwd, sandbox).
CURSOR_SHELL = {
    "conversation_id": "668320d2-2fd8-4888-b33c-2a466fec86e7",
    "generation_id": "490b90b7-a2ce-4c2c-bb76-cb77b125df2f",
    "model": "default",
    "hook_event_name": "beforeShellExecution",
    "cursor_version": "1.7.2",
    "workspace_roots": ["/Users/dev/project"],
    "user_email": None,
    "transcript_path": None,
    "command": "git status",
    "cwd": "/Users/dev/project",
    "sandbox": False,
}

# Same page: beforeMCPExecution carries tool_name, tool_input as a JSON string,
# and the server's url (or command, for a stdio server).
CURSOR_MCP = {
    **{k: v for k, v in CURSOR_SHELL.items() if k not in ("command", "cwd", "sandbox")},
    "hook_event_name": "beforeMCPExecution",
    "tool_name": "delete_bucket",
    "tool_input": '{"bucket": "logs"}',
    "url": "https://mcp.example.com/mcp",
}

# Codex, openai/codex codex-rs/hooks/src/events/pre_tool_use.rs
# (command_input_json) and the exec_command handler, which sends the raw command
# string as tool_input.command under the canonical tool_name "Bash".
CODEX_BASH = {
    "session_id": "0199a213-81c0-7800-8aa1-bbab2a035a53",
    "turn_id": "turn-1",
    "transcript_path": None,
    "cwd": "/home/dev/project",
    "hook_event_name": "PreToolUse",
    "model": "gpt-5-codex",
    "permission_mode": "default",
    "tool_name": "Bash",
    "tool_input": {"command": "git status"},
    "tool_use_id": "call-1",
}

# Claude Code, https://code.claude.com/docs/en/hooks (PreToolUse input).
CLAUDE_BASH = {
    "session_id": "abc123",
    "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
    "transcript_path": "/home/user/.claude/projects/x/transcript.jsonl",
    "cwd": "/home/user/my-project",
    "permission_mode": "default",
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "git status", "description": "Run", "timeout": 120000},
    "tool_use_id": "toolu_01ABC123",
}

ALLOW_CMD = "git status"
ASK_CMD = "terraform destroy -auto-approve"
DENY_CMD = "aws ec2 stop-instances --instance-ids i-1"


def _with(payload: dict, command: str) -> dict:
    p = json.loads(json.dumps(payload))
    if "tool_input" in p and isinstance(p["tool_input"], dict):
        p["tool_input"]["command"] = command
    else:
        p["command"] = command
    return p


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """A throwaway HOME and project, no policy overrides, no telemetry."""
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    for var in ("CODEX_HOME", "FINOPS_GUARD_STRICT", "FINOPS_POLICY_ALLOWED_ACTIONS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "UV_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: None)
    monkeypatch.setattr(g, "check_budget_gate", lambda: None)


def _deny_mode(monkeypatch):
    # An allowlist without stop_idle puts a stop-instances out of policy: deny.
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "ticket")


def _run(payload, harness=None):
    out, err = io.StringIO(), io.StringIO()
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    code = ga.run_hook(harness, stdin=io.StringIO(raw), stdout=out, stderr=err)
    body = out.getvalue()
    return code, (json.loads(body) if body else None), err.getvalue()


# ── which harness sent this ───────────────────────────────────────────────────

@pytest.mark.parametrize("payload,expected", [
    (CURSOR_SHELL, "cursor"),
    (CURSOR_MCP, "cursor"),
    (CODEX_BASH, "codex"),
    (CLAUDE_BASH, "claude"),
    ({**CLAUDE_BASH, "agent_id": "a1", "agent_type": "Explore"}, "claude"),
    ({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "claude"),
    ({}, "claude"),
])
def test_each_payload_is_recognised(payload, expected):
    assert ga.detect_harness(payload) == expected


def test_a_cursor_payload_without_an_event_name_is_still_cursor():
    """Cursor's own cookbook audit hook infers the event this way."""
    bare = {k: v for k, v in CURSOR_SHELL.items() if k != "hook_event_name"}
    assert ga.detect_harness(bare) == "cursor"


# ── Cursor round trip ─────────────────────────────────────────────────────────

def test_cursor_allow_is_the_documented_neutral_answer():
    code, body, _ = _run(_with(CURSOR_SHELL, ALLOW_CMD))
    assert code == 0 and body == {"permission": "allow"}


def test_cursor_ask_carries_the_reason_to_user_and_agent():
    code, body, _ = _run(_with(CURSOR_SHELL, ASK_CMD))
    assert code == 0
    assert body["permission"] == "ask"
    assert body["user_message"].startswith("nable guard")
    assert body["agent_message"] == body["user_message"]


def test_cursor_deny(monkeypatch):
    _deny_mode(monkeypatch)
    code, body, _ = _run(_with(CURSOR_SHELL, DENY_CMD))
    assert code == 0 and body["permission"] == "deny"
    assert "nable guard" in body["agent_message"]


def test_cursor_response_uses_only_documented_keys(monkeypatch):
    for cmd in (ALLOW_CMD, ASK_CMD):
        _, body, _ = _run(_with(CURSOR_SHELL, cmd))
        assert set(body) <= {"permission", "user_message", "agent_message"}
        assert body["permission"] in ("allow", "ask", "deny")


def test_cursor_mcp_calls_are_allowed_without_consulting_the_gate(monkeypatch):
    monkeypatch.setattr(g, "gate_command", lambda c: pytest.fail("gate consulted for MCP"))
    code, body, _ = _run(CURSOR_MCP)
    assert code == 0 and body == {"permission": "allow"}


def test_cursor_empty_command_is_allowed():
    code, body, _ = _run(_with(CURSOR_SHELL, "   "))
    assert code == 0 and body == {"permission": "allow"}


# ── Codex round trip ──────────────────────────────────────────────────────────

def test_codex_allow_is_silence():
    """Empty stdout is Codex's no-op; "allow" without updatedInput is an error there."""
    code, body, _ = _run(_with(CODEX_BASH, ALLOW_CMD))
    assert code == 0 and body is None


def test_codex_deny(monkeypatch):
    _deny_mode(monkeypatch)
    _, body, _ = _run(_with(CODEX_BASH, DENY_CMD))
    out = body["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"].startswith("nable guard")


def test_codex_ask_becomes_a_deny_that_says_why():
    """Codex logs permissionDecision "ask" as unsupported and runs the command.
    A one-way door must not go through on a technicality."""
    _, body, _ = _run(_with(CODEX_BASH, ASK_CMD))
    out = body["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "cannot pause" in out["permissionDecisionReason"]
    assert "run the command yourself" in out["permissionDecisionReason"]


def test_codex_over_budget_notice_warns_instead_of_stopping(monkeypatch):
    """Notify mode is the user's choice at install; turning it into a stop on
    every command would override that choice."""
    monkeypatch.setattr(g, "check_budget_gate", lambda: {
        "decision": "ask", "action_type": "ai_budget", "reason": "nable guard: over budget"})
    _, body, _ = _run(_with(CODEX_BASH, ALLOW_CMD))
    assert body == {"systemMessage": "nable guard: over budget"}


def test_codex_budget_stop_is_a_deny(monkeypatch):
    monkeypatch.setattr(g, "check_budget_gate", lambda: {
        "decision": "deny", "action_type": "ai_budget", "reason": "nable guard: stopped"})
    _, body, _ = _run(_with(CODEX_BASH, ALLOW_CMD))
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("verdict", [
    None,
    {"decision": "ask", "action_type": "delete_resource", "reason": "r"},
    {"decision": "deny", "action_type": "delete_resource", "reason": "r"},
    {"decision": "deny", "action_type": "delete_resource", "reason": ""},
    {"decision": "ask", "action_type": "ai_budget", "reason": "r"},
    {"decision": "something new", "reason": "r"},
])
def test_codex_never_gets_a_value_its_parser_rejects(verdict):
    """codex-rs output_parser: no ask, no allow without updatedInput, a deny
    needs a non-empty reason, and no continue:false / stopReason."""
    body = ga.codex_response(verdict)
    if body is None:
        return
    assert "continue" not in body and "stopReason" not in body
    out = body.get("hookSpecificOutput")
    if out is not None:
        assert out["permissionDecision"] == "deny"
        assert out["permissionDecisionReason"].strip()


@pytest.mark.parametrize("tool", ["apply_patch", "mcp__aws__delete_bucket", "spawn_agent"])
def test_codex_other_tools_are_left_alone(tool, monkeypatch):
    monkeypatch.setattr(g, "gate_command", lambda c: pytest.fail("gate consulted"))
    code, body, _ = _run({**CODEX_BASH, "tool_name": tool, "tool_input": {"command": ASK_CMD}})
    assert code == 0 and body is None


def test_codex_argv_style_command_is_still_judged():
    payload = {**CODEX_BASH, "tool_input": {"command": ["terraform", "destroy", "-auto-approve"]}}
    _, body, _ = _run(payload)
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


# ── Claude Code is untouched ──────────────────────────────────────────────────

@pytest.mark.parametrize("command", [ALLOW_CMD, ASK_CMD])
def test_claude_payloads_get_exactly_what_guard_run_hook_gives(command):
    payload = _with(CLAUDE_BASH, command)
    direct = io.StringIO()
    g.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=direct)
    code, body, _ = _run(payload)
    assert code == 0
    assert body == (json.loads(direct.getvalue()) if direct.getvalue() else None)


# ── fail open, and keep stdout clean ─────────────────────────────────────────

@pytest.mark.parametrize("raw", ["not json at all", "[1, 2]", "", "null"])
def test_unreadable_input_allows_and_says_why(raw):
    code, body, err = _run(raw, harness="cursor")
    assert code == 0 and body == {"permission": "allow"}
    assert "nable guard: allowing" in err

    code, body, err = _run(raw)                  # harness unknown: silence
    assert code == 0 and body is None and "allowing" in err


@pytest.mark.parametrize("payload,expected", [
    (_with(CURSOR_SHELL, ASK_CMD), {"permission": "allow"}),
    (_with(CODEX_BASH, ASK_CMD), None),
])
def test_a_gate_crash_allows_and_says_why(payload, expected, monkeypatch):
    def boom(command):
        raise RuntimeError("policy file exploded")
    monkeypatch.setattr(g, "gate_command", boom)
    code, body, err = _run(payload)
    assert code == 0 and body == expected
    assert "RuntimeError" in err and "policy file exploded" in err


@pytest.mark.parametrize("payload", [_with(CURSOR_SHELL, ASK_CMD), _with(CODEX_BASH, ASK_CMD),
                                     _with(CLAUDE_BASH, ASK_CMD)])
def test_stray_prints_cannot_corrupt_the_verdict(payload, monkeypatch):
    real = g.gate_command

    def chatty(command):
        print("debug: loading policy")
        return real(command)
    monkeypatch.setattr(g, "gate_command", chatty)
    code, body, err = _run(payload)
    assert code == 0 and body is not None      # stdout parsed as one JSON document
    assert "debug: loading policy" in err


def _cli(args, home, stdin=""):
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(SRC),
           "PYTHONDONTWRITEBYTECODE": "1", "NABLE_NO_TELEMETRY": "1"}
    env.pop("CODEX_HOME", None)
    return subprocess.run([sys.executable, "-m", "finops.entry", *args], input=stdin,
                          capture_output=True, text=True, env=env, timeout=60, check=False)


@pytest.mark.parametrize("payload,harness_flag,check", [
    (_with(CURSOR_SHELL, ASK_CMD), [], lambda b: b["permission"] == "ask"),
    (_with(CURSOR_SHELL, ASK_CMD), ["--harness", "cursor"], lambda b: b["permission"] == "ask"),
    (_with(CODEX_BASH, ASK_CMD), [], lambda b: b["hookSpecificOutput"]["permissionDecision"] == "deny"),
])
def test_the_real_cli_prints_one_json_document_and_nothing_else(payload, harness_flag, check, tmp_path):
    r = _cli(["guard", "hook", *harness_flag], tmp_path / "home", json.dumps(payload))
    assert r.returncode == 0, r.stderr
    assert check(json.loads(r.stdout)), r.stdout
