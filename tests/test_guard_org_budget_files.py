"""budgets.yaml is one of the guard's own files, wherever the org model is (D85).

An agent that edits the budget it is held to has no budget. So a write to
budgets.yaml asks, as a write to every org model file does: Claude Code's
Write and Edit tools, `sed -i`, a shell redirect and `tee`, and an MCP shell
tool, in a repo's nable.org/, in the nable data dir's org model, and in
FINOPS_ORG_DIR. The directory rule (guard_paths) already covered every file in
those directories; this holds budgets.yaml to it by name, so a change to that
rule cannot leave the budgets out.
"""
from __future__ import annotations

import io
import json
import os

import pytest

import finops.guard as g
import finops.guard_plugin as gp
from finops import ai_budget


@pytest.fixture
def machine(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".finops" / "org").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "nable.org").mkdir()
    (repo / "src").mkdir()
    other = tmp_path / "elsewhere-org"
    other.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo / "src")
    for var in ("FINOPS_DATA_DIR", "FINOPS_PROFILE", "FINOPS_ORG_DIR", "CLAUDE_CONFIG_DIR",
                "CLAUDE_PROJECT_DIR", "FINOPS_GUARD_STOP_ON_BUDGET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gp, "_data_root_override", None)
    monkeypatch.setattr(gp, "_user_dir_override", None)
    import sys
    db = sys.modules.get("finops.storage.db")
    if db is not None:
        monkeypatch.setattr(db, "_DATA_DIR", None)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    return {"home": home, "repo": repo, "other": other}


def _places(m) -> list[str]:
    return [str(m["repo"] / "nable.org" / "budgets.yaml"), "../nable.org/budgets.yaml",
            str(m["home"] / ".finops" / "org" / "budgets.yaml"), "~/.finops/org/budgets.yaml"]


def _edit(path: str, tool: str) -> dict:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": {key: path, "old_string": "a", "new_string": "b", "content": "x"},
            "cwd": os.getcwd()}


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
@pytest.mark.parametrize("via", ["plugin", None])
def test_an_editor_write_to_budgets_yaml_asks(machine, tool, via):
    for path in _places(machine):
        out = io.StringIO()
        assert gp.run_hook(None, via, io.StringIO(json.dumps(_edit(path, tool))), out,
                           io.StringIO()) == 0
        hso = json.loads(out.getvalue())["hookSpecificOutput"]
        assert hso["permissionDecision"] == "ask", (tool, path)
        assert "org model" in hso["permissionDecisionReason"]


@pytest.mark.parametrize("shape", [
    "sed -i 's/monthly_usd: 400/monthly_usd: 40000/' {p}",
    "echo '- fact: ai_budget' >> {p}",
    "printf 'x' > {p}",
    "cat new.yaml | tee {p}",
    "cp /tmp/loose.yaml {p}",
    "rm {p}",
])
def test_a_shell_write_to_budgets_yaml_asks(machine, shape):
    for path in _places(machine):
        v = g.gate_command(shape.format(p=path), record=False, cwd=os.getcwd())
        assert v is not None and v["decision"] in ("ask", "deny"), (shape, path)
        assert v["action_type"] == "protected_write"


def test_finops_org_dir_budgets_yaml_is_protected_too(machine, monkeypatch):
    monkeypatch.setenv("FINOPS_ORG_DIR", str(machine["other"]))
    p = machine["other"] / "budgets.yaml"
    v = g.gate_command(f"sed -i 's/4/4000/' {p}", record=False, cwd=os.getcwd())
    assert v is not None and v["decision"] == "ask"


def test_an_mcp_shell_tool_writing_budgets_yaml_asks(machine):
    p = machine["repo"] / "nable.org" / "budgets.yaml"
    v = g.gate_mcp_call("mcp__shell__run", {"command": f"sed -i 's/4/4000/' {p}"},
                        record=False, cwd=os.getcwd())
    assert v is not None and v["decision"] == "ask"


def test_reading_budgets_yaml_stays_silent(machine):
    for path in _places(machine):
        assert g.gate_command(f"cat {path}", record=False, cwd=os.getcwd()) is None


def test_budgets_yaml_is_named_by_the_guards_own_rule(machine):
    """Held by name, not only by the directory it is in: match() and
    match_file() both answer for budgets.yaml in each org dir."""
    from finops import guard_paths
    for path in _places(machine):
        if path.startswith(("..", "~")):
            continue
        assert guard_paths.match(path, os.getcwd()) is not None, path
        assert guard_paths.match_file(path, os.getcwd()) is not None, path
