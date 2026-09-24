"""The agent guard in harnesses other than Claude Code.

guard.py speaks Claude Code's PreToolUse protocol. This module carries the same
verdicts (guard.gate_command) into the hook protocols of other coding agents,
and installs the hook into their config files with the same safety bar:

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
import errno
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
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


# ── Where each harness keeps its hooks ────────────────────────────────────────

def _codex_home() -> Path:
    # Codex's find_codex_home: CODEX_HOME when set and non-empty, else ~/.codex.
    env = os.getenv("CODEX_HOME", "")
    return Path(env) if env else Path.home() / ".codex"


def config_dir(harness: str) -> Path:
    """The user-level directory whose presence means the harness is on this machine."""
    if harness == "claude":
        return Path.home() / ".claude"
    if harness == "cursor":
        return Path.home() / ".cursor"
    return _codex_home()


def hooks_path(harness: str, global_scope: bool) -> Path:
    """The file the guard hook goes into, per harness and scope."""
    if harness == "claude":
        return guard._settings_path(global_scope)
    if harness == "cursor":
        return (Path.home() if global_scope else Path.cwd()) / ".cursor" / "hooks.json"
    return (_codex_home() if global_scope else Path.cwd() / ".codex") / "hooks.json"


def detected() -> list[str]:
    """Harnesses whose config directory exists on this machine."""
    return [h for h in HARNESSES if config_dir(h).is_dir()]


def hook_command(harness: str) -> str:
    """The command a harness runs. The same as Claude Code's, on purpose (see
    the module docstring), except that Cursor gets an absolute uvx.

    Cursor is a desktop app, and an app started from the Dock does not always
    see the PATH a terminal does. A bare `uvx` it cannot find is a hook that
    fails open on every command while reporting itself installed. Codex runs
    hooks through a login shell, so the bare form resolves there.
    """
    cmd = guard._hook_command()
    if harness == "cursor" and cmd.startswith("uvx "):
        uvx = shutil.which("uvx")
        if uvx and not guard._is_ephemeral(uvx):
            cmd = (f'"{uvx}"' if " " in uvx else uvx) + cmd[len("uvx"):]
    return cmd


def _hook_timeout() -> int:
    # uvx resolves an environment per call; give the cold-cache case room.
    return 30 if guard._hook_command().startswith("uvx") else 10


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


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except ValueError:
        saved = _backup(path, raw)
        note = f" (a copy is at {saved.name})" if saved else ""
        raise _refuse(path, f"exists but is not valid JSON{note}")
    if not isinstance(data, dict):
        raise _refuse(path, f"contains a JSON {type(data).__name__}, not an object")
    return data


def _write(path: Path, data: dict) -> None:
    """Atomic replace: a crash or a full disk leaves the old file, never half
    of a new one. Writes through a symlink (a dotfiles repo) instead of over it."""
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
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2) + "\n")
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


# ── Cursor: {"version": 1, "hooks": {"beforeShellExecution": [{command, ...}]}} ──

def _cursor_entries(doc: dict, path: Path, *, create: bool) -> list | None:
    version = doc.get("version")
    if version is None and "hooks" not in doc and create:
        doc["version"] = 1
    elif version is not None and not (type(version) is int and version == 1):
        raise _refuse(path, f"declares hooks version {version!r}; nable understands version 1")
    hooks = _hooks_obj(doc, path, create=create)
    if hooks is None:
        return None
    return _list_at(hooks, _CURSOR_SHELL, path, f"hooks.{_CURSOR_SHELL}", create=create)


def _cursor_commands(doc: dict) -> list[Any]:
    hooks = doc.get("hooks")
    entries = hooks.get(_CURSOR_SHELL) if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        return []
    return [e.get("command") for e in entries if isinstance(e, dict)]


def _cursor_install(doc: dict, path: Path, cmd: str) -> str:
    entries = _cursor_entries(doc, path, create=True)
    ours = [e for e in entries if isinstance(e, dict) and _is_ours(e.get("command"))]
    if not ours:
        # failClosed is Cursor's default already. Spelled out because the whole
        # contract is fail-open, and a changed default must not flip it.
        entries.append({"command": cmd, "timeout": _hook_timeout(), "failClosed": False})
        return "new"
    return _repair(ours, cmd)


def _cursor_uninstall(doc: dict, path: Path) -> bool:
    entries = _cursor_entries(doc, path, create=False)
    if not entries:
        return False
    kept = [e for e in entries if not (isinstance(e, dict) and _is_ours(e.get("command")))]
    if len(kept) == len(entries):
        return False
    if kept:
        doc["hooks"][_CURSOR_SHELL] = kept
    else:
        del doc["hooks"][_CURSOR_SHELL]
    return True


# ── Codex: {"hooks": {"PreToolUse": [{"matcher", "hooks": [{type, command}]}]}} ──
# Codex parses hooks.json with deny_unknown_fields at the top level, so nothing
# but "hooks" (and the optional "description") may ever be added there.

def _codex_groups(doc: dict, path: Path, *, create: bool) -> list | None:
    hooks = _hooks_obj(doc, path, create=create)
    if hooks is None:
        return None
    return _list_at(hooks, "PreToolUse", path, "hooks.PreToolUse", create=create)


def _codex_handlers(doc: dict) -> list[dict]:
    hooks = doc.get("hooks")
    groups = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    out: list[dict] = []
    for group in groups if isinstance(groups, list) else []:
        inner = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(inner, list):
            out.extend(h for h in inner if isinstance(h, dict))
    return out


def _codex_install(doc: dict, path: Path, cmd: str) -> str:
    groups = _codex_groups(doc, path, create=True)
    ours = [h for h in _codex_handlers(doc) if _is_ours(h.get("command"))]
    if not ours:
        # Codex matchers are regular expressions; anchor so a tool whose name
        # merely contains "Bash" is not gated as a shell command.
        groups.append({"matcher": "^Bash$",
                       "hooks": [{"type": "command", "command": cmd,
                                  "timeout": _hook_timeout()}]})
        return "new"
    return _repair(ours, cmd)


def _codex_uninstall(doc: dict, path: Path) -> bool:
    groups = _codex_groups(doc, path, create=False)
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
            doc["hooks"]["PreToolUse"] = kept
        else:
            del doc["hooks"]["PreToolUse"]
    return removed


def _repair(ours: list[dict], cmd: str) -> str:
    """Our entry is already there. Point a dead one at a command that runs
    (the uv-cache-path failure); leave a live one, however it was written."""
    changed = False
    for entry in ours:
        if entry.get("command") != cmd and not _runnable(entry.get("command") or ""):
            entry["command"] = cmd
            entry["timeout"] = _hook_timeout()
            changed = True
    return "repaired" if changed else "already"


# ── Install / uninstall / status, per harness ─────────────────────────────────

_ADAPTERS = {
    "cursor": (_cursor_install, _cursor_uninstall, _cursor_commands),
    "codex": (_codex_install, _codex_uninstall, lambda d: [h.get("command") for h in _codex_handlers(d)]),
}


def install(harness: str, global_scope: bool = False) -> tuple[str, Path]:
    """Idempotently add the guard hook. Returns (outcome, path), outcome one of
    "new", "already", "repaired". Raises SystemExit when it refuses a file."""
    path = hooks_path(harness, global_scope)
    if harness == "claude":
        # guard.install leaves an existing entry alone, dead or not, so this
        # never reports "repaired" for Claude Code; the CLI flags a dead one.
        already = guard.is_installed(path)
        guard.install(global_scope)
        return ("already" if already else "new"), path
    doc = _load(path)
    outcome = _ADAPTERS[harness][0](doc, path, hook_command(harness))
    if outcome != "already":
        _write(path, doc)
    return outcome, path


def uninstall(harness: str, global_scope: bool = False) -> tuple[bool, Path]:
    """Remove the guard hook, and only ours. Returns (removed, path)."""
    path = hooks_path(harness, global_scope)
    if harness == "claude":
        return guard.uninstall(global_scope), path
    if not path.exists():
        return False, path
    doc = _load(path)
    removed = _ADAPTERS[harness][1](doc, path)
    if removed:
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
        doc = json.loads(path.read_text())
        ours = [c for c in _ADAPTERS[harness][2](doc) if _is_ours(c)]
    except Exception:
        return "absent"
    if not ours:
        return "absent"
    return "installed" if any(_runnable(c) for c in ours) else "broken"
