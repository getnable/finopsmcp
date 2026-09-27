"""The Claude Code settings hook fails safe, like every other harness's.

Claude Code blocks the tool call when a PreToolUse hook exits 2, and uvx exits
2 when it cannot reach the package index (the first call after an install
moves the pin to a new release, offline or in a sandbox without network, or
after `uv cache clean`). The bare `uvx --from finops-mcp==X finops guard hook`
that `nable guard install` wrote then stopped every Bash and MCP call. The
command now ends in `; exit 0`, which Claude Code's shells (sh, Git Bash,
PowerShell) all read the same way.

  - the written command exits 0 with nothing on stdout when uvx fails or is
    missing, and passes the verdict through when it runs
  - a bare hook from an earlier release is still ours (status, doctor,
    uninstall, the plugin's "a settings hook judges here" check) and
    install wraps it in place, reporting a repair
  - no harness ends up wrapped twice
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import finops.guard as g
import finops.guard_adapters as ga
import finops.guard_plugin as gp

BARE = g._UVX_HOOK_CMD
WRAPPED = g._UVX_HOOK_CMD + "; exit 0"
DURABLE = "/usr/local/bin/finops"


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Empty home and project, uv on PATH, no persistent finops binary."""
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    for var in ("CODEX_HOME", "COPILOT_HOME", "GEMINI_CLI_HOME", "CLINE_DIR", "UV_CACHE_DIR",
                "CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: None)
    uvx = home / "uv" / "bin" / "uvx"
    uvx.parent.mkdir(parents=True)
    uvx.touch(mode=0o755)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(home / "not-a-tmp"))
    monkeypatch.setattr("shutil.which",
                        lambda n, *a, **k: str(uvx) if n == "uvx" else None)
    return {"home": home, "project": proj / ".claude" / "settings.json",
            "global": home / ".claude" / "settings.json", "uvx": str(uvx)}


def _write(path: Path, command: str, matcher: str = g._HOOK_MATCHER) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-tool check"}]},
        {"matcher": matcher, "hooks": [{"type": "command", "command": command,
                                        "timeout": 30}]},
    ]}}))
    return path


def _ours(path: Path) -> str:
    [(_entry, h)] = g._read_our_hooks(path)
    return h["command"]


def _status() -> str:
    from finops import setup_wizard
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup_wizard._run_guard(argparse.Namespace(guard_action="status", guard_global=False))
    return out.getvalue()


# ── the written command, run the way Claude Code runs it ──────────────────────

def _fake_uvx(bin_dir: Path, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / "uvx"
    exe.write_text(f"#!/bin/sh\n{body}\n")
    exe.chmod(0o755)


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"),
                    reason="Claude Code runs hooks with sh; this runs the command in it")
@pytest.mark.parametrize("uvx", [
    "echo 'error: Failed to fetch: https://pypi.org/simple/finops-mcp/' >&2; exit 2",
    "echo 'error: something else' >&2; exit 1",
    None,                                   # no uvx on PATH at all
])
def test_a_launcher_that_fails_lets_the_call_through(machine, tmp_path, uvx):
    g.install()
    cmd = _ours(machine["project"])
    assert cmd == WRAPPED
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    if uvx is not None:
        _fake_uvx(bin_dir, uvx)
    r = subprocess.run(["/bin/sh", "-c", cmd], input="{}", capture_output=True, text=True,
                       env={"PATH": str(bin_dir)}, timeout=30, check=False)
    assert r.returncode == 0, "Claude Code blocks the tool call on exit 2"
    assert r.stdout == "", "stdout is Claude Code's verdict channel"


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"),
                    reason="Claude Code runs hooks with sh; this runs the command in it")
def test_the_verdict_still_reaches_claude_code(machine, tmp_path):
    g.install()
    verdict = '{"hookSpecificOutput": {"permissionDecision": "ask"}}'
    _fake_uvx(tmp_path / "fake-bin", f"echo '{verdict}'")
    r = subprocess.run(["/bin/sh", "-c", _ours(machine["project"])], input="{}",
                       capture_output=True, text=True,
                       env={"PATH": str(tmp_path / "fake-bin")}, timeout=30, check=False)
    assert r.returncode == 0 and json.loads(r.stdout) == json.loads(verdict)


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"),
                    reason="Claude Code runs hooks with sh; this runs the command in it")
def test_a_missing_binary_lets_the_call_through(machine, monkeypatch, tmp_path):
    gone = tmp_path / "gone" / "finops"
    monkeypatch.setattr(g, "_hook_command", lambda: f"{gone} guard hook")
    g.install()
    cmd = _ours(machine["project"])
    assert cmd == f"{gone} guard hook; exit 0"
    r = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True,
                       env={"PATH": str(tmp_path)}, timeout=30, check=False)
    assert r.returncode == 0 and r.stdout == ""


# ── what install writes ───────────────────────────────────────────────────────

def test_a_fresh_install_writes_the_uvx_form_fail_safe(machine):
    g.install()
    assert _ours(machine["project"]) == WRAPPED
    assert g.hook_form() == "uvx"
    assert g.blocking_hook_command(machine["project"]) is None
    assert g.hook_pin(WRAPPED) == "pinned" and g._timeout_for(WRAPPED) == 30


@pytest.mark.parametrize("found,written", [
    (DURABLE, f"{DURABLE} guard hook; exit 0"),
    ("/Users/x/My Tools/finops", '"/Users/x/My Tools/finops" guard hook; exit 0'),
])
def test_a_fresh_install_writes_the_binary_form_fail_safe(machine, monkeypatch, found, written):
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: found if n == "finops" else None)
    g.install()
    assert _ours(machine["project"]) == written
    assert g.hook_form() == "binary"
    assert g.hook_pin(written) is None and g._timeout_for(written) == 10


def test_reinstalling_the_wrapped_form_changes_nothing(machine):
    assert ga.install("claude")[0] == "new"
    before = machine["project"].read_text()
    assert ga.install("claude")[0] == "already"
    assert machine["project"].read_text() == before


# ── an existing bare hook: still ours, and wrapped in place ───────────────────

@pytest.mark.parametrize("command", [BARE, WRAPPED])
def test_either_form_is_ours(machine, command):
    path = _write(machine["project"], command)
    assert g.is_installed(path)
    assert g.broken_hook_command(path) is None
    assert g.hook_surfaces(path) == {"bash": True, "mcp": True, "editor": True}
    assert g.unpinned_hook_command(path) is None
    assert g.pinned_elsewhere_hook_command(path) is None
    assert ga.state("claude", False) == "installed"
    assert g.blocking_hook_command(path) == (BARE if command == BARE else None)


def test_install_wraps_a_bare_hook_in_place_and_calls_it_a_repair(machine):
    path = _write(machine["project"], BARE)
    assert ga.install("claude") == ("repaired", path)
    pre = json.loads(path.read_text())["hooks"]["PreToolUse"]
    assert len(pre) == 2, "wrapped in place, not appended"
    assert pre[0]["hooks"][0]["command"] == "other-tool check"
    assert pre[1]["hooks"][0] == {"type": "command", "command": WRAPPED, "timeout": 30}
    assert g.blocking_hook_command(path) is None
    assert ga.install("claude")[0] == "already"


def test_a_bare_healthy_binary_hook_keeps_its_program(machine, tmp_path):
    """Only the wrapper is added: install does not move a working hook to
    whatever it would resolve today (uvx, on this machine)."""
    exe = tmp_path / "venv" / "bin" / "finops"
    exe.parent.mkdir(parents=True)
    exe.touch(mode=0o755)
    path = _write(machine["project"], f"{exe} guard hook")
    g.install()
    assert _ours(path) == f"{exe} guard hook; exit 0"


def test_a_bare_hook_pinned_elsewhere_is_repinned_and_wrapped(machine):
    old = "uvx --from finops-mcp==0.0.1 finops guard hook"
    path = _write(machine["project"], old)
    assert ga.install("claude")[0] == "repaired"
    assert _ours(path) == WRAPPED


def test_the_cli_install_reports_and_counts_the_repair(machine, monkeypatch):
    from finops import setup_wizard
    events = []
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: events.append((e, p)))
    _write(machine["project"], BARE)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup_wizard._run_guard(argparse.Namespace(guard_action="install", guard_global=False))
    assert "Guard repaired: a hook that cannot start no longer blocks tool calls" in out.getvalue()
    [payload] = [p for e, p in events if e == "guard_installed"]
    assert payload["outcome"] == "repaired" and payload["hook_form"] == "uvx"
    assert _ours(machine["project"]) == WRAPPED


def test_the_cli_install_of_an_old_bare_pin_says_both(machine, monkeypatch):
    from finops import setup_wizard
    _write(machine["project"], "uvx --from finops-mcp==0.0.1 finops guard hook")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup_wizard._run_guard(argparse.Namespace(guard_action="install", guard_global=False))
    assert "re-pinned from finops-mcp==0.0.1" in out.getvalue()
    assert "no longer blocks tool calls" in out.getvalue()


@pytest.mark.parametrize("command", [BARE, WRAPPED])
def test_uninstall_removes_either_form_and_only_ours(machine, command):
    path = _write(machine["project"], command)
    assert ga.uninstall("claude") == (True, path)
    assert json.loads(path.read_text())["hooks"]["PreToolUse"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-tool check"}]}]
    assert not g.is_installed(path)


def test_status_flags_a_bare_hook_and_names_the_fix(machine):
    _write(machine["project"], BARE)
    out = _status()
    assert "installed, blocks tool calls when it cannot start" in out
    assert "which Claude Code reads as a block" in out
    assert "nable guard install" in out


def test_status_is_plain_installed_for_the_wrapped_form(machine):
    _write(machine["project"], WRAPPED)
    out = _status()
    assert "installed, blocks" not in out and "reads as a block" not in out


def test_doctor_offers_the_fix_for_a_bare_hook_only(machine):
    _write(machine["project"], BARE)
    d = g.doctor()
    assert d["surfaces"][0]["fail_safe"] is False
    [fix] = [f for f in d["recommendations"] if f.startswith("nable guard install")]
    assert "lets tool calls through when the hook cannot start" in fix
    g.install()
    d = g.doctor()
    assert d["surfaces"][0]["fail_safe"] is True
    assert not [f for f in d["recommendations"] if f.startswith("nable guard install")]


@pytest.mark.parametrize("command", [BARE, WRAPPED])
def test_the_plugin_stands_aside_for_either_form(machine, monkeypatch, command):
    monkeypatch.setattr(gp, "_user_dir_override", machine["home"] / ".claude")
    path = _write(machine["global"], command)
    assert gp.is_cli_hook(command)
    assert gp.cli_hook_covers("Bash", machine["project"].parent.parent) == path


# ── no harness is wrapped twice ───────────────────────────────────────────────

def test_the_wrappers_never_stack():
    for cmd in (BARE, WRAPPED, BARE + " || exit 0", BARE + ";exit 0", BARE + " ; exit 0 ;"):
        assert g._fail_safe(cmd) == WRAPPED
        assert ga._fail_safe(cmd) == WRAPPED
        assert ga._fail_safe_cmd_exe(cmd) == BARE + " || exit 0"
        assert g._bare(cmd) == BARE
    assert g.is_fail_safe(WRAPPED) and g.is_fail_safe(BARE + " || exit 0")
    assert not g.is_fail_safe(BARE) and not g.is_fail_safe(None)
    assert not g.is_fail_safe(BARE + "; exit 01")


def _harness_commands(harness: str) -> list[str]:
    if harness == "claude":
        return [h["command"] for _e, h in g._read_our_hooks(ga.hooks_path("claude", True))]
    return ga._our_commands(harness, True)


@pytest.mark.skipif(sys.platform == "win32", reason="Cline hooks are PowerShell on Windows")
@pytest.mark.parametrize("wrapped_base", [False, True])
@pytest.mark.parametrize("harness", ga.HARNESSES)
def test_every_harness_is_wrapped_once(machine, monkeypatch, harness, wrapped_base):
    """Every harness starts from guard._hook_command. Were it ever to come back
    already wrapped, no installer may add a second wrapper on top."""
    if wrapped_base:
        monkeypatch.setattr(g, "_hook_command", lambda: WRAPPED)
    ga.install(harness, True)
    ga.install(harness, True)                       # and again: still once
    commands = _harness_commands(harness)
    assert commands
    for cmd in commands:
        if harness == "cline":
            assert "exit 0" not in cmd, "Cline's script exits 0 itself"
        elif harness == "codex":
            assert cmd.endswith(" || exit 0") and cmd.count("exit 0") == 1
            assert ";" not in cmd, "cmd.exe would pass `; exit 0` to nable as arguments"
        else:
            assert cmd.endswith("; exit 0") and cmd.count("exit 0") == 1, cmd


def test_the_plugin_hook_is_wrapped_once():
    cmd = ga.plugin_hook_command()
    assert cmd.endswith("; exit 0") and cmd.count("exit 0") == 1
