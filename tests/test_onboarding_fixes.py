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
    assert entry["args"] and entry["args"][-1].startswith("finops-mcp")
    assert "uvx" in display


def test_uvx_args_pin_a_managed_python():
    # Every written config must force a clean managed interpreter, so an x86_64
    # conda base on Apple Silicon can't make uvx source-build for the wrong arch.
    args = W._uvx_args()
    assert args[:2] == ["--python", W._MANAGED_PYTHON]
    assert args[-1].startswith("finops-mcp")


def test_build_entry_carries_managed_python(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/uvx" if b == "uvx" else None)
    entry, _ = W._build_mcp_server_entry()
    assert "--python" in entry["args"] and W._MANAGED_PYTHON in entry["args"]


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


def test_one_click_is_opt_in_local_steps_are_the_default(monkeypatch, capsys):
    # Unpublished: only the fully-local console steps, no nable-hosted link.
    monkeypatch.setattr(I, "CFN_KEY_TEMPLATE_S3_URL", I._CFN_TEMPLATE_PLACEHOLDER)
    W._print_one_click_key_offer()
    out = capsys.readouterr().out
    assert "IAM -> Users" in out
    assert "console.aws.amazon.com/cloudformation" not in out

    # Published: local steps STAY the default; the one-click is shown only as an
    # optional addition, never replacing the local path.
    monkeypatch.setattr(I, "CFN_KEY_TEMPLATE_S3_URL", "https://real.s3.amazonaws.com/t.json")
    W._print_one_click_key_offer()
    out2 = capsys.readouterr().out
    assert "IAM -> Users" in out2  # local path remains the default
    assert "Optional one-click" in out2
    assert "console.aws.amazon.com/cloudformation" in out2


def test_value_moment_does_not_hang_on_blocking_scan(monkeypatch):
    # The bug: get_cost_summary can make a blocking call (SSO refresh, slow Cost
    # Explorer) that pins the event loop, so an asyncio timeout never fires and
    # setup hangs forever. The thread join must return on the wall-clock cap.
    import time
    from finops import server

    monkeypatch.setattr(WC, "_VALUE_MOMENT_TIMEOUT", 1)

    async def _block():
        time.sleep(30)  # blocking I/O on the loop, the exact hang case
        return {"grand_total_usd": 1.0}

    monkeypatch.setattr(server, "get_cost_summary", _block)
    t0 = time.monotonic()
    res = WC._value_moment_body(demo=False)
    elapsed = time.monotonic() - t0
    assert res is False
    assert elapsed < 10, f"value moment took {elapsed:.1f}s; the cap did not fire"


def test_and_list_prose():
    assert WC._and_list([]) == ""
    assert WC._and_list(["Cursor"]) == "Cursor"
    assert WC._and_list(["Claude Desktop", "Cursor"]) == "Claude Desktop and Cursor"
    assert WC._and_list(["A", "B", "C"]) == "A, B, and C"
