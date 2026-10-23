"""The authority ladder against the forms a class call is really written in
(review of iteration 9).

A class call the ladder does not recognise is judged as if no authority.yaml
named its class: rung `off` lets it through, and `bounded` never counts it.
What has to hold here:

  - a call inside a loop, a conditional, a subshell or a group is in its
    class, and so are `gh issue new`, `glab issue new` and `gh issue -R X
    create`; a scheduler target or input the guard cannot read is in
    offhours_schedule with its resources unread;
  - the command too long or too odd to split is judged in time: the rules
    for it are linear, so padding never runs the hook past its timeout;
  - a ticket's repository is the one gh files it in: GH_REPO, the remote
    `gh repo set-default` marked, or the one remote; anything else (several
    remotes and no default, a cd or a remote changed in the command) is
    unread, and the line records the repository judged at every rung;
  - max_per_day counts tickets, not lines, and a day the ledger read could
    not hold is not counted, never counted low;
  - a scheduled action that sets a capacity above zero is a capacity change
    the guard asks about, never in the class;
  - a tag or off-hours call made through an MCP server is judged as the
    same command through the shell;
  - the account an off-hours stop runs in is read from AWS_PROFILE too, and
    a stop whose account cannot be pinned down is outside the bound.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest

from finops import guard, guard_authority, guard_ledger
from tests.test_guard_authority_ladder import NOW, _scope, entry, lv
from tests.test_guard_authority_ladder import org as org  # noqa: PLC0414 - a fixture

KIB = 1024


@pytest.fixture(autouse=True)
def _no_ambient_repo_or_profile(monkeypatch, tmp_path):
    """Where gh files a ticket and which account the AWS CLI uses come from
    the environment too: a developer's own must never reach these tests, and
    git's global config is a file of the sandbox's, never the machine's."""
    for var in ("GH_REPO", "GH_HOST", "GITLAB_REPO", "GL_REPO", "GITLAB_HOST", "GL_HOST",
                "REMOTE_ALIAS", "GIT_REMOTE_URL_VAR", "GIT_DIR", "AWS_PROFILE",
                "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID", "FINOPS_GUARD_ACCOUNT",
                "XDG_CONFIG_HOME", "GIT_CONFIG_SYSTEM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global.gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _repo(tmp_path, config: str, name: str = "work"):
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(config)
    return str(repo)


TAGS = "aws ec2 create-tags --resources i-0abc1234 --tags Key=team,Value=pay"


# ── shell forms ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    f"for r in i-0abc1234; do {TAGS}; done",
    "for r in i-0abc1234; do aws ec2 create-tags --resources $r --tags Key=team,Value=pay; done",
    f"if true; then {TAGS}; fi",
    f"if false; then :; else {TAGS}; fi",
    f"if false; then :; elif true; then {TAGS}; fi",
    f"while true; do {TAGS}; break; done",
    f"until false; do {TAGS}; break; done",
    f"({TAGS})",
    f"( {TAGS} )",
    f"(({TAGS}))",
    f"{{ {TAGS}; }}",
    f"! {TAGS}",
    f"time {TAGS}",
    f"if {TAGS}; then echo ok; fi",
    f"f() {{ {TAGS}; }}; f",
    f"case x in x) {TAGS};; esac",
    f"eval '{TAGS}'",
])
def test_a_tag_inside_a_loop_a_conditional_or_a_subshell_is_in_its_class(org, command):
    org(entry("tag", "off"))
    v = lv(command)
    assert v is not None, command
    assert (v["authority_class"], v["decision"]) == ("tag", "deny"), command


@pytest.mark.parametrize("command", [
    "gh issue create -R acme/payments --title t --body b",
    "gh issue new -R acme/payments --title t --body b",
    "glab issue new -R acme/payments --title t --description b",
    "gh issue -R acme/payments create --title t --body b",
    "gh issue --repo acme/payments new --title t --body b",
    "jira -p PAY issue create -tBug -s x",
    "for i in 1 2 3; do gh issue create -R acme/payments --title t$i --body b; done",
])
def test_every_spelling_of_issue_create_is_a_ticket(org, command):
    org(entry("ticket", "off"))
    v = lv(command)
    assert v is not None, command
    assert (v["authority_class"], v["decision"]) == ("ticket", "deny"), command


def test_the_repository_after_the_issue_subcommand_is_read(org):
    org(entry("ticket", "bounded", max_per_day=10, repos=["acme/payments"]))
    v = lv("gh issue -R bigco/secret create --title t --body b")
    assert (v["decision"], v["within_bound"]) == ("ask", False)
    assert v["authority"]["repo"] == "bigco/secret"
    v = lv("gh issue -R acme/payments new --title t --body b")
    assert (v["decision"], v["within_bound"]) == ("allow", True)


@pytest.mark.parametrize("command", [
    ("aws scheduler create-schedule --name n --schedule-expression 'cron(0 20 * * ? *)' "
     "--flexible-time-window Mode=OFF --target file://target.json"),
    ("aws scheduler update-schedule --name n --schedule-expression x "
     "--flexible-time-window Mode=OFF --target https://example.invalid/t.json"),
    "aws scheduler create-schedule --cli-input-json file://schedule.json",
    "aws scheduler create-schedule --cli-input-yaml file://schedule.yaml",
    ("aws scheduler create-schedule --name n --schedule-expression x "
     "--flexible-time-window Mode=OFF --target \"$TARGET\""),
])
def test_a_schedule_whose_target_cannot_be_read_is_offhours_with_resources_unread(org,
                                                                                 command):
    org(entry("offhours_schedule", "off"))
    v = lv(command)
    assert v is not None and v["authority_class"] == "offhours_schedule", command
    assert v["decision"] == "deny"
    org(entry("offhours_schedule", "bounded", environments=["nonprod"]))
    v = lv(command, scope=_scope(("nonprod",)))
    assert (v["decision"], v["within_bound"]) == ("ask", False), command
    assert "could not be counted" in v["reason"]


def test_a_schedule_of_another_target_stays_out_of_the_class(org):
    org(entry("offhours_schedule", "off"))
    assert lv("aws scheduler create-schedule --name x --schedule-expression y "
              "--flexible-time-window Mode=OFF --target "
              "Arn=arn:aws:lambda:us-east-1:1:function:f,RoleArn=r") is None


# ── padding never runs the hook past its timeout ─────────────────────────────

_FILLS = ["aws ", "kubectl ", "gh ", "glab issue ", "issue ", "aws ec2 ", "label ",
          "aws scheduler ", "a" * 7 + " "]
_HINTS = ["issue", "create-tags", "label", "stop-instances", "scheduled-", "-schedule"]


def _timed(fn, *a, **k):
    t = time.perf_counter()
    out = fn(*a, **k)
    return out, time.perf_counter() - t


@pytest.mark.parametrize("fill", _FILLS)
def test_the_ladder_reads_a_padded_command_in_linear_time(fill):
    for hint in _HINTS:
        cmd = f"echo {hint}; " + fill * (256 * KIB // len(fill)) + "; echo done"
        _, took = _timed(guard_authority.classify, cmd)
        assert took < 1.0, f"{fill!r} with {hint!r}: {took:.2f}s for 256 KiB"


def test_a_padded_command_still_finds_the_class_call_in_it():
    cmd = "echo x; " + "aws " * 20_000 + "; aws ec2 create-tags --resources i-1 --tags Key=a,Value=b"
    calls = guard_authority.classify(cmd)
    assert [c.cls for c in calls] == ["tag"]
    assert calls[0].op == "unread"
    cmd = "echo x; " + "gh " * 20_000 + "; gh issue -R acme/x new --title t"
    assert [c.cls for c in guard_authority.classify(cmd)] == ["ticket"]


def test_a_padded_terminate_is_judged_inside_the_hook_budget(org):
    """The blowup the review found: a hint word and 60 KiB of `aws ` in front
    of a termination ran the guard for 34 s, past the hook's timeout, and a
    timed-out hook fails open."""
    org(entry("tag", "off"))
    cmd = ("echo issue; " + "aws " * 16_000
           + "; aws ec2 terminate-instances --instance-ids i-0abc1234")
    v, took = _timed(guard.gate_command, cmd, record=False)
    assert took < 5.0, f"{took:.2f}s"
    assert v is not None and v["decision"] in ("ask", "deny")


# ── where gh files a ticket ──────────────────────────────────────────────────

_FORK = ('[core]\n\trepositoryformatversion = 0\n'
         '[remote "origin"]\n\turl = git@github.com:acme/payments.git\n'
         '[remote "upstream"]\n\turl = https://github.com/bigco/payments.git\n')


def test_a_fork_with_two_remotes_and_no_default_is_unread(org, tmp_path):
    """gh, unable to prompt, files in upstream here: origin is not where the
    ticket goes, so the bound cannot be read from it."""
    org(entry("ticket", "bounded", max_per_day=10, repos=["acme/payments"]))
    v = lv("gh issue create --title t --body b", cwd=_repo(tmp_path, _FORK))
    assert (v["decision"], v["within_bound"]) == ("ask", False)
    assert "repository could not be read" in v["reason"]
    assert "repo" not in v["authority"]


def test_the_remote_gh_repo_set_default_marked_answers(org, tmp_path):
    org(entry("ticket", "bounded", max_per_day=10, repos=["acme/payments"]))
    marked = _FORK.replace("bigco/payments.git\n", "bigco/payments.git\n\tgh-resolved = base\n")
    v = lv("gh issue create --title t --body b", cwd=_repo(tmp_path, marked))
    assert (v["decision"], v["within_bound"]) == ("ask", False)
    assert v["authority"]["repo"] == "bigco/payments"
    marked = _FORK.replace("payments.git\n[remote", "payments.git\n\tgh-resolved = base\n[remote",
                           1)
    v = lv("gh issue create --title t --body b", cwd=_repo(tmp_path, marked, "two"))
    assert (v["decision"], v["within_bound"]) == ("allow", True)
    assert v["authority"]["repo"] == "acme/payments"


@pytest.mark.parametrize("command", [
    "GH_REPO=bigco/secret gh issue create --title t --body b",
    "export GH_REPO=bigco/secret; gh issue create --title t --body b",
    "env GH_REPO=bigco/secret gh issue create --title t --body b",
])
def test_gh_repo_in_the_command_is_where_the_ticket_goes(org, tmp_path, command):
    org(entry("ticket", "bounded", max_per_day=10, repos=["acme/payments"]))
    one = _repo(tmp_path, '[remote "origin"]\n\turl = git@github.com:acme/payments.git\n')
    v = lv(command, cwd=one)
    assert (v["decision"], v["within_bound"]) == ("ask", False), command
    assert v["authority"].get("repo") in (None, "bigco/secret")


def test_gh_repo_in_the_environment_is_where_the_ticket_goes(org, tmp_path, monkeypatch):
    org(entry("ticket", "bounded", max_per_day=10, repos=["acme/payments"]))
    one = _repo(tmp_path, '[remote "origin"]\n\turl = git@github.com:acme/payments.git\n')
    monkeypatch.setenv("GH_REPO", "bigco/secret")
    v = lv("gh issue create --title t --body b", cwd=one)
    assert (v["decision"], v["within_bound"], v["authority"]["repo"]) == \
        ("ask", False, "bigco/secret")
    assert lv("gh issue create -R acme/payments --title t", cwd=one)["within_bound"] is True


@pytest.mark.parametrize("command", [
    "cd ../other && gh issue create --title t --body b",
    "git remote set-url origin https://github.com/bigco/secret && gh issue create --title t",
    "gh repo set-default bigco/secret && gh issue create --title t",
    "env -C ../other gh issue create --title t",
])
def test_a_command_that_moves_where_gh_looks_leaves_the_repository_unread(org, tmp_path,
                                                                           command):
    org(entry("ticket", "bounded", max_per_day=10, repos=["acme/payments"]))
    one = _repo(tmp_path, '[remote "origin"]\n\turl = git@github.com:acme/payments.git\n')
    v = lv(command, cwd=one)
    assert (v["decision"], v["within_bound"]) == ("ask", False), command


def test_a_url_rewrite_is_followed_as_git_follows_it(org, tmp_path):
    org(entry("ticket", "bounded", max_per_day=10, repos=["acme/payments"]))
    (tmp_path / "global.gitconfig").write_text(
        '[url "https://github.com/bigco/"]\n\tinsteadOf = https://github.com/acme/\n')
    one = _repo(tmp_path, '[remote "origin"]\n\turl = https://github.com/acme/payments.git\n')
    v = lv("gh issue create --title t --body b", cwd=one)
    assert (v["within_bound"], v["authority"]["repo"]) == (False, "bigco/payments")


def test_the_repository_judged_is_on_the_line_at_every_rung(org, tmp_path):
    one = _repo(tmp_path, '[remote "origin"]\n\turl = git@github.com:Acme/Payments.git\n')
    for rung in ("approve_each", "off"):
        org(entry("ticket", rung))
        v = lv("gh issue create --title t --body b", cwd=one)
        assert v["authority"]["repo"] == "acme/payments", rung
    org()
    v = lv("gh issue create --title t --body b", cwd=one)
    assert (v["rung"], v["authority"]["repo"]) == ("none", "acme/payments")


# ── max_per_day counts tickets ───────────────────────────────────────────────

def _record(command, cwd=None):
    v = guard.gate_command(command, record=True, cwd=cwd)
    return "allow" if v is None else v["decision"]


TICKET = "gh issue create -R acme/payments --title {} --body b"


def test_two_tickets_in_one_command_count_as_two(org):
    org(entry("ticket", "bounded", repos=["acme/payments"], max_per_day=2))
    two = f"{TICKET.format('a')} && {TICKET.format('c')}"
    assert _record(two) == "allow"
    assert guard_authority._tickets_today(NOW) == 2
    assert _record(TICKET.format("e")) == "ask"


def test_a_command_of_more_tickets_than_the_bound_asks(org):
    org(entry("ticket", "bounded", repos=["acme/payments"], max_per_day=2))
    three = " && ".join(TICKET.format(t) for t in "abc")
    assert _record(three) == "ask"


def test_a_day_the_ledger_read_cannot_hold_is_not_counted_never_low(org):
    org(entry("ticket", "bounded", repos=["acme/payments"], max_per_day=5))
    path = guard_ledger.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    morning, later = NOW.replace(hour=1), NOW.replace(hour=2)
    with open(path, "w") as fh:
        fh.writelines(json.dumps({"ts": (morning + timedelta(minutes=i)).isoformat(
            timespec="seconds"), "decision": "allow", "authority_class": "ticket",
            "rung": "bounded", "tool": "Bash", "command": "gh issue create -R acme/payments"})
            + "\n" for i in range(5))
        fh.writelines(json.dumps({"ts": (later + timedelta(seconds=i)).isoformat(
            timespec="seconds"), "decision": "allow", "action_type": "infra_apply",
            "tool": "Bash", "command": "kubectl apply -f x.yaml", "note": "x" * 900})
            + "\n" for i in range(5000))
    assert guard_authority._tickets_today(NOW) is None
    v = lv(TICKET.format("f"))
    assert (v["decision"], v["within_bound"]) == ("ask", False)
    assert v["authority"]["tickets_today"] is None


def test_recent_says_when_its_window_did_not_fit(tmp_path):
    path = tmp_path / "ledger.jsonl"
    with open(path, "w") as fh:
        fh.writelines(json.dumps({"ts": (NOW - timedelta(minutes=50 - i)).isoformat(),
                                  "decision": "allow", "pad": "y" * 100}) + "\n"
                      for i in range(50))
    stats: dict = {}
    got = guard_ledger.recent(120, path=path, now=NOW, max_bytes=1000, stats=stats)
    assert stats == {"complete": False} and 0 < len(got) < 50
    stats = {}
    got = guard_ledger.recent(120, path=path, now=NOW, stats=stats)
    assert stats == {"complete": True} and len(got) == 50
    stats = {}
    got = guard_ledger.recent(10, path=path, now=NOW, stats=stats)
    assert stats == {"complete": True} and len(got) == 10


# ── a scheduled scale-up moves money ─────────────────────────────────────────

_SCHEDULED_UP = ("aws autoscaling put-scheduled-update-group-action --auto-scaling-group-name "
                 "web --scheduled-action-name burst --start-time 2026-10-23T00:00:00Z "
                 "--min-size 500 --max-size 500 --desired-capacity 500")


@pytest.mark.parametrize("command", [
    _SCHEDULED_UP,
    ("aws autoscaling put-scheduled-update-group-action --auto-scaling-group-name web "
     "--scheduled-action-name burst --recurrence '0 8 * * *' --desired-capacity 20"),
    ("aws autoscaling put-scheduled-update-group-action --auto-scaling-group-name web "
     "--scheduled-action-name burst --recurrence '0 8 * * *' --min-size=3"),
    ("aws autoscaling put-scheduled-update-group-action --auto-scaling-group-name web "
     "--scheduled-action-name burst --recurrence '0 8 * * *' --desired-capacity $N"),
    ("aws autoscaling batch-put-scheduled-update-group-action --auto-scaling-group-name web "
     "--scheduled-update-group-actions ScheduledActionName=up,Recurrence='0 8 * * *',"
     "DesiredCapacity=40"),
    ("aws autoscaling batch-put-scheduled-update-group-action --auto-scaling-group-name web "
     "--scheduled-update-group-actions '[{\"ScheduledActionName\": \"up\", "
     "\"MinSize\": 2}]'"),
    ("aws autoscaling batch-put-scheduled-update-group-action --auto-scaling-group-name web "
     "--scheduled-update-group-actions file://actions.json"),
    "aws autoscaling put-scheduled-update-group-action --cli-input-json file://up.json",
    ("aws --profile dev autoscaling put-scheduled-update-group-action "
     "--auto-scaling-group-name web --scheduled-action-name b --max-size 50"),
])
def test_a_scheduled_capacity_above_zero_is_a_capacity_change(org, monkeypatch, command):
    """Judged as the same change made now (update-auto-scaling-group): a
    capacity change, in no class, so bounded never lets it through and
    strict mode asks about it as it does about the immediate form."""
    org(entry("offhours_schedule", "bounded", environments=["nonprod"],
              max_resources_per_call=1))
    assert guard.classify_command(command) == ("two_way", "infra_apply"), command
    assert lv(command, scope=_scope(("nonprod",))) is None, command
    now = ("aws autoscaling update-auto-scaling-group --auto-scaling-group-name web "
           "--desired-capacity 500")
    assert guard.classify_command(now) == ("two_way", "infra_apply")
    monkeypatch.setenv("FINOPS_GUARD_STRICT", "1")
    v = guard.gate_command(command, record=False)
    assert v is not None and v["decision"] == "ask", command
    assert v["action_type"] == guard.gate_command(now, record=False)["action_type"]
    assert "authority_class" not in v


@pytest.mark.parametrize("command", [
    ("aws autoscaling put-scheduled-update-group-action --auto-scaling-group-name web "
     "--scheduled-action-name night --recurrence '0 20 * * *' --desired-capacity 0"),
    ("aws autoscaling put-scheduled-update-group-action --auto-scaling-group-name web "
     "--scheduled-action-name night --recurrence '0 20 * * *' --min-size 0 --max-size 0 "
     "--desired-capacity 0"),
    ("aws autoscaling batch-put-scheduled-update-group-action --auto-scaling-group-name web "
     "--scheduled-update-group-actions ScheduledActionName=night,Recurrence='0 20 * * *',"
     "MinSize=0,MaxSize=0,DesiredCapacity=0"),
])
def test_a_scale_to_zero_schedule_stays_in_the_class(org, command):
    org(entry("offhours_schedule", "bounded", environments=["nonprod"]))
    assert guard.classify_command(command) is None, command
    v = lv(command, scope=_scope(("nonprod",)))
    assert (v["authority_class"], v["within_bound"]) == ("offhours_schedule", True), command


# ── through an MCP server ────────────────────────────────────────────────────

@pytest.mark.parametrize("tool,args", [
    ("mcp__aws__call_aws", {"cli_command": TAGS}),
    ("mcp__awslabs_aws-api-mcp-server__call_aws", {"cli_command": TAGS}),
    ("mcp__shell__run_command", {"command": TAGS}),
    ("mcp__aws__call_aws", {"cli_command": "aws ec2 stop-instances --instance-ids i-0abc1234"}),
])
def test_a_class_call_through_an_mcp_server_is_judged_as_through_the_shell(org, tool, args):
    org(entry("tag", "off"), entry("offhours_schedule", "off"))
    v = guard.gate_mcp_call(tool, args, record=False)
    assert v is not None, tool
    assert v["decision"] == "deny" and v["authority_class"] in ("tag", "offhours_schedule")


def test_a_money_call_through_an_mcp_server_is_never_in_a_class(org):
    org(entry("tag", "bounded", tag_keys=["team"]))
    shell = f"{TAGS} && aws ec2 terminate-instances --instance-ids i-1"
    assert lv(shell, mcp=("mcp__shell__run_command", {"command": shell})) is None


def test_an_mcp_tool_that_creates_an_issue_still_answers_as_a_ticket(org):
    org(entry("ticket", "off"), entry("tag", "bounded", tag_keys=["team"]))
    v = lv("", mcp=("mcp__github__create_issue", {"owner": "acme", "repo": "payments"}))
    assert (v["authority_class"], v["decision"]) == ("ticket", "deny")


# ── the account an off-hours stop runs in ────────────────────────────────────

def _env_fact(account, env):
    return (f"- fact: environment\n  subject: {{kind: aws_account, id: '{account}'}}\n"
            f"  value: {{env: {env}}}\n  source: human\n  status: confirmed\n"
            "  confirmed_at: 2026-10-01\n")


@pytest.fixture
def accounts(org, tmp_path, monkeypatch):
    """offhours_schedule bounded to nonprod; profile payments-live is account
    111111111111 (prod), profile sandbox is 222222222222 (nonprod)."""
    path = org(entry("offhours_schedule", "bounded", environments=["nonprod"]))
    (path.parent / "environments.yaml").write_text(
        "# nable org model v1\n" + _env_fact("111111111111", "prod")
        + _env_fact("222222222222", "nonprod"))
    legacy = tmp_path / "legacy-accounts.yaml"
    legacy.write_text("accounts:\n  - name: live\n    account_id: '111111111111'\n"
                      "    profile: payments-live\n  - name: sb\n"
                      "    account_id: '222222222222'\n    profile: sandbox\n")
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(legacy))
    return path


STOP = "aws ec2 stop-instances --instance-ids i-0abc1234"


@pytest.mark.parametrize("command,within", [
    (f"{STOP} --profile sandbox", True),
    (f"{STOP} --profile payments-live", False),
    (f"AWS_PROFILE=payments-live {STOP}", False),
    (f"AWS_DEFAULT_PROFILE=payments-live {STOP}", False),
    (f"export AWS_PROFILE=payments-live; {STOP}", False),
    (f"AWS_PROFILE=sandbox {STOP}", True),
    (STOP, False),
    (f"{STOP} --profile nobody-knows", False),
])
def test_the_account_an_offhours_call_runs_in_is_pinned_down_or_asked(accounts, command,
                                                                      within):
    v = guard.gate_command(command, record=False)
    if within:
        assert v is None, (command, v and v.get("reason"))
        lad = lv(command, scope=guard._ladder_scope(command, None, command))
        assert (lad["decision"], lad["within_bound"]) == ("allow", True)
    else:
        assert v is not None and v["decision"] == "ask", command
        assert v.get("within_bound") is False, command


def test_the_profile_in_the_environment_is_the_account(accounts, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "payments-live")
    assert guard.gate_command(STOP, record=False)["decision"] == "ask"
    monkeypatch.setenv("AWS_PROFILE", "sandbox")
    assert guard.gate_command(STOP, record=False) is None
    assert guard.gate_command(f"{STOP} --profile payments-live", record=False)["decision"] \
        == "ask"
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "not-a-key")
    assert guard.gate_command(STOP, record=False)["decision"] == "ask"


def test_a_profile_in_the_environment_variable_is_not_hidden_behind_a_nonprod_scope(
        accounts, monkeypatch):
    """The review's case: the call's other subjects resolve to nonprod (here
    FINOPS_GUARD_ACCOUNT names the sandbox account), and AWS_PROFILE sends
    the stop to production. Read only from --profile, prod stayed hidden."""
    monkeypatch.setenv("FINOPS_GUARD_ACCOUNT", "222222222222")
    assert guard.gate_command(f"{STOP} --profile sandbox", record=False) is None
    v = guard.gate_command(f"AWS_PROFILE=payments-live {STOP}", record=False)
    assert v is not None and (v["decision"], v["within_bound"]) == ("ask", False)
    assert "prod" in v["reason"]


def test_a_padded_git_config_is_read_in_linear_time(org, tmp_path):
    """The repository's config is read on the hook path for a ticket: a long
    line or a run of blank lines in it never runs the hook past its budget."""
    org(entry("ticket", "bounded", max_per_day=10, repos=["acme/payments"]))
    pad = ('[remote "origin"]\n\turl = git@github.com:acme/payments.git\n'
           + "\n" * 100_000 + "\tx = " + " " * 100_000 + "y\n")
    v, took = _timed(lv, "gh issue create --title t", cwd=_repo(tmp_path, pad))
    assert took < 2.0, f"{took:.2f}s"
    assert v["authority"]["repo"] == "acme/payments"
