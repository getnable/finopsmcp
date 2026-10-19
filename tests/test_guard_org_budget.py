"""The guard holds an agent to the AI budgets in the org model (D85, D86).

A budget in nable.org/budgets.yaml is compared, at every call the AI budget
gate already sees (every Bash command and every MCP call in Claude Code), with
this machine's month-to-date coding-agent spend at list price. Over it, the
next call asks, naming the budget's file and fact key, the figure, where its
price came from, and that the figure is this machine's alone; with
FINOPS_GUARD_STOP_ON_BUDGET=1 it is denied. A proposed budget, or one from a
repo nobody trusted, only asks. The stricter of this and ai-budget.json
answers. Anything that cannot be read is no opinion, as before.

Whose budget it is: FINOPS_GUARD_PERSON, else git's user.email, read once and
kept (guard-person.json beside the ledger) until git's config files change.
The reader is stubbed here: nothing depends on what is on PATH.

Driven through gate_command, gate_mcp_call and the real hook body, against
real transcript files at fixture times, under a sandboxed HOME.
"""
from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from finops import ai_budget as ab
from finops import guard, guard_ledger, llm_prices

MONTH_START = datetime(2026, 10, 1, tzinfo=UTC).timestamp()
AT = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

BUDGET = """# nable org model v1
- fact: ai_budget
  subject: {{kind: {kind}, id: {ident}}}
  value: {value}
  source: human
  status: {status}
"""
TEAMS = """# nable org model v1
- fact: team
  subject: {kind: team, id: payments}
  value: {people: [Alice@Acme.example, bob@acme.example]}
  source: human
  status: confirmed
"""


def _transcript(claude: Path, session: str, *, model: str = "claude-sonnet-4-6",
                inp: int = 1_000_000, out: int = 100_000, n: int = 1,
                cwd: str = "/home/alice/work/secret-repo") -> None:
    """n responses. At claude-sonnet-4-6's $3/$15 a million in/out, each
    is $3.00 + $1.50 = $4.50."""
    f = claude / "projects" / "-home-alice-work" / f"{session}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        for i in range(n):
            ts = AT.replace(minute=i % 60).isoformat().replace("+00:00", "Z")
            fh.write(json.dumps({
                "timestamp": ts, "sessionId": session, "cwd": cwd,
                "requestId": f"req-{session}-{i}",
                "message": {"id": f"msg-{session}-{model}-{i}", "model": model, "usage": {
                    "input_tokens": inp, "output_tokens": out,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
            }) + "\n")
    os.utime(f, (AT.timestamp(), AT.timestamp()))


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A sandboxed machine: HOME, Claude Code's transcripts, nable's data dir,
    an org model dir, and no identity until a test gives one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    org = tmp_path / "org"
    org.mkdir()
    monkeypatch.setenv("FINOPS_ORG_DIR", str(org))
    for var in ("FINOPS_GUARD_PERSON", "FINOPS_GUARD_STOP_ON_BUDGET", "CLAUDE_CODE_SESSION_ID",
                "FINOPS_TAG_RULES", "FINOPS_ACCOUNTS_FILE", "FINOPS_REQUIRED_TAGS",
                "FINOPS_PROTECTED_TAGS", "FINOPS_POLICY_FILE",
                "FINOPS_POLICY_ON_BUDGET_BREACH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PATH", "")          # nothing here may depend on what is on PATH
    monkeypatch.setattr(ab, "_month_start_epoch", lambda: MONTH_START)
    reads: list[str | None] = []

    def no_git(cwd):
        reads.append(cwd)
    monkeypatch.setattr(ab, "_read_git_email", no_git)
    _transcript(tmp_path / "claude", "s-1")          # $4.50 this month on this machine
    return {"org": org, "claude": tmp_path / "claude", "reads": reads, "tmp": tmp_path}


def _budget(box, kind, ident, value, status="confirmed", teams=True):
    (box["org"] / "budgets.yaml").write_text(BUDGET.format(kind=kind, ident=ident,
                                                           value=value, status=status))
    if teams:
        (box["org"] / "teams.yaml").write_text(TEAMS)


def _key(box) -> str:
    from finops import org
    (f,) = org.load().by_kind("ai_budget")
    return f.key


def _person(monkeypatch, who="alice@acme.example"):
    monkeypatch.setenv("FINOPS_GUARD_PERSON", who)


# ── over a budget: the next shell call and the next MCP call ─────────────────

def test_over_a_persons_budget_the_next_bash_call_asks_and_says_why(box, monkeypatch):
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 4}")
    _person(monkeypatch, "ALICE@acme.example")
    v = guard.gate_command("ls -la", cwd=str(box["tmp"]))
    assert v["decision"] == "ask" and v["action_type"] == "ai_budget"
    r = v["reason"]
    assert str(box["org"] / "budgets.yaml") in r and _key(box) in r
    assert "$4.50" in r and "$4.00" in r
    assert "this machine only" in r
    assert "llm_prices" in r and llm_prices.AS_OF_BY_PROVIDER["anthropic"] in r
    assert "list-price" in r


def test_over_a_persons_budget_the_next_mcp_call_asks_too(box, monkeypatch):
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 4}")
    _person(monkeypatch)
    v = guard.gate_mcp_call("mcp__notes__search", {"q": "x"}, cwd=str(box["tmp"]))
    assert v["decision"] == "ask" and v["action_type"] == "ai_budget"
    assert _key(box) in v["reason"]


def test_through_the_hook_the_payloads_cwd_finds_the_repos_budget(box, monkeypatch):
    """No FINOPS_ORG_DIR: the agent's directory names the repo, and its
    trusted nable.org/ holds the budget."""
    from finops.org import store
    monkeypatch.delenv("FINOPS_ORG_DIR")
    repo = box["tmp"] / "repo"
    (repo / ".git").mkdir(parents=True)
    org_dir = repo / "nable.org"
    org_dir.mkdir()
    (org_dir / "budgets.yaml").write_text(BUDGET.format(
        kind="person", ident="alice@acme.example", value="{monthly_usd: 4}",
        status="confirmed"))
    monkeypatch.setattr(store, "_data_dir", lambda: box["tmp"] / "data")
    monkeypatch.setattr(store, "is_trusted", lambda root: True)
    _person(monkeypatch)
    out = io.StringIO()
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(repo),
               "hook_event_name": "PreToolUse", "session_id": "s-1"}
    assert guard.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
    hso = json.loads(out.getvalue())["hookSpecificOutput"]
    assert hso["permissionDecision"] == "ask"
    assert str(org_dir / "budgets.yaml") in hso["permissionDecisionReason"]


def test_under_the_budget_nothing_is_said(box, monkeypatch):
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 100}")
    _person(monkeypatch)
    assert guard.gate_command("ls -la") is None


def test_close_to_it_a_note_shows_and_nothing_stops(box, monkeypatch):
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 5}")      # 90%
    _person(monkeypatch)
    v = guard.gate_command("ls -la")
    assert v["decision"] == "warn" and "budgets.yaml" in v["reason"]


def test_the_hard_stop_denies(box, monkeypatch):
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 4}")
    _person(monkeypatch)
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    v = guard.gate_command("ls -la")
    assert v["decision"] == "deny" and "FINOPS_GUARD_STOP_ON_BUDGET" in v["reason"]


def test_the_policys_on_budget_breach_deny_denies(box, monkeypatch, tmp_path):
    pol = tmp_path / "policy.yaml"
    pol.write_text("on_budget_breach: deny\n")
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(pol))
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 4}")
    _person(monkeypatch)
    assert guard.gate_command("ls -la")["decision"] == "deny"


def test_a_proposed_budget_asks_and_never_denies(box, monkeypatch):
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 4}", status="proposed")
    _person(monkeypatch)
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    v = guard.gate_command("ls -la")
    assert v["decision"] == "ask"
    assert "proposed" in v["reason"]


def test_a_budget_from_a_repo_nobody_trusted_asks_and_never_denies(box, monkeypatch):
    from finops.org import store
    monkeypatch.delenv("FINOPS_ORG_DIR")
    repo = box["tmp"] / "cloned"
    (repo / ".git").mkdir(parents=True)
    (repo / "nable.org").mkdir()
    (repo / "nable.org" / "budgets.yaml").write_text(BUDGET.format(
        kind="person", ident="alice@acme.example", value="{monthly_usd: 4}",
        status="confirmed"))
    monkeypatch.setattr(store, "_data_dir", lambda: box["tmp"] / "data")
    _person(monkeypatch)
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    v = guard.gate_command("ls -la", cwd=str(repo))
    assert v["decision"] == "ask" and "trusted" in v["reason"]


# ── which budget ─────────────────────────────────────────────────────────────

def test_a_teams_per_person_usd_applies_when_the_person_has_none(box, monkeypatch):
    _budget(box, "team", "payments", "{monthly_usd: 5000, per_person_usd: 4}")
    _person(monkeypatch, "bob@acme.example")
    v = guard.gate_command("ls -la")
    assert v["decision"] == "ask" and "per person" in v["reason"]
    assert "team:payments" in v["reason"]


def test_a_proposed_personal_budget_does_not_silence_the_teams_confirmed_per_person(
        box, monkeypatch):
    """A proposal only asks. Beside a confirmed per_person_usd it must not
    take that budget's hard stop away: the stricter answers."""
    team = BUDGET.format(kind="team", ident="payments",
                         value="{monthly_usd: 100, per_person_usd: 4}", status="confirmed")
    (box["org"] / "teams.yaml").write_text(TEAMS)
    (box["org"] / "budgets.yaml").write_text(team)
    _person(monkeypatch)
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    assert guard.gate_command("ls -la")["decision"] == "deny"
    proposal = BUDGET.format(kind="person", ident="alice@acme.example",
                             value="{monthly_usd: 1000}", status="proposed")
    (box["org"] / "budgets.yaml").write_text(team + proposal.split("\n", 1)[1])
    v = guard.gate_command("ls -la")
    assert v is not None and v["decision"] == "deny" and "per person" in v["reason"]


def test_a_teams_monthly_usd_is_compared_with_this_machines_spend(box, monkeypatch):
    _budget(box, "team", "payments", "{monthly_usd: 4}")
    _person(monkeypatch, "bob@acme.example")
    v = guard.gate_command("ls -la")
    assert v["decision"] == "ask" and "team:payments" in v["reason"]
    assert "under-count" in v["reason"]


def test_the_orgs_monthly_usd_applies_to_anyone_even_nobody_known(box, monkeypatch):
    _budget(box, "org", "org", "{monthly_usd: 4}", teams=False)
    v = guard.gate_command("ls -la")
    assert v["decision"] == "ask" and "org:org" in v["reason"]


def test_someone_elses_budget_is_not_this_machines(box, monkeypatch):
    _budget(box, "person", "carol@acme.example", "{monthly_usd: 1}")
    _person(monkeypatch, "alice@acme.example")
    assert guard.gate_command("ls -la") is None


def test_unpriced_tokens_are_counted_and_carry_no_dollars(box, monkeypatch):
    """A model llm_prices has no row for is not priced at a guess: its tokens
    are said, its dollars are not added."""
    _transcript(box["claude"], "s-2", model="claude-haiku-5-5", inp=9_000_000, out=0)
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 5}")
    _person(monkeypatch)
    v = guard.gate_command("ls -la")
    assert v["decision"] == "warn"          # $4.50 of $5, the Haiku 5.5 tokens not priced
    assert "9,000,000 tokens" in v["reason"] and "not priced" in v["reason"]


# ── the stricter of this and ai-budget.json ───────────────────────────────────

def test_the_stricter_of_the_org_budget_and_ai_budget_json_answers(box, monkeypatch):
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 4}")     # over
    _person(monkeypatch)
    ab.set_budget(spend_cap=100)                                         # well under
    assert guard.gate_command("ls -la")["decision"] == "ask"
    ab.set_budget(spend_cap=1, on_breach="stop")                         # over, and a stop
    v = guard.gate_command("ls -la")
    assert v["decision"] == "deny"


def test_the_org_budget_answers_when_it_is_the_stricter(box, monkeypatch):
    """ai-budget.json only close (a note), the org budget over: the ask is
    the org budget's, with the note's reason after it."""
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 4}")     # over
    _person(monkeypatch)
    ab.set_budget(spend_cap=5)                                           # 90%: a note
    v = guard.gate_command("ls -la")
    assert v["decision"] == "ask" and v["action_type"] == "ai_budget"
    assert v["reason"].index("budgets.yaml") < v["reason"].index("close to its AI budget")


def test_ai_budget_json_still_answers_when_the_org_budget_is_under(box, monkeypatch):
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 1000}")
    _person(monkeypatch)
    ab.set_budget(spend_cap=1)
    v = guard.gate_command("ls -la")
    assert v["decision"] == "ask" and "--spend-cap" in v["reason"]


# ── what cannot be read is no opinion ────────────────────────────────────────

def test_an_unreadable_org_model_is_no_opinion(box, monkeypatch):
    from finops import guard_org
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 4}")
    _person(monkeypatch)

    def broken(cwd):
        raise OSError("the org model could not be read")
    monkeypatch.setattr(guard_org, "load_model", broken)
    assert guard.gate_command("ls -la") is None
    assert guard.check_budget_gate(None) is None


def test_unreadable_usage_is_no_opinion(box, monkeypatch):
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 4}")
    _person(monkeypatch)

    def broken(**kw):
        raise ValueError("a transcript could not be read")
    monkeypatch.setattr(ab, "month_agent_usage", broken)
    assert guard.gate_command("ls -la") is None


def test_a_budgets_yaml_that_is_not_yaml_is_no_opinion(box, monkeypatch):
    (box["org"] / "budgets.yaml").write_text("- fact: ai_budget\n  subject: [unclosed\n")
    _person(monkeypatch)
    assert guard.gate_command("ls -la") is None


def test_without_a_budgets_yaml_the_org_model_is_never_read(box, monkeypatch):
    from finops import guard_org

    def boom(cwd):
        raise AssertionError("an ordinary call read the org model")
    monkeypatch.setattr(guard_org, "load_model", boom)
    (box["org"] / "teams.yaml").write_text(TEAMS)
    assert guard.gate_command("ls -la") is None


# ── whose budget: FINOPS_GUARD_PERSON, else git's user.email, cached ─────────

def test_finops_guard_person_wins_over_git(box, monkeypatch):
    monkeypatch.setattr(ab, "_read_git_email", lambda cwd: "carol@acme.example")
    _person(monkeypatch, "alice@acme.example")
    assert ab.developer() == ("alice@acme.example", "FINOPS_GUARD_PERSON")


def test_git_user_email_is_read_once_and_kept(box, monkeypatch):
    calls = []

    def git(cwd):
        calls.append(cwd)
        return "Alice@Acme.example"
    monkeypatch.setattr(ab, "_read_git_email", git)
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 4}")
    for _ in range(3):
        assert guard.gate_command("ls -la")["decision"] == "ask"
    assert len(calls) == 1
    cache = guard_ledger.ledger_path().with_name("guard-person.json")
    assert "alice@acme.example" in cache.read_text().lower()


def test_a_change_to_gits_config_reads_it_again(box, monkeypatch):
    who = ["alice@acme.example"]
    calls = []

    def git(cwd):
        calls.append(cwd)
        return who[0]
    monkeypatch.setattr(ab, "_read_git_email", git)
    gitconfig = Path(os.environ["HOME"]) / ".gitconfig"
    gitconfig.write_text("[user]\n\temail = alice@acme.example\n")
    assert ab.developer()[0] == "alice@acme.example"
    who[0] = "carol@acme.example"
    gitconfig.write_text("[user]\n\temail = carol@acme.example\n  # longer now\n")
    assert ab.developer() == ("carol@acme.example", "git user.email")
    assert len(calls) == 2


def test_no_identity_at_all_still_meets_the_orgs_budget_and_nobody_elses(box, monkeypatch):
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 1}")
    assert ab.developer() == (None, "")
    assert guard.gate_command("ls -la") is None


# ── the identity is the budget's key: an agent setting it is asked ───────────

@pytest.mark.parametrize("cmd", [
    "git config user.email carol@acme.example",
    "git config --global user.email carol@acme.example",
    "git config --local User.Email 'carol@acme.example'",
    "cd repo && git -C . config user.email carol@acme.example",
    "git config --unset user.email",
    "git config --replace-all user.email carol@acme.example",
    "git config set user.email carol@acme.example",
    "git config unset --global user.email",
    "git config --remove-section user",
    "git config --edit --global",
    "/usr/bin/git config user.email x@y.z",
    # An include brings in another file's [user] section: the same change.
    "git config include.path /tmp/other.cfg",
    "git config --global include.path ~/elsewhere.cfg",
    "git config --local includeIf.gitdir:~/work/.path ~/work.cfg",
    "git config --add include.path ../x.cfg",
    "git config set include.path ../x.cfg",
    "git config --unset include.path",
    "git config --remove-section include",
    "git config --rename-section includeIf.gitdir:~/work/ foo",
])
def test_an_agent_changing_gits_user_email_is_asked(box, cmd):
    v = guard.gate_command(cmd)
    assert v is not None and v["decision"] == "ask", cmd
    assert v["action_type"] == "ai_budget_change" and "user.email" in v["reason"]


@pytest.mark.parametrize("cmd", [
    "git config user.email",
    "git config --get user.email",
    "git config get user.email",
    "git config --list",
    "git config user.name 'Alice'",
    "git -c user.email=x@y.z commit -m wip",
    "git commit -m 'git config user.email x@y.z'",
    "git log --format=%ae",
    "git config --get include.path",
    "git config --get-regexp '^includeif'",
    "git config remote.origin.url git@example.com:acme/x.git",
])
def test_reading_it_or_anything_else_stays_silent(box, cmd):
    assert guard.gate_command(cmd) is None, cmd


def _repo(box) -> Path:
    repo = box["tmp"] / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "config").write_text("[core]\n\tbare = false\n")
    return repo


@pytest.mark.parametrize("cmd", [
    "printf '[user]\\n\\temail = nobody@nowhere.example\\n' >> .git/config",
    "echo '[user] email = x@y.z' | tee -a .git/config",
    "sed -i 's/email = .*/email = x@y.z/' .git/config",
    "cp /tmp/other.cfg .git/config",
    "printf '[user]\\n\\temail = x@y.z\\n' >> ~/.gitconfig",
    "sed -i 's/email = .*/email = x@y.z/' ~/.gitconfig",
    "mkdir -p ~/.config/git && printf '[user]\\n\\temail = x@y.z\\n' > ~/.config/git/config",
    "rm .git/config",
])
def test_writing_a_git_config_file_directly_is_asked_too(box, cmd):
    """The same identity change, made by writing the file git reads it from
    instead of running git config: the repo's, the global and the XDG one."""
    repo = _repo(box)
    v = guard.gate_command(cmd, cwd=str(repo))
    assert v is not None and v["decision"] == "ask", cmd
    assert "user.email" in v["reason"], cmd


@pytest.mark.parametrize("cmd", [
    "cat .git/config",
    "grep email ~/.gitconfig",
    "git status",
    "git commit -m 'edit .git/config later'",
])
def test_reading_a_git_config_file_stays_silent(box, cmd):
    repo = _repo(box)
    assert guard.gate_command(cmd, cwd=str(repo)) is None, cmd


@pytest.mark.parametrize("tool,where", [("Edit", ".git/config"), ("Write", ".git/config"),
                                        ("Write", "~/.gitconfig")])
def test_an_editor_tool_writing_a_git_config_file_is_asked(box, tool, where):
    repo = _repo(box)
    path = str(Path(os.path.expanduser(where)) if where.startswith("~") else repo / where)
    inp = {"file_path": path, "old_string": "bare = false",
           "new_string": "bare = false\n[user]\n\temail = x@y.z"} if tool == "Edit" else {
        "file_path": path, "content": "[user]\n\temail = x@y.z\n"}
    v = guard.gate_editor(tool, inp, cwd=str(repo), record=False)
    assert v is not None and v["decision"] == "ask"
    assert "user.email" in v["reason"]


def test_an_identity_moved_some_other_way_still_answers_to_the_earlier_budget(box, monkeypatch):
    """A backstop for what no check above sees (a file an include names, a
    script): when git's user.email for a repo changes, the next call asks once,
    naming both, and this month the earlier identity's budgets still apply,
    since this machine's month holds what was spent under it."""
    repo = _repo(box)
    who = ["alice@acme.example"]
    monkeypatch.setattr(ab, "_read_git_email", lambda cwd: who[0])
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 4}")
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    v = guard.gate_command("ls -la", cwd=str(repo))
    assert v["decision"] == "deny" and v["action_type"] == "ai_budget"
    who[0] = "nobody@nowhere.example"
    (repo / ".git" / "config").write_text("[include]\n\tpath = /tmp/elsewhere.cfg\n")
    assert ab.developer(str(repo)) == ("nobody@nowhere.example", "git user.email")
    v = guard.gate_command("ls -la", cwd=str(repo))
    assert v is not None and v["decision"] == "deny"
    assert "alice@acme.example" in v["reason"]
    monkeypatch.delenv("FINOPS_GUARD_STOP_ON_BUDGET")
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 400}")
    v = guard.gate_command("ls -la", cwd=str(repo))
    assert v is not None and v["decision"] == "ask"
    assert v["action_type"] == "ai_budget_change"
    assert "alice@acme.example" in v["reason"] and "nobody@nowhere.example" in v["reason"]
    assert guard.gate_command("ls -la", cwd=str(repo)) is None      # asked once


def test_the_identity_ask_keeps_the_budgets_own_reason_beside_it(box, monkeypatch):
    repo = _repo(box)
    who = ["alice@acme.example"]
    monkeypatch.setattr(ab, "_read_git_email", lambda cwd: who[0])
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 4}")
    assert guard.gate_command("ls -la", cwd=str(repo))["action_type"] == "ai_budget"
    who[0] = "nobody@nowhere.example"
    (repo / ".git" / "config").write_text("[user]\n\temail = nobody@nowhere.example\n")
    v = guard.gate_command("ls -la", cwd=str(repo))
    assert v["decision"] == "ask" and v["action_type"] == "ai_budget_change"
    assert "changed from alice@acme.example" in v["reason"]
    assert "over an AI budget" in v["reason"] and "$4.00" in v["reason"]


def test_the_earlier_identity_is_held_only_for_the_month_it_was_seen(box, monkeypatch):
    repo = _repo(box)
    who = ["alice@acme.example"]
    monkeypatch.setattr(ab, "_read_git_email", lambda cwd: who[0])
    assert ab.developer(str(repo))[0] == "alice@acme.example"
    who[0] = "nobody@nowhere.example"
    (repo / ".git" / "config").write_text("[user]\n\temail = nobody@nowhere.example\n")
    assert ab.developer(str(repo))[0] == "nobody@nowhere.example"
    assert ab.earlier_developers(str(repo)) == ["alice@acme.example"]
    monkeypatch.setattr(ab, "_month_key", lambda: "2099-01")
    assert ab.earlier_developers(str(repo)) == []


def test_an_mcp_shell_tool_changing_it_is_asked(box):
    v = guard.gate_mcp_call("mcp__shell__run",
                            {"command": "git config --global user.email carol@acme.example"})
    assert v is not None and v["decision"] == "ask"


def test_the_identity_cache_is_one_of_the_guards_own_files(box):
    from finops import guard_paths
    hit = guard_paths.match_file(str(guard_ledger.ledger_path().with_name("guard-person.json")))
    assert hit is not None
    v = guard.gate_command(f"echo '{{}}' > {guard_ledger.ledger_path().with_name('guard-person.json')}")
    assert v is not None and v["decision"] == "ask"


# ── the doctor says which budgets the guard holds this machine to ─────────────

def test_the_doctor_names_the_budgets_whose_they_are_and_the_figure(box, monkeypatch):
    _budget(box, "team", "payments", "{monthly_usd: 5000, per_person_usd: 400}")
    _person(monkeypatch, "bob@acme.example")
    d = guard._doctor_ai_budgets([], [], lambda *a: None, cwd=str(box["tmp"]))
    assert d["person"] == "bob@acme.example" and d["person_source"] == "FINOPS_GUARD_PERSON"
    assert {(h["scope"], h["subject"]) for h in d["applies"]} == {
        ("per_person", "team:payments"), ("team", "team:payments")}
    assert d["month_usd"] == "4.50" and d["basis"] == "list_price"
    covered, gaps = [], []
    guard._doctor_ai_budgets(covered, gaps, lambda *a: None, cwd=str(box["tmp"]))
    assert any("AI budget" in c and "shell and MCP call" in c for c in covered)


def test_the_doctor_says_when_nobody_is_named(box):
    _budget(box, "person", "alice@acme.example", "{monthly_usd: 4}")
    gaps = []
    d = guard._doctor_ai_budgets([], gaps, lambda *a: None, cwd=str(box["tmp"]))
    assert d["person"] is None and d["applies"] == []
    assert any("FINOPS_GUARD_PERSON" in g for g in gaps)


def test_without_budgets_the_doctor_says_nothing_of_them(box):
    covered, gaps = [], []
    assert guard._doctor_ai_budgets(covered, gaps, lambda *a: None) == {"present": False}
    assert covered == [] and gaps == []
