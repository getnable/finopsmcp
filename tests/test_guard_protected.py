"""The guard's own files, pack rules and price books in the guard.

What has to stay true:
  - a shell command that writes, moves, deletes, links, truncates or changes
    the permissions of one of the guard's own files (the org model, the
    policy file, the packs, the off flag, the ledger, the budgets, the
    settings that carry the hook) asks; reading one stays silent, however
    the path is spelled (quotes, escapes, ~, $HOME, a relative path, a cd
    earlier on the line, a symlinked directory, a glob, braces)
  - `nable pack install|update|remove|sign|keygen` asks; list, audit,
    search, validate and new do not
  - Claude Code's Write, Edit, MultiEdit and NotebookEdit reach the hook; an
    edit to a protected file asks, any other is answered before the guard is
    imported
  - installed guard-rule packs tighten every verdict path and never loosen
    one; a broken pack is a recorded fail-open, never a silent one
  - an installed price book prices EC2, RDS and the VM tables at the org's
    rate, says so, and names the pack in the ledger; without one nothing
    changes
  - `nable guard doctor` lists the protected files, the file-edit coverage
    per harness, and the packs in effect
"""
from __future__ import annotations

import io
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

import finops.guard as g
import finops.guard_adapters as ga
import finops.guard_ledger as gl
import finops.guard_packs as gpk
import finops.guard_paths as gpa
import finops.guard_plugin as gp
from finops import ai_budget
from finops.aws_prices import EC2_HOURLY, HOURS_PER_MONTH
from tests import packs_support
from tests.packs_support import make_pack

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

packs_env = packs_support.packs_env


@pytest.fixture(autouse=True)
def _machine(tmp_path, monkeypatch):
    """A home with a nable data dir, a git repo with nable.org/ and a src/
    directory the agent works in, and a symlink to the data dir."""
    home = tmp_path / "home"
    (home / ".finops").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "nable.org").mkdir()
    (repo / "src").mkdir()
    (tmp_path / "linked").symlink_to(home / ".finops")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo / "src")
    for var in ("FINOPS_DATA_DIR", "FINOPS_PROFILE", "FINOPS_ORG_DIR", "FINOPS_GUARD_STRICT",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_POLICY_MAX_AUTO_USD", "CLAUDE_CONFIG_DIR",
                "CLAUDE_PROJECT_DIR", "FINOPS_TAG_RULES", "FINOPS_ACCOUNTS_FILE",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setattr(gp, "_data_root_override", None)
    monkeypatch.setattr(gp, "_user_dir_override", None)
    # The data dir is this home's, as in a real hook: storage.db caches the
    # one an earlier test saw, and conftest sandboxes the packs root.
    db = sys.modules.get("finops.storage.db")
    if db is not None:
        monkeypatch.setattr(db, "_DATA_DIR", None)
    from finops.packs import store
    monkeypatch.setattr(store, "_root_override", None)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    return {"home": home, "repo": repo, "tmp": tmp_path}


def _ask(command: str, **kw) -> dict | None:
    v = g.gate_command(command, record=False, cwd=os.getcwd(), **kw)
    return v if v and v["decision"] in ("ask", "deny") else None


def _records() -> list[dict]:
    p = gl.ledger_path()
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


# ── the list ───────────────────────────────────────────────────────────────────

def test_the_list_names_every_file_that_decides_what_the_guard_allows(_machine, monkeypatch):
    pol = _machine["tmp"] / "elsewhere" / "policy.yaml"
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(pol))
    home, repo = _machine["home"], _machine["repo"]
    shown = {e.shown: e for e in gpa.protected()}
    for want in ("~/.finops/org", "~/.finops/nable.policy.yaml", "~/.finops/packs",
                 "~/.finops/guard-off", "~/.nable/ai-budget.json",
                 "~/.claude/settings.json", "~/.cursor/hooks.json", "~/.codex/hooks.json",
                 "~/.codex/config.toml", "~/.gemini/settings.json", "~/.claude/plugins"):
        assert want in shown, want
    assert str(pol) in shown
    assert str(repo / "nable.org") in shown and shown[str(repo / "nable.org")].tree
    assert str(repo / ".claude" / "settings.json") in shown
    assert str(repo / ".claude" / "settings.local.json") in shown
    ledger = gl.ledger_path()
    for p in (ledger, ledger.with_name("guard-ledger.anchor.json"),
              ledger.with_name("budget-summary.json"), ledger.with_name("org-model-cache.json"),
              ledger.with_name(gpk.CACHE_NAME)):
        assert gpa.match(str(p)) is not None, p
    assert gpa.match(str(home / "notes.txt")) is None
    assert gpa.match("budget.yml").what.startswith("a budget file")


def test_the_org_dir_is_protected_wherever_it_resolves(_machine, monkeypatch, tmp_path):
    from finops.org import store
    elsewhere = tmp_path / "org-elsewhere"
    monkeypatch.setenv("FINOPS_ORG_DIR", str(elsewhere))
    assert store.resolve_dir()[0] == elsewhere
    assert gpa.match(str(elsewhere / "policy.yaml")).what == "the org model (FINOPS_ORG_DIR)"
    monkeypatch.delenv("FINOPS_ORG_DIR")
    assert store.resolve_dir()[0] == _machine["repo"] / "nable.org"
    assert gpa.match("../nable.org/policy.yaml").what == "this repo's org model"


def test_the_copied_rules_agree_with_the_modules_they_copy(_machine, monkeypatch, tmp_path):
    """guard_paths copies path rules so the file-edit fast path imports
    nothing; these pin that the copies still agree."""
    from finops.org import store
    monkeypatch.setattr(gl, "_path_override", None)
    for profile, data in (("", ""), ("", str(tmp_path / "d")), ("work", "")):
        monkeypatch.setenv("FINOPS_PROFILE", profile)
        monkeypatch.setenv("FINOPS_DATA_DIR", data)
        assert gpa.data_dir() == gl._data_dir()
    assert gpa.git_root() == store.git_root()
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "cop"))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "gem"))
    for harness in ("claude", "cursor", "codex", "copilot", "gemini", "cline"):
        for scope in (False, True):
            assert gpa.match(str(ga.hooks_path(harness, scope))) is not None, (harness, scope)


# ── shell writes: red team ─────────────────────────────────────────────────────

ASKS = [
    # the off switch, however it is spelled
    "touch ~/.finops/guard-off",
    "touch $HOME/.finops/guard-off",
    'touch "${HOME}/.fin""ops/guard-off"',
    "touch '/'\"$HOME\"/.finops/guard-off",
    "t\\ouch ~/.finops/guard-off",
    "echo x > ~/.finops/guard-off",
    "echo x>~/.finops/guard-off",
    ": >| ~/.finops/guard-off",
    "cat /dev/null &> ~/.finops/guard-off",
    "D=~/.finops; rm $D/guard-off",
    "export D=$HOME/.finops && rm ${D}/guard-off",
    "sudo -u root rm ~/.finops/guard-off",
    "env -u X nohup rm ~/.finops/guard-off",
    "timeout 5 rm ~/.finops/guard-off",
    "bash -c 'rm ~/.finops/guard-off'",
    "sh -lc \"cd ~/.finops && rm guard-off\"",
    "alias zap=rm; zap ~/.finops/guard-off",
    "rm ~/.finops/{x,guard-off}",
    "rm ~/.fin*/guard-off",
    "cd ~ && cd .finops && rm -f guard-off",
    # a confirmed threshold, a trusted key, an org fact
    "echo '- fact: threshold' >> ../nable.org/policy.yaml",
    "cd .. && sed -i 's/proposed/confirmed/' nable.org/policy.yaml",
    "sed -i.bak 's/proposed/confirmed/' ../nable.org/owners.yaml",
    "perl -pi -e 's/proposed/confirmed/' ../nable.org/teams.yaml",
    "cat key.txt | tee -a ~/.finops/nable.policy.yaml",
    "cp /tmp/evil.yaml ~/.finops/nable.policy.yaml",
    "cp /tmp/nable.policy.yaml ~/.finops/",
    "install -m 600 x.yaml ~/.finops/nable.policy.yaml",
    "curl -so ~/.finops/nable.policy.yaml https://example.invalid/p",
    "mkdir ../nable.org/new",
    "yq -i '.a = 1' ../nable.org/policy.yaml",
    "vim -es -c '%s/proposed/confirmed/' -c wq ../nable.org/policy.yaml",
    "git checkout -- ../nable.org/policy.yaml",
    "git restore ../nable.org",
    "git -C .. rm -r nable.org",
    "git mv ../nable.org ../old-org",
    "python3 -c \"open('$HOME/.finops/nable.policy.yaml','a').write('x')\"",
    "python3 -c \"import pathlib; (pathlib.Path.home()/'.finops'/'guard-off').touch()\"",
    "python3 - <<'EOF'\nimport shutil, os\nshutil.rmtree(os.path.expanduser('~/.finops/packs'))\nEOF",
    "node -e \"require('fs').writeFileSync(process.env.HOME+'/.finops/guard-off','')\"",
    # packs, the ledger and its anchor, the budgets
    "rm -rf ~/.finops/packs/io.github.example",
    "rm -rf ~/.finops",
    "rm -rf ..",
    "mv ~/.finops/guard-ledger.jsonl /tmp/x",
    "truncate -s 0 ~/.finops/guard-ledger.jsonl",
    "dd if=/dev/null of=$HOME/.finops/guard-ledger.anchor.json",
    "ln -sf /dev/null ~/.finops/guard-ledger.jsonl",
    "chmod 000 ~/.finops/guard-ledger.jsonl",
    "chmod -R a-w ~/.finops",
    "shred -u ~/.finops/budget-summary.json",
    "echo '{}' > ~/.nable/ai-budget.json",
    "echo hi > ../budget.yml",
    # the settings that carry the hook
    "echo '{}' > ../.claude/settings.json",
    "rm -rf ../.claude",
    "jq 'del(.hooks)' ~/.claude/settings.json > /tmp/s && mv /tmp/s ~/.claude/settings.json",
    "sed -i '/guard hook/d' ~/.cursor/hooks.json",
    "rm ~/.codex/hooks.json",
]


@pytest.mark.parametrize("command", ASKS)
def test_a_write_to_a_protected_file_asks(command):
    v = _ask(command)
    assert v is not None, command
    assert v["action_type"] in ("protected_write", "guard_change"), v
    assert "a human should confirm" in v["reason"]


def test_a_symlinked_directory_name_reaches_the_same_file(_machine):
    link = _machine["tmp"] / "linked"
    v = _ask(f"sed -i s/a/b/ {link}/nable.policy.yaml")
    assert v and "~/.finops/nable.policy.yaml" in v["reason"]
    assert _ask(f"rm {link}/guard-off")
    assert _ask(f"echo x > '{link}'/guard-off")


def test_a_path_with_a_space_in_it(_machine, monkeypatch):
    spaced = _machine["tmp"] / "My Files" / "nable.policy.yaml"
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(spaced))
    assert _ask(f'echo x > "{spaced}"')
    assert _ask(f"rm '{spaced}'")
    assert _ask(f"cat '{spaced}'") is None


SILENT = [
    "cat ~/.finops/nable.policy.yaml",
    "less ~/.finops/guard-ledger.jsonl",
    "head -5 ../nable.org/policy.yaml",
    "grep -r confirmed ../nable.org",
    "rg guard-off ~/.finops",
    "git diff ../nable.org",
    "git log -p -- ../nable.org",
    "git status",
    "git checkout main",
    "git checkout -b feature",
    "cp ~/.finops/nable.policy.yaml /tmp/backup.yaml",
    "cat ../nable.org/policy.yaml > /tmp/copy",
    "diff ~/.finops/nable.policy.yaml /tmp/other > /tmp/d",
    "ls -la ~/.finops 2>&1",
    "pytest -q 2>/dev/null",
    "python3 -c \"print(open('$HOME/.finops/nable.policy.yaml').read())\"",
    "python3 -c \"import json; json.load(open('../nable.org/policy.yaml'))\"",
    "rm -rf build/ dist/",
    "rm -f /tmp/nable.policy.yaml.bak",
    "find . -name '*.pyc' -delete",
    "sed 's/a/b/' ../nable.org/policy.yaml",
    "sed -n 1,5p ../nable.org/policy.yaml",
    "echo done > /tmp/log",
    'git commit -m "echo x > nable.org/policy.yaml; rm ~/.finops/guard-off"',
    'echo "touch ~/.finops/guard-off"',
    'grep -n "rm -rf ~/.finops" notes.md',
    "git clean -fd",
    "git clean -n",
    "mkdir -p build/out",
    "touch src/new_module.py",
]


@pytest.mark.parametrize("command", SILENT)
def test_reading_a_protected_file_or_writing_elsewhere_stays_silent(command):
    assert _ask(command) is None, command


def test_git_clean_and_find_delete_ask_only_when_they_would_delete_one(_machine):
    """Both delete only what is there, so a protected file that is not there
    is not a reason to ask. (Whether git tracks it is not read: a clean of a
    repo holding nable.org/ asks, tracked or not.)"""
    (_machine["repo"] / "nable.org").rmdir()
    assert _ask("git -C .. clean -fdx") is None
    assert _ask("find ~/.finops -name 'guard-*' -delete") is None
    (_machine["repo"] / ".claude").mkdir()
    (_machine["repo"] / ".claude" / "settings.local.json").write_text("{}")
    assert _ask("git -C .. clean -fdx")
    (_machine["home"] / ".finops" / "guard-ledger.jsonl").write_text("")
    assert _ask("find ~/.finops -name 'guard-*' -delete")
    assert _ask("find ~ -exec rm {} +")
    assert _ask("find ~/.finops -name '*.pyc' -delete") is None
    assert _ask("git -C .. clean -n") is None


def test_a_write_and_an_infra_change_in_one_command_is_judged_as_both(monkeypatch):
    """Confirming the one must never be a way past the other."""
    v = _ask("rm ~/.finops/guard-off && aws ec2 terminate-instances --instance-ids i-1")
    assert v["decision"] == "ask"
    assert "off switch" in v["reason"] and "cannot be undone" in v["reason"]
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "ticket")
    v = _ask("touch ~/.finops/guard-off; aws ec2 stop-instances --instance-ids i-1")
    assert v["decision"] == "deny"
    assert "off switch" in v["reason"]


def test_a_protected_write_is_recorded_with_the_file():
    v = g.gate_command("touch ~/.finops/guard-off", harness="claude-code", tool="Bash",
                       cwd=os.getcwd())
    assert v["decision"] == "ask"
    [rec] = _records()
    assert rec["action_type"] == "protected_write" and rec["decision"] == "ask"
    assert rec["protected_path"] == "~/.finops/guard-off"


def test_other_harnesses_shell_writes_ask_too():
    for payload in ({"hook_event_name": "beforeShellExecution",
                     "command": "touch ~/.finops/guard-off", "cwd": os.getcwd()},
                    {"hook_event_name": "PreToolUse", "turn_id": "t", "tool_name": "Bash",
                     "tool_input": {"command": "touch ~/.finops/guard-off"}}):
        out = io.StringIO()
        assert ga.run_hook(None, io.StringIO(json.dumps(payload)), out, io.StringIO()) == 0
        assert "off switch" in out.getvalue()


def test_the_write_check_is_linear_in_the_command():
    pad = "a" * (200 * 1024)
    for command in (f"echo {pad} > /tmp/x", f"rm {' '.join(['x'] * 20000)}",
                    f"cp {'> ' * 20000}x ~/.finops/"):
        t = time.perf_counter()
        g.gate_command(command, record=False)
        assert time.perf_counter() - t < 5.0, command[:40]


# ── pack commands ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command,asks", [
    ("nable pack install ./my-pack", True),
    ("nable pack install io.github.acme/rules --yes", True),
    ("nable pack update io.github.acme/rules", True),
    ("nable pack remove io.github.acme/rules", True),
    ("finops pack remove io.github.acme/rules", True),
    ("uvx --from finops-mcp finops pack remove io.github.acme/rules", True),
    ("nable pack sign ./my-pack --key org.pem", True),
    ("nable pack keygen --out org.pem", True),
    ("NABLE=1 n\\able pack 'install' ./p", True),
    ("nable pack list", False),
    ("nable pack audit", False),
    ("nable pack search install", False),
    ("nable pack validate ./my-pack", False),
    ("nable pack new my-pack", False),
    ('git commit -m "nable pack remove io.github.acme/rules"', False),
])
def test_pack_commands_that_change_what_is_installed_ask(command, asks):
    v = _ask(command)
    assert (v is not None) is asks, command
    if asks:
        assert v["action_type"] == "pack_change"


# ── MCP tools ──────────────────────────────────────────────────────────────────

def test_an_mcp_shell_or_file_tool_writing_a_protected_file_asks():
    v = g.gate_mcp_call("mcp__shell__run", {"command": "touch ~/.finops/guard-off"},
                        record=False)
    assert v["decision"] == "ask" and v["action_type"] == "protected_write"
    v = g.gate_mcp_call("mcp__filesystem__write_file",
                        {"path": "../nable.org/policy.yaml", "content": "x"}, record=False)
    assert v["decision"] == "ask" and "org model" in v["reason"]
    v = g.gate_mcp_call("mcp__filesystem__delete_directory", {"path": "~/.finops"},
                        record=False)
    assert v["decision"] == "ask"
    assert g.gate_mcp_call("mcp__filesystem__read_file", {"path": "~/.finops/guard-off"},
                           record=False) is None
    assert g.gate_mcp_call("mcp__filesystem__write_file", {"path": "/tmp/notes.md"},
                           record=False) is None


def test_an_mcp_write_and_an_infra_change_in_one_call_is_judged_as_both(monkeypatch):
    command = "touch ~/.finops/guard-off; aws ec2 stop-instances --instance-ids i-1"
    v = g.gate_mcp_call("mcp__shell__run", {"command": command}, record=False)
    assert v["decision"] == "ask" and "off switch" in v["reason"]
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "ticket")
    v = g.gate_mcp_call("mcp__shell__run", {"command": command}, record=False)
    assert v["decision"] == "deny" and "off switch" in v["reason"]


# ── Claude Code's file tools ──────────────────────────────────────────────────

def _edit(path: str, tool: str = "Edit", **extra) -> dict:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": {key: path, "old_string": "a", "new_string": "b"},
            "cwd": os.getcwd(), **extra}


def _hook(payload: dict, via: str | None = "plugin") -> str:
    out = io.StringIO()
    assert gp.run_hook(None, via, io.StringIO(json.dumps(payload)), out, io.StringIO()) == 0
    return out.getvalue()


@pytest.mark.parametrize("tool", gp.EDITOR_TOOLS)
@pytest.mark.parametrize("via", ["plugin", None])
def test_an_edit_to_a_protected_file_asks(tool, via, _machine):
    for path in (str(_machine["home"] / ".finops" / "nable.policy.yaml"),
                 "~/.finops/guard-off", "../nable.org/policy.yaml",
                 str(_machine["tmp"] / "linked" / "guard-off"),
                 str(_machine["repo"] / ".claude" / "settings.json")):
        out = json.loads(_hook(_edit(path, tool), via))["hookSpecificOutput"]
        assert out["permissionDecision"] == "ask", path
        assert f"with its {tool} tool" in out["permissionDecisionReason"]


@pytest.mark.parametrize("via", ["plugin", None])
def test_an_edit_to_any_other_file_is_answered_silently_and_unrecorded(via, _machine):
    assert _hook(_edit(str(_machine["repo"] / "src" / "app.py")), via) == ""
    assert _hook(_edit("README.md", "Write"), via) == ""
    assert _records() == []


def test_the_guard_answers_and_records_an_edit_it_is_handed():
    """guard.run_hook sees the same calls when something reaches it directly."""
    out = io.StringIO()
    g.run_hook(io.StringIO(json.dumps(_edit("~/.finops/guard-off", "Write"))), out)
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "ask"
    [rec] = _records()
    assert rec["tool"] == "Write" and rec["protected_path"] == "~/.finops/guard-off"
    out = io.StringIO()
    g.run_hook(io.StringIO(json.dumps(_edit("app.py"))), out)
    assert out.getvalue() == ""


def test_a_codex_payload_is_not_taken_for_a_claude_edit():
    payload = _edit("~/.finops/guard-off", "Edit", turn_id="t1")
    assert gp._editor_call(payload) is False
    assert "permissionDecision" not in _hook(payload, None)


def test_the_guard_off_switch_silences_the_edit_check_too():
    gp.set_off(True)
    assert _hook(_edit("~/.finops/nable.policy.yaml")) == ""
    gp.set_off(False)
    assert _hook(_edit("~/.finops/nable.policy.yaml")) != ""


@pytest.fixture
def settings(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: p)
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: "/usr/bin/uvx" if n == "uvx" else None)
    return p


def test_install_widens_the_previous_matcher_to_the_file_tools(settings):
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "^(Bash|mcp__.*)$", "hooks": [{"type": "command",
                                                  "command": g._UVX_HOOK_CMD + "; exit 0",
                                                  "timeout": 30}]}]}}))
    assert g.hook_surfaces(settings) == {"bash": True, "mcp": True, "editor": False}
    g.install()
    [entry] = json.loads(settings.read_text())["hooks"]["PreToolUse"]
    assert entry["matcher"] == g._HOOK_MATCHER
    assert g.hook_surfaces(settings)["editor"] is True


def test_someone_elses_hook_in_our_entry_is_not_widened_to_file_edits(settings):
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "^(Bash|mcp__.*)$", "hooks": [
            {"type": "command", "command": "other-tool check"},
            {"type": "command", "command": g._UVX_HOOK_CMD + "; exit 0", "timeout": 30}]}]}}))
    g.install()
    pre = json.loads(settings.read_text())["hooks"]["PreToolUse"]
    assert pre[0] == {"matcher": "^(Bash|mcp__.*)$",
                      "hooks": [{"type": "command", "command": "other-tool check"}]}
    assert pre[1]["matcher"] == g._HOOK_MATCHER


def test_the_plugin_stands_aside_for_edits_only_when_the_settings_hook_sees_them(tmp_path):
    exe = tmp_path / "bin" / "finops"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    user = Path.home() / ".claude" / "settings.json"
    user.parent.mkdir(parents=True)
    user.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "^(Bash|mcp__.*)$", "hooks": [
        {"type": "command", "command": f"{exe} guard hook", "timeout": 10}]}]}}))
    assert _hook(_edit("~/.finops/guard-off")) != "", "the old settings hook does not see edits"
    user.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": g._HOOK_MATCHER, "hooks": [
        {"type": "command", "command": f"{exe} guard hook", "timeout": 10}]}]}}))
    assert _hook(_edit("~/.finops/guard-off")) == "", "the settings hook judges it instead"


def _cli_env(home: Path) -> dict:
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(SRC),
           "PYTHONDONTWRITEBYTECODE": "1", "NABLE_NO_TELEMETRY": "1"}
    for var in ("FINOPS_DATA_DIR", "FINOPS_PROFILE", "FINOPS_GUARD", "CLAUDE_CONFIG_DIR",
                "CLAUDE_PROJECT_DIR", "FINOPS_ORG_DIR", "FINOPS_POLICY_FILE"):
        env.pop(var, None)
    return env


_PROBE = ("import sys, io, json, time; t = time.perf_counter();"
          "sys.argv = ['finops', 'guard', 'hook'] + sys.argv[1:];"
          "from finops.setup_wizard import main\n"
          "try:\n    main()\nexcept SystemExit as e:\n    assert e.code == 0\n"
          "sys.stderr.write(json.dumps([sorted(m for m in sys.modules "
          "if m.startswith('finops.guard')), time.perf_counter() - t]))")


@pytest.mark.parametrize("via", [["--via", "plugin"], []])
def test_an_ordinary_edit_never_imports_the_guard(via, _machine):
    payload = json.dumps(_edit(str(_machine["repo"] / "src" / "app.py")))
    r = subprocess.run([sys.executable, "-c", _PROBE, *via], input=payload,
                       capture_output=True, text=True, env=_cli_env(_machine["home"]),
                       timeout=60, check=False, cwd=str(_machine["repo"] / "src"))
    assert r.returncode == 0 and r.stdout == "", r.stderr
    modules, _ = json.loads(r.stderr.strip().splitlines()[-1])
    assert modules == ["finops.guard_paths", "finops.guard_plugin"]


@pytest.mark.parametrize("via", [["--via", "plugin"], []])
def test_an_ordinary_edit_is_fast_and_a_protected_one_asks(via, _machine):
    """Measured on a laptop: ~45 ms for the whole process, about what Python
    itself takes to start and import the CLI. The bound is generous."""
    ordinary = json.dumps(_edit(str(_machine["repo"] / "src" / "app.py")))
    protected = json.dumps(_edit("~/.finops/nable.policy.yaml"))
    env = _cli_env(_machine["home"])
    times = []
    for _ in range(5):
        t = time.perf_counter()
        r = subprocess.run([sys.executable, "-c", _PROBE, *via], input=ordinary,
                           capture_output=True, text=True, env=env, timeout=60, check=False,
                           cwd=str(_machine["repo"] / "src"))
        times.append(time.perf_counter() - t)
        assert r.returncode == 0 and r.stdout == ""
    assert statistics.median(times) < 1.0, times
    r = subprocess.run([sys.executable, "-c", _PROBE, *via], input=protected,
                       capture_output=True, text=True, env=env, timeout=60, check=False,
                       cwd=str(_machine["repo"] / "src"))
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_the_in_process_edit_check_is_cheap(_machine):
    target = str(_machine["repo"] / "src" / "app.py")
    gp.editor_target("Edit", {"file_path": target}, os.getcwd())
    t = time.perf_counter()
    for _ in range(20):
        assert gp.editor_target("Edit", {"file_path": target}, os.getcwd()) is None
    assert (time.perf_counter() - t) / 20 < 0.05


# ── pack guard rules ───────────────────────────────────────────────────────────

GUARD_RULES = """\
version: 1
rules:
  - id: ask-nat
    pattern: '\\baws\\s+ec2\\s+create-nat-gateway\\b'
    verdict: ask
    reason: NAT gateways bill hourly and per GB.
  - id: deny-destroy
    pattern: '\\bterraform\\s+destroy\\b'
    verdict: deny
    reason: Destroys go through the release pipeline.
  - id: ask-stop
    pattern: '\\baws\\s+ec2\\s+stop-instances\\b'
    verdict: ask
    reason: Stopping needs the on-call's word.
  - id: ask-mcp-exec
    target: mcp
    pattern: '^mcp__shell__exec '
    verdict: ask
    reason: The shell server runs anything.
"""


def _install_rules(tmp_path: Path, text: str = GUARD_RULES, **kw) -> Path:
    from finops.packs import install as inst
    src = make_pack(tmp_path / "rules-pack", **kw)
    (src / "guard" / "rules.yaml").write_text(text)
    inst.install(str(src), yes=True)
    gpk.invalidate()
    return src


def test_a_pack_rule_turns_silence_into_an_ask(packs_env, tmp_path):
    assert _ask("aws ec2 create-nat-gateway --subnet-id s-1") is None
    _install_rules(tmp_path)
    v = g.gate_command("aws ec2 create-nat-gateway --subnet-id s-1", record=True,
                       cwd=os.getcwd())
    assert v["decision"] == "ask" and v["action_type"] == "pack_rule"
    assert "NAT gateways bill hourly" in v["reason"]
    assert "rule ask-nat of pack io.github.example/demo" in v["reason"]
    [rec] = _records()
    assert rec["pack_rules"] == ["io.github.example/demo:ask-nat"]
    # Quoting does not get a command past a pack's pattern.
    assert _ask('"aws" ec2 "create-nat-gateway"')


def test_a_pack_rule_turns_an_ask_into_a_deny_and_never_loosens(packs_env, tmp_path,
                                                                 monkeypatch):
    _install_rules(tmp_path)
    v = _ask("terraform destroy -auto-approve")
    assert v["decision"] == "deny"
    assert "release pipeline" in v["reason"] and "cannot be undone" in v["reason"]
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "ticket")
    v = _ask("aws ec2 stop-instances --instance-ids i-1")
    assert v["decision"] == "deny", "an ask rule never softens the policy's deny"
    assert v["pack_rules"] == ["io.github.example/demo:ask-stop"]


def test_a_pack_rule_tightens_an_mcp_call(packs_env, tmp_path):
    _install_rules(tmp_path)
    v = g.gate_mcp_call("mcp__shell__exec", {"cmd": "ls"}, record=False)
    assert v["decision"] == "ask" and "runs anything" in v["reason"]
    # A recognised tool is judged as the command it amounts to, and a
    # `command` rule reads that command.
    v = g.gate_mcp_call("mcp__tf__ExecuteTerraformCommand",
                        {"command": "destroy", "working_directory": "/infra"}, record=False)
    assert v["decision"] == "deny" and "release pipeline" in v["reason"]
    assert g.gate_mcp_call("mcp__other__read", {"x": 1}, record=False) is None


def test_the_guard_reads_packs_through_its_cache(packs_env, tmp_path, monkeypatch):
    from finops.packs import runtime
    src = _install_rules(tmp_path)
    assert _ask("aws ec2 create-nat-gateway")
    assert gl.ledger_path().with_name(gpk.CACHE_NAME).exists()
    # A new process: nothing in memory, the cache on disk, the loader not called.
    gpk.invalidate()
    with monkeypatch.context() as m:
        m.setattr(runtime, "_load", lambda: (_ for _ in ()).throw(AssertionError("load")))
        assert _ask("aws ec2 create-nat-gateway")
    # Any edit to an installed file is a miss, and the loader refuses the pack.
    installed = packs_env.root / "io.github.example" / "demo" / "1.0.0" / "guard" / "rules.yaml"
    installed.write_text(installed.read_text().replace("verdict: ask", "verdict: deny"))
    gpk.invalidate()
    st = gpk.state()
    assert st["rules"] == [] and st["guard_problems"]
    assert src.exists()


def test_a_broken_pack_is_a_recorded_fail_open(packs_env, tmp_path, monkeypatch):
    _install_rules(tmp_path)
    installed = packs_env.root / "io.github.example" / "demo" / "1.0.0" / "guard" / "rules.yaml"
    installed.write_text("version: 1\nrules: []\n")
    gpk.invalidate()
    v = g.gate_command("terraform destroy", cwd=os.getcwd())
    assert v["decision"] == "ask", "the core verdict stands"
    recs = _records()
    assert any(r["decision"] == "fail_open" and r.get("check") == "packs"
               and r["error"] == "PackLoadProblem" for r in recs)
    # A silent call records it too, at most once in a while.
    before = len(_records())
    g.gate_command("ls -la", cwd=os.getcwd())
    g.gate_command("ls -la", cwd=os.getcwd())
    assert len(_records()) == before + 1


def test_a_pack_reader_that_raises_keeps_the_core_verdict(packs_env, monkeypatch):
    def boom():
        raise RuntimeError("index on fire")
    monkeypatch.setattr(gpk, "state", boom)
    v = g.gate_command("terraform destroy", cwd=os.getcwd())
    assert v["decision"] == "ask"
    assert g.gate_command("ls", cwd=os.getcwd()) is None
    fails = [r for r in _records() if r["decision"] == "fail_open"]
    assert [r["check"] for r in fails] == ["packs", "packs"]
    assert fails[0]["error"] == "RuntimeError"


def test_no_packs_costs_one_stat_and_no_packs_import():
    probe = ("import sys; import finops.guard as g;"
             "g.gate_command('ls', record=False);"
             "print(sorted(m for m in sys.modules if m.startswith('finops.packs')))")
    env = _cli_env(Path.home())
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env,
                       timeout=60, check=True)
    assert r.stdout.strip() == "[]"


def test_a_cached_pack_read_is_fast(packs_env, tmp_path):
    _install_rules(tmp_path)
    gpk.state()                             # builds and writes the cache
    times = []
    for _ in range(5):
        gpk.invalidate()
        t = time.perf_counter()
        assert gpk.state()["rules"]
        times.append(time.perf_counter() - t)
    assert statistics.median(times) < 0.25, times


# ── price books ────────────────────────────────────────────────────────────────

PRICES = """\
version: 1
rates:
  - provider: aws
    sku: p4d.24xlarge
    unit: hour
    rate: 21.5
    currency: USD
    effective_from: 2020-01-01
  - provider: aws
    sku: db.r6g.large
    unit: month
    rate: 100
    currency: USD
    effective_from: 2020-01-01
  - provider: aws
    sku: m5.large
    unit: hour
    rate: 0.05
    currency: EUR
    effective_from: 2020-01-01
  - provider: gcp
    sku: e2-standard-4
    unit: month
    rate: 50
    currency: USD
    effective_from: 2020-01-01
"""
P4D = "aws ec2 run-instances --instance-type p4d.24xlarge --count 2"


def _install_prices(tmp_path: Path) -> None:
    from finops.packs import install as inst
    src = make_pack(tmp_path / "price-pack", name="prices")
    (src / "prices" / "book.yaml").write_text(PRICES)
    inst.install(str(src), yes=True)
    gpk.invalidate()


def test_without_a_price_book_the_list_price_stands(packs_env):
    est = g.estimate_command_monthly_cost(P4D)
    assert est["hourly_usd"] == EC2_HOURLY["p4d.24xlarge"]
    assert "price book" not in est["line"] and "price_book" not in est


def test_a_price_book_prices_at_the_orgs_rate_and_says_so(packs_env, tmp_path):
    _install_prices(tmp_path)
    est = g.estimate_command_monthly_cost(P4D)
    assert est["hourly_usd"] == 21.5
    assert est["monthly_usd"] == round(21.5 * 2 * HOURS_PER_MONTH, 2)
    assert "at your price book rate of $21.50/hr" in est["line"]
    assert "io.github.example/prices" in est["basis"]
    assert est["price_book"]["pack"] == "io.github.example/prices"
    v = g.gate_command(P4D, cwd=os.getcwd())
    assert v["decision"] == "ask" and "at your price book rate" in v["reason"]
    [rec] = [r for r in _records() if r["decision"] == "ask"]
    assert rec["price_book"]["pack"] == "io.github.example/prices"


def test_a_monthly_rate_and_the_vm_tables(packs_env, tmp_path):
    _install_prices(tmp_path)
    est = g.estimate_command_monthly_cost(
        "aws rds create-db-instance --db-instance-class db.r6g.large --engine postgres")
    assert est["monthly_usd"] == 100.0 and "at your price book rate" in est["line"]
    est = g.estimate_command_monthly_cost(
        "gcloud compute instances create vm-1 vm-2 --machine-type e2-standard-4")
    assert est["monthly_usd"] == 100.0 and "at your price book rate of $50.00/mo" in est["line"]


def test_a_rate_in_another_currency_is_not_guessed_at(packs_env, tmp_path):
    _install_prices(tmp_path)
    est = g.estimate_command_monthly_cost("aws ec2 run-instances --instance-type m5.large")
    assert est["hourly_usd"] == EC2_HOURLY["m5.large"] and "price book" not in est["line"]


def test_the_plan_estimator_shares_the_price_book(packs_env, tmp_path):
    from finops.connectors.terraform_estimate import estimate_plan
    plan = {"resource_changes": [
        {"address": "aws_instance.gpu", "type": "aws_instance",
         "change": {"actions": ["create"], "after": {"instance_type": "p4d.24xlarge"}}},
        {"address": "aws_instance.web", "type": "aws_instance",
         "change": {"actions": ["create"], "after": {"instance_type": "t3.micro"}}}]}
    before = estimate_plan(plan)
    assert "price_books" not in before
    _install_prices(tmp_path)
    after = estimate_plan(plan)
    gpu = next(line for line in after["lines"] if line["address"] == "aws_instance.gpu")
    assert gpu["monthly_delta"] == round(21.5 * HOURS_PER_MONTH, 2)
    assert "your price book rate, io.github.example/prices" in gpu["detail"]
    assert after["price_books"] == {"packs": ["io.github.example/prices"], "resources": 1}
    web = next(line for line in after["lines"] if line["address"] == "aws_instance.web")
    assert web == next(line for line in before["lines"] if line["address"] == "aws_instance.web")


# ── the doctor ─────────────────────────────────────────────────────────────────

def test_the_doctor_lists_protected_files_editor_coverage_and_packs(packs_env, tmp_path,
                                                                    settings):
    _install_rules(tmp_path)
    _install_prices(tmp_path)
    g.install()
    d = ga.with_plugin(g.doctor())
    paths = {p["path"] for p in d["protected_paths"]}
    assert "~/.finops/guard-off" in paths
    assert d["editor_tools"]["claude-code"] == "covered"
    assert d["editor_tools"]["cursor"].startswith("not covered")
    assert "afterFileEdit" in d["editor_tools"]["cursor"]
    for name in ("codex", "copilot", "gemini", "cline"):
        assert d["editor_tools"][name].startswith("not covered")
    assert {r["id"] for r in d["packs"]["guard_rules"]} >= {"ask-nat", "deny-destroy"}
    assert {b["sku"] for b in d["packs"]["price_books"]} >= {"p4d.24xlarge"}
    assert any("guard rules from installed packs" in c for c in d["covered"])
    assert any("price book" in c for c in d["covered"])


def test_the_doctor_cli_prints_the_new_sections(packs_env, tmp_path, settings, capsys):
    import argparse

    from finops import setup_wizard
    _install_rules(tmp_path)
    g.install()
    setup_wizard._guard_doctor(argparse.Namespace(guard_json=False))
    out = capsys.readouterr().out
    assert "Protected files" in out and "~/.finops/guard-off" in out
    assert "File-edit tools" in out and "Packs in the guard" in out
    assert "rule ask-nat" in out


def test_the_doctor_reads_the_installed_plugins_matcher():
    plugins = Path.home() / ".claude" / "plugins" / "cache" / "nable" / "nable"
    assert ga.plugin_sees_editor() is None, "no plugin installed"

    def release(version: str, matcher: str) -> None:
        d = plugins / version / "hooks"
        d.mkdir(parents=True)
        (d / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [{
            "matcher": matcher, "hooks": [{"type": "command", "command":
                                           "uvx finops guard hook --via plugin; exit 0"}]}]}}))
    release("0.8.300", g._HOOK_MATCHER)
    assert ga.plugin_sees_editor() is True
    release("0.8.200", "^(Bash|mcp__.*)$")
    assert ga.plugin_sees_editor() is None, "two releases that disagree: unknown"
