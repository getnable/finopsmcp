"""The agent guard in harnesses other than Claude Code.

guard.py speaks Claude Code's PreToolUse protocol. This module carries the same
verdicts (guard.gate_command) into the hook protocols of other coding agents,
and installs the hook into their config files with the same safety bar:

  Cursor          beforeShellExecution and beforeMCPExecution, in ~/.cursor/hooks.json
                  or .cursor/hooks.json
  Codex CLI       PreToolUse on Bash and mcp__*, in $CODEX_HOME/hooks.json or
                  .codex/hooks.json
  GitHub Copilot  preToolUse on bash/powershell, in a file of our own:
                  ~/.copilot/hooks/nable-guard.json or .github/hooks/nable-guard.json
                  (the repository file also reaches the Copilot cloud agent)
  Gemini CLI      BeforeTool on run_shell_command, in ~/.gemini/settings.json or
                  .gemini/settings.json
  Cline           an executable PreToolUse script, in ~/Documents/Cline/Hooks/ or
                  .clinerules/hooks/

Where each protocol is written down, so the next person can re-check it when a
harness changes:

  Cursor  https://cursor.com/docs/hooks (payload, response, exit codes)
          https://github.com/cursor/cookbook/tree/main/hooks (first-party
          hooks.json and a beforeShellExecution script answering "allow")
  Codex   https://github.com/openai/codex, in codex-rs/:
          hooks/src/events/pre_tool_use.rs  (stdin payload, accepted output)
          config/src/hook_config.rs          (hooks.json schema)
          core/src/tools/hook_names.rs       (shell calls are tool_name "Bash")
          core/src/tools/handlers/mcp.rs     (MCP tools are mcp__<server>__<tool>
                                             with their raw arguments)
          features/src/lib.rs                (hooks are Stable, on by default)
  Copilot https://docs.github.com/en/copilot/reference/hooks-reference, source
          github/docs content/copilot/reference/hooks-reference.md (locations,
          file format, camelCase preToolUse payload, permissionDecision output,
          "ask" treated as "deny" under the cloud agent, fail-closed exit codes,
          progress lines, COPILOT_HOME, the cloud agent's environment) and
          content/copilot/tutorials/copilot-cli-hooks.md (toolArgs arrives as
          a JSON string)
  Gemini  https://github.com/google-gemini/gemini-cli:
          docs/hooks/reference.md, docs/hooks/index.md (settings locations,
                                             schema, payload, decision/reason)
          packages/core/src/hooks/types.ts         (event names, HookOutput)
          packages/core/src/hooks/hookRunner.ts    (a command runs in a shell;
                                             empty stdout means stderr is read as
                                             the answer; exit 1 warns, any other
                                             non-zero exit blocks)
          packages/core/src/scheduler/hook-utils.ts, scheduler.ts ("ask" waits
                                             for a confirmation that a headless
                                             run has no one to give)
          packages/core/src/utils/paths.ts         (GEMINI_CLI_HOME)
          packages/core/src/tools/definitions/base-declarations.ts
                                            (the shell tool is run_shell_command)
  Cline   https://github.com/cline/cline:
          .clinerules/hooks/README.md              (locations, payload, output)
          apps/vscode/src/core/hooks/utils.ts      (one PreToolUse file per
                                             directory, extensionless on Unix,
                                             PreToolUse.ps1 on Windows)
          apps/vscode/src/sdk/hooks-adapter.ts     (cancel stops the run; errors
                                             allow; hooks on unless turned off)
          sdk/packages/core/src/hooks/hook-file-hooks.ts (the CLI sends the same
                                             preToolUse block plus tool_call)
          sdk/packages/shared/src/storage/paths.ts (both read ~/Documents/Cline/Hooks)
          sdk/packages/core/src/extensions/tools/schemas.ts (run_commands input)

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
import errno
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import guard

HARNESSES = ("claude", "cursor", "codex", "copilot", "gemini", "cline")
LABELS = {"claude": "Claude Code", "cursor": "Cursor", "codex": "Codex CLI",
          "copilot": "GitHub Copilot", "gemini": "Gemini CLI", "cline": "Cline"}
_LABEL_WIDTH = max(len(v) for v in LABELS.values())

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
# Codex matchers are regexes against tool_name. Shell calls are "Bash"; MCP
# tools are mcp__<server>__<tool>, the same shape guard.gate_mcp_call reads.
_CODEX_MATCHER = "^(Bash|mcp__.*)$"
_CODEX_LEGACY_MATCHER = "^Bash$"          # what releases before MCP coverage wrote

# The Copilot cloud agent has no user: its docs say a preToolUse "ask" is
# treated as "deny". Copilot CLI shows a prompt for it, so only the cloud agent
# gets the deny, with a reason that says why. The cloud agent's sandbox sets
# COPILOT_AGENT_PROMPT (and the two tokens) for every job, per the hooks
# reference's "Cloud agent execution environment" table.
_COPILOT_CLOUD_ENV = ("COPILOT_AGENT_PROMPT", "GITHUB_COPILOT_API_TOKEN")
_COPILOT_SHELLS = ("bash", "powershell")
_COPILOT_NO_ASK = (" The Copilot cloud agent has no one to ask for a confirmation, so nable "
                   "blocked it instead. If you intend it, run the command yourself.")
# The file name is ours: Copilot loads every *.json in its hooks directories,
# so the guard never has to share (or rewrite) a file another tool owns.
_COPILOT_FILE = "nable-guard.json"

# Gemini CLI's source honours a BeforeTool "ask" with a confirmation prompt,
# but its docs do not list it and a headless run (`gemini -p`) has no listener
# for that prompt, so the tool call would wait forever. A deny that says why
# is the answer that behaves the same in every mode.
_GEMINI_EVENT = "BeforeTool"
_GEMINI_SHELL = "run_shell_command"
_GEMINI_ALLOW = {"decision": "allow"}
_GEMINI_NO_ASK = (" A Gemini CLI hook cannot reliably pause for a confirmation (a headless "
                  "run would wait forever), so nable blocked it instead. If you intend it, "
                  "run the command yourself.")
_GEMINI_NAME = "nable-guard"

# Cline's only answer is cancel, which stops the task; there is no ask.
_CLINE_SHELLS = ("run_commands", "execute_command")
_CLINE_ALLOW = {"cancel": False}
_CLINE_NO_ASK = (" Cline hooks cannot pause for a confirmation, so nable stopped the task "
                 "instead. If you intend it, run the command yourself.")
_CLINE_FILE = "PreToolUse"
_CLINE_MARKER = "# nable guard hook for Cline"


# ── Payload detection ──────────────────────────────────────────────────────────

def detect_harness(payload: dict) -> str:
    """Which harness sent this hook payload: "cursor", "codex", "copilot",
    "gemini", "cline" or "claude"."""
    event = payload.get("hook_event_name")
    # Gemini CLI names its events itself; no other harness sends "BeforeTool".
    if event == _GEMINI_EVENT:
        return "gemini"
    # Copilot's camelCase preToolUse input has no event name at all, and is
    # the only one with toolName + toolArgs (every other harness is snake_case).
    if event is None and "toolName" in payload and "toolArgs" in payload:
        return "copilot"
    # Every Cline payload, from the VS Code extension and from the CLI, carries
    # clineVersion and hookName.
    if event is None and "clineVersion" in payload and "hookName" in payload:
        return "cline"
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
        # A warn (a priced change near the policy threshold) proceeds, and
        # Cursor can say so without stopping: allow, with the two messages.
        note = _notice(verdict)
        if note:
            return {**_CURSOR_ALLOW, "user_message": note, "agent_message": note}
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
        # "warn" (a priced change near the policy threshold) proceeds; Codex
        # can show it without stopping, so it does.
        if (verdict or {}).get("decision") == "warn" and verdict.get("reason"):
            return {"systemMessage": str(verdict["reason"])}
        return None
    if decision == "ask" and (verdict or {}).get("action_type") == "ai_budget":
        return {"systemMessage": reason}
    if decision == "ask":
        reason += _CODEX_NO_ASK
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _mcp_arguments(raw: Any) -> dict | None:
    """MCP tool arguments as a dict: Cursor sends them as a JSON string,
    Codex as the object itself."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def _cursor_mcp_name(payload: dict) -> str | None:
    """Cursor's MCP tool as `mcp__<server>__<tool>`, the name gate_mcp_call reads.

    Cursor sends the bare tool name. guard_mcp matches the TOOL part only (a
    server key is whatever the user called it), so the server part only has
    to be present: mcp_server_name when Cursor sends it, "cursor" otherwise.
    A name that already carries the prefix is used as it is."""
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return None
    if tool.startswith("mcp__"):
        return tool
    server = payload.get("mcp_server_name")
    server = server if isinstance(server, str) and server else "cursor"
    return f"mcp__{server}__{tool}"


def _respond_cursor(payload: dict) -> dict[str, Any]:
    if payload.get("hook_event_name") == _CURSOR_MCP:
        # An MCP call is judged by what it amounts to: a Terraform, AWS or
        # Kubernetes tool is translated to its shell equivalent, anything else
        # is not the guard's business and gets the neutral answer.
        name = _cursor_mcp_name(payload)
        if name is None:
            return dict(_CURSOR_ALLOW)
        session = payload.get("conversation_id")
        return cursor_response(guard.gate_mcp_call(
            name, _mcp_arguments(payload.get("tool_input")), harness="cursor",
            session_id=session if isinstance(session, str) else None))
    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        return dict(_CURSOR_ALLOW)
    return cursor_response(guard.gate_command(command, session_id=_cursor_session(payload),
                                              harness="cursor", cwd=payload.get("cwd"),
                                              tool="shell"))


def _cursor_session(payload: dict) -> str:
    """The conversation_id, when the budget can measure that conversation.

    Cursor's usage is only readable through its Admin API, whose usage events
    carry conversationId (harness_usage). Without a key there is nothing to
    measure the conversation against, and no session at all is passed: the
    budget gate would otherwise guess the latest transcript, which is some
    other agent's session, and hold Cursor to that session's cap as "this
    session". The monthly budget still applies.
    """
    from . import ai_budget, harness_usage

    conv = payload.get("conversation_id")
    if isinstance(conv, str) and conv.strip() and harness_usage.cursor_enabled():
        return conv.strip()
    return ai_budget.NO_SESSION


def _respond_codex_mcp(payload: dict) -> dict[str, Any] | None:
    # Codex names an MCP tool mcp__<server>__<tool> in PreToolUse and passes
    # its raw arguments as tool_input (codex-rs/core/src/tools/handlers/mcp.rs,
    # hook_tool_name and pre_tool_use_payload), the same shape as Claude Code.
    session = payload.get("session_id")
    return codex_response(guard.gate_mcp_call(
        payload["tool_name"], _mcp_arguments(payload.get("tool_input")), harness="codex",
        session_id=session if isinstance(session, str) else None))


def _respond_codex(payload: dict) -> dict[str, Any] | None:
    tool = payload.get("tool_name")
    if isinstance(tool, str) and tool.startswith("mcp__"):
        return _respond_codex_mcp(payload)
    # Every Codex shell path reports the canonical name "Bash"
    # (codex-rs/core/src/tools/hook_names.rs HookToolName::bash, used by the
    # exec_command handler and the sandbox approval path); no other name is a
    # shell command there.
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if isinstance(command, list):         # an argv-style shell call
        command = " ".join(str(c) for c in command)
    if not isinstance(command, str) or not command.strip():
        return None
    # session_id is the root thread's id (codex-rs core/src/hook_runtime.rs),
    # the same id the session's rollouts carry, so a per-session cap is
    # measured against the Codex session making the call.
    # A payload without one gets no session rather than a guessed one.
    from . import ai_budget

    sid = payload.get("session_id")
    sid = sid.strip() if isinstance(sid, str) and sid.strip() else ai_budget.NO_SESSION
    return codex_response(guard.gate_command(command, session_id=sid, harness="codex",
                                             cwd=payload.get("cwd"), tool="Bash"))


def _worst(verdicts: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    """The verdict that matters most when one tool call carries several
    commands: deny, then ask, then a notice (warn or an over-budget ask),
    then allow. The first of equals wins."""
    def rank(v: dict[str, Any] | None) -> int:
        d = (v or {}).get("decision")
        if d == "deny":
            return 3
        if d == "ask":
            return 1 if (v or {}).get("action_type") == "ai_budget" else 2
        return 1 if d == "warn" else 0
    return max(verdicts, key=rank, default=None)


def _notice(verdict: dict[str, Any] | None) -> str:
    """The text of a verdict that lets the command run but has something to
    say: a warn, or the over-budget ask the user chose to be notified about."""
    v = verdict or {}
    if v.get("decision") == "warn" or (v.get("decision") == "ask"
                                       and v.get("action_type") == "ai_budget"):
        return str(v.get("reason") or "")
    return ""


# ── GitHub Copilot: {"permissionDecision", "permissionDecisionReason"} ─────────

def _copilot_cloud() -> bool:
    return any(os.getenv(k) is not None for k in _COPILOT_CLOUD_ENV)


def copilot_response(verdict: dict[str, Any] | None, *, cloud: bool = False) -> dict[str, Any] | None:
    """Copilot's preToolUse answer, or None for no output (its default flow).

    An "allow" is never sent: in Copilot it pre-approves the call, which would
    skip a permission prompt Copilot's own rules wanted. A notice goes out as a
    progress line, the one display-only channel preToolUse has; the CLI strips
    it from the output, so the call still takes the default path.
    """
    decision, reason = _decision(verdict)
    note = _notice(verdict)
    if note:
        return {"type": "progress", "message": note}
    if decision == "allow":
        return None
    if decision == "ask" and not cloud:
        return {"permissionDecision": "ask", "permissionDecisionReason": reason}
    if decision == "ask":
        reason += _COPILOT_NO_ASK
    return {"permissionDecision": "deny", "permissionDecisionReason": reason}


def _respond_copilot(payload: dict) -> dict[str, Any] | None:
    tool = payload.get("toolName")
    if tool not in _COPILOT_SHELLS:
        return None
    args = payload.get("toolArgs")
    if isinstance(args, str):             # the CLI sends a JSON string
        try:
            args = json.loads(args)
        except ValueError:
            return None
    command = args.get("command") if isinstance(args, dict) else None
    if not isinstance(command, str) or not command.strip():
        return None
    session = payload.get("sessionId")
    return copilot_response(guard.gate_command(command, session_id=session if isinstance(session, str) else None,
                                               harness="copilot", cwd=payload.get("cwd"), tool=tool),
                            cloud=_copilot_cloud())


# ── Gemini CLI: {"decision", "reason", "systemMessage"} ────────────────────────

def gemini_response(verdict: dict[str, Any] | None) -> dict[str, Any]:
    """Gemini's BeforeTool answer. Always a JSON object: with empty stdout
    Gemini reads stderr as the answer instead, and would show whatever the
    gate logged there as a message."""
    decision, reason = _decision(verdict)
    note = _notice(verdict)
    if note:
        return {**_GEMINI_ALLOW, "systemMessage": note}
    if decision == "allow":
        return dict(_GEMINI_ALLOW)
    if decision == "ask":
        reason += _GEMINI_NO_ASK
    # reason goes to the model as the tool error; systemMessage to the human.
    return {"decision": "deny", "reason": reason, "systemMessage": reason}


def _respond_gemini(payload: dict) -> dict[str, Any]:
    if payload.get("tool_name") != _GEMINI_SHELL:
        return dict(_GEMINI_ALLOW)
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return dict(_GEMINI_ALLOW)
    cwd = payload.get("cwd")
    sub = tool_input.get("dir_path")
    if isinstance(sub, str) and sub and isinstance(cwd, str):
        cwd = os.path.join(cwd, sub)
    session = payload.get("session_id")
    return gemini_response(guard.gate_command(command, session_id=session if isinstance(session, str) else None,
                                              harness="gemini", cwd=cwd, tool=_GEMINI_SHELL))


# ── Cline: {"cancel", "errorMessage", "contextModification"} ───────────────────

def cline_response(verdict: dict[str, Any] | None) -> dict[str, Any]:
    """Cline's PreToolUse answer. cancel is the only way to stop a command,
    and it stops the task. A notice goes to the model as context."""
    decision, reason = _decision(verdict)
    note = _notice(verdict)
    if note:
        return {**_CLINE_ALLOW, "contextModification": note}
    if decision == "allow":
        return dict(_CLINE_ALLOW)
    if decision == "ask":
        reason += _CLINE_NO_ASK
    return {"cancel": True, "errorMessage": reason}


def _cline_commands(payload: dict) -> tuple[Any, list[str]]:
    """(tool name, shell commands) from either Cline payload.

    Both carry preToolUse {toolName, parameters}, where parameters is a map of
    strings (non-string values arrive JSON-encoded). The CLI also sends
    tool_call {name, input} with the raw input, preferred when present.
    run_commands accepts several input shapes (schemas.ts,
    RunCommandsInputUnionSchema); execute_command has a plain `command`.
    """
    call = payload.get("tool_call")
    pre = payload.get("preToolUse")
    if isinstance(call, dict):
        name, args = call.get("name"), call.get("input")
    elif isinstance(pre, dict):
        name, args = pre.get("toolName"), pre.get("parameters")
    else:
        return None, []

    def decoded(value: Any) -> Any:
        """A map value the extension JSON-encoded, or the value as it came."""
        if isinstance(value, str):
            try:
                out = json.loads(value)
            except ValueError:
                return value
            return out if isinstance(out, (list, dict)) else value
        return value

    def flatten(value: Any, decode: bool = True) -> list[str]:
        if decode:
            value = decoded(value)
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [c for item in value for c in flatten(item, decode=False)]
        if isinstance(value, dict):
            if "commands" in value:
                return flatten(value["commands"])
            cmd = value.get("command", value.get("cmd"))
            if isinstance(cmd, str):
                extra = decoded(value.get("args"))
                if isinstance(extra, list):
                    return [" ".join([cmd, *(str(a) for a in extra)])]
                return [cmd]
        return []

    if name not in _CLINE_SHELLS:
        return name, []
    # A bare string at the top level is one command; decode only inside a map.
    if isinstance(args, str):
        return name, [args] if args.strip() else []
    return name, [c for c in flatten(args, decode=False) if c.strip()]


def _respond_cline(payload: dict) -> dict[str, Any]:
    if payload.get("hookName") not in ("PreToolUse", "tool_call"):
        return dict(_CLINE_ALLOW)
    tool, commands = _cline_commands(payload)
    if not commands:
        return dict(_CLINE_ALLOW)
    roots = payload.get("workspaceRoots")
    cwd = roots[0] if isinstance(roots, list) and roots and isinstance(roots[0], str) else None
    task = payload.get("taskId")
    verdicts = [guard.gate_command(c, session_id=task if isinstance(task, str) else None,
                                   harness="cline", cwd=cwd, tool=str(tool)) for c in commands]
    return cline_response(_worst(verdicts))


_RESPONDERS = {"cursor": _respond_cursor, "codex": _respond_codex,
               "copilot": _respond_copilot, "gemini": _respond_gemini, "cline": _respond_cline}

# What a harness hears when the guard fails open: its own neutral answer, or
# nothing where silence is the neutral answer.
_NEUTRAL = {"cursor": _CURSOR_ALLOW, "gemini": _GEMINI_ALLOW, "cline": _CLINE_ALLOW}


# ── Hook entry point ───────────────────────────────────────────────────────────

def _fail_open(harness: str | None, stdout: Any, stderr: Any, why: str) -> int:
    """Allow, in the harness's own words, and say why where a human can see it."""
    try:
        print(f"nable guard: allowing, {why}", file=stderr)
        if harness in _NEUTRAL:
            stdout.write(json.dumps(_NEUTRAL[harness]))
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
    # The answer is written before the ledger (guard.answer_first): a slow or
    # locked ledger file must never hold up the verdict.
    with guard.answer_first():
        try:
            with contextlib.redirect_stdout(stderr):
                if name == "claude":
                    return guard.run_hook(io.StringIO(raw), stdout)
                response = _RESPONDERS[name](payload)
        except Exception as e:
            return _fail_open(name, stdout, stderr, f"{type(e).__name__}: {e}")
        if response is not None:
            stdout.write(json.dumps(response))
            with contextlib.suppress(Exception):
                stdout.flush()
        return 0


# ── Where each harness keeps its hooks ────────────────────────────────────────

def _codex_home() -> Path:
    # Codex's find_codex_home: CODEX_HOME when set and non-empty, else ~/.codex.
    env = os.getenv("CODEX_HOME", "")
    return Path(env) if env else Path.home() / ".codex"


def _copilot_home() -> Path:
    # Copilot CLI: COPILOT_HOME replaces the whole ~/.copilot path.
    env = os.getenv("COPILOT_HOME", "")
    return Path(env) if env else Path.home() / ".copilot"


def _gemini_home() -> Path:
    # Gemini CLI's homedir(): GEMINI_CLI_HOME when set, else the user's home;
    # its global directory is .gemini under that.
    env = os.getenv("GEMINI_CLI_HOME", "")
    return (Path(env) if env else Path.home()) / ".gemini"


def _cline_dirs() -> list[Path]:
    # The VS Code extension keeps its global files in ~/Documents/Cline; the
    # CLI in ~/.cline (or CLINE_DIR). Both read hooks from ~/Documents/Cline/Hooks.
    env = os.getenv("CLINE_DIR", "").strip()
    return [Path.home() / "Documents" / "Cline", Path(env) if env else Path.home() / ".cline"]


def config_dir(harness: str) -> Path:
    """The user-level directory whose presence means the harness is on this machine."""
    if harness == "claude":
        return Path.home() / ".claude"
    if harness == "cursor":
        return Path.home() / ".cursor"
    if harness == "copilot":
        return _copilot_home()
    if harness == "gemini":
        return _gemini_home()
    if harness == "cline":
        return next((d for d in _cline_dirs() if d.is_dir()), _cline_dirs()[0])
    return _codex_home()


def hooks_path(harness: str, global_scope: bool) -> Path:
    """The file the guard hook goes into, per harness and scope."""
    if harness == "claude":
        return guard._settings_path(global_scope)
    if harness == "cursor":
        return (Path.home() if global_scope else Path.cwd()) / ".cursor" / "hooks.json"
    if harness == "copilot":
        return (_copilot_home() / "hooks" if global_scope
                else Path.cwd() / ".github" / "hooks") / _COPILOT_FILE
    if harness == "gemini":
        return (_gemini_home() if global_scope else Path.cwd() / ".gemini") / "settings.json"
    if harness == "cline":
        return (Path.home() / "Documents" / "Cline" / "Hooks" if global_scope
                else Path.cwd() / ".clinerules" / "hooks") / _CLINE_FILE
    return (_codex_home() if global_scope else Path.cwd() / ".codex") / "hooks.json"


def detected() -> list[str]:
    """Harnesses whose config directory exists on this machine. Cline on
    Windows is left out: its hooks there are PowerShell scripts, which nable
    does not write."""
    found = [h for h in HARNESSES if config_dir(h).is_dir()]
    return [h for h in found if not (h == "cline" and sys.platform == "win32")]


def hook_command(harness: str, global_scope: bool = True) -> str:
    """The command a harness runs. The same as Claude Code's, on purpose (see
    the module docstring), except that Cursor and Cline get an absolute uvx in
    the user's own (global) config.

    Cursor is a desktop app, and an app started from the Dock does not always
    see the PATH a terminal does. A bare `uvx` it cannot find is a hook that
    fails open on every command while reporting itself installed. Codex runs
    hooks through a login shell, so the bare form resolves there. Cline lives
    in VS Code, a desktop app with the same PATH problem as Cursor. Copilot CLI
    and Gemini CLI run in the user's terminal.

    A project file is shared through the repository, and this machine's uvx
    path means nothing on a teammate's, so project scope keeps the bare form.
    """
    cmd = guard._hook_command()
    if global_scope and harness in ("cursor", "cline") and cmd.startswith("uvx "):
        uvx = shutil.which("uvx")
        if uvx and not guard._is_ephemeral(uvx):
            cmd = (f'"{uvx}"' if " " in uvx else uvx) + cmd[len("uvx"):]
    return cmd


def _hook_timeout() -> int:
    # uvx resolves an environment per call; give the cold-cache case room.
    return 30 if guard._hook_command().startswith("uvx") else 10


# uvx options that take a value as the next argument (uv's `tool run` flags);
# anything else starting with "-" is a flag on its own.
_UVX_VALUE_OPTS = frozenset({
    "--from", "--with", "--with-editable", "--with-requirements", "--python", "-p",
    "--index", "--index-url", "-i", "--extra-index-url", "--default-index",
    "--find-links", "-f", "--index-strategy", "--keyring-provider", "--resolution",
    "--prerelease", "--exclude-newer", "--config-setting", "-C", "--link-mode",
    "--reinstall-package", "--refresh-package", "--upgrade-package", "-P",
    "--cache-dir", "--config-file", "--directory", "--project", "--color",
    "--python-platform", "--env-file", "--constraints", "--overrides", "-c",
})
_PIN_RE = re.compile(rf"{re.escape(guard._PYPI_NAME)}\s*(?:==|@)\s*([A-Za-z0-9.+!_-]+)")


def uvx_pin(cmd: Any) -> str | None:
    """How a uvx hook command pins nable, from its arguments rather than its
    spelling.

    "pinned"   finops-mcp at exactly this release
    "other"    finops-mcp pinned to some other release
    "unpinned" finops-mcp with no version: the newest PyPI release, fetched
               on every agent tool call
    None       not a uvx command for finops-mcp (a binary path has no pin)

    guard.hook_pin reads only a leading `uvx --from`. Older releases and
    hand edits also wrote `uvx finops-mcp ...`, `uvx --python 3.12 --from
    finops-mcp ...`, `uv tool run ...`, an absolute or quoted uvx path, and the
    `; exit 0` suffix this module adds, so this parses the argument list."""
    if not isinstance(cmd, str):
        return None
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    cut = next((i for i, t in enumerate(tokens) if t and set(t) <= set(";&|")), len(tokens))
    tokens = tokens[:cut]
    if not tokens:
        return None
    exe = os.path.basename(tokens[0]).lower()
    if exe in ("uvx", "uvx.exe"):
        args = tokens[1:]
    elif exe in ("uv", "uv.exe") and tokens[1:3] == ["tool", "run"]:
        args = tokens[3:]
    else:
        return None
    spec: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--from":
            spec = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a.startswith("--from="):
            spec = a[len("--from="):]
        elif a.startswith("-"):
            if a in _UVX_VALUE_OPTS:
                i += 1                  # skip the option's value
        else:
            spec = spec or a            # the command; its package when no --from
            break
        i += 1
    if not spec or not spec.lower().startswith(guard._PYPI_NAME):
        return None
    m = _PIN_RE.fullmatch(spec.strip())
    if not m or m.group(1).lower() == "latest":
        return "unpinned"
    return "pinned" if m.group(1) == guard.__version__ else "other"


def _needs_repin(cmd: Any) -> bool:
    return uvx_pin(cmd) in ("unpinned", "other")


def _is_ours(cmd: Any) -> bool:
    return isinstance(cmd, str) and guard._HOOK_MARKER in cmd and "finops" in cmd


def _runnable(cmd: str) -> bool:
    """Whether the program a hook command starts with still exists."""
    try:
        exe = cmd[1:cmd.index('"', 1)] if cmd.startswith('"') else cmd.split()[0]
    except (ValueError, IndexError):
        return False
    return bool(shutil.which(exe)) or Path(exe).exists()


# ── Safe JSON file handling ────────────────────────────────────────────────────

def _refuse(path: Path, what: str) -> SystemExit:
    """Never guess at a config file we do not understand. Same contract as the
    Claude installer: make the intended change, or change nothing and say why."""
    return SystemExit(f"  {path} {what}; fix it first, nothing was changed.")


def _backup(path: Path, raw: bytes) -> Path | None:
    """Keep a copy of a file we could not parse, named by its content so a
    repeated attempt on the same bytes does not pile up copies."""
    dest = path.with_name(f"{path.name}.nable-backup-{hashlib.sha256(raw).hexdigest()[:8]}")
    try:
        with open(dest, "xb") as f:
            f.write(raw)
    except FileExistsError:
        pass
    except OSError:
        return None
    return dest


def _load(path: Path, *, comments_allowed: bool = False) -> dict:
    if not path.exists():
        return {}
    raw = path.read_bytes()
    if not raw.strip():
        return {}                       # an empty file (a `touch`) holds no settings yet
    try:
        data = json.loads(raw)
    except ValueError:
        saved = _backup(path, raw)
        note = f" (a copy is at {saved.name})" if saved else ""
        if comments_allowed:
            # Gemini CLI reads its settings with comments stripped. Rewriting
            # the file as JSON would drop them, so it is refused the same way.
            note += "; if it has comments, nable will not rewrite it and drop them"
        raise _refuse(path, f"exists but is not valid JSON{note}")
    if not isinstance(data, dict):
        raise _refuse(path, f"contains a JSON {type(data).__name__}, not an object")
    return data


def _write(path: Path, data: dict) -> None:
    _write_text(path, json.dumps(data, indent=2) + "\n")


def _write_text(path: Path, text: str, *, executable: bool = False) -> None:
    """Atomic replace: a crash or a full disk leaves the old file, never half
    of a new one. Writes through a symlink (a dotfiles repo) instead of over it.
    `executable` adds the execute bits a hook script needs (as the umask allows)."""
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.exists()
    # os.replace only needs the directory to be writable, so it would happily
    # swap out a file the user made read-only. Respect the file's own mode.
    if existing and not os.access(target, os.W_OK):
        raise PermissionError(errno.EACCES, "Permission denied", str(target))
    if existing:
        mode = stat.S_IMODE(target.stat().st_mode)
    else:
        umask = os.umask(0)
        os.umask(umask)
        mode = (0o777 if executable else 0o666) & ~umask
    if executable:
        mode |= stat.S_IXUSR
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _list_at(parent: dict, key: str, path: Path, where: str, *, create: bool) -> list | None:
    """parent[key] as a list: absent or null is empty (created on request), any
    other type is refused rather than guessed at."""
    value = parent.get(key)
    if value is None:
        if not create:
            return None
        value = parent[key] = []
    elif not isinstance(value, list):
        raise _refuse(path, f"has a '{where}' value that is a {type(value).__name__}, not an array")
    return value


def _hooks_obj(doc: dict, path: Path, *, create: bool) -> dict | None:
    hooks = doc.get("hooks")
    if hooks is None:
        if not create:
            return None
        hooks = doc["hooks"] = {}
    elif not isinstance(hooks, dict):
        raise _refuse(path, f"has a 'hooks' value that is a {type(hooks).__name__}, not an object")
    return hooks


# ── Cursor: {"version": 1, "hooks": {"beforeShellExecution": [{command, ...}],
#                                    "beforeMCPExecution": [{command, ...}]}} ──

_CURSOR_EVENTS = (_CURSOR_SHELL, _CURSOR_MCP)


def _cursor_entries(doc: dict, path: Path, *, create: bool,
                    event: str = _CURSOR_SHELL) -> list | None:
    version = doc.get("version")
    if version is None and "hooks" not in doc and create:
        doc["version"] = 1
    elif version is not None and not (type(version) is int and version == 1):
        raise _refuse(path, f"declares hooks version {version!r}; nable understands version 1")
    hooks = _hooks_obj(doc, path, create=create)
    if hooks is None:
        return None
    return _list_at(hooks, event, path, f"hooks.{event}", create=create)


def _cursor_event_commands(doc: dict, event: str) -> list[Any]:
    hooks = doc.get("hooks")
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        return []
    return [e.get("command") for e in entries if isinstance(e, dict)]


def _cursor_commands(doc: dict) -> list[Any]:
    return [c for event in _CURSOR_EVENTS for c in _cursor_event_commands(doc, event)]


def _cursor_install(doc: dict, path: Path, cmd: str, stale: tuple[str, ...] = ()) -> str:
    """Our entry on both events: shell commands, and MCP tool calls (which the
    guard translates when they are Terraform, AWS or Kubernetes tools). An
    install from before MCP coverage gains the second entry as a repair."""
    outcomes = []
    for event in _CURSOR_EVENTS:
        entries = _cursor_entries(doc, path, create=True, event=event)
        ours = [e for e in entries if isinstance(e, dict) and _is_ours(e.get("command"))]
        if not ours:
            # failClosed is Cursor's default already. Spelled out because the
            # whole contract is fail-open, and a changed default must not flip it.
            entries.append({"command": cmd, "timeout": _hook_timeout(), "failClosed": False})
            outcomes.append("new")
        else:
            outcomes.append(_repair(ours, cmd, stale=stale))
    return _merge_outcomes(outcomes)


def _cursor_uninstall(doc: dict, path: Path) -> bool:
    removed = False
    for event in _CURSOR_EVENTS:
        entries = _cursor_entries(doc, path, create=False, event=event)
        if not entries:
            continue
        kept = [e for e in entries if not (isinstance(e, dict) and _is_ours(e.get("command")))]
        if len(kept) == len(entries):
            continue
        removed = True
        if kept:
            doc["hooks"][event] = kept
        else:
            del doc["hooks"][event]
    return removed


# ── Codex: {"hooks": {"PreToolUse": [{"matcher", "hooks": [{type, command}]}]}} ──
# Codex parses hooks.json with deny_unknown_fields at the top level, so nothing
# but "hooks" (and the optional "description") may ever be added there.

def _event_groups(doc: dict, path: Path, event: str, *, create: bool) -> list | None:
    """doc["hooks"][event], the matcher-group list Codex and Gemini CLI share."""
    hooks = _hooks_obj(doc, path, create=create)
    if hooks is None:
        return None
    return _list_at(hooks, event, path, f"hooks.{event}", create=create)


def _event_handlers(doc: dict, event: str) -> list[dict]:
    hooks = doc.get("hooks")
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    out: list[dict] = []
    for group in groups if isinstance(groups, list) else []:
        inner = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(inner, list):
            out.extend(h for h in inner if isinstance(h, dict))
    return out


def _event_uninstall(doc: dict, path: Path, event: str) -> bool:
    groups = _event_groups(doc, path, event, create=False)
    if not groups:
        return False
    removed = False
    kept = []
    for group in groups:
        inner = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(inner, list):
            kept.append(group)          # not ours; leave it exactly as found
            continue
        rest = [h for h in inner if not (isinstance(h, dict) and _is_ours(h.get("command")))]
        if len(rest) == len(inner):
            kept.append(group)
            continue
        removed = True
        if rest:
            group["hooks"] = rest
            kept.append(group)
    if removed:
        if kept:
            doc["hooks"][event] = kept
        else:
            del doc["hooks"][event]
    return removed


def _codex_groups(doc: dict, path: Path, *, create: bool) -> list | None:
    return _event_groups(doc, path, "PreToolUse", create=create)


def _codex_handlers(doc: dict) -> list[dict]:
    return _event_handlers(doc, "PreToolUse")


def _codex_install(doc: dict, path: Path, cmd: str) -> str:
    groups = _codex_groups(doc, path, create=True)
    ours = [h for h in _codex_handlers(doc) if _is_ours(h.get("command"))]
    if not ours:
        # Codex matchers are regular expressions; anchor so a tool whose name
        # merely contains "Bash" is not gated as a shell command.
        groups.append({"matcher": _CODEX_MATCHER,
                       "hooks": [{"type": "command", "command": cmd,
                                  "timeout": _hook_timeout()}]})
        return "new"
    widened = False
    for group in groups:
        # Only the exact matcher earlier releases wrote is widened to MCP
        # tools; any other matcher on our group is someone's choice.
        inner = group.get("hooks") if isinstance(group, dict) else None
        if (isinstance(inner, list) and group.get("matcher") == _CODEX_LEGACY_MATCHER
                and any(isinstance(h, dict) and _is_ours(h.get("command")) for h in inner)):
            group["matcher"] = _CODEX_MATCHER
            widened = True
    outcome = _repair(ours, cmd)
    return _merge_outcomes([outcome, "repaired"]) if widened else outcome


def _codex_sees_mcp(doc: dict) -> bool:
    """Does one of our Codex groups match an MCP tool name?"""
    hooks = doc.get("hooks")
    groups = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    for group in groups if isinstance(groups, list) else []:
        inner = group.get("hooks") if isinstance(group, dict) else None
        if not (isinstance(inner, list)
                and any(isinstance(h, dict) and _is_ours(h.get("command")) for h in inner)):
            continue
        matcher = group.get("matcher")
        try:
            if matcher in (None, "", "*") or re.search(matcher, "mcp__server__tool"):
                return True
        except (re.error, TypeError):
            continue
    return False


def _codex_uninstall(doc: dict, path: Path) -> bool:
    return _event_uninstall(doc, path, "PreToolUse")


def _repair(ours: list[dict], cmd: str, *, timeout_key: str = "timeout",
            timeout: int | None = None, stale: tuple[str, ...] = ()) -> str:
    """Our entry is already there. Point a dead one at a command that runs
    (the uv-cache-path failure), and re-pin a uvx one that is unpinned or
    pinned to another release (re-running install is an explicit choice of
    release, as for the Claude Code hook). Leave any other live one however it
    was written, unless it is one of `stale` (this machine's absolute uvx path
    in a project file that teammates share).

    Returns "repaired" (a dead or stale command), "repinned", or "already"."""
    kinds = set()
    for entry in ours:
        current = entry.get("command")
        if current == cmd:
            continue
        dead = current in stale or not _runnable(current or "")
        if dead or _needs_repin(current):
            entry["command"] = cmd
            entry[timeout_key] = _hook_timeout() if timeout is None else timeout
            kinds.add("repaired" if dead else "repinned")
    return "repaired" if "repaired" in kinds else "repinned" if kinds else "already"


def _merge_outcomes(outcomes: list[str]) -> str:
    """One outcome for several entries (Cursor's two events)."""
    if all(o == "new" for o in outcomes):
        return "new"
    if all(o == "already" for o in outcomes):
        return "already"
    if "repaired" in outcomes or "new" in outcomes:
        return "repaired"
    return "repinned"


def _fail_safe(cmd: str) -> str:
    """The hook command for a harness that blocks on a failed hook.

    Copilot denies the tool call when a preToolUse command exits non-zero for
    any reason but a timeout, and Gemini CLI blocks on any exit but 0 and 1.
    `finops guard hook` always exits 0, but a teammate without uv, a sandbox
    that cannot reach PyPI, or an older nable rejecting its arguments would
    otherwise stop every shell command. The trailing `exit 0` means the same in
    bash, sh and PowerShell, so one string serves both of Copilot's shells."""
    return f"{cmd}; exit 0"


# ── GitHub Copilot: {"version": 1, "hooks": {"preToolUse": [{type, command, ...}]}} ──
# The file is ours (nable-guard.json): Copilot loads every *.json in the hooks
# directory, so ours never shares a file with another tool's hooks.

def _copilot_entries(doc: dict, path: Path, *, create: bool) -> list | None:
    version = doc.get("version")
    if version is None and not doc and create:
        doc["version"] = 1
    elif version is not None and not (type(version) is int and version == 1):
        raise _refuse(path, f"declares hooks version {version!r}; nable understands version 1")
    hooks = _hooks_obj(doc, path, create=create)
    if hooks is None:
        return None
    return _list_at(hooks, "preToolUse", path, "hooks.preToolUse", create=create)


def _copilot_commands(doc: dict) -> list[Any]:
    hooks = doc.get("hooks")
    entries = hooks.get("preToolUse") if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        return []
    return [e.get("command") or e.get("bash") for e in entries if isinstance(e, dict)]


def _copilot_install(doc: dict, path: Path, cmd: str) -> str:
    entries = _copilot_entries(doc, path, create=True)
    ours = [e for e in entries if isinstance(e, dict) and _is_ours(e.get("command"))]
    if not ours:
        # The matcher is a regex anchored as ^(?:PATTERN)$ against toolName:
        # bash on macOS and Linux (and the cloud agent), powershell on Windows.
        entries.append({"type": "command", "command": _fail_safe(cmd),
                        "matcher": "|".join(_COPILOT_SHELLS),
                        "timeoutSec": _hook_timeout()})
        return "new"
    return _repair(ours, _fail_safe(cmd), timeout_key="timeoutSec")


def _copilot_uninstall(doc: dict, path: Path) -> bool:
    entries = _copilot_entries(doc, path, create=False)
    if not entries:
        return False
    kept = [e for e in entries if not (isinstance(e, dict) and _is_ours(e.get("command")))]
    if len(kept) == len(entries):
        return False
    if kept:
        doc["hooks"]["preToolUse"] = kept
    else:
        del doc["hooks"]["preToolUse"]
    return True


def _copilot_is_empty(doc: dict) -> bool:
    """Nothing left in our file but the frame we wrote around the hook."""
    return not doc.get("hooks") and set(doc) <= {"version", "hooks"}


# ── Gemini CLI: {"hooks": {"BeforeTool": [{"matcher", "hooks": [{type, command}]}]}} ──
# settings.json holds all of Gemini CLI's settings; only hooks.BeforeTool is touched.

def _gemini_install(doc: dict, path: Path, cmd: str) -> str:
    groups = _event_groups(doc, path, _GEMINI_EVENT, create=True)
    ours = [h for h in _event_handlers(doc, _GEMINI_EVENT) if _is_ours(h.get("command"))]
    timeout_ms = _hook_timeout() * 1000         # Gemini CLI counts milliseconds
    if not ours:
        # A regex against the tool name; anchored like the Codex one.
        groups.append({"matcher": f"^{_GEMINI_SHELL}$",
                       "hooks": [{"name": _GEMINI_NAME, "type": "command",
                                  "command": _fail_safe(cmd), "timeout": timeout_ms,
                                  "description": "nable guard: checks shell commands "
                                                 "against your cost policy"}]})
        return "new"
    return _repair(ours, _fail_safe(cmd), timeout=timeout_ms)


def _gemini_uninstall(doc: dict, path: Path) -> bool:
    return _event_uninstall(doc, path, _GEMINI_EVENT)


# ── Cline: an executable PreToolUse script, one per hooks directory ────────────
# Cline runs the file named exactly PreToolUse in each hooks directory, so the
# guard cannot sit beside another PreToolUse hook there. One that is not ours
# is refused, never replaced. The script answers every tool call but the shell
# ones itself, because Cline has no matcher and would otherwise start nable
# for every file read.

_CLINE_PIPE = "printf '%s' \"$input\" | "


def _cline_script(cmd: str) -> str:
    shells = "|".join(f"*'\"{t}\"'*" for t in _CLINE_SHELLS)
    return (
        "#!/bin/sh\n"
        f"{_CLINE_MARKER}: checks the shell commands Cline runs against your\n"
        "# cost policy. Written by `nable guard install --harness cline`;\n"
        "# `nable guard uninstall --harness cline` removes it.\n"
        "input=$(cat)\n"
        'case "$input" in\n'
        f"  {shells})\n"
        f"    {_CLINE_PIPE}{cmd}\n"
        "    exit 0 ;;\n"
        "esac\n"
        f"echo '{json.dumps(_CLINE_ALLOW)}'\n"
    )


def _cline_command_in(text: str) -> str | None:
    """The command our script pipes the payload to, or None when the file is
    not a script nable wrote."""
    if _CLINE_MARKER not in text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(_CLINE_PIPE):
            return line[len(_CLINE_PIPE):]
    return None


def _cline_read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def _refuse_cline_on_windows() -> None:
    if sys.platform == "win32":
        raise SystemExit(f"  Cline hooks on Windows are PowerShell scripts ({_CLINE_FILE}.ps1), "
                         "which nable does not write yet; nothing was changed.")


def _cline_install(path: Path, cmd: str, stale: tuple[str, ...] = ()) -> str:
    _refuse_cline_on_windows()
    text = _cline_read(path)
    if text is None:
        if path.exists():
            raise _refuse(path, "exists but is not a file")
        _write_text(path, _cline_script(cmd), executable=True)
        return "new"
    current = _cline_command_in(text)
    if current is None or not _is_ours(current):
        raise SystemExit(f"  {path} is another {_CLINE_FILE} hook, and Cline runs one per "
                         f"directory; nothing was changed. To keep both, pipe shell tool "
                         f"calls from it to `{cmd}`.")
    target = path.resolve() if path.is_symlink() else path
    runs = current == cmd or (current not in stale and _runnable(current))
    repin = runs and current != cmd and _needs_repin(current)
    executable = os.access(target, os.X_OK)
    if runs and not repin and executable:
        return "already"
    # A dead or unpinned command, or a script Cline cannot execute (Cline
    # requires the execute bit), is rewritten; any other live command is kept.
    _write_text(path, _cline_script(current if runs and not repin else cmd), executable=True)
    return "repinned" if repin and executable else "repaired"


def _cline_uninstall(path: Path) -> bool:
    text = _cline_read(path)
    current = _cline_command_in(text) if text is not None else None
    if current is None or not _is_ours(current):
        return False
    path.unlink()
    return True


def _cline_state(path: Path) -> str:
    text = _cline_read(path)
    current = _cline_command_in(text) if text is not None else None
    if current is None or not _is_ours(current):
        return "absent"
    target = path.resolve() if path.is_symlink() else path
    return "installed" if _runnable(current) and os.access(target, os.X_OK) else "broken"


# ── Install / uninstall / status, per harness ─────────────────────────────────

_ADAPTERS = {
    "cursor": (_cursor_install, _cursor_uninstall, _cursor_commands),
    "codex": (_codex_install, _codex_uninstall, lambda d: [h.get("command") for h in _codex_handlers(d)]),
    "copilot": (_copilot_install, _copilot_uninstall, _copilot_commands),
    "gemini": (_gemini_install, _gemini_uninstall,
               lambda d: [h.get("command") for h in _event_handlers(d, _GEMINI_EVENT)]),
}


def _stale_forms(harness: str, global_scope: bool) -> tuple[str, ...]:
    """Commands of ours to replace even though they run: in a project file,
    this machine's absolute uvx path (what earlier releases wrote there)."""
    if global_scope:
        return ()
    local = hook_command(harness, global_scope=True)
    return (local,) if local != hook_command(harness, global_scope=False) else ()


def install(harness: str, global_scope: bool = False) -> tuple[str, Path]:
    """Idempotently add the guard hook. Returns (outcome, path), outcome one of
    "new", "already", "repaired". Raises SystemExit when it refuses a file."""
    path = hooks_path(harness, global_scope)
    if harness == "claude":
        # guard.install repairs a dead entry and re-pins an unpinned uvx one in
        # place, so read both before it writes to report what it did.
        already = guard.is_installed(path)
        fixed = bool(guard.broken_hook_command(path) or guard.unpinned_hook_command(path))
        guard.install(global_scope)
        return ("repaired" if fixed else "already" if already else "new"), path
    if harness == "cline":
        # Refuse before resolving the command: nothing on Windows would use it,
        # and resolving it there walks PATH for nothing.
        _refuse_cline_on_windows()
    cmd = hook_command(harness, global_scope)
    if harness == "cline":
        return _cline_install(path, cmd, _stale_forms(harness, global_scope)), path
    doc = _load(path, comments_allowed=harness == "gemini")
    if harness == "cursor":
        outcome = _cursor_install(doc, path, cmd, _stale_forms(harness, global_scope))
    else:
        outcome = _ADAPTERS[harness][0](doc, path, cmd)
    if outcome != "already":
        _write(path, doc)
    return outcome, path


def uninstall(harness: str, global_scope: bool = False) -> tuple[bool, Path]:
    """Remove the guard hook, and only ours. Returns (removed, path)."""
    path = hooks_path(harness, global_scope)
    if harness == "claude":
        return guard.uninstall(global_scope), path
    if harness == "cline":
        return _cline_uninstall(path), path
    if not path.exists():
        return False, path
    doc = _load(path, comments_allowed=harness == "gemini")
    removed = _ADAPTERS[harness][1](doc, path)
    if removed:
        if harness == "copilot" and _copilot_is_empty(doc) and not path.is_symlink():
            path.unlink()               # the file was ours alone; leave no husk
        else:
            _write(path, doc)
    return removed, path


def state(harness: str, global_scope: bool) -> str:
    """"installed", "broken" or "absent". Read-only, so it never raises."""
    path = hooks_path(harness, global_scope)
    try:
        if harness == "claude":
            if not guard.is_installed(path):
                return "absent"
            return "broken" if guard.broken_hook_command(path) else "installed"
        if harness == "cline":
            return _cline_state(path)
        doc = json.loads(path.read_text())
        ours = [c for c in _ADAPTERS[harness][2](doc) if _is_ours(c)]
    except Exception:
        return "absent"
    if not ours:
        return "absent"
    # Cursor has an entry per event; one dead entry is a surface that went dark.
    return "installed" if all(_runnable(c) for c in ours) else "broken"


def _our_commands(harness: str, global_scope: bool) -> list[str]:
    path = hooks_path(harness, global_scope)
    if harness == "cline":
        text = _cline_read(path)
        current = _cline_command_in(text) if text is not None else None
        return [current] if current and _is_ours(current) else []
    doc = json.loads(path.read_text())
    return [c for c in _ADAPTERS[harness][2](doc) if _is_ours(c)]


def pin_state(harness: str, global_scope: bool) -> str | None:
    """How our installed hook pins nable: "pinned", "other", "unpinned", or
    "binary" (not the uvx form); None when it is not installed. The worst of
    several entries wins. Read-only, never raises."""
    try:
        if harness == "claude":
            return None
        pins = [uvx_pin(c) for c in _our_commands(harness, global_scope)]
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return None
    if not pins:
        return None
    for worst in ("unpinned", "other", "pinned"):
        if worst in pins:
            return worst
    return "binary"


def sees_mcp(harness: str, global_scope: bool) -> bool:
    """Does our installed hook see MCP tool calls, not only shell commands?
    Cursor and Codex can; an install from before MCP coverage cannot until
    install runs again. Read-only, never raises."""
    path = hooks_path(harness, global_scope)
    try:
        doc = json.loads(path.read_text())
        if harness == "cursor":
            return any(_is_ours(c) and _runnable(c)
                       for c in _cursor_event_commands(doc, _CURSOR_MCP))
        if harness == "codex":
            return _codex_sees_mcp(doc)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return False


# ── CLI ────────────────────────────────────────────────────────────────────────

_AFTER_INSTALL = {
    "claude": "Restart Claude Code to pick up the hook.",
    "cursor": "If Cursor is open, restart it so the hook is loaded.",
    "codex": ("Codex asks you to review new hooks when it starts (\"Hooks need review\"). "
              "Trust this one or it will not run. Codex hooks cannot ask for a "
              "confirmation, so a command the guard would ask about is denied there, "
              "with the reason."),
    "copilot": ("Copilot CLI loads hooks when a session starts. In a repository's "
                ".github/hooks the file also reaches the Copilot cloud agent, which "
                "has no one to ask: a command the guard would ask about is denied "
                "there, with the reason, and the guard only runs there when uv is "
                "installed in the agent's environment (otherwise it allows)."),
    "gemini": ("Gemini CLI warns before it runs a new project hook; allow it. A command "
               "the guard would ask about is denied there, with the reason, because a "
               "Gemini CLI hook cannot reliably pause for a confirmation."),
    "cline": ("Cline runs hooks unless they are turned off in its settings. Cline "
              "hooks cannot ask, so a command the guard would ask about or deny "
              "stops the task, with the reason."),
}

_WHAT_IT_DOES = ("before the agent runs an infra-mutating command "
                 "(terraform destroy, kubectl delete, aws ec2 terminate-instances), "
                 "nable checks it against your policy and asks, denies with the "
                 "reason, or stays silent.")


def cli(action: str, *, harness: str | None, everything: bool, global_scope: bool) -> int:
    """`nable guard install|uninstall --harness X` and `--all`. Returns the exit code."""
    from .welcome import _fire_telemetry, amber, bold, cyan, dim, green

    scope = "global" if global_scope else "project"
    flag = " --global" if global_scope else ""
    if everything:
        targets = HARNESSES if action == "uninstall" else detected()
        if not targets:
            looked = ", ".join(str(config_dir(h)) for h in HARNESSES)
            print(f"\n  No supported agent found (looked for {looked}).\n")
            return 1
    else:
        targets = (harness or "claude",)

    failed = 0
    installed = 0
    print()
    for name in targets:
        label = f"{LABELS[name]:<{_LABEL_WIDTH}}" if everything else LABELS[name]
        try:
            if action == "install":
                outcome, path = install(name, global_scope)
                _fire_telemetry("guard_installed", {
                    "scope": scope, "outcome": outcome, "harness": name,
                    "hook_form": "uvx" if guard._hook_command() == guard._UVX_HOOK_CMD else "binary",
                })
                verb = {"new": "installed", "already": "already installed",
                        "repaired": "repaired",
                        "repinned": f"pinned to {guard._PYPI_NAME}=={guard.__version__}"}[outcome]
                print(f"  {green('✓')} {label} guard {verb} → {path}")
                installed += 1
                if state(name, global_scope) == "broken":
                    print(f"      {amber('The hooked command does not exist, so the guard is not running.')}")
                elif outcome in ("new", "repaired"):
                    print(dim(f"      {_AFTER_INSTALL[name]}"))
            else:
                removed, path = uninstall(name, global_scope)
                if removed:
                    print(f"  {green('✓')} {label} guard removed from {path}")
                elif everything:
                    print(dim(f"  - {label} not installed in {path}"))
                else:
                    print(f"  {label} guard was not installed in {path}")
        except SystemExit as e:           # a refusal: the file was left as found
            failed += 1
            print(f"  {amber('!')} {label} {str(e.code).strip()}")
        except OSError as e:
            failed += 1
            print(f"  {amber('!')} {label} could not write {hooks_path(name, global_scope)}: "
                  f"{e.strerror or e}. Check the file's permissions and run it again.")
    if everything and action == "install":
        for name in HARNESSES:
            if name not in targets:
                print(dim(f"  - {LABELS[name]:<{_LABEL_WIDTH}} not found "
                          f"(no {config_dir(name)}), skipped"))
    if action == "install" and installed:
        print(f"\n  {bold('What it does:')} {_WHAT_IT_DOES}")
    if action == "install" and not failed:
        which = "--all" if everything else f"--harness {targets[0]}"
        print(f"\n  {dim('Remove any time:')} {cyan(f'nable guard uninstall {which}{flag}')}")
    print()
    return 1 if failed else 0


def status_lines() -> list[str]:
    """One line per non-Claude scope that is installed or whose agent is here,
    and under any that needs it, the command that fixes it (naming its scope:
    a bare install only ever touches this project)."""
    from .welcome import amber, cyan, dim, green

    here = set(detected())
    lines = []
    for name in HARNESSES:
        if name == "claude":
            continue
        for scope, is_global in (("project", False), ("global", True)):
            st = state(name, is_global)
            if st == "absent" and name not in here:
                continue
            pin = pin_state(name, is_global) if st == "installed" else None
            if st == "installed" and pin == "unpinned":
                shown = amber("installed, unpinned")
            elif st == "installed" and pin == "other":
                shown = amber("installed, pinned to another release")
            else:
                shown = {"installed": green("installed"), "broken": amber("installed, but broken"),
                         "absent": dim("not installed")}[st]
            lines.append(f"  {LABELS[name]:<{_LABEL_WIDTH}} {scope:<8} {shown}   "
                         f"{dim(str(hooks_path(name, is_global)))}")
            if st == "broken" or pin in ("unpinned", "other"):
                fix = f"nable guard install --harness {name}" + (" --global" if is_global else "")
                lines.append(f"  {'':<{_LABEL_WIDTH}} {'':<8} {dim('fix:')} {cyan(fix)}")
    return lines
