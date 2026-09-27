"""Review fixes to the guard's own-file checks, self rules and pack rules.

What has to stay true:
  - every check is linear in the command: padded to the hook's 256 KB limit
    with any repeated shape, a destroy is still judged (an ask naming the
    destroy) well inside the hook's timeout, and interpreter code over 16 KB
    asks as an oversize command does
  - the self rules (guard off and uninstall, the budgets, org decisions,
    pack changes) ask however nable is started: nable, finops, finops-mcp,
    uvx with a pin, python -m; and so does code that calls the org API
  - a glob in a write asks only when it expands to a protected file; a
    recursive change asks only when a protected file is there under it (or
    names nable.org/, which creating changes which org model applies)
  - a write inside `$(...)`, backticks or a process substitution asks
  - a pack rule does not match the quoted data of a commit message, a search
    or a PR title, and does match the command itself
  - find into a protected tree, an MCP call past the walk's cap, `uv run`
    and friends, git checkout/restore/stash over a protected file ask
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import finops.guard as g
import finops.guard_adapters as ga
import finops.guard_packs as gpk
import finops.guard_paths as gpa
import finops.guard_plugin as gp
from finops import ai_budget
from tests import packs_support
from tests.packs_support import make_pack

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
packs_env = packs_support.packs_env


@pytest.fixture(autouse=True)
def machine(monkeypatch):
    """A home with a nable data dir and a git repo without nable.org/, both
    outside pytest's own temp tree (so `rm -rf /tmp/pytest-*/*` touches
    neither); the agent works at the repo's root."""
    top = Path(tempfile.mkdtemp(prefix="nable-review-")).resolve()
    home = top / "home"
    (home / ".finops").mkdir(parents=True)
    repo = top / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo)
    for var in ("FINOPS_DATA_DIR", "FINOPS_PROFILE", "FINOPS_ORG_DIR", "FINOPS_GUARD_STRICT",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_POLICY_MAX_AUTO_USD", "CLAUDE_CONFIG_DIR",
                "CLAUDE_PROJECT_DIR", "FINOPS_TAG_RULES", "FINOPS_ACCOUNTS_FILE",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_POLICY_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setattr(gp, "_data_root_override", None)
    monkeypatch.setattr(gp, "_user_dir_override", None)
    db = sys.modules.get("finops.storage.db")
    if db is not None:
        monkeypatch.setattr(db, "_DATA_DIR", None)
    from finops.packs import store
    monkeypatch.setattr(store, "_root_override", None)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    yield {"home": home, "repo": repo, "top": top}
    shutil.rmtree(top, ignore_errors=True)


def _ask(command: str, cwd: str | None = None) -> dict | None:
    v = g.gate_command(command, record=False, cwd=cwd or os.getcwd())
    return v if v and v["decision"] in ("ask", "deny") else None


def _org(machine) -> Path:
    org = machine["repo"] / "nable.org"
    org.mkdir()
    (org / "policy.yaml").write_text("facts: []\n")
    return org


# ── 1. linear in the command, and still judged ────────────────────────────────

DESTROY = "terraform destroy -auto-approve; "


def _fill(prefix: str, unit, suffix: str = "") -> str:
    """`prefix`, then `unit` (a string, or i -> string for distinct words)
    as many times as fits, then `suffix`: MAX_JUDGED_CHARS at most."""
    make = unit if callable(unit) else (lambda _i: unit)
    out, n, i = [prefix], len(prefix) + len(suffix), 0
    while n + len(make(i)) <= g.MAX_JUDGED_CHARS:
        out.append(make(i))
        n += len(make(i))
        i += 1
    return "".join(out) + suffix


PADDED = [
    (DESTROY + "python3 -c '", "open(f, ", "'"),
    (DESTROY + "python3 -c ", "open(f,", ""),
    (DESTROY + "touch {", "a,", ""),
    (DESTROY + "touch ~/.finops/{", "a,", "b}"),
    (DESTROY + "touch ", "$(a ", ""),
    (DESTROY + "touch ", "`a ", ""),
    (DESTROY + "touch ", "<(a ", ""),
    (DESTROY + "rm -rf ", "*/", ""),
    (DESTROY + "rm -rf ", lambda i: f"a{i}* ", ""),
    (DESTROY + "rm -f ", lambda i: f"f{i} ", ""),
    (DESTROY + "cp ", lambda i: f"x{i}/* ", "d/"),
    (DESTROY + "sed -i s/a/b/ ", lambda i: f"k8s/*{i}.yaml ", ""),
    (DESTROY + "find . ", lambda i: f"-name x{i} ", "-delete"),
    (DESTROY, lambda i: f"cd d{i}; x=$(touch y{i}); ", ""),
    (DESTROY + "cd ", "a/", "; touch x"),
    (DESTROY + "python3 ", "-W ", "-c x"),
    (DESTROY + "python3 - <<'EOF'\n", lambda i: f"open('f{i}', 'w')\n", "EOF"),
    (DESTROY + "python3 -c 'from finops.org import store; ", "x;", "'"),
    (DESTROY + "echo ", "nable ", ""),
    (DESTROY + "git commit -m '", "aws ec2 create-nat-gateway ", "'"),
]


@pytest.mark.parametrize("prefix,unit,suffix", PADDED,
                         ids=[f"{i}" for i in range(len(PADDED))])
def test_a_command_padded_to_the_limit_is_judged_in_time(prefix, unit, suffix):
    """Measured at under a second each; the bound is the hook's own
    shortest timeout (10 s), which a timed-out hook fails open past."""
    command = _fill(prefix, unit, suffix)
    assert len(command) <= g.MAX_JUDGED_CHARS
    t = time.perf_counter()
    v = g.gate_command(command, record=False, cwd=os.getcwd())
    elapsed = time.perf_counter() - t
    assert elapsed < 5.0, (prefix[:50], elapsed)
    assert v is not None and v["decision"] == "ask", prefix[:50]
    assert "destroy" in v["reason"], "the destroy is still what the human reads about"


def _cli_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update({"HOME": str(home), "FINOPS_DATA_DIR": str(home / ".finops"),
                "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1",
                "NABLE_NO_TELEMETRY": "1"})
    for var in ("FINOPS_PROFILE", "FINOPS_GUARD", "CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR",
                "FINOPS_ORG_DIR", "FINOPS_POLICY_FILE"):
        env.pop(var, None)
    return env


@pytest.mark.parametrize("prefix,unit", [(DESTROY + "python3 -c '", "open(f, "),
                                         (DESTROY + "touch {", "a,")])
def test_the_reviewers_padding_through_the_plugin_hook(prefix, unit, machine):
    """The shapes that took the plugin hook 62 s and 15 s: the payload goes
    in on stdin, the command is never run."""
    command = _fill(prefix, unit, "'" if prefix.endswith("'") else "")
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                          "tool_input": {"command": command}, "session_id": "s",
                          "cwd": str(machine["repo"])})
    t = time.perf_counter()
    r = subprocess.run([sys.executable, "-m", "finops.setup_wizard", "guard", "hook",
                        "--via", "plugin"], input=payload, capture_output=True, text=True,
                       env=_cli_env(machine["home"]), cwd=str(machine["repo"]), timeout=120,
                       check=False)
    elapsed = time.perf_counter() - t
    assert elapsed < 10.0, elapsed
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"
    assert "destroy" in out["permissionDecisionReason"]


def test_open_with_a_write_mode_is_found_in_one_pass():
    assert g._code_writes("open(os.path.expanduser(~/.finops/guard-off),w)")
    assert g._code_writes("open(p, mode=a)")
    assert g._code_writes("with open(f, wb) as fh: pass")
    assert not g._code_writes("print(open(p).read())")
    assert not g._code_writes("json.load(open(../nable.org/policy.yaml))")
    t = time.perf_counter()
    assert not g._code_writes("open(f, " * 40000)
    assert not g._code_writes("open(" + "y," * 100000)
    assert time.perf_counter() - t < 2.0


def test_braces_are_read_in_one_pass_and_a_long_list_keeps_what_matters():
    entries = gpa.protected()
    assert gpa._braces("~/.finops/{a,guard-off}", entries) == ["~/.finops/a",
                                                               "~/.finops/guard-off"]
    assert gpa._braces("{x}{a,b}", entries) == ["{x}a", "{x}b"]
    assert gpa._braces("{" + "a," * 100000, entries) == ["{" + "a," * 100000]
    many = "~/.finops/{" + ",".join(f"f{i}" for i in range(500)) + ",guard-off}"
    assert "~/.finops/guard-off" in gpa._braces(many, entries)
    assert gpa.match(many) is not None
    t = time.perf_counter()
    gpa.match("{" + "a," * 100000)
    gpa.match("{" + "a," * 60000 + "b}")
    assert time.perf_counter() - t < 2.0


@pytest.mark.parametrize("command", [
    "python3 -c '{}'", 'node -e "{}"', "perl -ne '{}'", "ruby -e '{}'", "python3 -Bc '{}'",
    "uv run python -c '{}'",
])
def test_interpreter_code_over_16_kb_asks_as_oversize(command):
    big = command.format("x = 1; " * 3000)
    v = _ask(big)
    assert v and v["action_type"] == "oversize_command" and "KB" in v["reason"]
    assert _ask(command.format("x = 1; " * 1000)) is None


def test_oversize_code_and_a_destroy_are_judged_as_both():
    v = _ask("terraform destroy -auto-approve; python3 -c '" + "x = 1; " * 3000 + "'")
    assert v["decision"] == "ask"
    assert "destroy" in v["reason"] and "one-liner" in v["reason"]


def test_more_paths_than_the_guard_checks_asks():
    v = _ask("rm -f " + " ".join(f"f{i}" for i in range(g._PATHS_MAX + 10)))
    assert v and v["action_type"] == "oversize_command"
    # The same path named again and again is looked at once.
    assert _ask("rm -f " + " ".join(["build/out.o"] * 20000)) is None


# ── 2. the self rules, however nable is started ───────────────────────────────

ENTRY_POINTS = [
    "nable", "finops", "finops-mcp", "uvx nable", "uvx finops-mcp", "uvx finops-mcp@1.2.3",
    "uvx finops-mcp@latest", "uvx --from finops-mcp==1.2.3 finops",
    "uvx --from finops-mcp==1.2.3 finops-mcp", "uvx --from 'finops-mcp[aws]==1.2.3' nable",
    "uvx --python 3.12 --from finops-mcp==0.8.218 finops", "pipx run finops-mcp",
    "python -m finops.setup_wizard", "python3 -m finops.entry", "python3.12 -mfinops.entry",
    "python -m finops.server", "uv run python -m finops.setup_wizard", "uvx nable@0.8.2",
    "/opt/venv/bin/finops-mcp", "~/.local/bin/nable", "sudo -E nable",
]
SELF_RULES = [
    ("guard off", "guard_change"),
    ("guard uninstall", "guard_change"),
    ("guard uninstall --global", "guard_change"),
    ("uninstall", "guard_change"),
    ("ai-budget --spend-cap 999", "ai_budget_change"),
    ("ai-budget --reset", "ai_budget_change"),
    ("budget ci-gate --budget-file budget.yml", "budget_change"),
    ("org confirm owner:aws_account:1", "org_change"),
    ("org reject owner:aws_account:1", "org_change"),
    ("org set owner --subject aws_account:1 --team pay", "org_change"),
    ("org trust", "org_change"),
    ("pack install ./p", "pack_change"),
    ("pack update io.github.acme/rules", "pack_change"),
    ("pack remove io.github.acme/rules", "pack_change"),
    ("pack sign ./p --key k.pem", "pack_change"),
    ("pack keygen --out k.pem", "pack_change"),
    ("pack secret set io.github.acme/rules API_TOKEN", "pack_change"),
    ("pack secret remove io.github.acme/rules API_TOKEN", "pack_change"),
    ("pack secret --yes set io.github.acme/rules API_TOKEN", "pack_change"),
]


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("rule,action", SELF_RULES)
def test_every_self_rule_asks_from_every_entry_point(entry, rule, action):
    v = _ask(f"{entry} {rule}")
    assert v is not None, f"{entry} {rule}"
    assert v["action_type"] == action


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("rule", ["guard status", "guard doctor", "org status", "org review",
                                  "pack list", "pack secret list io.github.acme/rules",
                                  "budget status", "ai-budget", "scan"])
def test_reading_from_every_entry_point_stays_silent(entry, rule):
    assert _ask(f"{entry} {rule}") is None, f"{entry} {rule}"


@pytest.mark.parametrize("command", [
    "python -m finops.org.cli confirm owner:x",
    "python3 -m finops.org.cli reject owner:x --as me",
    "python -m finops.org.cli set owner --subject a:b --team t",
    "python -m finops.org.cli trust",
])
def test_the_org_cli_module_asks(command):
    assert _ask(command)["action_type"] == "org_change"


@pytest.mark.parametrize("command", [
    "python3 -c \"from finops.org import store; store.confirm('owner:x', by='me')\"",
    "python3 -c \"import finops.org.store as s; s.reject('owner:x', 'me')\"",
    "python -c 'from finops.org.store import set_fact; set_fact(f, by=\"x\")'",
    "python3 -c 'from finops.org import store; store.confirm_many([\"k\"], \"me\")'",
    "python3 -c 'from finops.org import store; store.reject_many([\"k\"], \"me\")'",
    "python3 -c 'from finops.org import store; store.import_legacy()'",
    "python3 -c 'from finops.org import store; store.trust(\"repo\")'",
    "python3 -c 'from finops.org.store import confirm as c; c(\"k\", \"me\")'",
    "python3 - <<'EOF'\nfrom finops.org import store\nstore.confirm('owner:x', by='me')\nEOF",
    "uv run python -c 'import finops.org.store as s; s.confirm(\"k\", \"me\")'",
    "python3 -c 'from finops import guard_plugin; guard_plugin.set_off(True)'",
    "python3 -c 'import finops.guard as g; g.uninstall()'",
])
def test_code_that_calls_the_org_api_or_turns_the_guard_off_asks(command):
    v = _ask(command)
    assert v is not None, command
    assert v["action_type"] in ("org_change", "guard_change")


@pytest.mark.parametrize("command", [
    "python3 -c 'from finops.org import store; print(store.resolve_dir())'",
    "python3 -c 'import finops.guard_plugin as p; print(p.off_reason())'",
    'git commit -m "call store.confirm from finops.org in the CLI only"',
    'grep -rn "def confirm(" src/finops/org/store.py',
])
def test_code_that_only_reads_the_org_model_stays_silent(command):
    assert _ask(command) is None, command


def test_an_mcp_shell_running_a_self_rule_asks_and_is_judged_as_both(monkeypatch):
    v = g.gate_mcp_call("mcp__shell__run", {"command": "uvx finops-mcp@1.2.3 guard off"},
                        record=False)
    assert v and v["decision"] == "ask" and v["action_type"] == "guard_change"
    v = g.gate_mcp_call("mcp__shell__run",
                        {"command": "nable guard off; terraform destroy -auto-approve"},
                        record=False)
    assert v["decision"] == "ask"
    assert "turning the guard off" in v["reason"] and "cannot be undone" in v["reason"]
    assert v["reason"].count("turning the guard off") == 1
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "ticket")
    v = g.gate_mcp_call("mcp__shell__run",
                        {"command": "nable org confirm k; aws ec2 stop-instances --instance-ids i"},
                        record=False)
    assert v["decision"] == "deny", "the self rule never softens the policy's deny"


# ── 3 and 4. globs and recursive changes ask only over what is there ──────────

SILENT_IN_A_REPO = [
    "rm -rf build/*", "chmod +x scripts/*", "cp config/*.yaml deploy/", "mv logs/* archive/",
    'sed -i "s/v1/v2/" k8s/*.yaml', "rm -rf /tmp/pytest-*/*", "git rm -r --cached .",
    "chmod -R 755 .", "sudo chown -R $USER .", "rm -rf -- *", "rm -rf ./*",
    "cp budget.yml budget.yml.bak", "git checkout .", "git restore .", "git stash",
    "git stash pop", "git clean -fd",
]


@pytest.mark.parametrize("command", SILENT_IN_A_REPO)
def test_ordinary_globs_and_recursive_changes_stay_silent(command, machine):
    repo = machine["repo"]
    for d in ("build", "scripts", "config", "deploy", "logs", "archive", "k8s"):
        (repo / d).mkdir()
        (repo / d / "file.yaml").write_text("x")
    assert _ask(command) is None, command


@pytest.mark.parametrize("command", ["git rm -r --cached .", "chmod -R 755 .",
                                     "sudo chown -R $USER .", "rm -rf -- *", "rm -rf ./*",
                                     "git checkout .", "git restore .", "git stash",
                                     "git stash pop", "git clean -fd"])
def test_the_same_over_a_repo_with_nable_org_asks(command, machine):
    _org(machine)
    v = _ask(command)
    assert v is not None, command
    assert "nable.org" in v["reason"]


def test_a_path_that_is_not_there_is_not_named(machine):
    (machine["repo"] / ".claude").mkdir()
    (machine["repo"] / ".claude" / "settings.json").write_text("{}")
    v = _ask("chmod -R 755 .")
    assert v is not None and ".claude/settings.json" in v["reason"]
    assert "nable.org" not in v["reason"]


@pytest.mark.parametrize("command,made", [
    ("cp config/*.yaml deploy/", "config/budget.yaml"),
    ('sed -i "s/v1/v2/" k8s/*.yaml', "k8s/budget.yaml"),
    ("rm -rf build/*", "build/budget.yml"),
    ("chmod +x scripts/*", "scripts/budget.yml"),
    ("rm -f **/*.yml", "a/b/budget.yml"),
])
def test_a_glob_that_expands_to_a_budget_file_asks(command, made, machine):
    repo = machine["repo"]
    for d in ("config", "deploy", "k8s", "build", "scripts"):
        (repo / d).mkdir()
    assert _ask(command) is None, command
    (repo / made).parent.mkdir(parents=True, exist_ok=True)
    (repo / made).write_text("budgets: []\n")
    v = _ask(command)
    assert v is not None and "budget file" in v["reason"], command


def test_a_glob_that_names_a_file_literally_asks_without_it():
    assert _ask("touch */budget.yml")
    assert _ask("rm ~/.fin*/guard-off")


def test_a_glob_that_climbs_back_out_and_nested_braces_are_read(machine):
    (machine["repo"] / ".claude").mkdir()
    (machine["repo"] / ".claude" / "settings.json").write_text("{}")
    assert _ask("rm -rf src/*/../../.claude")
    assert _ask("rm -rf */../.claude")
    assert _ask("touch ~/.finops/{a,{b,guard-off}}")
    assert _ask("touch ~/.finops/{a,{b,c}}") is None


def test_paths_grown_past_the_limit_by_their_variables_are_charged_for():
    """`$V$V$V...` with a long V is not expanded past 64 KB, and each one
    costs the check what reading that much would."""
    words = " ".join(f"{'$V' * 17}/{i}" for i in range(40))
    t = time.perf_counter()
    v = _ask("V=" + "a" * 4000 + "; rm " + words)
    assert time.perf_counter() - t < 5.0
    assert v and v["action_type"] == "oversize_command"


@pytest.mark.parametrize("command", [
    "cp budget.yml.bak budget.yml", "cp /tmp/x.yml ./budget.yaml", "mv new.yml budget.yml",
    "cp budget.yml.bak deploy/budget.yml",
])
def test_writing_a_budget_file_by_its_name_still_asks(command, machine):
    (machine["repo"] / "deploy").mkdir()
    assert _ask(command), command


@pytest.mark.parametrize("command", [
    "mkdir nable.org", "mkdir -p nable.org/teams", "touch nable.org/policy.yaml",
    "echo x > services/pay/nable.org/policy.yaml", "mkdir -p sub/repo/nable.org",
    "cp /tmp/p.yaml src/nable.org/", "touch */nable.org/x.yaml",
])
def test_creating_an_org_model_asks(command):
    v = _ask(command)
    assert v is not None, command
    assert "org model" in v["reason"]


def test_writing_into_nable_org_with_the_editor_asks(machine):
    for path in ("nable.org/x.yaml", str(machine["repo"] / "nable.org" / "teams.yaml"),
                 "services/pay/nable.org/owners.yaml"):
        assert gp.editor_target("Write", {"file_path": path}, str(machine["repo"])), path
    assert gp.editor_target("Write", {"file_path": "src/app.py"}, str(machine["repo"])) is None


@pytest.mark.parametrize("command", [
    "echo '{}' > ~/.finops/org-parse-cache/abc.json",
    "rm -rf ~/.finops/org-parse-cache",
    "mkdir -p ~/.finops/org-parse-cache",
    "cp /tmp/facts.json ~/.finops/org-parse-cache/",
    "echo '[\"/tmp/evil\"]' > ~/.finops/org/trusted-repos.json",
    "rm ~/.finops/org/trusted-repos.json",
    "python3 -c \"open(p + '/trusted-repos.json', 'w').write('[]')\"",
])
def test_the_org_parse_cache_and_trusted_repos_are_protected(command):
    v = _ask(command)
    assert v is not None, command
    assert v["action_type"] == "protected_write"


def test_the_org_files_are_protected_beside_a_moved_ledger_too(machine, monkeypatch):
    import finops.guard_ledger as gl
    moved = machine["top"] / "ledger-elsewhere" / "guard-ledger.jsonl"
    monkeypatch.setattr(gl, "_path_override", moved)
    assert gpa.match(str(moved.parent / "org-parse-cache" / "x.json")) is not None
    assert gpa.match(str(machine["home"] / ".finops" / "org-parse-cache" / "x.json"))
    assert gpa.match(str(machine["home"] / ".finops" / "org" / "trusted-repos.json"))
    assert gpa.match("~/.finops/org/trusted-repos.json").what.startswith("the org model")


@pytest.mark.parametrize("path", ["nable.org", "a/b/nable.org", "a/nable.org/b/c.yaml",
                                  "./x/../y/nable.org/teams.yaml"])
def test_a_nable_org_component_anywhere_under_the_cwd_is_protected(path, machine):
    """Present or not: creating one is what makes it an org model."""
    for command in (f"mkdir -p {path}", f"touch {path}", f"rm -rf {path}"):
        v = _ask(command)
        assert v is not None and "org model" in v["reason"], command
    assert gp.editor_target("Write", {"file_path": path}, str(machine["repo"]))


def test_the_data_dir_as_a_whole_is_protected_and_a_file_in_it_is_not(machine):
    assert _ask("rm -rf ~/.finops")
    assert _ask("rm -rf ~/.fin*")
    assert _ask("touch ~/.finops/notes.txt") is None
    assert _ask("mkdir -p ~/.finops") is None


# ── 5. writes inside substitutions ─────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "x=$(touch ~/.finops/guard-off)",
    "`touch ~/.finops/guard-off`",
    'echo "$(touch ~/.finops/guard-off)"',
    'git commit -m "$(touch ~/.finops/guard-off)"',
    "echo $(echo $(rm ~/.finops/guard-off))",
    "diff a >(tee ~/.finops/guard-off)",
    "D=~/.finops; x=$(rm $D/guard-off)",
    "cd ~/.finops && x=$(rm guard-off)",
    "x=`cd ~/.finops && rm guard-off`",
    "echo $((1 + 2)) $(python3 -c \"open('$HOME/.finops/guard-off','w')\")",
])
def test_a_write_inside_a_substitution_asks(command):
    v = _ask(command)
    assert v is not None, command
    assert v["action_type"] == "protected_write"


@pytest.mark.parametrize("command", [
    "x=$(date)",
    'echo "$(git rev-parse HEAD)" > /tmp/rev',
    "git commit -m \"$(cat <<'EOF'\nfix: typo in the README\nEOF\n)\"",
    "echo $(ls) rm budget.yml",
    "cd $(mktemp -d) && touch x",
])
def test_an_ordinary_substitution_stays_silent(command):
    assert _ask(command) is None, command


# ── 6. pack rules and quoted data ──────────────────────────────────────────────

NAT_RULE = """\
version: 1
rules:
  - id: ask-nat
    pattern: '\\baws\\s+ec2\\s+create-nat-gateway\\b'
    verdict: ask
    reason: NAT gateways bill hourly and per GB.
"""


def _install_rules(tmp_path: Path, text: str = NAT_RULE) -> None:
    from finops.packs import install as inst
    src = make_pack(tmp_path / "rules-pack")
    (src / "guard" / "rules.yaml").write_text(text)
    inst.install(str(src), yes=True)
    gpk.invalidate()


@pytest.mark.parametrize("command", [
    'git commit -m "docs: explain why aws ec2 create-nat-gateway needs a ticket"',
    'grep -rn "aws ec2 create-nat-gateway" docs/',
    'gh pr create --title "Ask before aws ec2 create-nat-gateway" --body "See the pack."',
    'gh issue comment 12 --body "aws ec2 create-nat-gateway ran twice"',
    "echo 'aws ec2 create-nat-gateway' | wc -c",
])
def test_a_pack_rule_does_not_match_quoted_data(command, packs_env, tmp_path):
    _install_rules(tmp_path)
    assert _ask(command) is None, command


@pytest.mark.parametrize("command", [
    "aws ec2 create-nat-gateway --subnet-id s-1",
    '"aws" ec2 "create-nat-gateway"',
    'echo "x" && aws ec2 create-nat-gateway --subnet-id s-1',
    'bash -c "aws ec2 create-nat-gateway --subnet-id s-1"',
    'git commit -m "wip" && aws ec2 create-nat-gateway --subnet-id s-1',
])
def test_a_pack_rule_matches_the_command_itself(command, packs_env, tmp_path):
    _install_rules(tmp_path)
    v = _ask(command)
    assert v and "NAT gateways bill hourly" in v["reason"], command


def test_a_pack_rule_on_an_mcp_shell_call_reads_the_same_way(packs_env, tmp_path):
    _install_rules(tmp_path)
    quiet = 'git commit -m "aws ec2 create-nat-gateway is gated now"'
    assert g.gate_mcp_call("mcp__shell__run", {"command": quiet}, record=False) is None
    v = g.gate_mcp_call("mcp__shell__run", {"command": "aws ec2 create-nat-gateway"},
                        record=False)
    assert v and v["decision"] == "ask"


# ── 7. find into a protected tree ──────────────────────────────────────────────

def test_find_into_a_protected_tree_asks(machine):
    _org(machine)
    assert _ask("find nable.org -name '*.yaml' -exec sed -i s/proposed/confirmed/ {} +")
    assert _ask("find . -name '*.yaml' -exec sed -i s/a/b/ {} +")
    assert _ask("find . -iname 'POLICY.YAML' -delete")
    assert _ask("find nable.org -name '*.yaml'") is None, "no action: a read"
    assert _ask("find . -name '*.pyc' -delete") is None, "no .pyc in the org model"


# ── 8. MCP calls past the walk's cap ───────────────────────────────────────────

def test_an_mcp_write_past_the_walk_cap_asks_and_a_read_does_not():
    rows = [{"note": f"n{i}"} for i in range(g._MCP_WALK_MAX + 50)]
    args = {"rows": rows + [{"path": "/tmp/elsewhere.txt"}]}
    v = g.gate_mcp_call("mcp__fs__write_many", args, record=False)
    assert v and v["decision"] == "ask" and "values the guard checks" in v["reason"]
    assert g.gate_mcp_call("mcp__fs__read_many", args, record=False) is None
    # Nothing checkable among what is left: nothing to ask about.
    assert g.gate_mcp_call("mcp__notes__save", {"rows": rows}, record=False) is None


def test_mcp_path_arguments_are_checked_first():
    args = {"content": [f"line {i}" for i in range(2000)], "path": "~/.finops/guard-off"}
    v = g.gate_mcp_call("mcp__fs__write_file", args, record=False)
    assert v and "off switch" in v["reason"]


def test_the_cap_regex_covers_every_key_the_walk_checks():
    from finops.guard_mcp import _COMMAND_KEYS
    for key in g._MCP_PATH_KEYS | _COMMAND_KEYS:
        assert g._MCP_CHECKED_KEY_RE.search(json.dumps({key: "x"})), key


@pytest.mark.parametrize("name,writes", [
    ("write_file", True), ("writeFile", True), ("createDirectory", True),
    ("put_object", True), ("mkdirs", True), ("writefile", True), ("edit_notebook", True),
    ("move_file", True), ("delete", True), ("rm", True), ("upload", True),
    ("compute_instance_list", False), ("get_output", False), ("read_input", False),
    ("describe_computers", False), ("list_outputs", False), ("search", False),
])
def test_an_mcp_tool_name_is_read_a_word_at_a_time(name, writes):
    assert g._mcp_name_says(name, g._MCP_WRITE_WORDS) is writes


# ── 9. interpreters behind runners and nested calls ────────────────────────────

@pytest.mark.parametrize("command", [
    "python3 -c \"import os; open(os.path.expanduser('~/.finops/guard-off'),'w')\"",
    "python3 -c \"open(os.path.join(os.environ['HOME'], '.finops', 'guard-off'), 'a')\"",
    "uv run python -c \"import pathlib; pathlib.Path('~/.finops/guard-off').expanduser().touch()\"",
    "uv run --with pyyaml python3 -c \"open('$HOME/.finops/nable.policy.yaml','w')\"",
    "uv tool run python -c \"open('$HOME/.finops/guard-off','w')\"",
    "poetry run python -c \"open('$HOME/.finops/guard-off','w')\"",
    "pipenv run python -c \"open('$HOME/.finops/guard-off','w')\"",
    "pdm run python -c \"open('$HOME/.finops/guard-off','w')\"",
    "hatch run python -c \"open('$HOME/.finops/guard-off','w')\"",
    "conda run -n base python -c \"open('$HOME/.finops/guard-off','w')\"",
    "poetry run rm ~/.finops/guard-off",
])
def test_interpreters_behind_runners_and_nested_calls_ask(command):
    v = _ask(command)
    assert v is not None, command
    assert v["action_type"] == "protected_write"


@pytest.mark.parametrize("command", [
    "uv run pytest -q", "poetry run python -c 'print(1)'", "uv run ruff check src",
    "hatch run test", "pdm run lint",
])
def test_ordinary_runner_commands_stay_silent(command):
    assert _ask(command) is None, command


# ── 10. git over a protected file, copies, the doctor ──────────────────────────

def test_git_stash_list_and_show_stay_silent_over_nable_org(machine):
    _org(machine)
    assert _ask("git stash list") is None
    assert _ask("git stash show -p") is None
    assert _ask("git checkout main") is None
    assert _ask("git restore src/") is None


def test_git_stash_from_a_subdirectory_is_the_whole_repo(machine):
    _org(machine)
    assert _ask("git stash", cwd=str(machine["repo"] / "src"))
    assert _ask("git stash push -- src/", cwd=str(machine["repo"])) is None


GIT = shutil.which("git")


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", *args], cwd=repo, env=env, check=True,
                   capture_output=True, timeout=60)


@pytest.fixture
def git_repo(machine):
    """A real repo: src/ and, when asked, nable.org/ committed; Claude Code's
    .claude/settings.local.json there but untracked, as it leaves it."""
    if GIT is None:
        pytest.skip("git is not installed")
    repo = machine["repo"]
    shutil.rmtree(repo / ".git")

    def make(org: bool) -> Path:
        _git(repo, "init", "-q")
        (repo / "src" / "app.py").write_text("x = 1\n")
        if org:
            _org(machine)
        past = time.time() - 120
        for p in repo.rglob("*"):
            if ".git" not in p.parts:
                os.utime(p, (past, past))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.local.json").write_text("{}")
        return repo
    return make


@pytest.mark.parametrize("command", ["git stash", "git stash push", "git checkout .",
                                     "git restore .", "git rm -r --cached .", "git stash pop",
                                     "git restore --staged ."])
def test_an_untracked_local_settings_file_does_not_make_git_ask(command, git_repo):
    git_repo(org=False)
    assert _ask(command) is None, command


def test_git_stash_with_untracked_files_counts_what_is_there(git_repo):
    git_repo(org=False)
    assert _ask("git stash -u")
    assert _ask("git stash push --include-untracked")


def test_git_puts_back_only_what_it_tracks_and_what_changed(git_repo):
    repo = git_repo(org=True)
    for command in ("git stash", "git checkout .", "git restore .", "git restore --staged ."):
        assert _ask(command) is None, f"nable.org is unchanged: {command}"
    for command in ("git stash pop", "git stash apply", "git checkout HEAD~0 -- .",
                    "git restore --source=HEAD .", "git rm -r --cached ."):
        v = _ask(command)
        assert v is not None and "nable.org" in v["reason"], command
    (repo / "nable.org" / "policy.yaml").write_text("facts: [changed]\n")
    for command in ("git stash", "git checkout .", "git restore .", "git checkout -- nable.org"):
        v = _ask(command)
        assert v is not None and "nable.org" in v["reason"], command


def test_the_doctor_reads_the_plugin_matcher_past_another_plugins_broken_file(monkeypatch,
                                                                              tmp_path):
    user = tmp_path / "claude"
    monkeypatch.setattr(gp, "_user_dir_override", user)
    broken = user / "plugins" / "cache" / "aaa-other" / "hooks"
    broken.mkdir(parents=True)
    (broken / "hooks.json").write_text("{ not json")
    ours = user / "plugins" / "cache" / "nable" / "hooks"
    ours.mkdir(parents=True)
    hook = {"type": "command",
            "command": "uvx --from finops-mcp==1 finops guard hook --via plugin; exit 0"}
    (ours / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": g._HOOK_MATCHER, "hooks": [hook]}]}}))
    assert ga.plugin_sees_editor() is True
    (ours / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "^(Bash|mcp__.*)$", "hooks": [hook]}]}}))
    assert ga.plugin_sees_editor() is False
