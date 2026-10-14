"""Can uvx start the finops-mcp release a guard hook is pinned to?

The Claude Code plugin's hook, and the hook `nable guard install` writes where
no `finops` stays on PATH, start nable through uvx at one exact release:

    uvx --python 3.12 --from finops-mcp==X finops guard hook --via plugin; exit 0

When uvx cannot install X (the release is not on the package index yet, it
was yanked, or the index cannot be reached), uvx exits non-zero, the hook's
`; exit 0` turns that into Claude Code's silent allow (on purpose: a guard
that cannot start must never block the agent), and uvx's error goes to Claude
Code's debug log. Every command then runs unchecked while `nable guard
status` said "on" and `nable guard doctor` listed the calls as covered.
Measured on 0.11.0, which the plugin pinned before it was on PyPI.

This asks uvx itself, the way the hook does, in at most two bounded runs:

  1. `uvx --offline ... finops --version`: the release is in uv's cache, so
     the hook starts. No network.
  2. `uvx ... finops --version`: what the hook's first call does, which also
     caches the release for it. Skipped under FINOPS_AIRGAP.

A release that cannot be told either way (no uvx, a timeout, air-gapped)
reads as no problem: this reports what uvx said, never a guess. Answers are
kept for the life of the process. FINOPS_GUARD_RELEASE_CHECK=0 turns the
check off (the suite sets it, and replaces _probe_override, so tests never
run uvx or reach an index: tests/conftest.py).
"""
from __future__ import annotations

import json
import os
import re
import shutil
# Runs uvx with a fixed argument list and no shell.
import subprocess  # nosec B404
from collections.abc import Callable
from pathlib import Path
from typing import Any

_RELEASE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+!_-]*")
_PYTHON_RE = re.compile(r"--python(?:=|\s+)([0-9][0-9.]*)\b")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

CHECK_ENV = "FINOPS_GUARD_RELEASE_CHECK"
OFFLINE_TIMEOUT_S = 30
ONLINE_TIMEOUT_S = 120

# Tests: a callable (release, python) -> str | None standing in for _probe.
_probe_override: Callable[[str, str | None], str | None] | None = None
_answers: dict[tuple[str, str | None], str | None] = {}


def _airgapped() -> bool:
    return os.environ.get("FINOPS_AIRGAP", "").strip().lower() not in ("", "0", "false", "no",
                                                                        "off")


def _condense(text: str) -> str:
    """uv's error, one line: no colour, no box drawing, the sentence that
    says why first."""
    text = _ANSI_RE.sub("", text or "")
    text = re.sub(r"[×╰─▶│]", " ", text)
    flat = " ".join(text.split())
    at = flat.find("Because ")
    if at > 0:
        flat = flat[at:]
    return flat[:300].rstrip()


def _run(argv: list[str], timeout: float) -> tuple[bool | None, str]:
    """(started, stderr): True when the command exited 0, False when it ran
    and failed, None when it could not be run or did not finish."""
    env = {**os.environ, "NO_COLOR": "1", "UV_NO_PROGRESS": "1"}
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,  # nosec B603
                           env=env, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return r.returncode == 0, r.stderr or r.stdout or ""


def _probe(release: str, python: str | None) -> str | None:
    uvx = shutil.which("uvx")
    if not uvx:
        return None                     # status already says uvx is missing
    tail = [*(["--python", python] if python else []), "--from",
            f"finops-mcp=={release}", "finops", "--version"]
    started, _ = _run([uvx, "--offline", *tail], OFFLINE_TIMEOUT_S)
    if started:
        return None
    if _airgapped():
        return None
    started, err = _run([uvx, *tail], ONLINE_TIMEOUT_S)
    if started is not False:
        return None
    return _condense(err) or "uvx could not install it"


def problem(release: str, python: str | None = None,
            note: Callable[[str], Any] | None = None) -> str | None:
    """Why uvx cannot start finops-mcp==`release` here (uvx's own words), or
    None when it can or when that cannot be told. `note` hears a line before
    the one slow case, an online check, so a terminal is not left silent."""
    if not isinstance(release, str) or not _RELEASE_RE.fullmatch(release):
        return None
    if python is not None and not re.fullmatch(r"[0-9][0-9.]*", python):
        python = None
    key = (release, python)
    if key in _answers:
        return _answers[key]
    if os.environ.get(CHECK_ENV, "").strip().lower() in ("0", "false", "no", "off"):
        return None
    if _probe_override is not None:
        answer = _probe_override(release, python)
    else:
        if note is not None and not _airgapped():
            note(f"checking that uvx can start finops-mcp {release}, the release the "
                 "guard hook runs")
        answer = _probe(release, python)
    _answers[key] = answer
    return answer


def hook_problem(cmd: Any, note: Callable[[str], Any] | None = None) -> tuple[str, str] | None:
    """(release, why) when `cmd` is a uvx guard hook pinned to a release uvx
    cannot start here, else None (a binary hook, an unpinned one, or one that
    starts)."""
    from .guard_adapters import uvx_release
    release = uvx_release(cmd) if isinstance(cmd, str) else None
    if not release:
        return None
    m = _PYTHON_RE.search(cmd)
    why = problem(release, m.group(1) if m else None, note)
    return (release, why) if why else None


# ── the installed Claude Code plugin ───────────────────────────────────────────

def _installed_plugin_dirs(plugins: Path) -> list[Path]:
    """Where Claude Code says the nable plugin is installed
    (installed_plugins.json), or [] when it does not say."""
    from .guard_plugin import PLUGIN_KEY
    try:
        doc = json.loads((plugins / "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = (doc.get("plugins") or {}).get(PLUGIN_KEY) if isinstance(doc, dict) else None
    if isinstance(entries, dict):
        entries = [entries]
    dirs = []
    for e in entries if isinstance(entries, list) else []:
        path = e.get("installPath") if isinstance(e, dict) else None
        if isinstance(path, str) and path:
            dirs.append(Path(path))
    return dirs


def plugin_hook_commands() -> list[str]:
    """The guard hook commands of the nable plugin Claude Code installed: its
    hooks.json under Claude Code's plugins directory, narrowed to the install
    installed_plugins.json names when it names one. Read-only, never raises."""
    from .guard_adapters import plugin_hook_files
    from .guard_plugin import claude_user_dir
    try:
        plugins = claude_user_dir() / "plugins"
    except (OSError, ValueError, RuntimeError):
        return []
    installed = _installed_plugin_dirs(plugins)
    found: list[str] = []
    for path, cmds in plugin_hook_files(plugins):
        if installed and not any(d == path.parent.parent or d in path.parents
                                 for d in installed):
            continue
        found += [c for c in cmds if c not in found]
    return found


def plugin_problem(note: Callable[[str], Any] | None = None) -> tuple[str, str] | None:
    """(release, why) when the installed plugin's guard hook cannot start
    because uvx cannot install the release it pins. With several installed
    releases, only when none of them starts: which one runs is Claude Code's
    to say."""
    cmds = plugin_hook_commands()
    stuck = [hook_problem(c, note) for c in cmds]
    if cmds and all(stuck):
        return stuck[0]
    return None
