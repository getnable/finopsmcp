"""The agent guard in harnesses other than Claude Code.

guard.py speaks Claude Code's PreToolUse protocol. This module carries the same
verdicts (guard.gate_command) into the hook protocols of other coding agents:

  Cursor     beforeShellExecution, in ~/.cursor/hooks.json or .cursor/hooks.json
  Codex CLI  PreToolUse on Bash, in $CODEX_HOME/hooks.json or .codex/hooks.json

Where each protocol is written down, so the next person can re-check it when a
harness changes:

  Cursor  https://cursor.com/docs/hooks (payload, response, exit codes)
          https://github.com/cursor/cookbook/tree/main/hooks (first-party
          hooks.json and a beforeShellExecution script answering "allow")
  Codex   https://github.com/openai/codex, in codex-rs/:
          hooks/src/events/pre_tool_use.rs  (stdin payload, accepted output)
          config/src/hook_config.rs          (hooks.json schema)
          core/src/tools/hook_names.rs       (shell calls are tool_name "Bash")
          features/src/lib.rs                (hooks are Stable, on by default)

Every harness runs the SAME command, `finops guard hook`, and the payload says
which harness sent it. A `--harness` flag in the installed command would read
more plainly, but an older nable that predates the flag rejects it with
argparse's exit status 2, and both Cursor and Codex read exit 2 as "block".
A stale uvx cache running yesterday's nable would then stop every shell
command the agent tries. Detection has no such failure: an older nable given a
payload it does not recognise stays silent, which both harnesses read as allow.

Fails open everywhere, like the Claude hook: an adapter error allows the
command and says why on stderr. stdout carries the harness's JSON and nothing
else.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from typing import Any

from . import guard

HARNESSES = ("claude", "cursor", "codex")
LABELS = {"claude": "Claude Code", "cursor": "Cursor", "codex": "Codex CLI"}

# Cursor names its events itself, so these alone identify a Cursor payload.
_CURSOR_SHELL = "beforeShellExecution"
_CURSOR_MCP = "beforeMCPExecution"
# Cursor's documented neutral answer. Its own cookbook audit hook returns
# exactly this for a command it has no opinion on.
_CURSOR_ALLOW = {"permission": "allow"}

# Codex has no "ask" in PreToolUse: it logs permissionDecision "ask" as an
# unsupported value and runs the command anyway. A one-way door the policy wants
# a human to see must not quietly go through, so it becomes a deny that says so.
_CODEX_NO_ASK = (" Codex hooks cannot pause for a confirmation, so nable blocked it "
                 "instead. If you intend it, run the command yourself.")


# ── Payload detection ──────────────────────────────────────────────────────────

def detect_harness(payload: dict) -> str:
    """Which harness sent this hook payload: "cursor", "codex" or "claude"."""
    event = payload.get("hook_event_name")
    if event in (_CURSOR_SHELL, _CURSOR_MCP):
        return "cursor"
    # Cursor's own cookbook audit hook still infers the event from the fields
    # when hook_event_name is missing; a bare top-level command is a shell call.
    if event is None and "tool_name" not in payload and isinstance(payload.get("command"), str):
        return "cursor"
    # Codex's PreToolUse input is Claude-shaped on purpose (its engine is named
    # ClaudeHooksEngine), so the event name cannot separate the two. turn_id
    # can: Codex always sends it and Claude Code's documented input has none.
    # Anything ambiguous stays Claude, which is what `guard hook` always was.
    if event == "PreToolUse" and "turn_id" in payload:
        return "codex"
    return "claude"


# ── Verdict to response ────────────────────────────────────────────────────────

def _decision(verdict: dict[str, Any] | None) -> tuple[str, str]:
    """("allow"|"ask"|"deny", reason). Anything unrecognised reads as allow."""
    if not verdict:
        return "allow", ""
    decision = verdict.get("decision")
    if decision not in ("ask", "deny"):
        return "allow", ""
    return decision, str(verdict.get("reason") or "nable guard: a human must review this action.")


def cursor_response(verdict: dict[str, Any] | None) -> dict[str, Any]:
    """Cursor's beforeShellExecution answer: permission plus the two messages."""
    decision, reason = _decision(verdict)
    if decision == "allow":
        return dict(_CURSOR_ALLOW)
    return {"permission": decision, "user_message": reason, "agent_message": reason}


def codex_response(verdict: dict[str, Any] | None) -> dict[str, Any] | None:
    """Codex's PreToolUse answer, or None for no output (Codex's no-op).

    Codex only honours deny, and requires a non-empty reason with it. An
    over-budget "ask" is the notify mode the user chose at install, so it
    becomes a visible warning rather than a stop; any other ask is a deny.
    """
    decision, reason = _decision(verdict)
    if decision == "allow":
        return None
    if decision == "ask" and (verdict or {}).get("action_type") == "ai_budget":
        return {"systemMessage": reason}
    if decision == "ask":
        reason += _CODEX_NO_ASK
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _respond_cursor(payload: dict) -> dict[str, Any]:
    # gate_command judges shell commands. An MCP call only reaches this if
    # someone wired beforeMCPExecution by hand, and it gets the neutral answer.
    if payload.get("hook_event_name") == _CURSOR_MCP:
        return dict(_CURSOR_ALLOW)
    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        return dict(_CURSOR_ALLOW)
    return cursor_response(guard.gate_command(command))


def _respond_codex(payload: dict) -> dict[str, Any] | None:
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if isinstance(command, list):         # an argv-style shell call
        command = " ".join(str(c) for c in command)
    if not isinstance(command, str) or not command.strip():
        return None
    return codex_response(guard.gate_command(command))


_RESPONDERS = {"cursor": _respond_cursor, "codex": _respond_codex}


# ── Hook entry point ───────────────────────────────────────────────────────────

def _fail_open(harness: str | None, stdout: Any, stderr: Any, why: str) -> int:
    """Allow, in the harness's own words, and say why where a human can see it."""
    try:
        print(f"nable guard: allowing, {why}", file=stderr)
        if harness == "cursor":
            stdout.write(json.dumps(_CURSOR_ALLOW))
    except Exception:
        pass
    return 0


def run_hook(harness: str | None = None, stdin: Any = None, stdout: Any = None,
             stderr: Any = None) -> int:
    """`finops guard hook`: read one hook payload, answer in its harness's format.

    `harness` forces a format; None detects it from the payload. Claude Code
    payloads go to guard.run_hook unchanged, which owns that protocol. Always
    exits 0: a guard bug must never be the reason an agent cannot run a command.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        raw = stdin.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TypeError(f"a JSON {type(payload).__name__}, not an object")
    except Exception as e:
        return _fail_open(harness, stdout, stderr, f"the hook input was unreadable ({e})")

    name = harness or detect_harness(payload)
    # Anything the gate prints by accident (a library warning, a stray debug
    # line) goes to stderr: one extra line on stdout and the harness cannot
    # parse the verdict at all.
    try:
        with contextlib.redirect_stdout(stderr):
            if name == "claude":
                return guard.run_hook(io.StringIO(raw), stdout)
            response = _RESPONDERS[name](payload)
    except Exception as e:
        return _fail_open(name, stdout, stderr, f"{type(e).__name__}: {e}")
    if response is not None:
        stdout.write(json.dumps(response))
    return 0
