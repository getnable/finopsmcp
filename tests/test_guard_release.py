"""A guard hook pinned to a release uvx cannot install says so.

Found on a first run: the nable plugin's hook ran
`uvx --python 3.12 --from finops-mcp==0.11.0 finops guard hook --via plugin;
exit 0` while 0.11.0 was not on PyPI. uvx failed, the hook's `; exit 0`
turned that into Claude Code's silent allow, and `terraform destroy` went
through unchecked. Meanwhile `nable guard status` said "plugin  on (via the
Claude Code plugin)", `nable guard doctor` listed Bash and MCP as covered, and
`nable doctor` said nothing about the guard at all.

Now status, guard doctor (text and --json) and nable doctor name the release,
quote uvx, and give the step that puts a working hook in place. The hook
itself still exits 0: a guard that cannot start never blocks the agent.

guard_release asks uvx itself. Here uvx is a stand-in script, or the probe is
replaced, so nothing reaches a package index.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

import finops.guard as g
import finops.guard_adapters as ga
import finops.guard_plugin as gp
import finops.setup_wizard as sw
from finops import doctor, guard_release

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "nable"
RELEASE = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]
UV_SAYS = ("Because there is no version of finops-mcp=={v} and you require "
           "finops-mcp=={v}, we can conclude that your requirements are unsatisfiable.")


@pytest.fixture(autouse=True)
def _machine(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "FINOPS_DATA_DIR", "FINOPS_PROFILE",
                "FINOPS_GUARD", "FINOPS_AIRGAP"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gp, "_data_root_override", None)
    monkeypatch.setattr(gp, "_user_dir_override", None)
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: None)
    monkeypatch.setattr(g, "check_budget_gate", lambda *a, **k: None)


def _uvx_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which",
                        lambda n, *a, **k: "/usr/bin/uvx" if n == "uvx" else None)


def _unfetchable(monkeypatch, *releases: str):
    """uvx cannot install these releases; every other one starts."""
    monkeypatch.delenv(guard_release.CHECK_ENV, raising=False)
    monkeypatch.setattr(guard_release, "_probe_override",
                        lambda release, python: UV_SAYS.format(v=release)
                        if release in releases else None)


def _install_plugin(version: str = RELEASE) -> Path:
    """The nable plugin as `/plugin install nable@nable` leaves it."""
    claude = Path.home() / ".claude"
    cache = claude / "plugins" / "cache" / "nable" / "nable" / version
    shutil.copytree(PLUGIN, cache)
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps({
        "version": 2, "plugins": {gp.PLUGIN_KEY: [{"scope": "user", "installPath": str(cache),
                                                   "version": version}]}}))
    (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {gp.PLUGIN_KEY: True}}))
    return cache


def _run(args, capsys) -> str:
    try:
        sw.main(args)
    except SystemExit as e:
        assert not e.code, e.code
    return capsys.readouterr().out


def _doctor_checks(capsys) -> list[dict]:
    capsys.readouterr()
    doctor.run_doctor(as_json=True)
    return json.loads(capsys.readouterr().out)


# ── the plugin ─────────────────────────────────────────────────────────────────

def test_status_says_the_plugins_hook_cannot_start_its_release(monkeypatch, capsys):
    _uvx_on_path(monkeypatch)
    _install_plugin()
    _unfetchable(monkeypatch, RELEASE)
    out = _run(["guard", "status"], capsys)
    flat = " ".join(out.split())
    assert re.search(rf"plugin\s+on \(via the Claude Code plugin\), but its hook cannot "
                     rf"start finops-mcp {re.escape(RELEASE)}", out)
    assert f"there is no version of finops-mcp=={RELEASE}" in flat
    assert "checks nothing" in flat and "nable guard install --global --force" in flat
    assert "/mcp lists nable as failed" in flat


def test_status_is_plain_on_when_the_release_starts(monkeypatch, capsys):
    _uvx_on_path(monkeypatch)
    _install_plugin()
    _unfetchable(monkeypatch, "0.0.1")
    out = _run(["guard", "status"], capsys)
    assert re.search(r"plugin\s+on \(via the Claude Code plugin\)\s", out)
    assert "cannot start" not in out


def test_doctor_does_not_count_a_plugin_hook_that_cannot_start(monkeypatch, capsys):
    _uvx_on_path(monkeypatch)
    _install_plugin()
    _unfetchable(monkeypatch, RELEASE)
    d = ga.with_plugin(g.doctor())
    assert not any("via the Claude Code plugin" in c for c in d["covered"])
    assert any(c.startswith("Claude Code (plugin): its hook runs finops-mcp " + RELEASE)
               for c in d["not_covered"])
    assert "nable guard install --global --force" in d["recommendations"][0]
    assert not any(f.startswith("nable guard install  (this project")
                   for f in d["recommendations"]), "a plain install stands aside for the plugin"
    assert d["plugin"]["runs"] is False and d["plugin"]["release"] == RELEASE
    assert "there is no version" in d["plugin"]["problem"]
    [row] = [r for r in d["surfaces"] if r.get("via") == "plugin"]
    assert row["runs"] is False
    out = _run(["guard", "doctor"], capsys)
    assert f"its hook cannot start finops-mcp {RELEASE}" in out
    doc = json.loads(_run(["guard", "doctor", "--json"], capsys))
    assert doc["plugin"]["release"] == RELEASE


def test_nable_doctor_names_the_plugins_hook(monkeypatch, capsys):
    _uvx_on_path(monkeypatch)
    _install_plugin()
    _unfetchable(monkeypatch, RELEASE)
    [check] = [c for c in _doctor_checks(capsys) if c["name"].startswith("Agent guard")]
    assert check["ok"] is False
    assert f"finops-mcp {RELEASE}" in check["detail"] and "checks nothing" in check["detail"]
    assert any("there is no version" in w for w in check["warnings"])
    assert "nable guard install" in check["recommendation"]


def test_nable_doctor_says_nothing_of_a_guard_that_is_not_installed(capsys):
    assert not [c for c in _doctor_checks(capsys) if c["name"].startswith("Agent guard")]


# ── a settings hook pinned through uvx ────────────────────────────────────────

def test_a_settings_hook_pinned_to_an_unfetchable_release_is_not_installed_and_fine(
        monkeypatch, capsys):
    _uvx_on_path(monkeypatch)
    monkeypatch.setattr(g, "_hook_command", lambda: g._UVX_HOOK_CMD)
    g.install(True)
    _unfetchable(monkeypatch, g.__version__)
    out = _run(["guard", "status"], capsys)
    assert re.search(rf"global\s+installed, but uvx cannot install finops-mcp "
                     rf"{re.escape(g.__version__)}", out)
    assert "there is no version" in " ".join(out.split())
    d = g.doctor()
    assert not any(c.startswith("Claude Code:") for c in d["covered"])
    assert any("which uvx cannot install here" in c for c in d["not_covered"])


# ── the probe itself, against a stand-in uvx ──────────────────────────────────

def _fake_uvx(tmp_path, monkeypatch, *, offline_ok: bool, online_ok: bool) -> Path:
    """A uvx that answers --offline and online runs as told, logging each."""
    log = tmp_path / "uvx.log"
    exe = tmp_path / "bin" / "uvx"
    exe.parent.mkdir()
    exe.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        'case " $* " in *" --offline "*) '
        f'{"exit 0" if offline_ok else "echo not in the cache >&2; exit 1"};; esac\n'
        + ("exit 0\n" if online_ok else
           "printf '  \\033[31mx\\033[0m No solution found when resolving tool dependencies:"
           "\\n  ╰─▶ Because there is no version of finops-mcp==9.9.9 and you require\\n"
           "      finops-mcp==9.9.9, we can conclude that your requirements are\\n"
           "      unsatisfiable.\\n' >&2\nexit 1\n"))
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(exe.parent))
    monkeypatch.delenv(guard_release.CHECK_ENV, raising=False)
    monkeypatch.setattr(guard_release, "_probe_override", None)
    return log


@pytest.mark.skipif(os.name == "nt", reason="the stand-in uvx is a shell script")
def test_the_probe_quotes_uvx_when_it_cannot_install_the_release(tmp_path, monkeypatch):
    log = _fake_uvx(tmp_path, monkeypatch, offline_ok=False, online_ok=False)
    why = guard_release.problem("9.9.9", "3.12")
    assert why == ("Because there is no version of finops-mcp==9.9.9 and you require "
                   "finops-mcp==9.9.9, we can conclude that your requirements are "
                   "unsatisfiable.")
    calls = log.read_text().splitlines()
    assert calls == ["--offline --python 3.12 --from finops-mcp==9.9.9 finops --version",
                     "--python 3.12 --from finops-mcp==9.9.9 finops --version"]
    assert guard_release.problem("9.9.9", "3.12") == why
    assert len(log.read_text().splitlines()) == 2, "asked once per process"


@pytest.mark.skipif(os.name == "nt", reason="the stand-in uvx is a shell script")
def test_a_cached_release_needs_no_network(tmp_path, monkeypatch):
    log = _fake_uvx(tmp_path, monkeypatch, offline_ok=True, online_ok=False)
    assert guard_release.problem("9.9.9") is None
    assert log.read_text().splitlines() == ["--offline --from finops-mcp==9.9.9 finops --version"]


@pytest.mark.skipif(os.name == "nt", reason="the stand-in uvx is a shell script")
def test_air_gapped_it_never_goes_online(tmp_path, monkeypatch):
    log = _fake_uvx(tmp_path, monkeypatch, offline_ok=False, online_ok=False)
    monkeypatch.setenv("FINOPS_AIRGAP", "1")
    assert guard_release.problem("9.9.9") is None
    assert len(log.read_text().splitlines()) == 1


@pytest.mark.skipif(os.name == "nt", reason="the stand-in uvx is a shell script")
def test_the_check_can_be_turned_off(tmp_path, monkeypatch):
    log = _fake_uvx(tmp_path, monkeypatch, offline_ok=False, online_ok=False)
    monkeypatch.setenv(guard_release.CHECK_ENV, "0")
    assert guard_release.problem("9.9.9") is None
    assert not log.exists()


def test_the_plugin_hook_commands_are_read_from_the_installed_copy():
    _install_plugin("0.9.0")
    old = Path.home() / ".claude" / "plugins" / "cache" / "nable" / "nable" / "0.8.1"
    shutil.copytree(PLUGIN, old)
    cmds = guard_release.plugin_hook_commands()
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    assert cmds == [h["command"] for e in ("PreToolUse", "PostToolUse")
                    for grp in hooks[e] for h in grp["hooks"]]
