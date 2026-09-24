"""Opening a report never hands the vault to the browser."""
from __future__ import annotations

from finops import open_file
from finops.security import vault


def test_viewer_runs_without_vault_secrets(monkeypatch):
    seen = {}
    monkeypatch.setattr(open_file.sys, "platform", "linux")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD_T", "from-vault")
    monkeypatch.setattr(vault, "_INJECTED_KEYS", {"SNOWFLAKE_PASSWORD_T"})
    monkeypatch.setattr(open_file.subprocess, "Popen",
                        lambda argv, env=None, **kw: seen.update(argv=argv, env=env))
    open_file.open_local_file("/tmp/r.html")
    assert seen["argv"] == ["xdg-open", "/tmp/r.html"]
    assert "SNOWFLAKE_PASSWORD_T" not in seen["env"]


def test_viewer_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(open_file.sys, "platform", "darwin")

    def boom(*a, **k):
        raise FileNotFoundError("open")

    monkeypatch.setattr(open_file.subprocess, "Popen", boom)
    open_file.open_local_file("/tmp/r.html")
