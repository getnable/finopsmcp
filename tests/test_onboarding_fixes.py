"""Tests for the terminal-onboarding drop-off fixes.

Covers the evidence-backed friction the diagnosis flagged:
  - multi-client config writing (Cursor + Claude Code), not just Claude Desktop
  - never advertising the one-click key link while its template 404s
  - honest "which clients are wired" prose
"""
import json
import pathlib
import tempfile

from finops import setup_wizard as W
from finops import welcome as WC
from finops.security import iam_setup as I


def _tmp(name: str) -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp()) / name


def test_merge_write_preserves_other_servers():
    p = _tmp("mcp.json")
    p.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    assert W._merge_write_mcpservers(p, {"command": "uvx", "args": ["finops-mcp"]})
    res = json.loads(p.read_text())
    assert res["mcpServers"]["other"] == {"command": "x"}
    assert res["mcpServers"]["nable"]["command"] == "uvx"


def test_merge_write_migrates_legacy_finops_key():
    p = _tmp("mcp.json")
    p.write_text(json.dumps({"mcpServers": {"finops": {"command": "old"}}}))
    W._merge_write_mcpservers(p, {"command": "new"})
    res = json.loads(p.read_text())
    assert "finops" not in res["mcpServers"]
    assert res["mcpServers"]["nable"]["command"] == "new"


def test_merge_write_refuses_unparseable_config():
    # Never clobber a config we cannot read.
    p = _tmp("mcp.json")
    p.write_text("{ not json")
    assert W._merge_write_mcpservers(p, {"command": "uvx"}) is False
    assert p.read_text() == "{ not json"


def test_build_entry_pins_version_under_uvx_when_available(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/uvx" if b == "uvx" else None)
    entry, display = W._build_mcp_server_entry()
    assert entry["command"] == "/usr/bin/uvx"
    assert entry["args"] and entry["args"][0].startswith("finops-mcp")
    assert "uvx" in display


def test_configure_cursor_writes_when_path_present(monkeypatch):
    target = _tmp("mcp.json")
    monkeypatch.setattr(W, "_cursor_config_path", lambda: target)
    assert W._configure_cursor({"command": "uvx", "args": ["finops-mcp"]}) is True
    assert json.loads(target.read_text())["mcpServers"]["nable"]["command"] == "uvx"


def test_configure_cursor_noop_when_cursor_absent(monkeypatch):
    monkeypatch.setattr(W, "_cursor_config_path", lambda: None)
    assert W._configure_cursor({"command": "uvx"}) is False


def test_quick_create_unavailable_for_placeholder(monkeypatch):
    monkeypatch.setattr(I, "CFN_KEY_TEMPLATE_S3_URL", I._CFN_TEMPLATE_PLACEHOLDER)
    assert I.quick_create_available() is False
    monkeypatch.setattr(I, "CFN_KEY_TEMPLATE_S3_URL", "https://real.s3.amazonaws.com/t.json")
    assert I.quick_create_available() is True


def test_offer_hides_dead_link_until_published(monkeypatch, capsys):
    # Placeholder URL -> must NOT print a console quick-create link, show steps instead.
    monkeypatch.setattr(I, "CFN_KEY_TEMPLATE_S3_URL", I._CFN_TEMPLATE_PLACEHOLDER)
    W._print_one_click_key_offer()
    out = capsys.readouterr().out
    assert "console.aws.amazon.com/cloudformation" not in out
    assert "IAM -> Users" in out

    # Published URL -> the one-click link appears.
    monkeypatch.setattr(I, "CFN_KEY_TEMPLATE_S3_URL", "https://real.s3.amazonaws.com/t.json")
    W._print_one_click_key_offer()
    out2 = capsys.readouterr().out
    assert "console.aws.amazon.com/cloudformation" in out2


def test_and_list_prose():
    assert WC._and_list([]) == ""
    assert WC._and_list(["Cursor"]) == "Cursor"
    assert WC._and_list(["Claude Desktop", "Cursor"]) == "Claude Desktop and Cursor"
    assert WC._and_list(["A", "B", "C"]) == "A, B, and C"
