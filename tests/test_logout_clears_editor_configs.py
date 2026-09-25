"""Signing out signs out everywhere nable put the key.

Activation and `nable setup claude` copied the license key in plaintext into
claude_desktop_config.json (env.FINOPS_LICENSE_KEY), and an env key wins over
the vault in check_license. `nable logout` cleared only the vault, so Claude
Desktop stayed Pro after logout, and the key sat in a config file people paste
into bug reports. The server reads the vault, so the key never needed to be in
an editor config at all.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

import finops.license as L
import finops.setup_wizard as W

_TEST_PRIV = "8fbe8En53x3KhJ93ZwEmE3L0IVLHQm6yI-gn3FGIpeg"
_TEST_PUB = "sxzvFKJjtkqH4xZWXQZLvrYhRxQFVoaJ5YRiEu18dMw"


class _FakeVault:
    def __init__(self):
        self.data: dict = {}

    def store(self, k, v):
        self.data[k] = v

    def get(self, k):
        return self.data.get(k)

    def delete(self, k):
        self.data.pop(k, None)

    def list_keys(self):
        return list(self.data)


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("FINOPS_LICENSE_PRIVATE_KEY", _TEST_PRIV)
    monkeypatch.delenv("FINOPS_LICENSE_KEY", raising=False)
    monkeypatch.setattr(L, "_PUBLIC_KEY_B64", _TEST_PUB)
    monkeypatch.setattr(L, "_status", None)
    vault = _FakeVault()
    from finops.security import vault as vault_mod
    monkeypatch.setattr(vault_mod.Vault, "default", classmethod(lambda cls: vault))
    import finops.telemetry as tel
    monkeypatch.setattr(tel, "_send_event", lambda *a, **k: None)
    return tmp_path


def _write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, mode)
    return path


def _desktop(home):
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def _configs(home, key="FINOPS-2-old-key"):
    other = {"command": "npx", "args": ["other-server"], "env": {"OTHER_TOKEN": "keep-me"}}
    nable = {"command": "uvx", "args": ["finops-mcp==1.0"],
             "env": {"FINOPS_LICENSE_KEY": key, "AWS_REGION": "us-east-1"}}
    desktop = _write(_desktop(home), {"mcpServers": {"nable": dict(nable), "other": other},
                                      "globalShortcut": "x"})
    cursor = _write(home / ".cursor" / "mcp.json", {"mcpServers": {"finops": dict(nable),
                                                                   "other": other}})
    code = _write(home / ".claude.json", {
        "numStartups": 4,
        "mcpServers": {"nable": dict(nable), "other": other},
        "projects": {"/repo": {"mcpServers": {"nable": dict(nable)}, "allowedTools": []}},
    }, mode=0o644)
    return desktop, cursor, code


def test_activation_does_not_write_the_key_into_the_desktop_config(home):
    desktop = _write(_desktop(home), {"mcpServers": {"nable": {"command": "uvx"}}})
    key = L.generate_key("buyer@example.com", plan="pro")
    W._run_license_setup(key)
    assert key not in desktop.read_text()


def test_the_mcp_entry_carries_no_license_key(home):
    from finops.security.vault import Vault
    Vault.default().store("FINOPS_LICENSE_KEY", "FINOPS-2-secret")
    entry, _ = W._build_mcp_server_entry()
    assert "FINOPS_LICENSE_KEY" not in json.dumps(entry)


def test_merge_write_drops_a_stale_key(home):
    p = _write(home / ".cursor" / "mcp.json",
               {"mcpServers": {"nable": {"command": "old", "env": {"FINOPS_LICENSE_KEY": "k",
                                                                  "X": "1"}}}})
    W._merge_write_mcpservers(p, {"command": "new"})
    env = json.loads(p.read_text())["mcpServers"]["nable"].get("env", {})
    assert "FINOPS_LICENSE_KEY" not in env and env.get("X") == "1"


def test_logout_removes_the_key_from_every_editor_config(home, capsys):
    desktop, cursor, code = _configs(home)
    before_code_mode = stat.S_IMODE(code.stat().st_mode)
    W._run_logout()
    out = capsys.readouterr().out
    for p in (desktop, cursor, code):
        assert "FINOPS_LICENSE_KEY" not in p.read_text(), p
        assert str(p) in out
    d = json.loads(desktop.read_text())
    assert d["globalShortcut"] == "x"
    assert d["mcpServers"]["other"]["env"] == {"OTHER_TOKEN": "keep-me"}
    assert d["mcpServers"]["nable"]["env"] == {"AWS_REGION": "us-east-1"}
    c = json.loads(code.read_text())
    assert c["numStartups"] == 4 and c["projects"]["/repo"]["allowedTools"] == []
    assert stat.S_IMODE(code.stat().st_mode) == before_code_mode


def test_logout_leaves_an_unparseable_config_alone(home):
    p = _desktop(home)
    p.parent.mkdir(parents=True)
    p.write_text("{ not json")
    W._run_logout()
    assert p.read_text() == "{ not json"


def test_logout_warns_when_the_shell_still_carries_a_key(home, monkeypatch, capsys):
    monkeypatch.setenv("FINOPS_LICENSE_KEY", "FINOPS-2-from-env")
    W._run_logout()
    out = capsys.readouterr().out
    assert "FINOPS_LICENSE_KEY" in out and "environment" in out


def test_after_logout_the_plan_is_not_paid(home):
    key = L.generate_key("buyer@example.com", plan="pro")
    L.store_license(key)
    assert L.check_license().mode == "pro"
    _configs(home, key)
    W._run_logout()
    assert L.check_license().mode in ("trial", "free")
