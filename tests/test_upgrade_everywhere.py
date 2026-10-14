"""`nable upgrade` moves every pin nable wrote, and nothing it did not.

It used to repin Claude Desktop only. The setup wizard also writes Cursor's
~/.cursor/mcp.json and tells Claude Code users the `claude mcp add` line that
lands in ~/.claude.json, and `nable guard install` writes a uvx hook pinned to
one release into six agents' settings. After an upgrade all of those still ran
the old release. An entry without a finops-mcp==X pin is one the user wrote
(the README's manual config, Cursor's one-click link): it stays as written.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import finops.guard as g
import finops.guard_adapters as ga
import finops.setup_wizard as sw

V = g.__version__
SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def machine(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    for var in ("CODEX_HOME", "COPILOT_HOME", "GEMINI_CLI_HOME", "CLINE_DIR", "APPDATA",
                "UV_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
    uvx = home / "uv" / "bin" / "uvx"
    uvx.parent.mkdir(parents=True)
    uvx.touch(mode=0o755)
    # The fake uvx lives under the test's tmp dir; it must not read as a
    # throwaway path to the guard (guard._is_ephemeral).
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(home / "not-a-tmp"))
    monkeypatch.setattr("shutil.which", lambda n: str(uvx) if n == "uvx" else None)
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: None)
    monkeypatch.setattr(sw, "_upgrade_running_cli", lambda current, target: True)
    return {"home": home, "proj": proj, "uvx": str(uvx)}


def _fake_run(calls: list, on_guard=None):
    def run(cmd, **kw):
        calls.append(list(cmd))
        if on_guard and "guard" in cmd:
            on_guard(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def _write(path: Path, doc: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path


def _entry(release: str, extras: str = "") -> dict:
    pkg = f"finops-mcp[{extras}]" if extras else "finops-mcp"
    return {"command": "/opt/uv/uvx", "args": ["--python", "3.12", f"{pkg}=={release}"]}


# ── the MCP configs the wizard writes ─────────────────────────────────────────

def test_every_pinned_mcp_config_moves_and_nothing_else_does(machine, monkeypatch, capsys):
    home = machine["home"]
    desktop = _write(home / ".config" / "Claude" / "claude_desktop_config.json",
                     {"mcpServers": {"nable": _entry("0.8.50")}})
    cursor = _write(home / ".cursor" / "mcp.json", {"mcpServers": {
        "nable": _entry("0.8.50", "azure"),
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
    }})
    claude_code = _write(home / ".claude.json", {
        "numStartups": 12,
        "mcpServers": {"nable": {"type": "stdio", **_entry("0.9.1"), "env": {}}},
        "projects": {"/work/app": {"mcpServers": {"nable": _entry("0.9.1")}}},
    })
    calls: list = []
    monkeypatch.setattr("subprocess.run", _fake_run(calls))

    assert sw._run_upgrade("9.9.9") == 0

    def args(path, *keys):
        doc = json.loads(path.read_text())
        for k in keys:
            doc = doc[k]
        return doc["args"]
    assert args(desktop, "mcpServers", "nable") == ["--python", "3.12", "finops-mcp==9.9.9"]
    # The entry's own extras stay: only the version moves.
    assert args(cursor, "mcpServers", "nable") == ["--python", "3.12",
                                                   "finops-mcp[azure]==9.9.9"]
    assert json.loads(cursor.read_text())["mcpServers"]["github"]["args"] == [
        "-y", "@modelcontextprotocol/server-github"]
    assert args(claude_code, "mcpServers", "nable")[-1] == "finops-mcp==9.9.9"
    assert args(claude_code, "projects", "/work/app", "mcpServers", "nable")[-1] == \
        "finops-mcp==9.9.9"
    assert json.loads(claude_code.read_text())["numStartups"] == 12
    out = capsys.readouterr().out
    for client in ("Claude Desktop", "Cursor", "Claude Code"):
        assert client in out
    # The cache was warmed at the new release before any pin moved.
    assert calls and "finops-mcp==9.9.9" in " ".join(calls[0])


def test_entries_without_a_pin_are_left_as_written(machine, monkeypatch, capsys):
    """The README's manual config and Cursor's one-click link both write an
    unpinned `uvx --python 3.12 finops-mcp`: the user chose "latest"."""
    home = machine["home"]
    readme_form = {"mcpServers": {"nable": {"command": "uvx",
                                            "args": ["--python", "3.12", "finops-mcp"]}}}
    desktop = _write(home / ".config" / "Claude" / "claude_desktop_config.json", readme_form)
    cursor = _write(home / ".cursor" / "mcp.json",
                    {"mcpServers": {"nable": {"command": "/usr/local/bin/finops-mcp"}}})
    before = (desktop.read_text(), cursor.read_text())
    monkeypatch.setattr("subprocess.run", _fake_run([]))

    assert sw._run_upgrade("9.9.9") == 0

    assert (desktop.read_text(), cursor.read_text()) == before
    out = capsys.readouterr().out
    assert str(desktop) in out and "no finops-mcp==" in out


def test_a_config_already_on_the_target_is_not_rewritten(machine, monkeypatch):
    cfg = _write(machine["home"] / ".cursor" / "mcp.json",
                 {"mcpServers": {"nable": _entry("9.9.9")}})
    cfg.chmod(0o640)
    mtime = cfg.stat().st_mtime_ns
    monkeypatch.setattr("subprocess.run", _fake_run([]))
    assert sw._run_upgrade("9.9.9") == 0
    assert cfg.stat().st_mtime_ns == mtime


# ── the guard hooks `nable guard install` wrote ───────────────────────────────

_HARNESSES = [h for h in ga.HARNESSES if not (h == "cline" and sys.platform == "win32")]


def _install_hooks_pinned_to(release: str) -> dict[str, Path]:
    paths = {}
    for h in _HARNESSES:
        _outcome, path = ga.install(h, True)
        path.write_text(path.read_text().replace(f"finops-mcp=={V}", f"finops-mcp=={release}"))
        paths[h] = path
    return paths


def _releases(h: str) -> set:
    if h == "claude":
        return {g.hook_release(hk["command"])
                for _e, hk in g._read_our_hooks(ga.hooks_path("claude", True))}
    return {ga.uvx_release(c) for c in ga._our_commands(h, True)}


def test_hooks_pinned_to_an_old_release_follow_the_upgrade(machine, monkeypatch, capsys):
    """At the running release, the guard's own install repins each hook in
    place (guard_adapters.install), the same repair `nable guard install`
    makes: nothing here rewrites a hook by hand."""
    paths = _install_hooks_pinned_to("0.0.1")
    other = {"type": "command", "command": "my-own-linter --check"}
    codex = json.loads(paths["codex"].read_text())
    codex["hooks"]["PreToolUse"].append({"matcher": "^Bash$", "hooks": [other]})
    paths["codex"].write_text(json.dumps(codex))
    repaired: list = []
    real = ga.install
    monkeypatch.setattr(ga, "install", lambda h, s=False: repaired.append((h, s)) or real(h, s))
    calls: list = []
    monkeypatch.setattr("subprocess.run", _fake_run(calls))

    assert sw._run_upgrade(V) == 0

    assert sorted(repaired) == sorted((h, True) for h in _HARNESSES)
    for h in _HARNESSES:
        assert _releases(h) == {V}, h
    assert other in json.loads(paths["codex"].read_text())["hooks"]["PreToolUse"][-1]["hooks"]
    assert not any("guard" in c for c in calls)
    out = capsys.readouterr().out
    assert "Codex CLI" in out and "0.0.1" in out


def test_at_a_new_release_the_hooks_are_repinned_by_that_release(machine, monkeypatch):
    """The process running the upgrade is the old release; its install would
    pin the old one. The new release's own `guard install`, from the uvx cache
    the upgrade just warmed, repins each hook instead."""
    paths = _install_hooks_pinned_to(V)

    def new_release_installs(cmd):
        h = cmd[cmd.index("--harness") + 1]
        p = paths[h]
        p.write_text(p.read_text().replace(f"finops-mcp=={V}", "finops-mcp==9.9.9"))
    calls: list = []
    monkeypatch.setattr("subprocess.run", _fake_run(calls, new_release_installs))

    assert sw._run_upgrade("9.9.9") == 0

    guard_calls = [c for c in calls if "guard" in c]
    for h in _HARNESSES:
        want = [machine["uvx"], "--from", "finops-mcp==9.9.9", "finops", "guard", "install",
                "--harness", h, "--global"] + (["--force"] if h == "claude" else [])
        assert want in guard_calls, h
        assert _releases(h) == {"9.9.9"}, h


def test_a_hook_that_did_not_move_fails_the_upgrade(machine, monkeypatch, capsys):
    _install_hooks_pinned_to(V)
    monkeypatch.setattr("subprocess.run", _fake_run([]))     # the install changes nothing
    assert sw._run_upgrade("9.9.9") == 1
    assert "still" in capsys.readouterr().out


def test_binary_and_hand_written_hooks_are_not_touched(machine, monkeypatch):
    exe = machine["home"] / "bin" / "finops"
    exe.parent.mkdir()
    exe.touch(mode=0o755)
    hooks = ga.hooks_path("codex", True)
    _write(hooks, {"hooks": {"PreToolUse": [{"matcher": "^Bash$", "hooks": [
        {"type": "command", "command": f"{exe} guard hook"}]}]}})
    before = hooks.read_text()
    calls: list = []
    monkeypatch.setattr("subprocess.run", _fake_run(calls))
    assert sw._run_upgrade("9.9.9") == 0
    assert hooks.read_text() == before
    assert not any("guard" in c for c in calls)


# ── offline, and the help text ────────────────────────────────────────────────

def test_upgrade_without_pypi_exits_1(machine, monkeypatch, capsys):
    """"Could not reach PyPI" and exit 0 read as an upgrade to a script."""
    monkeypatch.setattr(sw, "_latest_pypi_version", lambda: None)
    with pytest.raises(SystemExit) as e:
        sw.main(["upgrade"])
    assert e.value.code == 1
    assert "Could not reach PyPI" in capsys.readouterr().err


def test_a_release_that_cannot_be_cached_exits_1_and_moves_no_pin(machine, monkeypatch):
    cfg = _write(machine["home"] / ".cursor" / "mcp.json",
                 {"mcpServers": {"nable": _entry("0.8.50")}})
    before = cfg.read_text()
    monkeypatch.setattr("subprocess.run",
                        lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="no"))
    assert sw._run_upgrade("9.9.9") == 1
    assert cfg.read_text() == before


def test_upgrade_help_says_what_it_repins(tmp_path):
    r = subprocess.run([sys.executable, "-m", "finops.entry", "upgrade", "--help"], check=False,
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                       env={"HOME": str(tmp_path), "PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin",
                            "NABLE_NO_TELEMETRY": "1"})
    assert r.returncode == 0, r.stderr
    text = " ".join(r.stdout.split())
    for needle in ("Claude Desktop", "~/.cursor/mcp.json", "~/.claude.json",
                   "nable guard install", "Codex CLI", "Gemini CLI", "Cline",
                   "finops-mcp==", "left as it is"):
        assert needle in text, needle
