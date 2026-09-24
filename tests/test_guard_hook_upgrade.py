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


# ── the uvx form is pinned to the release that wrote it ───────────────────────
#
# Unpinned, `uvx --from finops-mcp` resolved the newest PyPI release on every
# agent tool call: whoever could publish to that name could run code on every
# guarded machine on the next Bash call, with no install step and no prompt.

from finops import __version__  # noqa: E402

LEGACY = "uvx --from finops-mcp finops guard hook"
PINNED = f"uvx --from finops-mcp=={__version__} finops guard hook"


def test_the_uvx_fallback_is_pinned_to_this_release(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: None)
    assert g._hook_command() == PINNED


@pytest.mark.parametrize("cmd,pin", [
    (LEGACY, "unpinned"),
    ("uvx --from finops-mcp@latest finops guard hook", "unpinned"),
    ("uvx --from finops-mcp>=0.8 finops guard hook", "unpinned"),
    (PINNED, "pinned"),
    ("uvx --from 'finops-mcp==0.0.1' finops guard hook", "other"),
    ("uvx --from=finops-mcp==0.0.1 finops guard hook", "other"),
    ("/usr/local/bin/finops guard hook", None),
    ('"/Users/a b/venv/bin/finops" guard hook', None),
])
def test_hook_pin_reads_every_form(cmd, pin):
    assert g.hook_pin(cmd) == pin


def _legacy_settings(path, command=LEGACY):
    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-tool check"}]},
        {"matcher": "Bash", "hooks": [{"type": "command", "command": command,
                                       "timeout": 30}]},
    ]}}))


def test_install_pins_a_legacy_unpinned_hook_in_place(settings, monkeypatch):
    _legacy_settings(settings)
    _uv_only(monkeypatch)
    assert g.unpinned_hook_command(settings) == LEGACY
    assert g.broken_hook_command(settings) is None, "legacy form is runnable, just unpinned"

    g.install()

    pre = _pre(settings)
    assert len(pre) == 2
    assert pre[0]["hooks"][0]["command"] == "other-tool check"
    assert pre[1]["hooks"][0]["command"] == PINNED
    assert g.unpinned_hook_command(settings) is None


def test_install_moves_an_older_pin_forward(settings, monkeypatch):
    _legacy_settings(settings, "uvx --from finops-mcp==0.0.1 finops guard hook")
    _uv_only(monkeypatch)
    g.install()
    assert _pre(settings)[1]["hooks"][0]["command"] == PINNED


def test_a_current_pin_is_left_alone(settings, monkeypatch):
    _legacy_settings(settings, PINNED)
    before = settings.read_text()
    _uv_only(monkeypatch)
    g.install()
    assert settings.read_text() == before


# ── the CLI says so, and counts it ────────────────────────────────────────────

def _run(action, **kw):
    import argparse
    import contextlib
    import io

    from finops import setup_wizard
    out = io.StringIO()
    kw.setdefault("guard_global", False)
    with contextlib.redirect_stdout(out):
        setup_wizard._run_guard(argparse.Namespace(guard_action=action, **kw))
    return out.getvalue()


def test_status_flags_an_unpinned_global_hook_and_names_the_scope(tmp_path, monkeypatch):
    glob = tmp_path / "global.json"
    _legacy_settings(glob)
    monkeypatch.setattr(g, "_settings_path",
                        lambda global_scope: glob if global_scope else tmp_path / "absent.json")
    _uv_only(monkeypatch)
    out = _run("status")
    assert "installed, unpinned" in out
    assert "newest finops-mcp from PyPI" in out
    assert "nable guard install --global" in out, "the fix must name the scope that needs it"


def test_install_reports_and_counts_a_repin(settings, monkeypatch):
    events = []
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: events.append((e, p)))
    _legacy_settings(settings)
    _uv_only(monkeypatch)
    out = _run("install")
    assert f"pinned to finops-mcp=={__version__}" in out
    assert [p["outcome"] for e, p in events if e == "guard_installed"] == ["repinned"]
