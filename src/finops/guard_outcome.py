"""The post hook: how a person answered the guard's ask.

The guard's PreToolUse hook exits before the person answers an ask, so the
decision ledger knew that the guard asked and never what came of it. The post
hook runs after a tool call went ahead and appends an outcome record, `ran`,
linked to the ask it answers. An ask with a `ran` was approved; an ask
without one was declined, which is derived when the ledger is read
(guard_ledger.ask_outcomes), never written. That answer is what the learning
loop reads (finops.recommendations.learning: repeated approvals become a
proposed threshold a person confirms, repeated declines a tighter one).

    finops guard hook --post [--via plugin]

Where it is installed (the harness's own name for "after a tool ran"):

  Claude Code     PostToolUse, the same matcher as PreToolUse, in the settings
                  file `nable guard install` writes and in the plugin's
                  hooks.json (guard.install, guard_adapters.plugin_hooks)
  Cursor          afterShellExecution (shell commands; Cursor's first-party
                  cookbook hooks.json and audit-log.sh show the event and its
                  payload: command, output, duration)
  GitHub Copilot  postToolUse on bash/powershell (hooks-reference.md: camelCase
                  sessionId, toolName, toolArgs, toolResult)

Codex CLI, Gemini CLI and Cline document a post event too (PostToolUse,
AfterTool, PostToolUse), and none is installed: those harnesses cannot pause
to ask, so every ask there is a deny, recorded as not_run when it happens, and
a post hook would have nothing to link to. Cursor's MCP calls are not covered
(afterMCPExecution is not in the sources guard_adapters cites), and neither is
a Claude Code tool call that fails after approval (PostToolUseFailure, which an
older Claude Code would reject as an unknown event, taking the guard's own
PreToolUse with it). guard_ledger.ask_outcomes reads those asks as unknown, or
as declined for a failed call, never as approved.

Linking. Claude Code sends tool_use_id with both events, and the pre hook's
verdict carries it, so the link is exact: the same session and the same
tool_use_id, however long the person took to answer. Elsewhere (and for a verdict written
before tool_use_id was recorded) the link is the same session and the same
command summary (redacted as the verdict's was) within LINK_WINDOW of the ask.
The outcome names the ask by the sha256 of its line, the hash the chain
already uses, and an ask already answered is not answered twice (the plugin's
hook and a settings hook can both run).

The fast path, like the pre hook's silent answers: standard library only,
never the guard (guard.py costs a few hundred ms to import), one bounded read
from the end of the ledger and at most one append. Fails safe: always exit 0,
nothing on stdout but Cursor's empty answer, and an error costs the record,
never the tool call. `nable guard off` turns it off with the pre hook.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from . import guard_ledger, guard_plugin

POST_FLAG = guard_plugin.POST_FLAG
# How far back the post hook looks for the ask it answers. A person can leave
# a prompt open a while; tool_use_id makes a late answer exact.
LOOKBACK_MINUTES = 120.0
# Without tool_use_id, the ask must be this recent: an identical command asked
# about an hour ago is a different decision.
LINK_WINDOW = timedelta(minutes=10)
# The most of the ledger's tail one post hook reads.
_READ_MAX_BYTES = 1024 * 1024

# Claude Code's post events. PostToolUseFailure is read when it arrives
# (a person's own settings may add it) though nable does not install it.
_CLAUDE_EVENTS = ("PostToolUse", "PostToolUseFailure")
_CURSOR_EVENT = "afterShellExecution"
_EDITOR_TOOLS = guard_plugin.EDITOR_TOOLS
_EDITOR_PATH_KEYS = ("file_path", "notebook_path")
_TOOL_USE_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_MODE_RE = re.compile(r"[A-Za-z]{1,40}")
_NO_SESSION = "<no-session>"            # ai_budget.NO_SESSION, without importing it


def tool_use_id(raw: Any) -> str | None:
    """A harness's tool call id, kept only when it looks like one (letters,
    digits and _.:-, at most 128). It is not redacted: an id the redactor
    rewrote to [REDACTED] would link every call to every other."""
    return raw if isinstance(raw, str) and _TOOL_USE_ID_RE.fullmatch(raw) else None


def digest(summary: str) -> str:
    """The command digest an outcome records: sha256 of the redacted summary."""
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16]


def _redacted(obs: dict[str, Any], name: str) -> str | None:
    """obs's command or session as the verdict recorded it (redacted, as
    guard._record and guard._session_field do), worked out on first use: the
    post hook of a tool call nobody asked about never redacts anything."""
    if name not in obs:
        raw = obs[f"_{name}"]
        obs[name] = (None if not isinstance(raw, str) or not raw else
                     guard_ledger.redact(raw, limit=128) if name == "session"
                     else guard_ledger.redact(raw))
    return obs[name]


def _args(raw: Any) -> dict | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def observed(payload: dict, harness: str | None = None) -> dict[str, Any] | None:
    """What a post payload says ran: {harness (as verdicts record it),
    event, tool, tool_use_id, and _session and _command as sent}, or None
    when it is not a post payload this hook reads. _redacted() gives the
    session and the command summary as a verdict holds them (the command is
    None where it cannot be rebuilt here: an MCP call's summary is the
    guard's translation)."""
    event = payload.get("hook_event_name")
    if harness in (None, "claude") and event in _CLAUDE_EVENTS and "turn_id" not in payload:
        tool = payload.get("tool_name")
        if not isinstance(tool, str):
            return None
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) \
            else {}
        command = None
        if tool == "Bash" and isinstance(tool_input.get("command"), str):
            command = tool_input["command"]
        elif tool in _EDITOR_TOOLS:
            path = next((tool_input[k] for k in _EDITOR_PATH_KEYS
                         if isinstance(tool_input.get(k), str) and tool_input[k]), None)
            command = f"{tool} {path}" if path else None
        mode = payload.get("permission_mode")
        return {"harness": "claude-code", "event": event, "tool": tool,
                "_session": payload.get("session_id"),
                "tool_use_id": tool_use_id(payload.get("tool_use_id")),
                "_command": command,
                "permission_mode": mode if isinstance(mode, str)
                and _MODE_RE.fullmatch(mode) else None}
    if harness in (None, "cursor") and (
            event == _CURSOR_EVENT
            or (event is None and isinstance(payload.get("command"), str)
                and isinstance(payload.get("output"), str))):
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        # The pre hook's session (guard_adapters._cursor_session): the
        # conversation only when Cursor's Admin API can measure it.
        conv = payload.get("conversation_id")
        sid = (conv.strip() if isinstance(conv, str) and conv.strip()
               and os.getenv("CURSOR_ADMIN_API_KEY", "").strip() else _NO_SESSION)
        return {"harness": "cursor", "event": _CURSOR_EVENT, "tool": "shell",
                "_session": sid, "tool_use_id": None, "_command": command}
    if harness in (None, "copilot") and event is None and "toolName" in payload \
            and "toolArgs" in payload:
        tool = payload.get("toolName")
        args = _args(payload.get("toolArgs"))
        command = args.get("command") if args else None
        if not isinstance(tool, str) or not isinstance(command, str) or not command.strip():
            return None
        return {"harness": "copilot", "event": "postToolUse", "tool": tool,
                "_session": payload.get("sessionId"), "tool_use_id": None,
                "_command": command}
    return None


def _same_session(obs: dict[str, Any], rec: dict[str, Any]) -> bool:
    """Does the verdict belong to the post payload's session? Compared as
    sent first (a session id is rarely changed by redaction), redacted only
    when that differs."""
    have = rec.get("session") or None
    raw = obs["_session"] if isinstance(obs["_session"], str) and obs["_session"] else None
    return have == raw or have == _redacted(obs, "session")


def find_ask(obs: dict[str, Any], recs: list[dict[str, Any]], *,
             now: datetime | None = None) -> tuple[dict[str, Any], str] | None:
    """(the ask this tool call answers, how it was linked) or None.

    `recs` is recent(..., outcomes=True, hashes=True), oldest first. The
    newest unanswered ask wins: an agent that retried after a "no" asked
    twice, and the second ask is the one that ran."""
    now = now or datetime.now(UTC)
    answered = {r.get("verdict") for r in recs if guard_ledger.is_outcome(r)}
    for r in reversed(recs):
        if (guard_ledger.is_outcome(r) or r.get("decision") != "ask"
                or r.get("harness") != obs["harness"]):
            continue
        if obs["tool_use_id"] and r.get("tool_use_id"):
            if r["tool_use_id"] != obs["tool_use_id"] or not _same_session(obs, r):
                continue
            return (None if r.get("_hash") in answered else (r, "tool_use_id"))
        if obs["_command"] is None or r.get("command") != _redacted(obs, "command"):
            continue
        if not _same_session(obs, r):
            continue
        try:
            age = now - datetime.fromisoformat(r["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if not timedelta(0) <= age <= LINK_WINDOW or r.get("_hash") in answered:
            continue
        return r, "command"
    return None


def record(obs: dict[str, Any], ask: dict[str, Any], linked_by: str) -> bool:
    """Append the `ran` outcome for `ask`. Never raises (guard_ledger.append)."""
    return guard_ledger.append({
        "kind": guard_ledger.OUTCOME,
        "outcome": guard_ledger.RAN,
        "harness": obs["harness"],
        "event": obs["event"],
        **({"session": _redacted(obs, "session")} if _redacted(obs, "session") else {}),
        "tool": obs["tool"],
        **({"tool_use_id": obs["tool_use_id"]} if obs["tool_use_id"] else {}),
        **({"command_digest": digest(_redacted(obs, "command"))}
           if _redacted(obs, "command") else {}),
        "verdict": ask["_hash"],
        "verdict_ts": ask.get("ts"),
        "linked_by": linked_by,
        "action_type": ask.get("action_type"),
        # Claude Code's permission mode when the call ran: under
        # bypassPermissions nobody may have been asked (ask_outcomes).
        **({"permission_mode": obs["permission_mode"]} if obs.get("permission_mode") else {}),
    })


def run_post(harness: str | None = None, stdin: Any = None, stdout: Any = None) -> int:
    """`finops guard hook --post`. Reads one post payload, appends at most one
    outcome, exits 0 whatever happens. Prints nothing, except Cursor's empty
    answer `{}` (what its cookbook's after-hooks print) to a Cursor payload."""
    obs = None
    with contextlib.suppress(Exception):
        if guard_plugin.is_off():
            return 0                    # off means unexamined and unrecorded
        payload = json.loads((stdin or sys.stdin).read())
        if not isinstance(payload, dict):
            return 0
        obs = observed(payload, harness)
        if obs is None:
            return 0
        recs = guard_ledger.recent(LOOKBACK_MINUTES, outcomes=True, hashes=True,
                                   max_bytes=_READ_MAX_BYTES)
        hit = find_ask(obs, recs)
        if hit is not None:
            record(obs, *hit)
    if obs is not None and obs["harness"] == "cursor":
        with contextlib.suppress(Exception):
            (stdout or sys.stdout).write("{}")
    return 0
