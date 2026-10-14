"""An engineer's first hour with the CLI: the commands the docs teach, run the
way a person runs them, in a fresh home with no cloud account.

Each test here started as a finding from walking that path:

  - `nable doctor --json` was refused ("unrecognized arguments") although
    `finops-doctor --json` and every other read command take --json
  - a data directory the user cannot write (the `sudo nable ...` once case)
    made `nable doctor`, `nable org init` and `nable learn list` end in a
    Python traceback, with no word on what to do
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from finops import setup_wizard

SRC = Path(__file__).resolve().parents[1] / "src"


def _cli(args: list[str], home: Path, **env_extra: str) -> subprocess.CompletedProcess:
    """`nable <args>` in a fresh home, with no cloud credentials in reach."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AWS_", "AZURE_", "GOOGLE_", "FINOPS_", "CLAUDE"))}
    env.update(HOME=str(home), PYTHONPATH=str(SRC), NABLE_NO_TELEMETRY="1",
               PYTHONDONTWRITEBYTECODE="1", **env_extra)
    return subprocess.run([sys.executable, "-m", "finops.entry", *args], cwd=str(home),
                          capture_output=True, text=True, env=env, timeout=120, check=False)


# ── nable doctor --json ───────────────────────────────────────────────────────

def test_doctor_takes_json_like_finops_doctor(tmp_path):
    r = _cli(["doctor", "--json"], tmp_path)
    assert "unrecognized arguments" not in r.stderr, r.stderr
    checks = json.loads(r.stdout)
    assert isinstance(checks, list) and checks
    assert {"Python version", "License"} <= {c["name"] for c in checks}
    assert "Traceback" not in r.stderr


def test_doctor_audit_flags_reach_the_doctor(tmp_path):
    r = _cli(["doctor", "--audit", "--hours", "1", "--limit", "5", "--json"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert isinstance(json.loads(r.stdout), (list, dict))


# ── a data directory this user cannot write ───────────────────────────────────

@pytest.fixture
def unwritable_data_dir(tmp_path, monkeypatch):
    """~/.finops exists, and every attempt to write under it fails the way it
    does when a `sudo nable ...` run left it owned by root. Running as root,
    the suite cannot get that refusal from the file system, so the two calls
    the tracebacks went through raise it."""
    home = tmp_path / "home"
    data = home / ".finops"
    data.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FINOPS_DATA_DIR", raising=False)
    monkeypatch.delenv("FINOPS_DEBUG", raising=False)
    monkeypatch.chdir(home)

    def refuse(*_a, **_k):
        raise PermissionError(errno.EACCES, "Permission denied", str(data / "org"))
    monkeypatch.setattr("finops.org.store.ensure_dir", refuse)

    def refuse_chmod(*_a, **_k):
        raise PermissionError(errno.EPERM, "Operation not permitted", str(data))
    monkeypatch.setattr("finops.storage.db._DATA_DIR", None)
    monkeypatch.setattr("finops.storage.db.data_dir", refuse_chmod)
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: None)
    return data


@pytest.mark.parametrize("argv", [["org", "init", "--no-adapters"], ["doctor"]])
def test_an_unwritable_data_dir_says_what_and_how_without_a_traceback(
        argv, unwritable_data_dir, capsys):
    with pytest.raises(SystemExit) as e:
        setup_wizard.main(argv)
    err = capsys.readouterr().err + str(e.value.code if not isinstance(e.value.code, int) else "")
    assert e.value.code not in (0, None)
    assert str(unwritable_data_dir) in err
    assert "chown" in err and "FINOPS_DATA_DIR" in err
    assert "Traceback" not in err


def test_finops_debug_still_shows_the_traceback(unwritable_data_dir, monkeypatch):
    monkeypatch.setenv("FINOPS_DEBUG", "1")
    with pytest.raises(PermissionError):
        setup_wizard.main(["org", "init", "--no-adapters"])


def test_a_database_it_cannot_open_in_someone_elses_dir_gets_the_same_fix(
        tmp_path, monkeypatch, capsys):
    """`nable learn list` met the unwritable directory in SQLite first and
    ended in sqlalchemy's "unable to open database file" traceback."""
    data = tmp_path / "data"
    (data / "finops.db").mkdir(parents=True)     # SQLite cannot open a directory
    monkeypatch.setenv("FINOPS_DATA_DIR", str(data))
    monkeypatch.delenv("FINOPS_DEBUG", raising=False)
    monkeypatch.setattr("finops.storage.db._DATA_DIR", None)
    monkeypatch.setattr("finops.storage.db._ENGINE", None)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(data).st_uid + 4242)
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: None)
    with pytest.raises(SystemExit) as e:
        setup_wizard.main(["learn", "list"])
    err = capsys.readouterr().err
    assert e.value.code == 1
    assert str(data) in err and "chown" in err and "Traceback" not in err


def test_brief_names_the_directory_instead_of_the_exception_class(
        unwritable_data_dir, monkeypatch, capsys):
    def refuse(**_k):
        raise PermissionError(errno.EACCES, "Permission denied",
                              str(unwritable_data_dir / "briefs"))
    monkeypatch.setattr("finops.briefing.run.run_overnight", refuse)
    with pytest.raises(SystemExit) as e:
        setup_wizard.main(["brief", "--no-save"])
    err = capsys.readouterr().err
    assert e.value.code == 1
    assert "failed (PermissionError)" not in err
    assert str(unwritable_data_dir) in err and "chown" in err


# ── no uvx ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def guard_home(tmp_path, monkeypatch):
    import finops.guard_plugin as gp
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "FINOPS_DATA_DIR", "FINOPS_GUARD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gp, "_data_root_override", None)
    monkeypatch.setattr(gp, "_user_dir_override", None)
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: None)
    return home


def _no_uvx(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: None)


def _out(argv, capsys) -> str:
    try:
        setup_wizard.main(argv)
    except SystemExit as e:
        assert not e.code, e.code
    return " ".join(capsys.readouterr().out.split())


def test_install_without_uvx_says_the_hook_cannot_start(guard_home, monkeypatch, capsys):
    """With no uvx and no `finops` that stays on PATH, install wrote a uvx
    hook Claude Code cannot run and printed "Agent cost guardrail installed"."""
    _no_uvx(monkeypatch)
    out = _out(["guard", "install", "--global"], capsys)
    assert "Agent cost guardrail installed" not in out
    assert "uvx is not on PATH" in out and "https://docs.astral.sh/uv/" in out


def test_status_and_doctor_name_uv_not_a_reinstall_that_writes_the_same(
        guard_home, monkeypatch, capsys):
    """`nable guard status` said "Re-run: nable guard install", which writes
    the same hook again: the fix is uv (or a `finops` on PATH)."""
    import finops.guard as g
    monkeypatch.setattr(g, "_hook_command", lambda: g._UVX_HOOK_CMD)
    g.install(True)
    _no_uvx(monkeypatch)
    out = _out(["guard", "status"], capsys)
    assert "uvx is not on PATH" in out and "https://docs.astral.sh/uv/" in out
    d = g.doctor()
    assert any("uvx, which is not on PATH" in c for c in d["not_covered"])
    assert any("https://docs.astral.sh/uv/" in r for r in d["recommendations"])


def test_the_status_footer_says_what_each_hook_sees(guard_home, capsys):
    """The footer said Cursor and Codex hooks see shell commands only and that
    --all covers Claude Code, Cursor and Codex. Both install MCP hooks too
    (beforeMCPExecution, mcp__.*), and --all covers all six agents."""
    out = _out(["guard", "status"], capsys)
    assert "in Cursor and Codex, shell commands" not in out
    assert "(Claude Code, Cursor, Codex: each one found)" not in out
    assert ("In Claude Code, Cursor and Codex CLI the hook sees shell commands and MCP tool "
            "calls") in out
    assert "every supported agent found" in out


def test_setup_slack_bot_does_not_send_people_to_a_command_that_is_not_here(
        tmp_path, monkeypatch, capsys):
    """Choice 3 of `nable setup slack` collected three tokens and ended with
    "Start it: finops-slack", a console script this package no longer has."""
    monkeypatch.setenv("HOME", str(tmp_path))
    answers = iter(["3"])
    monkeypatch.setattr(setup_wizard, "_prompt", lambda *a, **k: next(answers))
    assert setup_wizard.setup_slack() is False
    out = capsys.readouterr().out
    assert "finops-slack" not in out and "self-hosted" in out


# ── nable brief with no usable AWS credentials ────────────────────────────────

def _unreadable_audit(code: str, seen: dict):
    """run_deep_audit on a machine whose every AWS call fails with `code`, as
    it does with no credentials (NoCredentialsError) or an expired SSO login
    (TokenRetrievalError)."""
    import logging

    def audit(*_a, **_k):
        seen["analyzer_warnings"] = logging.getLogger(
            "finops.analyzers.optimizer").isEnabledFor(logging.WARNING)
        return {"findings": [], "checks_run": [], "regions_scanned": ["us-east-1"],
                "checks_failed": [{"check": c, "region": "us-east-1", "error_code": code}
                                  for c in ("ebs", "nat", "rds")]}
    return audit


@pytest.mark.parametrize("code,exit_code,fix", [
    ("NoCredentialsError", 6, "aws configure sso"),
    ("TokenRetrievalError", 3, "aws sso login --profile dev"),
])
def test_brief_on_an_unreadable_account_says_how_to_fix_it(code, exit_code, fix, tmp_path,
                                                          monkeypatch, capsys):
    """`nable brief` exited 0 here, listed every check as failed with the
    exception's class name, and printed one log line per failed check, but
    never said what to do. It now ends the way `nable scan` does."""
    seen: dict = {}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AWS_PROFILE", "dev")
    monkeypatch.delenv("FINOPS_DEBUG", raising=False)
    monkeypatch.setattr("finops.storage.db._DATA_DIR", None)
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: None)
    monkeypatch.setattr("finops.analyzers.optimizer.run_deep_audit",
                        _unreadable_audit(code, seen))
    with pytest.raises(SystemExit) as e:
        setup_wizard.main(["brief", "--no-save"])
    err = capsys.readouterr().err
    assert e.value.code == exit_code
    assert fix in err
    assert seen["analyzer_warnings"] is False, "one log line per failed check, again"
