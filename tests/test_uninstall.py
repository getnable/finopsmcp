"""`nable uninstall` takes nable out of everything nable put it into.

There was no uninstall. `nable uninstall` answered "unknown command, did you
mean: mistral", and removing the package left nable registered in Claude
Desktop, Cursor and Claude Code (each then failing to start a server that no
longer existed) and the guard hook in every agent it was installed into, which
then blocked or errored on every shell command.
"""
from __future__ import annotations

import contextlib
import io
import json

import pytest

import finops.setup_wizard as W


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("APPDATA", str(h / "AppData" / "Roaming"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.chdir(proj)
    import finops.telemetry as tel
    monkeypatch.setattr(tel, "_send_event", lambda *a, **k: None)
    return h


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return path


def _setup(h):
    other = {"command": "npx", "args": ["other-server"]}
    nable = {"command": "uvx", "args": ["--python", "3.12", "finops-mcp==1.0"]}
    desktop = _write(h / ".config" / "Claude" / "claude_desktop_config.json",
                     {"mcpServers": {"nable": nable, "other": other}, "theme": "dark"})
    cursor = _write(h / ".cursor" / "mcp.json", {"mcpServers": {"finops": nable, "other": other}})
    code = _write(h / ".claude.json", {
        "mcpServers": {"nable": nable, "other": other},
        "projects": {"/r": {"mcpServers": {"nable": nable}, "history": [1]}}})
    from finops import guard_adapters as ga
    ga.install("claude", True)
    ga.install("claude", False)
    ga.install("cursor", True)
    for d in (".finops", ".finops-mcp", ".config/finops", ".nable"):
        (h / d).mkdir(parents=True, exist_ok=True)
        (h / d / "state").write_text("x")
    return desktop, cursor, code


def _run(*args, answers=()):
    it = iter(answers)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        W._run_uninstall(*args, prompt=lambda *a, **k: next(it, "n"))
    return out.getvalue()


def test_uninstall_removes_editor_entries_and_hooks_and_keeps_data(home):
    desktop, cursor, code = _setup(home)
    out = _run(False, True, False)
    for p in (desktop, cursor, code):
        doc = json.loads(p.read_text())
        assert "nable" not in doc["mcpServers"] and "finops" not in doc["mcpServers"]
        assert doc["mcpServers"]["other"] == {"command": "npx", "args": ["other-server"]}
    assert json.loads(desktop.read_text())["theme"] == "dark"
    assert json.loads(code.read_text())["projects"]["/r"] == {"mcpServers": {}, "history": [1]}
    from finops import guard_adapters as ga
    for scope in (True, False):
        assert ga.state("claude", scope) == "absent"
    assert ga.state("cursor", True) == "absent"
    for d in (".finops", ".finops-mcp", ".config/finops", ".nable"):
        assert (home / d).exists()
        assert str(home / d) in out
    assert "--purge" in out


def test_purge_deletes_the_state_directories(home):
    _setup(home)
    _run(True, True, False)
    for d in (".finops", ".finops-mcp", ".config/finops", ".nable"):
        assert not (home / d).exists(), d


def test_purge_without_yes_asks_and_a_no_keeps_everything(home):
    _setup(home)
    out = _run(True, False, False, answers=("y", "n"))
    for d in (".finops", ".finops-mcp", ".config/finops", ".nable"):
        assert (home / d).exists(), d
    assert "kept" in out.lower()


def test_without_yes_a_no_changes_nothing(home):
    desktop, _, _ = _setup(home)
    before = desktop.read_text()
    _run(False, False, False, answers=("n",))
    assert desktop.read_text() == before


def test_dry_run_changes_nothing(home):
    desktop, cursor, code = _setup(home)
    before = [p.read_text() for p in (desktop, cursor, code)]
    out = _run(True, True, True)
    assert [p.read_text() for p in (desktop, cursor, code)] == before
    assert (home / ".finops").exists()
    assert str(desktop) in out


def test_uninstall_is_a_command_and_a_typo_suggests_it(capsys):
    with pytest.raises(SystemExit):
        W.main(["unistall"])
    err = capsys.readouterr().err
    assert "uninstall" in err
    assert "mistral" not in err
