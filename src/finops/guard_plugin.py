"""The guard as a Claude Code plugin hook, and the guard's off switch.

Installing the nable plugin (`/plugin install nable@nable`) turns the guard on:
the plugin ships a PreToolUse hook (plugins/nable/hooks/hooks.json) that runs

    uvx --python 3.12 --from finops-mcp==<release> finops guard hook --via plugin; exit 0

`--via plugin` is how the hook knows where it came from. An environment
prefix (FINOPS_GUARD_VIA=plugin finops ...) would read the same in sh and Git
Bash, but Claude Code falls back to PowerShell on Windows without Git Bash,
where that prefix is a syntax error; a flag means the same in every shell.
The flag is safe here although guard_adapters keeps flags out of the hooks
it installs (an older nable rejects one with exit 2): the plugin's hook runs
the release it pins, which knows the flag, and ends in `; exit 0`.

Two things only the plugin's hook has to handle:

  One verdict, not two. Claude Code runs a plugin's hook AND an identical one
  from settings.json (it deduplicates settings files, not plugins), and the
  most restrictive answer wins. Someone who ran `nable guard install` before
  installing the plugin would get every command judged, and recorded, twice.
  So the plugin's hook stands aside, silently and without a ledger record,
  when a hook `nable guard install` wrote is in a settings file Claude Code
  applies here (user, project, project local), would see this tool call, and
  names a program that exists. Anything less than all three and the plugin
  judges: a settings hook Claude Code cannot run guards nothing.

  An off switch. Claude Code has no per-plugin hook toggle (only the global
  disableAllHooks), so `nable guard off` writes a flag file and every guard
  hook, the plugin's and the ones `nable guard install` wrote, lets each call
  through unexamined and unrecorded until `nable guard on`. FINOPS_GUARD=off in
  the agent's environment does the same without the file.

Everything here runs on every Bash and MCP tool call, before the guard is
imported, so it is standard library only and reads at most three small JSON
files. Like the rest of the guard it fails open: anything unexpected falls
through to the normal hook, which judges.

The flag file lives in nable's data directory, FINOPS_DATA_DIR or ~/.finops,
and not in a FINOPS_PROFILE directory: the hook runs in the agent's
environment, which does not carry the profile the CLI was run under.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

PLUGIN_KEY = "nable@nable"          # enabledPlugins key: <plugin>@<marketplace>
VIA_PLUGIN = "plugin"
VIA_FLAG = "--via"
OFF_ENV = "FINOPS_GUARD"
OFF_FLAG_NAME = "guard-off"
_OFF_VALUES = ("off", "0", "false", "no", "disabled")
_VIA_PLUGIN_RE = re.compile(r"--via(?:=|\s+)plugin\b")
_HARNESSES = ("claude", "cursor", "codex", "copilot", "gemini", "cline")

# Tests point these at throwaway directories (tests/conftest.py), so a
# developer's own `nable guard off` or installed plugin never reaches the
# suite; nothing else sets them.
_data_root_override: Path | None = None
_user_dir_override: Path | None = None


# ── The off switch ─────────────────────────────────────────────────────────────

def data_root() -> Path:
    """nable's data directory, without creating it (see the module docstring
    on profiles)."""
    if _data_root_override is not None:
        return _data_root_override
    raw = os.environ.get("FINOPS_DATA_DIR", "")
    return Path(raw).expanduser() if raw else Path.home() / ".finops"


def off_flag_path() -> Path:
    return data_root() / OFF_FLAG_NAME


def off_reason() -> str | None:
    """"env" when FINOPS_GUARD=off, "flag" when `nable guard off` left its
    flag file, None when the guard is on."""
    if os.environ.get(OFF_ENV, "").strip().lower() in _OFF_VALUES:
        return "env"
    try:
        if off_flag_path().exists():
            return "flag"
    except OSError:
        pass
    return None


def is_off() -> bool:
    return off_reason() is not None


def set_off(off: bool) -> Path:
    """Write (off) or remove (on) the flag file. Returns its path."""
    path = off_flag_path()
    if off:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("The nable guard is off: `nable guard on` turns it back on.\n")
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return path


# ── Claude Code settings files ────────────────────────────────────────────────

def claude_user_dir() -> Path:
    # Claude Code reads its user settings from CLAUDE_CONFIG_DIR when set.
    if _user_dir_override is not None:
        return _user_dir_override
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "")
    return Path(raw).expanduser() if raw else Path.home() / ".claude"


def settings_files(project_dir: str | os.PathLike | None = None) -> list[Path]:
    """The settings files Claude Code applies in `project_dir`, lowest
    precedence first: user, project, project local. Managed settings are left
    out: their path depends on the platform and the organisation's setup."""
    project = Path(project_dir) if project_dir else Path.cwd()
    return [claude_user_dir() / "settings.json",
            project / ".claude" / "settings.json",
            project / ".claude" / "settings.local.json"]


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def plugin_enabled(project_dir: str | os.PathLike | None = None, *,
                   user_only: bool = False) -> Path | None:
    """The settings file that turns the nable plugin on here, or None.

    `/plugin install` records the plugin under enabledPlugins
    ({"nable@nable": true}) in the settings file of the scope it was installed
    at; a later scope can turn it off again with false. `user_only` asks about
    every project (what a --global install is for), which only the user file
    answers."""
    files = settings_files(project_dir)[:1] if user_only else settings_files(project_dir)
    decided: Path | None = None
    for path in files:
        data = _read(path)
        plugins = data.get("enabledPlugins") if data else None
        if isinstance(plugins, dict) and PLUGIN_KEY in plugins:
            decided = path if plugins[PLUGIN_KEY] is True else None
    return decided


# ── Is a settings hook already judging this call? ─────────────────────────────

def matcher_covers(matcher: Any, tool_name: str) -> bool:
    """guard.matcher_covers, copied so the plugin's hook need not import the
    guard to decide it has nothing to do (tests pin that the two agree)."""
    if matcher in (None, "", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    if re.fullmatch(r"[A-Za-z0-9_\-\s,|]*", matcher):
        return tool_name in {m.strip() for m in re.split(r"[|,]", matcher)}
    try:
        return re.search(matcher, tool_name) is not None
    except re.error:
        return False


def is_cli_hook(cmd: Any) -> bool:
    """A guard hook command `nable guard install` wrote: the guard's marker,
    and not the plugin's own command (copied into a settings file, it would
    otherwise stand aside for itself and nothing would judge)."""
    return (isinstance(cmd, str) and "guard hook" in cmd and "finops" in cmd
            and not _VIA_PLUGIN_RE.search(cmd))


def _runs(cmd: str) -> bool:
    """Does the program a hook command starts with exist?"""
    cmd = cmd.strip()
    if cmd.startswith("& "):                    # PowerShell's call operator
        cmd = cmd[2:].lstrip()
    try:
        exe = cmd[1:cmd.index('"', 1)] if cmd.startswith('"') else cmd.split()[0]
    except (ValueError, IndexError):
        return False
    return bool(shutil.which(exe)) or Path(exe).exists()


def cli_hook_covers(tool_name: Any, project_dir: str | os.PathLike | None = None) -> Path | None:
    """The settings file holding a working `nable guard install` hook that
    Claude Code will also run for `tool_name`, or None."""
    if not isinstance(tool_name, str):
        return None
    for path in settings_files(project_dir):
        data = _read(path)
        hooks = data.get("hooks") if data else None
        groups = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
        for group in groups if isinstance(groups, list) else []:
            inner = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(inner, list) or not matcher_covers(group.get("matcher"), tool_name):
                continue
            for h in inner:
                cmd = h.get("command") if isinstance(h, dict) else None
                if h.get("type", "command") == "command" and is_cli_hook(cmd) and _runs(cmd):
                    return path
    return None


# ── The hook ───────────────────────────────────────────────────────────────────

def run_hook(harness: str | None = None, via: str | None = None, stdin: Any = None,
             stdout: Any = None, stderr: Any = None) -> int:
    """`finops guard hook [--via plugin]`. Always exits 0.

    The guard itself is imported only when something is left to judge, so the
    two silent answers cost a JSON parse and a few file reads."""
    if via != VIA_PLUGIN:
        from .guard_adapters import run_hook as judge
        return judge(harness, stdin, stdout, stderr)
    if is_off():
        return 0                        # silence is Claude Code's allow
    raw = ""
    try:
        raw = (stdin or sys.stdin).read()
        payload = json.loads(raw)
        if isinstance(payload, dict):
            # Claude Code's project settings sit in the project root, which
            # the payload's cwd stops being after a `cd`.
            project = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd")
            if cli_hook_covers(payload.get("tool_name"),
                               project if isinstance(project, str) else None):
                return 0
    except Exception:
        pass                            # the guard reads it again, and fails open
    from .guard_adapters import run_hook as judge
    return judge(harness, io.StringIO(raw), stdout, stderr)


def parse_hook_args(argv: list[str]) -> tuple[str | None, str | None] | None:
    """(harness, via) from the arguments after `guard hook`, or None when they
    are anything else (argparse then answers as it always has)."""
    harness = via = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        name, eq, value = arg.partition("=")
        if name not in ("--harness", VIA_FLAG):
            return None
        if not eq:
            if i + 1 >= len(argv):
                return None
            value = argv[i + 1]
            i += 1
        if name == "--harness":
            if value not in _HARNESSES:
                return None
            harness = value
        elif value == VIA_PLUGIN:
            via = value
        else:
            return None
        i += 1
    return harness, via


def hook_main(argv: list[str]) -> int | None:
    """The plugin's hook, straight from the command line: skips the CLI's
    start-up (telemetry, the argument parser for forty commands) on the path
    that runs on every tool call. None means "not a plugin hook call"."""
    parsed = parse_hook_args(argv)
    if parsed is None or parsed[1] != VIA_PLUGIN:
        return None
    return run_hook(*parsed)
