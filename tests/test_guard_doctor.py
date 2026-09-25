"""`nable guard doctor` says plainly what is covered on this machine.

A guard people believe covers more than it does is worse than none: they
stop looking. So the doctor has to name each surface it covers (Bash, MCP,
which agents), each one it does not, and that the whole thing is a seatbelt
rather than a security boundary, with the one control that is: read-only
credentials for the agent.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import types

import pytest

import finops.guard as g
import finops.guard_ledger as gl
import finops.guard_mcp as gm
from finops import __version__

LEGACY = "uvx --from finops-mcp finops guard hook"


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """An empty home, project and user settings files, and uv on PATH. The
    Cursor/Codex adapters ship with nable, so they answer for this empty home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    files = {False: tmp_path / "project.json", True: home / ".claude" / "settings.json"}
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: files[global_scope])
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: "/usr/bin/uvx" if n == "uvx" else None)
    return {"home": home, "project": files[False], "global": files[True]}


def _write(path, command, matcher="Bash"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": matcher, "hooks": [{"type": "command", "command": command,
                                        "timeout": 30}]}]}}))


def test_an_unguarded_machine_is_told_so(machine):
    d = g.doctor()
    assert d["covered"] == []
    assert "Claude Code: no working guard hook" in d["not_covered"]
    assert any(f.startswith("nable guard install") for f in d["recommendations"])
    assert d["ok"] is False


def test_a_fresh_install_covers_bash_and_every_recognised_mcp_tool(machine):
    g.install()
    d = g.doctor()
    n = sum(len(r.names) for r in gm.MCP_RULES)
    assert d["covered"] == [
        "Claude Code: Bash commands",
        f"Claude Code: MCP tool calls ({n} recognised tools: AWS, Kubernetes, Terraform)",
    ]
    project = d["surfaces"][0]
    assert (project["pin"], project["bash"], project["mcp"]) == ("pinned", True, True)
    assert d["ok"] is True


def test_a_legacy_hook_gets_one_fix_that_does_both_upgrades(machine):
    _write(machine["project"], LEGACY)
    d = g.doctor()
    assert d["covered"] == ["Claude Code: Bash commands"]
    assert "Claude Code (project): MCP tool calls (the hook only sees Bash)" in d["not_covered"]
    [fix] = [f for f in d["recommendations"] if f.startswith("nable guard install")]
    assert "widens the hook to MCP tools" in fix and "pins the hook to this release" in fix
    # ... and running that fix clears both
    g.install()
    d = g.doctor()
    assert not [f for f in d["recommendations"] if f.startswith("nable guard install")]


def test_a_dead_global_hook_is_not_counted_as_cover(machine):
    _write(machine["global"], "/gone/bin/finops guard hook", matcher=g._HOOK_MATCHER)
    d = g.doctor()
    assert d["covered"] == []
    assert "Claude Code (global): the hooked command no longer exists" in d["not_covered"]
    assert any(f.startswith("nable guard install --global") for f in d["recommendations"])


def test_one_scope_that_sees_mcp_covers_the_machine(machine):
    _write(machine["project"], g._UVX_HOOK_CMD)                         # Bash only
    _write(machine["global"], g._UVX_HOOK_CMD, matcher=g._HOOK_MATCHER)
    d = g.doctor()
    assert not any("MCP tool calls (the hook only sees Bash)" in x for x in d["not_covered"])


def test_an_agent_on_this_machine_without_a_hook_is_named(machine):
    (machine["home"] / ".cursor").mkdir()
    d = g.doctor()
    assert "Cursor: on this machine, no working guard hook" in d["not_covered"]
    assert "nable guard install --harness cursor" in d["recommendations"]
    assert not any("Codex" in x for x in d["not_covered"]), "Codex is not installed here"


def test_adapter_state_is_read_when_the_adapters_are_present(machine, monkeypatch):
    """The contract with guard_adapters: detected(), state(harness, global_scope)
    and hooks_path(harness, global_scope)."""
    stub = types.ModuleType("finops.guard_adapters")
    stub.detected = lambda: ["cursor", "codex"]
    stub.state = lambda h, is_global: "installed" if (h, is_global) == ("cursor", True) else "absent"
    stub.hooks_path = lambda h, is_global: machine["home"] / f".{h}" / "hooks.json"
    monkeypatch.setitem(sys.modules, "finops.guard_adapters", stub)
    import finops
    monkeypatch.setattr(finops, "guard_adapters", stub, raising=False)
    d = g.doctor()
    assert "Cursor: shell commands" in d["covered"]
    assert "Codex CLI: on this machine, no working guard hook" in d["not_covered"]
    assert "nable guard install --harness codex" in d["recommendations"]


def test_what_no_hook_can_see_is_always_listed(machine):
    g.install()
    gaps = " ".join(g.doctor()["not_covered"])
    assert "commands inside scripts" in gaps
    assert "MCP servers outside the recognised table" in gaps


def test_a_broken_ledger_is_surfaced(machine):
    g.install()
    for d in ("ask", "deny"):
        gl.append({"decision": d})
    p = gl.ledger_path()
    p.write_bytes(p.read_bytes().replace(b'"ask"', b'"allow"'))
    d = g.doctor()
    assert d["ok"] is False
    assert any(f.startswith("nable guard verify-log") for f in d["recommendations"])


def test_doctor_changes_nothing(machine):
    _write(machine["project"], LEGACY)
    before = machine["project"].read_bytes()
    g.doctor()
    assert machine["project"].read_bytes() == before
    assert not gl.ledger_path().exists(), "doctor wrote a ledger"


# ── the CLI ───────────────────────────────────────────────────────────────────

def _cli(**kw):
    from finops import setup_wizard
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup_wizard._run_guard(argparse.Namespace(guard_action="doctor", guard_global=False,
                                                   **kw))
    return out.getvalue()


def test_the_cli_says_seatbelt_and_read_only_credentials(machine):
    g.install()
    out = _cli(guard_json=False)
    flat = " ".join(out.split())
    assert "seatbelt, not a security boundary" in flat
    assert "read-only cloud credentials" in flat
    assert "Covered on this machine" in out and "Not covered" in out
    assert "sees Bash + MCP, pinned to this release" in out
    assert f"finops-mcp {__version__}" in out


def test_status_points_at_the_doctor_and_the_report(machine):
    from finops import setup_wizard
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup_wizard._run_guard(argparse.Namespace(guard_action="status", guard_global=False))
    assert "nable guard doctor" in out.getvalue() and "nable guard report" in out.getvalue()


def test_the_cli_json_is_the_doctor_dict(machine):
    data = json.loads(_cli(guard_json=True))
    assert set(data) >= {"covered", "not_covered", "recommendations", "seatbelt", "surfaces",
                         "ledger", "mcp_tools"}


def test_a_build_without_adapters_still_names_the_agent(machine, monkeypatch):
    """The fallback for a build that cannot load guard_adapters."""
    import finops
    monkeypatch.setitem(sys.modules, "finops.guard_adapters", None)
    monkeypatch.delattr(finops, "guard_adapters", raising=False)
    (machine["home"] / ".cursor").mkdir()
    d = g.doctor()
    assert "Cursor: on this machine, and this nable has no hook for it" in d["not_covered"]
