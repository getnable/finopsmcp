"""Re-running `nable guard install` has to fix the hook it finds, in place.

Two promises depended on this and neither was kept:

  - `nable guard status` tells anyone whose hook binary has vanished to re-run
    `nable guard install`, and
  - the 0.8.195 changelog told every uvx user the same about the dead
    uv-cache-path hook.

install() returned early on any existing entry, so the dead hook stayed dead
while the telemetry counted the run as "repaired". The rewrite has to stay
surgical: our entry only, same position, every foreign hook untouched.
"""
from __future__ import annotations

import json

import pytest

import finops.guard as g


@pytest.fixture
def settings(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: p)
    return p


def _uv_only(monkeypatch):
    """A machine with uv on PATH and no persistent finops binary."""
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/uvx" if n == "uvx" else None)


def _pre(path):
    return json.loads(path.read_text())["hooks"]["PreToolUse"]


def test_reinstalling_repairs_a_dead_hook_in_place(settings, monkeypatch):
    settings.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-tool check"}]},
        {"matcher": "Bash", "hooks": [{"type": "command",
                                       "command": "/gone/bin/finops guard hook",
                                       "timeout": 10}]},
    ]}}))
    _uv_only(monkeypatch)
    assert g.broken_hook_command(settings) == "/gone/bin/finops guard hook"

    g.install()

    pre = _pre(settings)
    assert len(pre) == 2, "repair must rewrite, not append a second guard"
    assert pre[0]["hooks"][0]["command"] == "other-tool check"
    ours = pre[1]["hooks"][0]
    assert ours["command"] == g._UVX_HOOK_CMD
    assert ours["timeout"] == 30, "the uvx form needs its longer cold-cache timeout"
    assert g.broken_hook_command(settings) is None
    assert json.loads(settings.read_text())["model"] == "opus"


def test_a_healthy_hook_is_left_byte_for_byte(settings, monkeypatch, tmp_path):
    exe = tmp_path / "bin" / "finops"
    exe.parent.mkdir(parents=True)
    exe.touch(mode=0o755)
    body = json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command",
                                       "command": f"{exe} guard hook", "timeout": 10}]},
    ]}}, indent=4)
    settings.write_text(body)
    monkeypatch.setattr("shutil.which", lambda n: None)
    g.install()
    assert settings.read_text() == body, "install rewrote a hook that was fine"


def test_a_user_raised_timeout_survives_the_repair(settings, monkeypatch):
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command",
                                       "command": "/gone/bin/finops guard hook",
                                       "timeout": 90}]},
    ]}}))
    _uv_only(monkeypatch)
    g.install()
    assert _pre(settings)[0]["hooks"][0]["timeout"] == 90
