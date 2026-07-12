"""First-contact confirmation: one-time 'your setup worked' after the editor
restart. MCP sessions only (a CLI-invoked tool call must never burn the
sentinel before the editor ever loads), and at most once per install."""
from __future__ import annotations

from finops import server


def _reset(monkeypatch, tmp_path, mcp_session):
    monkeypatch.setattr(server, "_MCP_SESSION", mcp_session)
    monkeypatch.setattr(server, "_editor_confirmed_this_process", False)
    monkeypatch.setattr(server, "_EDITOR_CONFIRM_SENTINEL", tmp_path / ".editor_confirmed")


def test_cli_context_never_confirms(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path, mcp_session=False)
    assert server._maybe_editor_confirmation() is None
    assert not (tmp_path / ".editor_confirmed").exists()  # sentinel untouched


def test_mcp_session_confirms_exactly_once_per_install(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path, mcp_session=True)
    note = server._maybe_editor_confirmation()
    assert note and "setup worked" in note
    assert (tmp_path / ".editor_confirmed").exists()
    # same process: never again
    assert server._maybe_editor_confirmation() is None
    # new process, same install: sentinel blocks it
    monkeypatch.setattr(server, "_editor_confirmed_this_process", False)
    assert server._maybe_editor_confirmation() is None
