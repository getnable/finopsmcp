"""
Version-pin + finops upgrade tests.

The footgun: configs that launch `uvx finops-mcp` unpinned re-resolve "latest"
on the first cold start after every PyPI release, which can blow past Claude
Desktop's startup timeout ("Server disconnected"). The fix: the wizard writes
a pinned `finops-mcp==X`, and `finops upgrade` moves the pin deliberately,
warming the cache outside any client startup window.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import finops.setup_wizard as sw


def test_pinned_package_uses_installed_version():
    pinned = sw._pinned_package()
    assert pinned.startswith("finops-mcp==")
    assert pinned == f"finops-mcp=={sw._installed_version()}"


def test_pinned_package_explicit_target():
    assert sw._pinned_package("9.9.9") == "finops-mcp==9.9.9"


def test_pinned_package_falls_back_unpinned(monkeypatch):
    monkeypatch.setattr(sw, "_installed_version", lambda: "")
    assert sw._pinned_package() == "finops-mcp"


def _fake_config(tmp_path, args):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"nable": {"command": "/usr/bin/uvx", "args": args}}
    }))
    return cfg


def _run_upgrade_with(monkeypatch, cfg, target, warm_rc=0):
    monkeypatch.setattr(sw, "_claude_config_file", lambda: cfg)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uvx" if name == "uvx" else None)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=warm_rc, stdout="", stderr="boom" if warm_rc else "")

    monkeypatch.setattr("subprocess.run", fake_run)
    sw._run_upgrade(target)
    return calls


def test_upgrade_moves_the_pin(tmp_path, monkeypatch, capsys):
    cfg = _fake_config(tmp_path, ["finops-mcp==0.8.50"])
    calls = _run_upgrade_with(monkeypatch, cfg, "9.9.9")

    # Cache warm ran against the exact target, outside any client startup
    assert any("finops-mcp==9.9.9" in " ".join(c) for c in calls)
    saved = json.loads(cfg.read_text())
    assert saved["mcpServers"]["nable"]["args"] == ["finops-mcp==9.9.9"]
    assert "Restart Claude Desktop" in capsys.readouterr().out


def test_upgrade_pins_legacy_unpinned_entry(tmp_path, monkeypatch):
    cfg = _fake_config(tmp_path, ["finops-mcp"])
    _run_upgrade_with(monkeypatch, cfg, "9.9.9")
    saved = json.loads(cfg.read_text())
    assert saved["mcpServers"]["nable"]["args"] == ["finops-mcp==9.9.9"]


def test_failed_cache_warm_leaves_config_untouched(tmp_path, monkeypatch, capsys):
    """If the new version can't even be downloaded, never break the working pin."""
    cfg = _fake_config(tmp_path, ["finops-mcp==0.8.50"])
    _run_upgrade_with(monkeypatch, cfg, "9.9.9", warm_rc=1)
    saved = json.loads(cfg.read_text())
    assert saved["mcpServers"]["nable"]["args"] == ["finops-mcp==0.8.50"]
    assert "NOT changed" in capsys.readouterr().out


def test_plugin_pin_matches_package_version():
    """The Claude Code plugin pins the server version. If a release bumps
    pyproject but forgets the plugin pin, installs would silently run an old
    server. This fails the suite at release time instead."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text()
    pkg_version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)

    plugin = json.loads((root / "plugins/nable/.claude-plugin/plugin.json").read_text())
    args = plugin["mcpServers"]["nable"]["args"]
    # The finops-mcp token is pinned and last; a managed --python prefix is fine.
    assert args[-1] == f"finops-mcp=={pkg_version}"
    assert "--python" in args
    assert plugin["version"] == pkg_version


def test_server_json_version_matches_package():
    """server.json feeds the MCP Registry publish, which rejects a duplicate or
    stale version. If a release bumps pyproject but forgets server.json, the
    registry leg fails (0.8.78 and 0.8.79 both did, silently, with no test here).
    This fails the suite at release time instead."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text()
    pkg_version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)

    server = json.loads((root / "server.json").read_text())
    # The registry version and the PyPI package it points at must both track the
    # package version, or registry-discovered installs pin to a stale release.
    assert server["version"] == pkg_version
    assert server["packages"][0]["version"] == pkg_version


def test_server_json_description_within_registry_limit():
    """The MCP Registry hard-caps server.json `description` at 100 chars and
    rejects the whole publish with a 422 if exceeded. A 243-char description
    shipped in 0.8.125 silently broke the registry leg for three releases (the
    registry froze at 0.8.124 while PyPI moved on) because nothing checked the
    length. This fails the suite at edit time instead."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    server = json.loads((root / "server.json").read_text())
    desc = server["description"]
    assert len(desc) <= 100, (
        f"server.json description is {len(desc)} chars; the MCP Registry rejects "
        f"anything over 100 with a 422 and the publish silently fails: {desc!r}"
    )


def test_dashboard_template_force_included_in_wheel():
    """The dashboard template is matched by an over-broad .gitignore rule
    ("dashboard.html"), so hatchling's VCS selection drops it from the wheel
    unless it is force-included. Without it, every pip or uvx install serves
    "Dashboard template missing" instead of the dashboard. Guard the force-include
    so it cannot be removed silently again."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text()
    assert "[tool.hatch.build.targets.wheel.force-include]" in pyproject
    assert "src/finops/static/dashboard.html" in pyproject
    assert (root / "src" / "finops" / "static" / "dashboard.html").is_file()


def test_upgrade_preserves_other_args(tmp_path, monkeypatch):
    """Only the finops-mcp token moves; any other args stay put."""
    cfg = _fake_config(tmp_path, ["--python", "3.12", "finops-mcp==0.8.50"])
    _run_upgrade_with(monkeypatch, cfg, "9.9.9")
    saved = json.loads(cfg.read_text())
    assert saved["mcpServers"]["nable"]["args"] == ["--python", "3.12", "finops-mcp==9.9.9"]
