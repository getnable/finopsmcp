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
  - installing is idempotent, never touches another tool's hooks, refuses a
    file it does not understand (keeping a copy) and never half-writes one
  - `--all` installs into exactly the agents present on this machine

Payload fixtures are copied from the sources guard_adapters.py cites.
"""
from __future__ import annotations

import io
import json
import os
import stat
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
                "FINOPS_GUARD_STOP_ON_BUDGET", "UV_CACHE_DIR", "COPILOT_HOME",
                "GEMINI_CLI_HOME", "CLINE_DIR", "COPILOT_AGENT_PROMPT",
                "GITHUB_COPILOT_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: None)
    monkeypatch.setattr(g, "check_budget_gate", lambda *a, **k: None)


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


def test_cursor_mcp_calls_are_never_judged_as_shell_commands(monkeypatch):
    """An MCP tool nable has no rule for (delete_bucket here) gets the neutral
    answer; tests/test_guard_harnesses.py covers the ones it translates."""
    monkeypatch.setattr(g, "gate_command", lambda c, *a, **k: pytest.fail("gate consulted for MCP"))
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
    monkeypatch.setattr(g, "check_budget_gate", lambda *a, **k: {
        "decision": "ask", "action_type": "ai_budget", "reason": "nable guard: over budget"})
    _, body, _ = _run(_with(CODEX_BASH, ALLOW_CMD))
    assert body == {"systemMessage": "nable guard: over budget"}


def test_codex_budget_stop_is_a_deny(monkeypatch):
    monkeypatch.setattr(g, "check_budget_gate", lambda *a, **k: {
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


@pytest.mark.parametrize("tool", ["apply_patch", "spawn_agent"])
def test_codex_other_tools_are_left_alone(tool, monkeypatch):
    monkeypatch.setattr(g, "gate_command", lambda c, *a, **k: pytest.fail("gate consulted"))
    code, body, _ = _run({**CODEX_BASH, "tool_name": tool, "tool_input": {"command": ASK_CMD}})
    assert code == 0 and body is None


def test_codex_unknown_mcp_tool_is_judged_on_the_command_line_it_carries():
    """An MCP tool the table does not know is left alone, unless an argument
    is itself a command line: a shell server running `terraform destroy` is
    that destroy."""
    code, body, _ = _run({**CODEX_BASH, "tool_name": "mcp__shell__run_command",
                          "tool_input": {"command": ASK_CMD}})
    assert code == 0 and body["hookSpecificOutput"]["permissionDecision"] == "deny"
    code, body, _ = _run({**CODEX_BASH, "tool_name": "mcp__aws__delete_bucket",
                          "tool_input": {"bucket": "b"}})
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
    def boom(command, *a, **k):
        raise RuntimeError("policy file exploded")
    monkeypatch.setattr(g, "gate_command", boom)
    code, body, err = _run(payload)
    assert code == 0 and body == expected
    assert "RuntimeError" in err and "policy file exploded" in err


@pytest.mark.parametrize("payload", [_with(CURSOR_SHELL, ASK_CMD), _with(CODEX_BASH, ASK_CMD),
                                     _with(CLAUDE_BASH, ASK_CMD)])
def test_stray_prints_cannot_corrupt_the_verdict(payload, monkeypatch):
    real = g.gate_command

    def chatty(command, *a, **k):
        print("debug: loading policy")
        return real(command, *a, **k)
    monkeypatch.setattr(g, "gate_command", chatty)
    code, body, err = _run(payload)
    assert code == 0 and body is not None      # stdout parsed as one JSON document
    assert "debug: loading policy" in err


def _cli(args, home, stdin=""):
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(SRC),
           "PYTHONDONTWRITEBYTECODE": "1", "NABLE_NO_TELEMETRY": "1"}
    for var in ("CODEX_HOME", "COPILOT_HOME", "GEMINI_CLI_HOME", "CLINE_DIR"):
        env.pop(var, None)
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


# ── install: shape and command ───────────────────────────────────────────────

def _uvx(monkeypatch) -> str:
    """No finops on PATH (the uvx install case), uvx at a durable location.

    pytest's tmp_path lives under $TMPDIR, which guard._is_ephemeral rightly
    rejects, so the temp root is pointed elsewhere for the exe to count."""
    uvx = Path.home() / "uv" / "bin" / "uvx"
    uvx.parent.mkdir(parents=True, exist_ok=True)
    uvx.touch(mode=0o755)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(Path.home() / "not-a-tmp"))
    monkeypatch.setattr("shutil.which", lambda n: str(uvx) if n == "uvx" else None)
    return str(uvx)


def test_cursor_install_writes_the_documented_shape(monkeypatch):
    uvx = _uvx(monkeypatch)
    outcome, path = ga.install("cursor", global_scope=True)
    assert outcome == "new" and path == Path.home() / ".cursor" / "hooks.json"
    doc = json.loads(path.read_text())
    assert doc["version"] == 1
    [entry] = doc["hooks"]["beforeShellExecution"]
    assert entry["failClosed"] is False
    assert entry["timeout"] >= 30                 # cold uvx resolve
    # A desktop app may not see a terminal's PATH, so uvx is spelled out.
    # Pinned to this release, like the Claude Code hook: an unpinned uvx
    # fetches the newest PyPI release on every shell command. Cursor blocks on
    # exit 2, which uvx returns when PyPI is out of reach, so it ends in exit 0.
    assert entry["command"] == (f"{uvx} --from finops-mcp=={g.__version__} finops guard hook"
                                "; exit 0")


def test_codex_install_writes_the_documented_shape(monkeypatch):
    _uvx(monkeypatch)
    outcome, path = ga.install("codex", global_scope=True)
    assert outcome == "new" and path == Path.home() / ".codex" / "hooks.json"
    doc = json.loads(path.read_text())
    assert set(doc) <= {"hooks", "description"}, "Codex denies unknown top-level keys"
    [group] = doc["hooks"]["PreToolUse"]
    # Shell calls are "Bash"; MCP tools are mcp__<server>__<tool> (codex-rs
    # core/src/tools/handlers/mcp.rs), which the guard translates when known.
    assert group["matcher"] == "^(Bash|mcp__.*)$"
    [handler] = group["hooks"]
    assert handler["type"] == "command"
    # Codex runs hooks through a login shell, so the bare uvx form resolves.
    # It blocks on exit 2 and uses cmd.exe on Windows, hence `|| exit 0`.
    assert handler["command"] == f"{g._UVX_HOOK_CMD} || exit 0"


@pytest.mark.parametrize("harness", ["cursor", "codex"])
def test_the_installed_command_carries_no_harness_flag(harness, monkeypatch):
    """An older nable rejects an unknown flag with exit 2, which both Cursor and
    Codex read as "block": a stale uvx cache would stop every command."""
    _uvx(monkeypatch)
    ga.install(harness, global_scope=True)
    assert "--harness" not in ga.hooks_path(harness, True).read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="runs the hook command in sh")
@pytest.mark.parametrize("harness", ["cursor", "codex"])
def test_a_launcher_that_exits_2_does_not_block(harness, monkeypatch, tmp_path):
    """uvx exits 2 when it cannot reach PyPI, before nable runs, and both
    Cursor and Codex read exit 2 as "block": every shell command stopped."""
    uvx = _uvx(monkeypatch)
    ga.install(harness, global_scope=True)
    doc = json.loads(ga.hooks_path(harness, True).read_text())
    [cmd, *_] = [c for c in ga._ADAPTERS[harness][2](doc) if ga._is_ours(c)]
    failing = tmp_path / "uvx-offline"
    failing.write_text("#!/bin/sh\necho 'error: failed to fetch' >&2\nexit 2\n")
    failing.chmod(0o755)
    run = cmd.replace(uvx, str(failing), 1) if uvx in cmd else cmd.replace("uvx", str(failing), 1)
    r = subprocess.run(["sh", "-c", run], capture_output=True, text=True, check=False)
    assert r.returncode == 0 and r.stdout == ""


@pytest.mark.parametrize("harness,suffix", [("cursor", "; exit 0"), ("codex", " || exit 0")])
def test_a_bare_command_from_an_earlier_release_is_wrapped_and_still_ours(harness, suffix,
                                                                         monkeypatch):
    _uvx(monkeypatch)
    bare = ga.hook_command(harness, True)
    path = ga.hooks_path(harness, True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if harness == "cursor":
        path.write_text(json.dumps({"version": 1, "hooks": {
            e: [{"command": bare, "timeout": 30}] for e in ga._CURSOR_EVENTS}}))
    else:
        path.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": ga._CODEX_MATCHER, "hooks": [{"type": "command", "command": bare}]}]}}))
    assert ga.state(harness, True) == "installed" and ga.pin_state(harness, True) == "pinned"
    assert ga.install(harness, True)[0] == "repaired"
    ours = ga._our_commands(harness, True)
    assert ours and all(c == bare + suffix for c in ours)
    assert ga.state(harness, True) == "installed" and ga.pin_state(harness, True) == "pinned"
    assert ga.install(harness, True)[0] == "already"
    assert ga.uninstall(harness, True)[0] is True
    assert "guard hook" not in path.read_text()


def test_codex_home_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom-codex"))
    assert ga.hooks_path("codex", True) == tmp_path / "custom-codex" / "hooks.json"
    monkeypatch.setenv("CODEX_HOME", "")
    assert ga.hooks_path("codex", True) == Path.home() / ".codex" / "hooks.json"


def test_project_scope_writes_under_the_project():
    assert ga.hooks_path("cursor", False) == Path.cwd() / ".cursor" / "hooks.json"
    assert ga.hooks_path("codex", False) == Path.cwd() / ".codex" / "hooks.json"


# ── install: safety ──────────────────────────────────────────────────────────

FOREIGN = {
    "cursor": {"version": 1, "hooks": {
        "beforeShellExecution": [{"command": "./audit.sh", "timeout": 5}],
        "afterFileEdit": [{"command": "./format.sh"}]}},
    "codex": {"description": "team hooks", "hooks": {
        "PreToolUse": [{"matcher": "^Bash$",
                        "hooks": [{"type": "command", "command": "python3 policy.py"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "notify"}]}]}},
}


# Cursor gets one entry per event (shell commands and MCP calls).
OUR_ENTRIES = {"cursor": 2, "codex": 1}


def _ours(harness, path):
    doc = json.loads(path.read_text())
    cmds = ga._ADAPTERS[harness][2](doc)
    return [c for c in cmds if ga._is_ours(c)], [c for c in cmds if not ga._is_ours(c)], doc


@pytest.mark.parametrize("harness", ["cursor", "codex"])
def test_install_is_idempotent_and_does_not_rewrite(harness, monkeypatch):
    _uvx(monkeypatch)
    _, path = ga.install(harness, True)
    first = path.read_bytes()
    for _ in range(3):
        assert ga.install(harness, True)[0] == "already"
    assert path.read_bytes() == first
    assert len(_ours(harness, path)[0]) == OUR_ENTRIES[harness]


@pytest.mark.parametrize("harness", ["cursor", "codex"])
def test_other_hooks_survive_install_and_uninstall(harness, monkeypatch):
    _uvx(monkeypatch)
    path = ga.hooks_path(harness, True)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(FOREIGN[harness]))

    ga.install(harness, True)
    ours, theirs, _ = _ours(harness, path)
    assert len(ours) == OUR_ENTRIES[harness] and theirs, "a foreign hook went missing on install"

    assert ga.uninstall(harness, True)[0] is True
    assert json.loads(path.read_text()) == FOREIGN[harness], "uninstall did not restore the file"


def test_codex_uninstall_keeps_a_foreign_handler_sharing_our_group(monkeypatch):
    _uvx(monkeypatch)
    path = ga.hooks_path("codex", True)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "^Bash$", "hooks": [
        {"type": "command", "command": "python3 policy.py"},
        {"type": "command", "command": g._UVX_HOOK_CMD}]}]}}))
    assert ga.uninstall("codex", True)[0] is True
    assert json.loads(path.read_text())["hooks"]["PreToolUse"] == [
        {"matcher": "^Bash$", "hooks": [{"type": "command", "command": "python3 policy.py"}]}]


@pytest.mark.parametrize("harness", ["cursor", "codex"])
def test_uninstall_twice_and_uninstall_nothing(harness, monkeypatch):
    _uvx(monkeypatch)
    path = ga.hooks_path(harness, True)
    assert ga.uninstall(harness, True)[0] is False
    assert not path.exists(), "uninstall created a file"
    ga.install(harness, True)
    assert ga.uninstall(harness, True)[0] is True
    assert ga.uninstall(harness, True)[0] is False


@pytest.mark.parametrize("harness", ["cursor", "codex"])
def test_malformed_json_is_refused_backed_up_and_left_alone(harness):
    path = ga.hooks_path(harness, True)
    path.parent.mkdir(parents=True)
    body = b'{"version": 1, "hooks": { THIS IS NOT JSON'
    path.write_bytes(body)
    for action in (ga.install, ga.install, ga.uninstall):
        with pytest.raises(SystemExit) as e:
            action(harness, True)
        assert "not valid JSON" in str(e.value.code) and "nothing was changed" in str(e.value.code)
    assert path.read_bytes() == body, "refused but still wrote to the file"
    backups = list(path.parent.glob("hooks.json.nable-backup-*"))
    assert len(backups) == 1, f"expected one backup for one broken file, got {backups}"
    assert backups[0].read_bytes() == body
    assert backups[0].name in str(e.value.code)


@pytest.mark.parametrize("harness,body,described_as", [
    ("cursor", '["a", "list"]', "list"),
    ("codex", '"a string"', "str"),
    ("cursor", '{"version": 1, "hooks": "wat"}', "str"),
    ("cursor", '{"version": 1, "hooks": {"beforeShellExecution": {"command": "x"}}}', "dict"),
    ("cursor", '{"version": 2, "hooks": {}}', "version 2"),
    ("codex", '{"hooks": {"PreToolUse": {"matcher": "Bash"}}}', "dict"),
])
def test_unexpected_shapes_are_refused_rather_than_guessed_at(harness, body, described_as):
    path = ga.hooks_path(harness, True)
    path.parent.mkdir(parents=True)
    path.write_text(body)
    with pytest.raises(SystemExit) as e:
        ga.install(harness, True)
    assert described_as in str(e.value.code)
    assert path.read_text() == body


@pytest.mark.parametrize("harness,body", [
    ("cursor", '{"version": 1, "hooks": null}'),
    ("cursor", '{"version": 1, "hooks": {"beforeShellExecution": null}}'),
    ("cursor", "{}"),
    ("codex", '{"hooks": null}'),
    ("codex", '{"hooks": {"PreToolUse": null}}'),
])
def test_null_keys_are_treated_as_absent(harness, body, monkeypatch):
    _uvx(monkeypatch)
    path = ga.hooks_path(harness, True)
    path.parent.mkdir(parents=True)
    path.write_text(body)
    assert ga.install(harness, True)[0] == "new"
    assert len(_ours(harness, path)[0]) == OUR_ENTRIES[harness]


def test_a_versionless_cursor_file_is_not_given_a_version(monkeypatch):
    """Only what we need changes; a file that works without a version keeps working."""
    _uvx(monkeypatch)
    path = ga.hooks_path("cursor", True)
    path.parent.mkdir(parents=True)
    path.write_text('{"hooks": {"afterFileEdit": [{"command": "./f.sh"}]}}')
    ga.install("cursor", True)
    assert "version" not in json.loads(path.read_text())


@pytest.mark.parametrize("harness", ["cursor", "codex"])
def test_a_failed_replace_leaves_the_old_file_and_no_debris(harness, monkeypatch):
    _uvx(monkeypatch)
    path = ga.hooks_path(harness, True)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(FOREIGN[harness]))
    before = path.read_bytes()

    def disk_full(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(ga.os, "replace", disk_full)
    with pytest.raises(OSError):
        ga.install(harness, True)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["hooks.json"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root writes through a read-only mode")
@pytest.mark.parametrize("harness", ["cursor", "codex"])
def test_a_read_only_file_is_not_swapped_out_from_under_its_mode(harness, monkeypatch):
    """os.replace only needs the directory writable; the file's mode still counts."""
    _uvx(monkeypatch)
    path = ga.hooks_path(harness, True)
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    path.chmod(stat.S_IRUSR)
    try:
        with pytest.raises(PermissionError):
            ga.install(harness, True)
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert path.read_text() == "{}"


def test_a_read_only_file_is_refused_even_when_access_is_mocked(monkeypatch):
    """The same guarantee, checked where running as root would hide it."""
    _uvx(monkeypatch)
    path = ga.hooks_path("cursor", True)
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    monkeypatch.setattr(ga.os, "access", lambda p, mode: False)
    with pytest.raises(PermissionError):
        ga.install("cursor", True)
    assert path.read_text() == "{}"


def test_a_dotfiles_symlink_stays_a_symlink(tmp_path, monkeypatch):
    _uvx(monkeypatch)
    real = tmp_path / "dotfiles" / "cursor-hooks.json"
    real.parent.mkdir()
    real.write_text(json.dumps(FOREIGN["cursor"]))
    link = ga.hooks_path("cursor", True)
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    ga.install("cursor", True)
    assert link.is_symlink(), "replaced the user's symlink with a plain file"
    assert len(_ours("cursor", real)[0]) == OUR_ENTRIES["cursor"]


def test_the_file_mode_is_kept(monkeypatch):
    _uvx(monkeypatch)
    path = ga.hooks_path("codex", True)
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    path.chmod(0o640)
    ga.install("codex", True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


# ── dead hooks ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("harness", ["cursor", "codex"])
def test_a_dead_hook_is_repaired_and_a_live_one_left_alone(harness, monkeypatch):
    _uvx(monkeypatch)
    ga.install(harness, True)
    path = ga.hooks_path(harness, True)
    doc = json.loads(path.read_text())
    entry = (doc["hooks"]["beforeShellExecution"][0] if harness == "cursor"
             else doc["hooks"]["PreToolUse"][0]["hooks"][0])
    entry["command"] = "/gone/uv/archive-v0/x/bin/finops guard hook"
    path.write_text(json.dumps(doc))
    assert ga.state(harness, True) == "broken"

    assert ga.install(harness, True)[0] == "repaired"
    assert ga.state(harness, True) == "installed"
    cmds, _, _ = _ours(harness, path)
    assert len(cmds) == OUR_ENTRIES[harness] and not any("/gone/" in c for c in cmds)

    # A live command of ours that differs (a hand-edited wrapper) is kept.
    exe = Path.cwd() / "wrapper-finops"
    exe.touch()
    doc = json.loads(path.read_text())
    entry = (doc["hooks"]["beforeShellExecution"][0] if harness == "cursor"
             else doc["hooks"]["PreToolUse"][0]["hooks"][0])
    entry["command"] = f"{exe} guard hook"
    path.write_text(json.dumps(doc))
    assert ga.install(harness, True)[0] == "already"
    assert f"{exe} guard hook" in _ours(harness, path)[0]


@pytest.mark.parametrize("harness", ["cursor", "codex"])
def test_state_never_raises_on_a_file_it_cannot_understand(harness):
    path = ga.hooks_path(harness, True)
    path.parent.mkdir(parents=True)
    for body in ("[]", "null", '"x"', '{"hooks": null}', "not json",
                 '{"hooks": {"PreToolUse": [null, {"hooks": [null]}]}}',
                 '{"hooks": {"beforeShellExecution": [null, 3]}}'):
        path.write_text(body)
        assert ga.state(harness, True) == "absent", f"raised or true-d on {body!r}"


# ── --all ────────────────────────────────────────────────────────────────────

def _cli_in_process(action, capsys, **kw):
    kw = {"harness": None, "everything": True, "global_scope": True, **kw}
    code = ga.cli(action, **kw)
    return code, capsys.readouterr().out


def test_only_agents_present_on_this_machine_are_detected(tmp_path, monkeypatch):
    assert ga.detected() == []
    (Path.home() / ".cursor").mkdir()
    assert ga.detected() == ["cursor"]
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ch"))
    (tmp_path / "ch").mkdir()
    (Path.home() / ".claude").mkdir()
    assert ga.detected() == ["claude", "cursor", "codex"]


def test_install_all_wires_each_agent_found_and_skips_the_rest(capsys, monkeypatch):
    _uvx(monkeypatch)
    (Path.home() / ".claude").mkdir()
    (Path.home() / ".codex").mkdir()
    code, out = _cli_in_process("install", capsys)
    assert code == 0
    assert g.is_installed(Path.home() / ".claude" / "settings.json")
    assert ga.state("codex", True) == "installed"
    assert not (Path.home() / ".cursor").exists(), "installed into an agent that is not here"
    assert "Claude Code" in out and "Codex CLI" in out
    assert "Cursor" in out and "not found" in out
    assert "Hooks need review" in out, "Codex users must be told to trust the hook"


def test_install_all_with_no_agent_found_says_so_and_fails(capsys):
    code, out = _cli_in_process("install", capsys)
    assert code == 1 and "No supported agent found" in out


def test_one_refused_file_does_not_stop_the_others(capsys, monkeypatch):
    _uvx(monkeypatch)
    (Path.home() / ".codex").mkdir()
    cursor = Path.home() / ".cursor" / "hooks.json"
    cursor.parent.mkdir()
    cursor.write_text("{ nope")
    code, out = _cli_in_process("install", capsys)
    assert code == 1
    assert cursor.read_text() == "{ nope"
    assert ga.state("codex", True) == "installed"
    assert "not valid JSON" in out


def test_uninstall_all_removes_ours_everywhere_and_nothing_else(capsys, monkeypatch):
    _uvx(monkeypatch)
    for d in (".claude", ".cursor", ".codex"):
        (Path.home() / d).mkdir()
    settings = Path.home() / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-tool check"}]}]}}))
    assert _cli_in_process("install", capsys)[0] == 0

    code, out = _cli_in_process("uninstall", capsys)
    assert code == 0 and out.count("removed") == 3
    assert all(ga.state(h, True) == "absent" for h in ga.HARNESSES)
    assert "other-tool" in settings.read_text()


def test_single_harness_cli(capsys, monkeypatch):
    _uvx(monkeypatch)
    code, out = _cli_in_process("install", capsys, harness="cursor", everything=False,
                                global_scope=False)
    assert code == 0 and (Path.cwd() / ".cursor" / "hooks.json").exists()
    assert "nable guard uninstall --harness cursor" in out
    code, out = _cli_in_process("uninstall", capsys, harness="cursor", everything=False,
                                global_scope=False)
    assert code == 0 and "removed" in out


def test_install_telemetry_names_the_harness_and_nothing_identifying(capsys, monkeypatch):
    _uvx(monkeypatch)
    events = []
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: events.append((e, p)))
    ga.cli("install", harness="codex", everything=False, global_scope=True)
    [(name, payload)] = events
    assert name == "guard_installed" and payload["harness"] == "codex"
    assert set(payload) <= {"scope", "outcome", "hook_form", "harness"}
    assert str(Path.home()) not in repr(payload) and "guard hook" not in repr(payload)


def test_status_lists_the_other_agents(monkeypatch):
    _uvx(monkeypatch)
    assert ga.status_lines() == []               # nothing here, nothing to say
    (Path.home() / ".cursor").mkdir()
    ga.install("cursor", True)
    lines = "\n".join(ga.status_lines())
    assert "Cursor" in lines and "installed" in lines and "Codex" not in lines


def test_the_real_cli_installs_everywhere_it_should(tmp_path):
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True, exist_ok=True)
    (home / ".codex").mkdir()
    r = _cli(["guard", "install", "--all", "--global"], home)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / ".cursor" / "hooks.json").exists()
    assert (home / ".codex" / "hooks.json").exists()
    assert not (home / ".claude").exists()

    r = _cli(["guard", "status"], home)
    assert r.returncode == 0 and "Cursor" in r.stdout and "Codex CLI" in r.stdout

    r = _cli(["guard", "uninstall", "--harness", "codex", "--global"], home)
    assert r.returncode == 0 and "removed" in r.stdout


# ── the ledger names the harness that asked ───────────────────────────────────

@pytest.mark.parametrize("payload,harness", [(_with(CURSOR_SHELL, ASK_CMD), "cursor"),
                                             (_with(CODEX_BASH, ASK_CMD), "codex")])
def test_verdicts_are_attributed_to_their_harness(payload, harness, monkeypatch):
    seen = {}
    real = g.gate_command

    def spy(command, *a, **k):
        seen.update(k)
        return real(command, *a, **k)
    monkeypatch.setattr(g, "gate_command", spy)
    _run(payload)
    assert seen.get("harness") == harness


def test_codex_shows_a_warn_without_stopping():
    out = ga.codex_response({"decision": "warn", "reason": "nable guard: 99% of threshold"})
    assert out == {"systemMessage": "nable guard: 99% of threshold"}
