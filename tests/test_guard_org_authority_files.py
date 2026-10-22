"""authority.yaml is one of the guard's own files, wherever the org model is (D134).

An agent that edits the file saying who may approve a purchase decides its
own authority. The file only narrows a paid workspace's opt-in, and the
workspace still needs a named person to approve, but an agent that deletes
an `off` entry has widened what the workspace may propose to buy. So every
write, move, truncation or deletion asks: Claude Code's Write, Edit and
MultiEdit tools, `sed -i`, a shell redirect, `tee`, `cp`, `mv`, `truncate`,
`rm` and a glob that reaches it, and an MCP shell tool, in a repo's
nable.org/, in the nable data dir's org model and in FINOPS_ORG_DIR. The
reason names authority.yaml, so the person asked knows which file it is.
"""
from __future__ import annotations

import io
import json
import os

import pytest

import finops.guard as g
import finops.guard_plugin as gp
from finops import ai_budget, guard_paths


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
    for d in (repo / "nable.org", home / ".finops" / "org", other):
        (d / "authority.yaml").write_text("# nable org model v1\n[]\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo / "src")
    for var in ("FINOPS_DATA_DIR", "FINOPS_PROFILE", "FINOPS_ORG_DIR", "CLAUDE_CONFIG_DIR",
                "CLAUDE_PROJECT_DIR", "FINOPS_GUARD_STOP_ON_BUDGET", "XDG_CONFIG_HOME",
                "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"):
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
    return [str(m["repo"] / "nable.org" / "authority.yaml"), "../nable.org/authority.yaml",
            str(m["home"] / ".finops" / "org" / "authority.yaml"),
            "~/.finops/org/authority.yaml"]


def _edit(path: str, tool: str) -> dict:
    return {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": {"file_path": path, "old_string": "off", "new_string": "approve_each",
                           "content": "[]\n"}, "cwd": os.getcwd()}


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
@pytest.mark.parametrize("via", ["plugin", None])
def test_an_editor_write_to_authority_yaml_asks_and_names_it(machine, tool, via):
    for path in _places(machine):
        out = io.StringIO()
        assert gp.run_hook(None, via, io.StringIO(json.dumps(_edit(path, tool))), out,
                           io.StringIO()) == 0
        hso = json.loads(out.getvalue())["hookSpecificOutput"]
        assert hso["permissionDecision"] == "ask", (tool, path)
        assert "authority.yaml" in hso["permissionDecisionReason"], path
        assert "purchase authority" in hso["permissionDecisionReason"]


@pytest.mark.parametrize("shape", [
    "sed -i 's/off/approve_each/' {p}",                 # write in place
    "echo '- fact: authority' >> {p}",                   # append through a redirect
    "printf '[]' > {p}",                                 # overwrite through a redirect
    ": > {p}",                                           # truncate through a redirect
    "truncate -s 0 {p}",                                 # truncate
    "cat new.yaml | tee {p}",
    "cp /tmp/loose.yaml {p}",
    "mv {p} {p}.bak",                                    # move it away
    "mv /tmp/loose.yaml {p}",                            # move one over it
    "rm {p}",                                            # delete
    "rm -f {p}",
    "python3 -c \"open('{p}', 'w').write('[]')\"",
])
def test_a_shell_write_move_truncation_or_delete_asks(machine, shape):
    for path in _places(machine):
        v = g.gate_command(shape.format(p=path), record=False, cwd=os.getcwd())
        assert v is not None and v["decision"] in ("ask", "deny"), (shape, path)
        assert v["action_type"] == "protected_write", (shape, path)


@pytest.mark.parametrize("glob", ["../nable.org/auth*.yaml", "../nable.org/*.yaml",
                                  "../nable.org/authority.y?ml", "~/.finops/org/a*",
                                  "~/.fin*/org/authority.yaml", "../nable.org/{authority,x}.yaml"])
def test_a_glob_that_reaches_authority_yaml_asks(machine, glob):
    for shape in ("rm {g}", "sed -i 's/off/approve_each/' {g}", "echo x > {g}"):
        v = g.gate_command(shape.format(g=glob), record=False, cwd=os.getcwd())
        assert v is not None and v["decision"] in ("ask", "deny"), (shape, glob)


def test_finops_org_dir_authority_yaml_is_protected_too(machine, monkeypatch):
    monkeypatch.setenv("FINOPS_ORG_DIR", str(machine["other"]))
    p = machine["other"] / "authority.yaml"
    for cmd in (f"sed -i 's/off/approve_each/' {p}", f"rm {p}", f"echo x > {p}",
                f"mv {p} /tmp/x", f"truncate -s 0 {p}", f"rm {machine['other']}/*.yaml"):
        v = g.gate_command(cmd, record=False, cwd=os.getcwd())
        assert v is not None and v["decision"] == "ask", cmd
    hit = guard_paths.match(str(p), os.getcwd())
    assert hit is not None and "authority.yaml" in hit.what


def test_an_mcp_shell_tool_writing_authority_yaml_asks(machine):
    p = machine["repo"] / "nable.org" / "authority.yaml"
    for cmd in (f"sed -i 's/off/approve_each/' {p}", f"rm {p}", f"echo x > {p}"):
        v = g.gate_mcp_call("mcp__shell__run", {"command": cmd}, record=False,
                            cwd=os.getcwd())
        assert v is not None and v["decision"] == "ask", cmd


def test_reading_authority_yaml_stays_silent(machine):
    for path in _places(machine):
        for cmd in (f"cat {path}", f"grep rung {path}", f"less {path}"):
            assert g.gate_command(cmd, record=False, cwd=os.getcwd()) is None, cmd


def test_authority_yaml_is_named_by_the_guards_own_rule_in_every_org_dir(machine,
                                                                         monkeypatch):
    """By name, not only by the directory it is in: match() and match_file()
    both answer for authority.yaml with what it is, and a nable.org/ in a
    repo the agent is not working in says the same."""
    monkeypatch.setenv("FINOPS_ORG_DIR", str(machine["other"]))
    paths = [p for p in _places(machine) if not p.startswith(("..", "~"))]
    paths.append(str(machine["other"] / "authority.yaml"))
    for path in paths:
        for hit in (guard_paths.match(path, os.getcwd()),
                    guard_paths.match_file(path, os.getcwd())):
            assert hit is not None and "authority.yaml" in hit.what, path
    elsewhere = machine["repo"].parent / "another" / "nable.org" / "authority.yaml"
    hit = guard_paths.match(str(elsewhere), os.getcwd())
    assert hit is not None and "authority.yaml" in hit.what
    # Another file in the same directory is still the org model's.
    hit = guard_paths.match(str(machine["repo"] / "nable.org" / "owners.yaml"), os.getcwd())
    assert hit is not None and "authority.yaml" not in hit.what
