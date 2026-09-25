"""The agent guard in GitHub Copilot, Gemini CLI and Cline, MCP calls in
Cursor and Codex, and keeping installed hooks pinned.

Same invariants as tests/test_guard_adapters.py, for the harnesses added
after it:

  - each payload is recognised from the payload alone, and none of the new
    detections claims a Claude Code, Cursor or Codex payload
  - allow / ask / deny / warn map onto each harness's own answer, and a
    harness that cannot pause for a human gets a deny (or a stop) that says so
  - a hook that fails to start allows: Copilot and Gemini CLI block on a
    failed hook, so their command ends in `; exit 0`
  - installing is idempotent, refuses what it does not own, and uninstall
    leaves the files as they were

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

# GitHub Copilot, content/copilot/reference/hooks-reference.md (camelCase
# preToolUse input) and content/copilot/tutorials/copilot-cli-hooks.md
# (toolArgs is a JSON string).
COPILOT_BASH = {
    "sessionId": "b3c0f7a2-5f0e-4a57-9b33-7d4f9c1e2a10",
    "timestamp": 1758700000000,
    "cwd": "/home/dev/project",
    "toolName": "bash",
    "toolArgs": json.dumps({"command": "git status", "description": "Show status"}),
}

# The same reference, VS Code compatible (PascalCase) format. nable never
# installs this form; it is Claude-shaped, and stays Claude.
COPILOT_PASCAL = {
    "hook_event_name": "PreToolUse",
    "session_id": "b3c0f7a2",
    "timestamp": "2026-09-24T10:00:00Z",
    "cwd": "/home/dev/project",
    "tool_name": "Bash",
    "tool_input": {"command": "git status"},
}

# Gemini CLI, docs/hooks/reference.md (base input + BeforeTool fields) and
# packages/core/src/hooks/hookEventHandler.ts (createBaseInput).
GEMINI_SHELL = {
    "session_id": "6f1c1f0e-3a55-4f4e-8a5c-1b7f0c7e9d21",
    "transcript_path": "/home/dev/.gemini/tmp/abc/chats/session.json",
    "cwd": "/home/dev/project",
    "hook_event_name": "BeforeTool",
    "timestamp": "2026-09-24T10:00:00.000Z",
    "tool_name": "run_shell_command",
    "tool_input": {"command": "git status", "description": "Show status"},
}

# Cline, .clinerules/hooks/README.md (the VS Code extension's payload; the
# SDK adapter stringifies each parameter, apps/vscode/src/sdk/hooks-adapter.ts).
CLINE_EXT = {
    "clineVersion": "3.40.0",
    "hookName": "PreToolUse",
    "timestamp": "1758700000000",
    "taskId": "task-1",
    "workspaceRoots": ["/home/dev/project"],
    "userId": "dev",
    "preToolUse": {"toolName": "run_commands",
                   "parameters": {"commands": json.dumps(["git status"])}},
}

# Cline CLI, sdk/packages/core/src/hooks/hook-file-hooks.ts (beforeTool): the
# same preToolUse block plus tool_call with the raw input.
CLINE_CLI = {
    "clineVersion": "1.0.0",
    "hookName": "tool_call",
    "timestamp": "2026-09-24T10:00:00.000Z",
    "taskId": "conv-1",
    "workspaceRoots": ["/home/dev/project"],
    "userId": "dev",
    "agent_id": "agent-1",
    "parent_agent_id": None,
    "iteration": 1,
    "tool_call": {"id": "call-1", "name": "run_commands", "input": {"commands": ["git status"]}},
    "preToolUse": {"toolName": "run_commands",
                   "parameters": {"commands": json.dumps(["git status"])}},
}

# Existing harnesses, as in tests/test_guard_adapters.py.
CLAUDE_BASH = {
    "session_id": "abc123", "transcript_path": "/t.jsonl", "cwd": "/p",
    "permission_mode": "default", "hook_event_name": "PreToolUse",
    "tool_name": "Bash", "tool_input": {"command": "git status"}, "tool_use_id": "toolu_1",
}
CODEX_BASH = {
    "session_id": "s", "turn_id": "turn-1", "transcript_path": None, "cwd": "/p",
    "hook_event_name": "PreToolUse", "model": "gpt-5-codex", "permission_mode": "default",
    "tool_name": "Bash", "tool_input": {"command": "git status"}, "tool_use_id": "call-1",
}
CURSOR_SHELL = {
    "conversation_id": "c", "generation_id": "g", "model": "default",
    "hook_event_name": "beforeShellExecution", "cursor_version": "1.7.2",
    "workspace_roots": ["/p"], "command": "git status", "cwd": "/p", "sandbox": False,
}
# beforeMCPExecution: tool_name, tool_input as a JSON string, and the server's
# url or command. mcp_server_name is optional to the adapter.
CURSOR_MCP = {
    **{k: v for k, v in CURSOR_SHELL.items() if k not in ("command", "cwd", "sandbox")},
    "hook_event_name": "beforeMCPExecution",
    "tool_name": "call_aws",
    "tool_input": json.dumps({"cli_command": "aws s3 ls"}),
    "command": "uvx awslabs.aws-api-mcp-server@latest",
}

ALLOW_CMD = "git status"
ASK_CMD = "terraform destroy -auto-approve"
DENY_CMD = "aws ec2 stop-instances --instance-ids i-1"
TERMINATE = "aws ec2 terminate-instances --instance-ids i-1"

NEW = ("copilot", "gemini", "cline")


def _copilot(command: str, tool: str = "bash", as_string: bool = True) -> dict:
    args = {"command": command}
    return {**COPILOT_BASH, "toolName": tool, "toolArgs": json.dumps(args) if as_string else args}


def _gemini(command: str) -> dict:
    return {**GEMINI_SHELL, "tool_input": {"command": command}}


def _cline(*commands: str, cli: bool = False) -> dict:
    if cli:
        return {**CLINE_CLI,
                "tool_call": {**CLINE_CLI["tool_call"], "input": {"commands": list(commands)}},
                "preToolUse": {"toolName": "run_commands",
                               "parameters": {"commands": json.dumps(list(commands))}}}
    return {**CLINE_EXT, "preToolUse": {"toolName": "run_commands",
                                        "parameters": {"commands": json.dumps(list(commands))}}}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    for var in ("CODEX_HOME", "COPILOT_HOME", "GEMINI_CLI_HOME", "CLINE_DIR",
                "COPILOT_AGENT_PROMPT", "GITHUB_COPILOT_API_TOKEN", "FINOPS_GUARD_STRICT",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_STOP_ON_BUDGET", "UV_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: None)
    monkeypatch.setattr(g, "check_budget_gate", lambda *a, **k: None)


def _deny_mode(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "ticket")


def _run(payload, harness=None):
    out, err = io.StringIO(), io.StringIO()
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    code = ga.run_hook(harness, stdin=io.StringIO(raw), stdout=out, stderr=err)
    body = out.getvalue()
    return code, (json.loads(body) if body else None), err.getvalue()


def _uvx(monkeypatch) -> str:
    uvx = Path.home() / "uv" / "bin" / "uvx"
    uvx.parent.mkdir(parents=True, exist_ok=True)
    uvx.touch(mode=0o755)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(Path.home() / "not-a-tmp"))
    monkeypatch.setattr("shutil.which", lambda n: str(uvx) if n == "uvx" else None)
    return str(uvx)


# ── which harness sent this ───────────────────────────────────────────────────

@pytest.mark.parametrize("payload,expected", [
    (COPILOT_BASH, "copilot"),
    ({**COPILOT_BASH, "toolArgs": {"command": "ls"}}, "copilot"),
    (GEMINI_SHELL, "gemini"),
    ({**GEMINI_SHELL, "tool_name": "read_file"}, "gemini"),
    (CLINE_EXT, "cline"),
    (CLINE_CLI, "cline"),
])
def test_each_new_payload_is_recognised(payload, expected):
    assert ga.detect_harness(payload) == expected


@pytest.mark.parametrize("payload,expected", [
    (CLAUDE_BASH, "claude"),
    ({**CLAUDE_BASH, "agent_id": "a1", "agent_type": "Explore"}, "claude"),
    ({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "claude"),
    ({}, "claude"),
    (CODEX_BASH, "codex"),
    ({**CODEX_BASH, "tool_name": "mcp__aws__call_aws"}, "codex"),
    (CURSOR_SHELL, "cursor"),
    (CURSOR_MCP, "cursor"),
    ({k: v for k, v in CURSOR_SHELL.items() if k != "hook_event_name"}, "cursor"),
    # Copilot's Claude-compatible format is Claude-shaped on purpose.
    (COPILOT_PASCAL, "claude"),
])
def test_the_new_detections_never_claim_an_existing_payload(payload, expected):
    assert ga.detect_harness(payload) == expected


# ── GitHub Copilot ────────────────────────────────────────────────────────────

def test_copilot_allow_is_silence():
    """permissionDecision "allow" pre-approves the call in Copilot, which would
    skip a prompt its own rules wanted; empty output is its default flow."""
    code, body, _ = _run(_copilot(ALLOW_CMD))
    assert code == 0 and body is None


def test_copilot_cli_asks_with_the_reason():
    _, body, _ = _run(_copilot(ASK_CMD))
    assert body["permissionDecision"] == "ask"
    assert body["permissionDecisionReason"].startswith("nable guard")


@pytest.mark.parametrize("var", ["COPILOT_AGENT_PROMPT", "GITHUB_COPILOT_API_TOKEN"])
def test_copilot_cloud_agent_ask_becomes_a_deny_that_says_why(var, monkeypatch):
    """The cloud agent treats "ask" as "deny" with no one to ask; nable says so."""
    monkeypatch.setenv(var, "x")
    _, body, _ = _run(_copilot(ASK_CMD))
    assert body["permissionDecision"] == "deny"
    assert "cloud agent has no one to ask" in body["permissionDecisionReason"]
    assert "run the command yourself" in body["permissionDecisionReason"]


def test_copilot_deny(monkeypatch):
    _deny_mode(monkeypatch)
    _, body, _ = _run(_copilot(DENY_CMD))
    assert set(body) == {"permissionDecision", "permissionDecisionReason"}
    assert body["permissionDecision"] == "deny"
    assert body["permissionDecisionReason"].startswith("nable guard")


def test_copilot_warn_and_budget_notice_are_progress_lines(monkeypatch):
    """A progress line is display-only and stripped before the decision is
    parsed, so the call keeps its default flow."""
    assert ga.copilot_response({"decision": "warn", "reason": "nable guard: 99%"}) == {
        "type": "progress", "message": "nable guard: 99%"}
    monkeypatch.setattr(g, "check_budget_gate", lambda *a, **k: {
        "decision": "ask", "action_type": "ai_budget", "reason": "nable guard: over budget"})
    _, body, _ = _run(_copilot(ALLOW_CMD))
    assert body == {"type": "progress", "message": "nable guard: over budget"}


@pytest.mark.parametrize("verdict", [
    None, {"decision": "ask", "reason": "r"}, {"decision": "deny", "reason": "r"},
    {"decision": "deny", "reason": ""}, {"decision": "warn", "reason": "w"},
    {"decision": "something new", "reason": "r"},
])
@pytest.mark.parametrize("cloud", [False, True])
def test_copilot_never_pre_approves_and_always_explains_a_deny(verdict, cloud):
    body = ga.copilot_response(verdict, cloud=cloud)
    if body is None:
        return
    assert body.get("permissionDecision") != "allow"
    if body.get("permissionDecision") in ("deny", "ask"):
        assert body["permissionDecisionReason"].strip()
    if cloud:
        assert body.get("permissionDecision") != "ask"


def test_copilot_tool_args_as_an_object_and_powershell_are_judged():
    _, body, _ = _run(_copilot(ASK_CMD, as_string=False))
    assert body["permissionDecision"] == "ask"
    _, body, _ = _run(_copilot(ASK_CMD, tool="powershell"))
    assert body["permissionDecision"] == "ask"


@pytest.mark.parametrize("tool", ["edit", "view", "create", "web_fetch", "task"])
def test_copilot_other_tools_are_left_alone(tool, monkeypatch):
    monkeypatch.setattr(g, "gate_command", lambda c, *a, **k: pytest.fail("gate consulted"))
    code, body, _ = _run(_copilot(ASK_CMD, tool=tool))
    assert code == 0 and body is None


def test_copilot_unparseable_tool_args_allow(monkeypatch):
    monkeypatch.setattr(g, "gate_command", lambda c, *a, **k: pytest.fail("gate consulted"))
    code, body, _ = _run({**COPILOT_BASH, "toolArgs": "{not json"})
    assert code == 0 and body is None


# ── Gemini CLI ────────────────────────────────────────────────────────────────

def test_gemini_allow_is_an_explicit_allow():
    """With empty stdout Gemini reads stderr as the answer, so an allow is
    always written out."""
    code, body, _ = _run(_gemini(ALLOW_CMD))
    assert code == 0 and body == {"decision": "allow"}


def test_gemini_ask_becomes_a_deny_that_says_why():
    _, body, _ = _run(_gemini(ASK_CMD))
    assert body["decision"] == "deny"
    assert "cannot reliably pause" in body["reason"]
    assert body["systemMessage"] == body["reason"]


def test_gemini_deny(monkeypatch):
    _deny_mode(monkeypatch)
    _, body, _ = _run(_gemini(DENY_CMD))
    assert body["decision"] == "deny" and body["reason"].startswith("nable guard")


def test_gemini_warn_proceeds_with_a_message():
    assert ga.gemini_response({"decision": "warn", "reason": "nable guard: 99%"}) == {
        "decision": "allow", "systemMessage": "nable guard: 99%"}


@pytest.mark.parametrize("verdict", [
    None, {"decision": "ask", "reason": "r"}, {"decision": "deny", "reason": ""},
    {"decision": "warn", "reason": "w"}, {"decision": "ask", "action_type": "ai_budget", "reason": "b"},
])
def test_gemini_only_gets_documented_values(verdict):
    body = ga.gemini_response(verdict)
    assert body["decision"] in ("allow", "deny")
    assert set(body) <= {"decision", "reason", "systemMessage"}
    if body["decision"] == "deny":
        assert body["reason"].strip()


def test_gemini_other_tools_are_allowed_without_the_gate(monkeypatch):
    monkeypatch.setattr(g, "gate_command", lambda c, *a, **k: pytest.fail("gate consulted"))
    code, body, _ = _run({**GEMINI_SHELL, "tool_name": "write_file",
                          "tool_input": {"file_path": "a", "content": ASK_CMD}})
    assert code == 0 and body == {"decision": "allow"}


def test_gemini_dir_path_is_the_working_directory(monkeypatch):
    seen = {}
    monkeypatch.setattr(g, "gate_command", lambda c, *a, **k: seen.update(k))
    _run({**GEMINI_SHELL, "tool_input": {"command": ALLOW_CMD, "dir_path": "infra"}})
    assert seen["cwd"] == os.path.join(GEMINI_SHELL["cwd"], "infra")
    assert seen["session_id"] == GEMINI_SHELL["session_id"]


# ── Cline ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cli", [False, True])
def test_cline_allow_and_stop(cli):
    _, body, _ = _run(_cline(ALLOW_CMD, cli=cli))
    assert body == {"cancel": False}
    _, body, _ = _run(_cline(ASK_CMD, cli=cli))
    assert body["cancel"] is True
    assert "stopped the task" in body["errorMessage"]


def test_cline_the_worst_of_several_commands_wins(monkeypatch):
    _deny_mode(monkeypatch)
    _, body, _ = _run(_cline(ALLOW_CMD, DENY_CMD, ASK_CMD))
    assert body["cancel"] is True
    # A deny outranks an ask, so the reason is the deny's, without the ask note.
    assert ga._CLINE_NO_ASK.strip() not in body["errorMessage"]


@pytest.mark.parametrize("payload,field,why", [
    (lambda: _gemini(ASK_CMD), ("reason",), "A Gemini CLI hook cannot reliably pause"),
    (lambda: _cline(ASK_CMD), ("errorMessage",), "Cline hooks cannot pause to ask you"),
    (lambda: {"hook_event_name": "PreToolUse", "turn_id": "t", "tool_name": "Bash",
              "session_id": "s", "tool_input": {"command": ASK_CMD}},
     ("hookSpecificOutput", "permissionDecisionReason"), "Codex hooks cannot pause"),
])
def test_a_harness_that_cannot_ask_does_not_say_confirm_to_proceed(payload, field, why):
    """"It cannot be undone; confirm to proceed. ... so nable blocked it
    instead" asked for a confirmation nobody could give."""
    _, body, _ = _run(payload())
    for key in field:
        body = body[key]
    assert "confirm to proceed" not in body.lower()
    assert "It cannot be undone." in body and why in body
    assert body.endswith("run the command yourself.")


def test_a_budget_ask_reads_as_a_sentence_where_it_cannot_ask():
    reason = ("nable guard: This goes over the budget. Confirm to proceed, or raise the "
              "budget. Set FINOPS_GUARD_STOP_ON_BUDGET=1 to make this a hard stop.")
    out = ga._cannot_ask(reason, ga._GEMINI_NO_ASK)
    assert "over the budget. Raise the budget. Set" in out and "onfirm" not in out


@pytest.mark.parametrize("parameters,tool", [
    ({"command": ASK_CMD, "requires_approval": "false"}, "execute_command"),
    ({"commands": ASK_CMD}, "run_commands"),
    ({"commands": json.dumps([{"command": "terraform", "args": ["destroy", "-auto-approve"]}])},
     "run_commands"),
    ({"command": ASK_CMD}, "run_commands"),
    ({"cmd": ASK_CMD}, "run_commands"),
])
def test_cline_command_shapes(parameters, tool):
    payload = {**CLINE_EXT, "preToolUse": {"toolName": tool, "parameters": parameters}}
    _, body, _ = _run(payload)
    assert body["cancel"] is True, parameters


def test_cline_a_command_that_looks_like_json_is_still_a_command():
    """Only map values are decoded; a command string like `true` stays one."""
    assert ga._cline_commands({**CLINE_EXT, "preToolUse": {
        "toolName": "run_commands", "parameters": {"commands": json.dumps(["true", "[1]"])}}}
    ) == ("run_commands", ["true", "[1]"])


def test_cline_other_tools_and_events_are_allowed_without_the_gate(monkeypatch):
    monkeypatch.setattr(g, "gate_command", lambda c, *a, **k: pytest.fail("gate consulted"))
    _, body, _ = _run({**CLINE_EXT, "preToolUse": {"toolName": "read_files",
                                                   "parameters": {"paths": ASK_CMD}}})
    assert body == {"cancel": False}
    _, body, _ = _run({**CLINE_EXT, "hookName": "PostToolUse", "preToolUse": None,
                       "postToolUse": {"toolName": "run_commands", "parameters": {}}})
    assert body == {"cancel": False}


def test_cline_warn_is_context_not_a_stop():
    assert ga.cline_response({"decision": "warn", "reason": "nable guard: 99%"}) == {
        "cancel": False, "contextModification": "nable guard: 99%"}


# ── fail open, in each harness's words ────────────────────────────────────────

@pytest.mark.parametrize("payload,expected", [
    (_copilot(ASK_CMD), None),
    (_gemini(ASK_CMD), {"decision": "allow"}),
    (_cline(ASK_CMD), {"cancel": False}),
])
def test_a_gate_crash_allows_and_says_why(payload, expected, monkeypatch):
    def boom(command, *a, **k):
        raise RuntimeError("policy file exploded")
    monkeypatch.setattr(g, "gate_command", boom)
    code, body, err = _run(payload)
    assert code == 0 and body == expected
    assert "RuntimeError" in err


@pytest.mark.parametrize("harness,expected", [
    ("copilot", None), ("gemini", {"decision": "allow"}), ("cline", {"cancel": False})])
def test_unreadable_input_allows(harness, expected):
    code, body, err = _run("not json", harness=harness)
    assert code == 0 and body == expected and "allowing" in err


@pytest.mark.parametrize("payload", [_copilot(ASK_CMD), _gemini(ASK_CMD), _cline(ASK_CMD)])
def test_stray_prints_cannot_corrupt_the_verdict(payload, monkeypatch):
    real = g.gate_command

    def chatty(command, *a, **k):
        print("debug: loading policy")
        return real(command, *a, **k)
    monkeypatch.setattr(g, "gate_command", chatty)
    code, body, err = _run(payload)
    assert code == 0 and body is not None and "debug: loading policy" in err


@pytest.mark.parametrize("payload,harness", [(_copilot(ASK_CMD), "copilot"),
                                             (_gemini(ASK_CMD), "gemini"),
                                             (_cline(ASK_CMD), "cline")])
def test_verdicts_are_attributed_to_their_harness(payload, harness, monkeypatch):
    seen = {}
    real = g.gate_command

    def spy(command, *a, **k):
        seen.update(k)
        return real(command, *a, **k)
    monkeypatch.setattr(g, "gate_command", spy)
    _run(payload)
    assert seen.get("harness") == harness


def _cli(args, home, stdin=""):
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(SRC),
           "PYTHONDONTWRITEBYTECODE": "1", "NABLE_NO_TELEMETRY": "1"}
    for var in ("CODEX_HOME", "COPILOT_HOME", "GEMINI_CLI_HOME", "CLINE_DIR",
                "COPILOT_AGENT_PROMPT", "GITHUB_COPILOT_API_TOKEN"):
        env.pop(var, None)
    return subprocess.run([sys.executable, "-m", "finops.entry", *args], input=stdin,
                          capture_output=True, text=True, env=env, timeout=60, check=False)


@pytest.mark.parametrize("payload,check", [
    (_copilot(ASK_CMD), lambda b: b["permissionDecision"] == "ask"),
    (_gemini(ASK_CMD), lambda b: b["decision"] == "deny"),
    (_cline(ASK_CMD), lambda b: b["cancel"] is True),
])
def test_the_real_cli_prints_one_json_document(payload, check, tmp_path):
    r = _cli(["guard", "hook"], tmp_path / "home", json.dumps(payload))
    assert r.returncode == 0, r.stderr
    assert check(json.loads(r.stdout)), r.stdout


# ── install: shape ───────────────────────────────────────────────────────────

def test_copilot_install_writes_its_own_file(monkeypatch):
    _uvx(monkeypatch)
    outcome, path = ga.install("copilot", global_scope=False)
    assert outcome == "new" and path == Path.cwd() / ".github" / "hooks" / "nable-guard.json"
    doc = json.loads(path.read_text())
    assert doc["version"] == 1
    [entry] = doc["hooks"]["preToolUse"]
    assert entry["type"] == "command" and entry["matcher"] == "bash|powershell"
    assert entry["timeoutSec"] >= 30
    # A failed hook denies the call in Copilot; the suffix keeps a missing uv
    # or a stale nable from blocking every shell command.
    assert entry["command"] == g._UVX_HOOK_CMD + "; exit 0"


def test_copilot_global_honours_copilot_home(tmp_path, monkeypatch):
    assert ga.hooks_path("copilot", True) == Path.home() / ".copilot" / "hooks" / "nable-guard.json"
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "ch"))
    assert ga.hooks_path("copilot", True) == tmp_path / "ch" / "hooks" / "nable-guard.json"
    assert ga.config_dir("copilot") == tmp_path / "ch"


def test_gemini_install_adds_to_settings_and_keeps_the_rest(monkeypatch):
    _uvx(monkeypatch)
    path = ga.hooks_path("gemini", True)
    path.parent.mkdir(parents=True)
    theirs = {"model": {"name": "gemini-2.5-pro"}, "hooksConfig": {"enabled": True},
              "hooks": {"AfterTool": [{"hooks": [{"type": "command", "command": "./log.sh"}]}]}}
    path.write_text(json.dumps(theirs))
    assert ga.install("gemini", True)[0] == "new"
    doc = json.loads(path.read_text())
    assert doc["model"] == theirs["model"] and doc["hooks"]["AfterTool"] == theirs["hooks"]["AfterTool"]
    [group] = doc["hooks"]["BeforeTool"]
    assert group["matcher"] == "^run_shell_command$"
    [hook] = group["hooks"]
    assert hook["type"] == "command" and hook["name"] == "nable-guard"
    assert hook["timeout"] >= 30000, "Gemini CLI counts milliseconds"
    assert hook["command"].endswith("; exit 0")
    assert ga.uninstall("gemini", True)[0] is True
    assert json.loads(path.read_text()) == theirs


def test_gemini_home_is_honoured(tmp_path, monkeypatch):
    assert ga.hooks_path("gemini", True) == Path.home() / ".gemini" / "settings.json"
    assert ga.hooks_path("gemini", False) == Path.cwd() / ".gemini" / "settings.json"
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "gh"))
    assert ga.hooks_path("gemini", True) == tmp_path / "gh" / ".gemini" / "settings.json"


def test_a_gemini_settings_file_with_comments_is_refused_and_kept():
    """Gemini CLI strips comments when it reads settings.json; rewriting it as
    JSON would drop them, so it is refused like any file nable cannot parse."""
    path = ga.hooks_path("gemini", True)
    path.parent.mkdir(parents=True)
    body = '{\n  // my model\n  "model": {"name": "gemini-2.5-pro"}\n}\n'
    path.write_text(body)
    with pytest.raises(SystemExit) as e:
        ga.install("gemini", True)
    assert "comments" in str(e.value.code) and "nothing was changed" in str(e.value.code)
    assert path.read_text() == body


@pytest.mark.parametrize("harness", ["copilot", "gemini"])
def test_the_fail_safe_suffix_really_exits_zero(harness, monkeypatch, tmp_path):
    """The suffix means the same in sh as it will in bash and PowerShell."""
    _uvx(monkeypatch)
    ga.install(harness, True)
    doc = json.loads(ga.hooks_path(harness, True).read_text())
    cmd = next(c for c in ga._ADAPTERS[harness][2](doc) if ga._is_ours(c))
    missing = cmd.replace("uvx", str(tmp_path / "no-such-uvx"), 1)
    r = subprocess.run(["sh", "-c", missing], capture_output=True, text=True, check=False)
    assert r.returncode == 0 and r.stdout == ""


def test_cline_install_writes_an_executable_script(monkeypatch):
    uvx = _uvx(monkeypatch)
    outcome, path = ga.install("cline", True)
    assert outcome == "new" and path == Path.home() / "Documents" / "Cline" / "Hooks" / "PreToolUse"
    assert os.access(path, os.X_OK), "Cline only runs an executable hook"
    text = path.read_text()
    assert text.startswith("#!/bin/sh\n")
    # A desktop app's PATH may lack uvx, so the user's own copy spells it out.
    assert ga._cline_command_in(text) == f"{uvx} --from finops-mcp=={g.__version__} finops guard hook"
    assert ga.state("cline", True) == "installed"
    assert ga.hooks_path("cline", False) == Path.cwd() / ".clinerules" / "hooks" / "PreToolUse"


@pytest.mark.skipif(sys.platform == "win32", reason="the script is POSIX sh")
@pytest.mark.parametrize("payload,judged", [
    (_cline(ASK_CMD), True),
    (_cline(ASK_CMD, cli=True), True),
    ({**CLINE_EXT, "preToolUse": {"toolName": "execute_command",
                                  "parameters": {"command": ASK_CMD}}}, True),
    ({**CLINE_EXT, "preToolUse": {"toolName": "read_files", "parameters": {"paths": "a"}}}, False),
])
def test_the_cline_script_only_starts_nable_for_shell_calls(payload, judged, tmp_path):
    """Cline has no matcher; the script answers every other tool call itself."""
    fake = f"{sys.executable} -c 'import sys; sys.stdin.read(); print(\"JUDGED\")' # finops guard hook"
    script = tmp_path / "PreToolUse"
    script.write_text(ga._cline_script(fake))
    script.chmod(0o755)
    r = subprocess.run([str(script)], input=json.dumps(payload), capture_output=True,
                       text=True, check=False)
    assert r.returncode == 0
    if judged:
        assert r.stdout.strip() == "JUDGED"
    else:
        assert json.loads(r.stdout) == {"cancel": False}


@pytest.mark.skipif(sys.platform == "win32", reason="the script is POSIX sh")
def test_the_cline_script_allows_when_nable_is_missing(tmp_path):
    script = tmp_path / "PreToolUse"
    script.write_text(ga._cline_script(f"{tmp_path}/no-such-finops guard hook"))
    script.chmod(0o755)
    r = subprocess.run([str(script)], input=json.dumps(_cline(ASK_CMD)), capture_output=True,
                       text=True, check=False)
    assert r.returncode == 0 and r.stdout == ""


@pytest.mark.parametrize("harness", NEW)
def test_the_installed_command_carries_no_harness_flag(harness, monkeypatch):
    _uvx(monkeypatch)
    ga.install(harness, True)
    assert "--harness" not in ga.hooks_path(harness, True).read_text().replace(
        "nable guard install --harness cline", "").replace("nable guard uninstall --harness cline", "")


# ── install: safety ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("harness", NEW)
def test_install_is_idempotent_and_uninstall_leaves_nothing(harness, monkeypatch):
    _uvx(monkeypatch)
    _, path = ga.install(harness, True)
    first = path.read_bytes()
    for _ in range(3):
        assert ga.install(harness, True)[0] == "already"
    assert path.read_bytes() == first
    assert ga.uninstall(harness, True)[0] is True
    assert ga.uninstall(harness, True)[0] is False
    if harness in ("copilot", "cline"):
        assert not path.exists(), "the file was ours alone and should be gone"
    else:
        assert json.loads(path.read_text()) == {"hooks": {}}


def test_copilot_uninstall_keeps_a_file_someone_added_to(monkeypatch):
    _uvx(monkeypatch)
    _, path = ga.install("copilot", False)
    doc = json.loads(path.read_text())
    doc["hooks"]["sessionStart"] = [{"type": "command", "bash": "./banner.sh"}]
    path.write_text(json.dumps(doc))
    assert ga.uninstall("copilot", False)[0] is True
    assert json.loads(path.read_text()) == {"version": 1, "hooks": {
        "sessionStart": [{"type": "command", "bash": "./banner.sh"}]}}


def test_a_copilot_entry_under_the_bash_key_is_ours_everywhere(monkeypatch):
    """status read `command or bash`, install and uninstall read `command`
    only: a hand-written bash entry showed as installed, install added a
    second hook beside it, and uninstall left it running."""
    _uvx(monkeypatch)
    path = ga.hooks_path("copilot", False)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "hooks": {"preToolUse": [
        {"type": "command", "bash": f"uvx --from {g._PYPI_NAME} finops guard hook"}]}}))
    assert ga.state("copilot", False) == "installed"
    assert ga.install("copilot", False)[0] == "repinned"
    [entry] = json.loads(path.read_text())["hooks"]["preToolUse"]
    assert "command" not in entry and entry["bash"] == g._UVX_HOOK_CMD + "; exit 0"
    assert ga.install("copilot", False)[0] == "already"
    assert ga.uninstall("copilot", False)[0] is True
    assert ga.state("copilot", False) == "absent"


@pytest.mark.parametrize("harness,body,described_as", [
    ("copilot", '{"version": 2, "hooks": {}}', "version 2"),
    ("copilot", '{"version": 1, "hooks": {"preToolUse": {"bash": "x"}}}', "dict"),
    ("gemini", '{"hooks": {"BeforeTool": "x"}}', "str"),
    ("gemini", '["a"]', "list"),
])
def test_unexpected_shapes_are_refused(harness, body, described_as):
    path = ga.hooks_path(harness, True)
    path.parent.mkdir(parents=True)
    path.write_text(body)
    with pytest.raises(SystemExit) as e:
        ga.install(harness, True)
    assert described_as in str(e.value.code)
    assert path.read_text() == body


def test_cline_never_replaces_someone_elses_hook(monkeypatch):
    _uvx(monkeypatch)
    path = ga.hooks_path("cline", False)
    path.parent.mkdir(parents=True)
    body = "#!/usr/bin/env bash\necho '{\"cancel\": false}'\n"
    path.write_text(body)
    with pytest.raises(SystemExit) as e:
        ga.install("cline", False)
    assert "Cline runs one per directory" in str(e.value.code)
    assert path.read_text() == body
    assert ga.uninstall("cline", False)[0] is False and path.read_text() == body
    assert ga.state("cline", False) == "absent"


def test_cline_a_script_that_lost_its_execute_bit_is_repaired(monkeypatch):
    _uvx(monkeypatch)
    _, path = ga.install("cline", True)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert ga.state("cline", True) == "broken"
    assert ga.install("cline", True)[0] == "repaired"
    assert os.access(path, os.X_OK) and ga.state("cline", True) == "installed"


def test_cline_a_dead_command_is_repaired(monkeypatch):
    _uvx(monkeypatch)
    _, path = ga.install("cline", True)
    path.write_text(ga._cline_script("/gone/uv/archive-v0/x/bin/finops guard hook"))
    path.chmod(0o755)
    assert ga.state("cline", True) == "broken"
    assert ga.install("cline", True)[0] == "repaired"
    assert "/gone/" not in path.read_text()


def test_cline_on_windows_is_refused_not_guessed(monkeypatch):
    monkeypatch.setattr(ga.sys, "platform", "win32")
    with pytest.raises(SystemExit) as e:
        ga.install("cline", True)
    assert "PreToolUse.ps1" in str(e.value.code)
    (Path.home() / "Documents" / "Cline").mkdir(parents=True)
    assert "cline" not in ga.detected()


@pytest.mark.parametrize("harness", ["copilot", "gemini"])
def test_malformed_json_is_refused_backed_up_and_left_alone(harness):
    path = ga.hooks_path(harness, True)
    path.parent.mkdir(parents=True)
    body = b'{"hooks": { NOT JSON'
    path.write_bytes(body)
    path.chmod(0o600)
    with pytest.raises(SystemExit) as e:
        ga.install(harness, True)
    assert path.read_bytes() == body
    backups = list(path.parent.glob(f"{path.name}.nable-backup-*"))
    if harness == "gemini":
        # Nothing is rewritten, and settings.json holds MCP server tokens: no
        # copy for anyone to find, and no claim of one.
        assert backups == [] and "copy" not in str(e.value.code)
    else:
        [backup] = backups
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600, "a copy wider than the file"


@pytest.mark.parametrize("harness", ["cursor", "codex", "copilot", "gemini"])
def test_an_empty_file_is_an_empty_config(harness, monkeypatch):
    """A zero-byte file (a `touch`) holds nothing to lose: install into it."""
    _uvx(monkeypatch)
    path = ga.hooks_path(harness, True)
    path.parent.mkdir(parents=True)
    path.write_text("")
    assert ga.install(harness, True)[0] == "new"
    assert ga.state(harness, True) == "installed"
    assert not list(path.parent.glob("*.nable-backup-*"))


@pytest.mark.parametrize("harness", NEW)
def test_state_never_raises(harness):
    path = ga.hooks_path(harness, True)
    path.parent.mkdir(parents=True)
    for body in ("[]", "null", "not json", '{"hooks": {"preToolUse": [null, 3]}}',
                 '{"hooks": {"BeforeTool": [{"hooks": [null]}]}}', "#!/bin/sh\n"):
        path.write_text(body)
        assert ga.state(harness, True) == "absent"
        assert ga.pin_state(harness, True) is None


# ── --all, status and doctor ──────────────────────────────────────────────────

def test_new_agents_are_detected_by_their_config_directory(monkeypatch):
    assert ga.detected() == []
    (Path.home() / ".gemini").mkdir()
    (Path.home() / ".copilot").mkdir()
    (Path.home() / "Documents" / "Cline").mkdir(parents=True)
    assert ga.detected() == ["copilot", "gemini", "cline"]


def test_the_cline_cli_directory_counts_too():
    (Path.home() / ".cline").mkdir()
    assert "cline" in ga.detected()


def test_install_all_includes_the_new_agents_found(capsys, monkeypatch):
    _uvx(monkeypatch)
    (Path.home() / ".gemini").mkdir()
    code = ga.cli("install", harness=None, everything=True, global_scope=True)
    out = capsys.readouterr().out
    assert code == 0
    assert ga.state("gemini", True) == "installed"
    assert not (Path.home() / ".copilot").exists()
    assert "Gemini CLI" in out and "GitHub Copilot" in out and "not found" in out
    assert "What it does:" in out


def test_single_harness_cli_and_status(capsys, monkeypatch):
    _uvx(monkeypatch)
    for name in NEW:
        assert ga.cli("install", harness=name, everything=False, global_scope=False) == 0
    out = capsys.readouterr().out
    assert "nable guard uninstall --harness cline" in out
    lines = "\n".join(ga.status_lines())
    assert "GitHub Copilot" in lines and "Gemini CLI" in lines and "Cline" in lines


def test_the_real_cli_installs_and_removes_a_new_harness(tmp_path):
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    r = _cli(["guard", "install", "--harness", "gemini", "--global"], home)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "BeforeTool" in (home / ".gemini" / "settings.json").read_text()
    r = _cli(["guard", "status"], home)
    assert r.returncode == 0 and "Gemini CLI" in r.stdout
    r = _cli(["guard", "uninstall", "--harness", "gemini", "--global"], home)
    assert r.returncode == 0 and "removed" in r.stdout


def test_doctor_names_a_new_agent_without_a_hook_and_covers_one_with():
    (Path.home() / ".gemini").mkdir()
    d = g.doctor()
    assert "Gemini CLI: on this machine, no working guard hook" in d["not_covered"]
    assert "nable guard install --harness gemini" in d["recommendations"]


def test_doctor_covers_an_installed_new_agent(monkeypatch):
    _uvx(monkeypatch)
    ga.install("copilot", True)
    d = g.doctor()
    assert any(c.startswith("GitHub Copilot: shell commands") for c in d["covered"])


# ── Cursor and Codex: MCP tool calls ──────────────────────────────────────────

def _cursor_mcp(tool: str, args: dict, server: str | None = "aws") -> dict:
    p = {**CURSOR_MCP, "tool_name": tool, "tool_input": json.dumps(args)}
    if server:
        p["mcp_server_name"] = server
    return p


@pytest.mark.parametrize("server", ["aws", None])
def test_cursor_mcp_one_way_door_asks(server):
    """guard_mcp matches the tool part only, so the server's name is not needed."""
    _, body, _ = _run(_cursor_mcp("call_aws", {"cli_command": TERMINATE}, server))
    assert body["permission"] == "ask"
    assert "terminate-instances" in body["user_message"]


def test_cursor_mcp_read_and_unknown_tools_are_allowed():
    _, body, _ = _run(_cursor_mcp("call_aws", {"cli_command": "aws s3 ls"}))
    assert body == {"permission": "allow"}
    _, body, _ = _run(_cursor_mcp("delete_everything", {"x": 1}, "mystery"))
    assert body == {"permission": "allow"}


def test_cursor_mcp_calls_are_attributed(monkeypatch):
    seen = {}
    real = g.gate_mcp_call

    def spy(name, args, **k):
        seen.update(k, name=name, args=args)
        return real(name, args, **k)
    monkeypatch.setattr(g, "gate_mcp_call", spy)
    _run(_cursor_mcp("call_aws", {"cli_command": TERMINATE}))
    assert seen["harness"] == "cursor" and seen["name"] == "mcp__aws__call_aws"
    assert seen["args"] == {"cli_command": TERMINATE}


def test_cursor_warn_is_shown_without_stopping():
    body = ga.cursor_response({"decision": "warn", "reason": "nable guard: 99% of threshold"})
    assert body == {"permission": "allow", "user_message": "nable guard: 99% of threshold",
                    "agent_message": "nable guard: 99% of threshold"}


def test_codex_mcp_one_way_door_is_a_deny_that_says_why():
    payload = {**CODEX_BASH, "tool_name": "mcp__aws__call_aws",
               "tool_input": {"cli_command": TERMINATE}}
    _, body, _ = _run(payload)
    out = body["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "cannot pause" in out["permissionDecisionReason"]


def test_codex_unknown_mcp_tools_and_non_shell_names_are_silent():
    _, body, _ = _run({**CODEX_BASH, "tool_name": "mcp__memory__create_entities",
                       "tool_input": {"entities": []}})
    assert body is None
    # Every Codex shell path reports "Bash" (codex-rs hook_names.rs); a
    # "shell" tool name is not something Codex sends.
    _, body, _ = _run({**CODEX_BASH, "tool_name": "shell", "tool_input": {"command": ASK_CMD}})
    assert body is None


def test_cursor_install_registers_both_events_and_uninstall_removes_both(monkeypatch):
    _uvx(monkeypatch)
    ga.install("cursor", True)
    doc = json.loads(ga.hooks_path("cursor", True).read_text())
    assert len(doc["hooks"]["beforeShellExecution"]) == 1
    [mcp] = doc["hooks"]["beforeMCPExecution"]
    assert mcp["failClosed"] is False and ga._is_ours(mcp["command"])
    assert ga.sees_mcp("cursor", True) is True
    assert ga.uninstall("cursor", True)[0] is True
    assert json.loads(ga.hooks_path("cursor", True).read_text()) == {"version": 1, "hooks": {}}


def test_an_old_cursor_install_gains_mcp_coverage(monkeypatch):
    _uvx(monkeypatch)
    ga.install("cursor", True)
    path = ga.hooks_path("cursor", True)
    doc = json.loads(path.read_text())
    del doc["hooks"]["beforeMCPExecution"]
    path.write_text(json.dumps(doc))
    assert ga.sees_mcp("cursor", True) is False
    d = g.doctor()
    assert "Cursor: MCP tool calls (the hook only sees shell commands)" in d["not_covered"]
    assert ga.install("cursor", True)[0] == "repaired"
    assert ga.sees_mcp("cursor", True) is True


def test_an_old_codex_matcher_is_widened_and_a_hand_written_one_is_not(monkeypatch):
    _uvx(monkeypatch)
    path = ga.hooks_path("codex", True)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "^Bash$", "hooks": [{"type": "command", "command": g._UVX_HOOK_CMD}]}]}}))
    assert ga.sees_mcp("codex", True) is False
    assert ga.install("codex", True)[0] == "repaired"
    assert json.loads(path.read_text())["hooks"]["PreToolUse"][0]["matcher"] == "^(Bash|mcp__.*)$"
    assert ga.sees_mcp("codex", True) is True

    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": g._UVX_HOOK_CMD}]}]}}))
    assert ga.install("codex", True)[0] == "repaired"      # the bare command gains || exit 0
    assert json.loads(path.read_text())["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert ga.install("codex", True)[0] == "already"


# ── project files stay portable ───────────────────────────────────────────────

@pytest.mark.parametrize("harness", ["cursor", "cline"])
def test_a_project_hook_uses_the_path_not_this_machines_uvx(harness, monkeypatch):
    """A project file is shared with teammates, whose uvx lives elsewhere."""
    uvx = _uvx(monkeypatch)
    _, path = ga.install(harness, False)
    assert uvx not in path.read_text()
    assert g._UVX_HOOK_CMD in path.read_text()
    _, gpath = ga.install(harness, True)
    assert uvx in gpath.read_text()


def test_a_project_cursor_hook_with_this_machines_uvx_is_rewritten(monkeypatch):
    uvx = _uvx(monkeypatch)
    path = ga.hooks_path("cursor", False)
    path.parent.mkdir(parents=True)
    local = f"{uvx} --from finops-mcp=={g.__version__} finops guard hook"
    path.write_text(json.dumps({"version": 1, "hooks": {
        "beforeShellExecution": [{"command": local, "timeout": 30, "failClosed": False}],
        "beforeMCPExecution": [{"command": local, "timeout": 30, "failClosed": False}]}}))
    assert ga.install("cursor", False)[0] == "repaired"
    assert uvx not in path.read_text()


# ── pins ─────────────────────────────────────────────────────────────────────

V = g.__version__


@pytest.mark.parametrize("cmd,expected", [
    (f"uvx --from finops-mcp=={V} finops guard hook", "pinned"),
    ("uvx --from finops-mcp finops guard hook", "unpinned"),
    ("uvx --from=finops-mcp finops guard hook", "unpinned"),
    ("uvx finops-mcp guard hook", "unpinned"),
    ("uvx --python 3.12 --from finops-mcp finops guard hook", "unpinned"),
    ("uvx --python=3.12 --from finops-mcp==0.0.1 finops guard hook", "other"),
    ("/Users/dev/.local/bin/uvx --from finops-mcp finops guard hook", "unpinned"),
    ('"/Applications/My Tools/uvx" --from finops-mcp==0.0.1 finops guard hook', "other"),
    (f"uvx --from finops-mcp@{V} finops guard hook", "pinned"),
    ("uvx --from finops-mcp@latest finops guard hook", "unpinned"),
    ("uv tool run --from finops-mcp finops guard hook", "unpinned"),
    ("uvx --from finops-mcp finops guard hook; exit 0", "unpinned"),
    (f"uvx --from finops-mcp=={V} finops guard hook; exit 0", "pinned"),
    ("/usr/local/bin/finops guard hook", None),
    ("uvx --from other-tool finops guard hook", None),
    ("uvx 'unterminated", None),
    (None, None),
])
def test_uvx_pin_reads_the_arguments_not_the_spelling(cmd, expected):
    assert ga.uvx_pin(cmd) == expected


def _write_hook(harness: str, cmd: str) -> Path:
    path = ga.hooks_path(harness, True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if harness == "cursor":
        doc = {"version": 1, "hooks": {e: [{"command": cmd, "timeout": 30, "failClosed": False}]
                                       for e in ("beforeShellExecution", "beforeMCPExecution")}}
    else:
        doc = {"hooks": {"PreToolUse": [{"matcher": "^(Bash|mcp__.*)$",
                                         "hooks": [{"type": "command", "command": cmd}]}]}}
    path.write_text(json.dumps(doc))
    return path


@pytest.mark.parametrize("harness", ["cursor", "codex"])
@pytest.mark.parametrize("old,pin", [
    ("uvx --from finops-mcp finops guard hook", "unpinned"),
    ("uvx --python 3.12 --from finops-mcp finops guard hook", "unpinned"),
    ("uvx --from finops-mcp==0.0.1 finops guard hook", "other"),
])
def test_an_unpinned_hook_is_reported_and_repinned(harness, old, pin, monkeypatch, capsys):
    _uvx(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda n: None)     # bare uvx form for both
    path = _write_hook(harness, old)
    monkeypatch.setattr(ga, "_runnable", lambda c: True)
    assert ga.state(harness, True) == "installed"
    assert ga.pin_state(harness, True) == pin
    lines = "\n".join(ga.status_lines())
    assert f"nable guard install --harness {harness} --global" in lines
    d = g.doctor()
    assert any(f.startswith(f"nable guard install --harness {harness} --global")
               for f in d["recommendations"])

    assert ga.cli("install", harness=harness, everything=False, global_scope=True) == 0
    assert f"pinned to finops-mcp=={V}" in capsys.readouterr().out
    assert ga.pin_state(harness, True) == "pinned"
    assert old not in path.read_text()
    assert ga.install(harness, True)[0] == "already"


def test_a_binary_hook_is_never_repinned(monkeypatch):
    _uvx(monkeypatch)
    exe = Path.home() / "bin" / "finops"
    exe.parent.mkdir()
    exe.touch(mode=0o755)
    path = _write_hook("codex", f"{exe} guard hook")
    before = path.read_text()
    assert ga.pin_state("codex", True) == "binary"
    assert ga.install("codex", True)[0] == "already"
    assert path.read_text() == before


def test_doctor_repairs_a_broken_global_hook_in_its_own_scope(monkeypatch):
    """A bare install would add a project hook and leave the dead global one."""
    _uvx(monkeypatch)
    _write_hook("codex", "/gone/uv/archive-v0/x/bin/finops guard hook")
    assert ga.state("codex", True) == "broken"
    d = g.doctor()
    assert "Codex CLI (global): the hooked command no longer exists" in d["not_covered"]
    assert any(f.startswith("nable guard install --harness codex --global")
               for f in d["recommendations"])
    assert all("--global" in f for f in d["recommendations"] if "--harness codex" in f)
    assert "nable guard install --harness codex --global" in "\n".join(ga.status_lines())
