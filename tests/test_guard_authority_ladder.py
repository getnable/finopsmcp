"""The authority ladder in the guard: guard_authority.ladder_verdict (seam S1,
D172 to D176).

ladder_verdict classifies one call into a class that moves no money (tag,
ticket, offhours_schedule), reads authority.yaml's rung and bound for it and
the workspace's standing, and answers allow, ask or deny. The guard merges
that with its own verdict, the more severe answering (_merged below, exactly
as S1 says), so the ladder never removes an ask the guard raises without the
file. What has to hold:

  - with no authority.yaml a class call answers rung `none` and adds no ask;
  - approve_each asks naming the approvers; off and recommend deny with a
    reason a person can act on; bounded adds no ask within its bound and
    asks outside it; a removal is never within a bound;
  - scope() is called only for an offhours_schedule call at bounded;
  - ticket's max_per_day counts this machine's ledger by UTC day;
  - the box only narrows: a demotion asks where the file says bounded, a
    block saying bounded where the file says approve_each still asks, and a
    standing that cannot be read never waits;
  - a call in no class is None, fast; a call any money-moving, one-way or
    priced rule matches is never in a class; nothing raises.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from finops import authority_standing, cloud_standing, guard, guard_authority, guard_ledger
from finops.org import model

NOW = datetime(2026, 10, 22, 15, 30, tzinfo=UTC)
APPROVER = "email:ana@acme.example"
BOX = "https://box.nable.example"
WS = "ws_" + "a" * 32
TAG = "aws ec2 create-tags --resources i-0abc1234 --tags Key=team,Value=pay"
STOP = "aws ec2 stop-instances --instance-ids i-0abc1234"
TICKET = "gh issue create --title 'Disk is full' --body x -R acme/payments"


class _Fixed(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture
def org(tmp_path, monkeypatch):
    """write(*entries): authority.yaml in FINOPS_ORG_DIR, the guard's org
    model; time is NOW for the ladder and the ledger alike."""
    d = tmp_path / "org"
    d.mkdir()
    monkeypatch.setenv("FINOPS_ORG_DIR", str(d))
    monkeypatch.setattr(guard_authority, "_now", lambda: NOW)
    monkeypatch.setattr(guard_ledger, "datetime", _Fixed)

    def write(*entries):
        (d / "authority.yaml").write_text("# nable org model v1\n" + "".join(entries))
        return d / "authority.yaml"
    return write


def entry(cls, rung="approve_each", *, status="confirmed", **bound):
    value = {"rung": rung}
    if rung in ("approve_each", "bounded"):
        value.update(approvers=[APPROVER], expires="2027-06-30")
    value.update(bound)
    when = "confirmed_at" if status == "confirmed" else "proposed_at"
    return (f"- fact: authority\n  subject: {{kind: action_class, id: {cls}}}\n"
            f"  value: {json.dumps(value)}\n  source: human\n  status: {status}\n"
            f"  {when}: 2026-10-01\n")


def _never():
    raise AssertionError("scope() was called")


def lv(command, *, scope=_never, mcp=None, cwd=None, harness="claude-code"):
    return guard_authority.ladder_verdict(command, cwd=cwd, harness=harness, scope=scope,
                                          mcp=mcp)


_SEVERITY = {"allow": 0, "warn": 1, "ask": 2, "deny": 3}


def _merged(guard_v, ladder_v):
    """S1's merge, as the guard makes it: the more severe answers, and the
    class, rung and bound travel with the line whichever answered."""
    if ladder_v is None:
        return guard_v
    keep = {k: ladder_v[k] for k in ("authority_class", "rung", "within_bound")}
    if ladder_v.get("authority"):
        keep["authority"] = ladder_v["authority"]
    if guard_v is None or _SEVERITY[ladder_v["decision"]] > _SEVERITY[guard_v["decision"]]:
        return {**ladder_v, **keep}
    return {**guard_v, **keep}


def run(command, **kw):
    """The ladder merged with the guard's own verdict, and the line recorded
    as the guard records it: what the next call's ticket count reads."""
    lad = lv(command, **kw)
    v = _merged(guard.gate_command(command, record=False, cwd=kw.get("cwd")), lad)
    if v is not None:
        guard_ledger.append({"harness": "claude-code", "tool": "Bash",
                             "command": guard_ledger.redact(command),
                             "action_type": v.get("action_type"), "decision": v["decision"],
                             **({"authority_class": v["authority_class"], "rung": v["rung"]}
                                if v.get("authority_class") else {})})
    return v


# ── the answer's shape ───────────────────────────────────────────────────────

def test_with_no_authority_yaml_a_tag_call_is_rung_none_and_adds_no_ask(org):
    v = lv(TAG)
    assert v == {"decision": "allow", "action_type": "authority_class",
                 "authority_class": "tag", "rung": "none", "within_bound": None,
                 "authority": v["authority"]}
    assert v["authority"]["op"] == "add" and v["authority"]["keys"] == ["team"]
    assert _merged(guard.gate_command(TAG, record=False), v)["decision"] == "allow"


def test_a_call_in_no_class_is_none(org):
    for command in ("ls -la", "git status", "aws s3 ls", "terraform plan", "gh pr create",
                    "kubectl get pods", "echo create-tags", ("aws iam tag-role --role-name r "
                    "--tags Key=team,Value=x")):
        assert lv(command) is None, command


@pytest.mark.parametrize("service", ["iam", "kms", "organizations", "sso-admin"])
def test_tagging_that_can_grant_access_stays_out_of_the_class(org, service):
    org(entry("tag", "off"))
    assert lv(f"aws {service} tag-resource --resource-arn arn:x --tags Key=team,Value=x") \
        is None


# ── tag ──────────────────────────────────────────────────────────────────────

def test_tag_at_approve_each_asks_naming_the_approvers(org):
    org(entry("tag"))
    v = lv(TAG)
    assert (v["decision"], v["rung"], v["within_bound"]) == ("ask", "approve_each", None)
    assert v["reason"].startswith("nable guard: ") and "ana@acme.example" in v["reason"]


def test_tag_at_bounded_lets_a_call_within_its_bound_through(org):
    org(entry("tag", "bounded", tag_keys=["team"]))
    v = lv(TAG)
    assert (v["decision"], v["rung"], v["within_bound"]) == ("allow", "bounded", True)
    assert "reason" not in v


def test_tag_at_bounded_asks_for_a_key_the_bound_does_not_name(org):
    org(entry("tag", "bounded", tag_keys=["team"]))
    v = lv("aws ec2 create-tags --resources i-0abc1234 --tags Key=Owner,Value=ana")
    assert (v["decision"], v["within_bound"]) == ("ask", False)
    assert "Owner" in v["reason"]


@pytest.mark.parametrize("command", [
    "aws ec2 delete-tags --resources i-0abc1234 --tags Key=team",
    ("aws resourcegroupstaggingapi untag-resources --resource-arn-list arn:aws:s3:::b "
    "--tag-keys team"),
    ("aws rds remove-tags-from-resource --resource-name arn:aws:rds:us-east-1:1:db:x "
    "--tag-keys team"),
    "aws lambda untag-resource --resource arn:x --tag-keys team",
    "kubectl label pods web-1 team-",
])
def test_a_removal_is_in_the_class_and_never_within_a_bound(org, command):
    org(entry("tag", "bounded", tag_keys=["team"]))
    v = lv(command)
    assert (v["authority_class"], v["decision"], v["within_bound"]) == ("tag", "ask", False)
    assert v["authority"]["op"] == "remove"
    assert "never within a bound" in v["reason"]


def test_put_bucket_tagging_replaces_the_set_and_is_never_within_a_bound(org):
    org(entry("tag", "bounded", tag_keys=["team"]))
    v = lv("aws s3api put-bucket-tagging --bucket b --tagging 'TagSet=[{Key=team,Value=x}]'")
    assert (v["decision"], v["within_bound"]) == ("ask", False)


def test_tag_at_off_denies_with_a_reason_a_person_can_act_on(org):
    org(entry("tag", "off"))
    v = lv(TAG)
    assert (v["decision"], v["rung"]) == ("deny", "off")
    assert "nable org set authority --class tag" in v["reason"]
    org(entry("tag", "recommend"))
    v = lv(TAG)
    assert (v["decision"], v["rung"]) == ("deny", "recommend")


@pytest.mark.parametrize("command,within", [
    ("aws ec2 create-tags --resources i-1 i-2 i-3 --tags Key=team,Value=x", True),
    ("aws ec2 create-tags --resources i-1 i-2 i-3 i-4 --tags Key=team,Value=x", False),
    ("aws ec2 create-tags --resources $(cat ids) --tags Key=team,Value=x", False),
    ("cat ids | xargs aws ec2 create-tags --tags Key=team,Value=x --resources", False),
    ("aws ec2 create-tags --resources i-1 --tags '[{\"Key\": \"team\", \"Value\": \"x\"}]'",
     True),
    ("aws ec2 create-tags --resources i-1 --tags file://tags.json", False),
    (("aws resourcegroupstaggingapi tag-resources --resource-arn-list arn:a arn:b "
     "--tags team=x"), True),
    ("aws lambda tag-resource --resource arn:x --tags team=x", True),
    ("aws sns tag-resource --resource-arn arn:x --tags Key=team,Value=x", True),
    (("aws rds add-tags-to-resource --resource-name arn:x --tags Key=team,Value=x "
     "Key=env,Value=y"), False),
    ("kubectl label pods web-1 web-2 team=pay", True),
    ("kubectl label pods -l app=web team=pay", False),
    ("kubectl label pods --all team=pay", False),
    ("kubectl -n prod label deploy/web team=pay --overwrite", True),
    (("aws --profile dev ec2 create-tags --region us-east-1 --resources i-1 "
     "--tags Key=team,Value=x"), True),
    ("AWS_PROFILE=dev sudo -E aws ec2 create-tags --resources i-1 --tags Key=team,Value=x",
     True),
    ("bash -c 'aws ec2 create-tags --resources i-1 --tags Key=team,Value=x'", True),
])
def test_the_bound_counts_resources_and_reads_keys(org, command, within):
    org(entry("tag", "bounded", tag_keys=["team"], max_resources_per_call=3))
    v = lv(command)
    assert v is not None and v["authority_class"] == "tag", command
    assert v["within_bound"] is within, (command, v.get("reason"))
    assert v["decision"] == ("allow" if within else "ask")


# ── offhours_schedule ────────────────────────────────────────────────────────

def _scope(envs, prod=None, calls=None):
    def scope():
        if calls is not None:
            calls.append(1)
        return envs, prod
    return scope


@pytest.mark.parametrize("envs,prod,within", [
    (("nonprod",), None, True),
    (("nonprod", "sandbox"), None, True),
    (("nonprod", "prod"), None, False),
    (("unknown",), None, False),
    (None, None, False),
    ((), None, False),
    (("nonprod",), "--profile prod", False),
])
def test_offhours_at_bounded_asks_unless_every_environment_is_in_its_bound(org, envs, prod,
                                                                           within):
    org(entry("offhours_schedule", "bounded", environments=["nonprod", "sandbox"]))
    calls = []
    v = lv(STOP, scope=_scope(envs, prod, calls))
    assert calls == [1]
    assert (v["within_bound"], v["decision"]) == (within, "allow" if within else "ask")


def test_scope_is_called_only_for_offhours_at_bounded(org):
    org(entry("tag", "bounded", tag_keys=["team"]), entry("ticket", "bounded", max_per_day=3),
        entry("offhours_schedule", "approve_each"))
    assert lv(TAG)["decision"] == "allow"
    assert lv(TICKET)["decision"] == "allow"
    assert lv(STOP)["decision"] == "ask"
    assert lv("ls") is None


def test_a_scope_that_raises_is_outside_the_bound_never_a_crash(org):
    org(entry("offhours_schedule", "bounded", environments=["nonprod"]))

    def broken():
        raise RuntimeError("org model")
    v = lv(STOP, scope=broken)
    assert (v["decision"], v["within_bound"]) == ("ask", False)


@pytest.mark.parametrize("command,cls_in,within", [
    ("aws ec2 start-instances --instance-ids i-1 i-2", True, True),
    ("aws rds stop-db-instance --db-instance-identifier staging-db", True, True),
    ("aws rds start-db-cluster --db-cluster-identifier staging", True, True),
    (("aws autoscaling put-scheduled-update-group-action --auto-scaling-group-name web "
     "--scheduled-action-name night --recurrence '0 20 * * *' --desired-capacity 0"), True, True),
    (("aws scheduler create-schedule --name night --schedule-expression 'cron(0 20 * * ? *)' "
     "--flexible-time-window Mode=OFF --target '{\"Arn\": "
     "\"arn:aws:scheduler:::aws-sdk:ec2:stopInstances\", \"RoleArn\": \"arn:r\", "
     "\"Input\": \"{\\\"InstanceIds\\\": [\\\"i-1\\\"]}\"}'"), True, True),
    (("aws scheduler update-schedule --name night --state DISABLED --schedule-expression x "
     "--flexible-time-window Mode=OFF --target Arn=arn:aws:scheduler:::aws-sdk:ec2:"
     "stopInstances,RoleArn=arn:r"), True, False),
    (("aws autoscaling delete-scheduled-action --auto-scaling-group-name web "
     "--scheduled-action-name night"), True, False),
    (("aws autoscaling batch-delete-scheduled-action --auto-scaling-group-name web "
     "--scheduled-action-names night morning"), True, False),
    ("aws scheduler delete-schedule --name night", True, False),
    (("aws scheduler create-schedule --name x --schedule-expression y --flexible-time-window "
     "Mode=OFF --target Arn=arn:aws:lambda:us-east-1:1:function:f,RoleArn=r"), False, None),
    ("aws ec2 stop-instances --instance-ids " + " ".join(f"i-{n}" for n in range(11)),
     True, False),
])
def test_offhours_forms(org, command, cls_in, within):
    org(entry("offhours_schedule", "bounded", environments=["nonprod"]))
    v = lv(command, scope=_scope(("nonprod",)))
    if not cls_in:
        assert v is None
        return
    assert v["authority_class"] == "offhours_schedule", command
    assert v["within_bound"] is within, (command, v.get("reason"))


# ── ticket ───────────────────────────────────────────────────────────────────

def test_the_fourth_ticket_of_the_utc_day_asks_at_max_per_day_3(org):
    org(entry("ticket", "bounded", max_per_day=3))
    got = [run(TICKET)["decision"] for _ in range(4)]
    assert got == ["allow", "allow", "allow", "ask"]
    assert "4" in run(TICKET)["reason"] or "today" in run(TICKET)["reason"]


def test_tickets_from_yesterday_do_not_count(org, monkeypatch):
    org(entry("ticket", "bounded", max_per_day=1))
    monkeypatch.setattr(guard_ledger, "datetime", type("_Y", (datetime,), {
        "now": classmethod(lambda cls, tz=None: datetime(2026, 10, 21, 23, 50, tzinfo=UTC))}))
    run(TICKET)
    monkeypatch.setattr(guard_ledger, "datetime", _Fixed)
    assert run(TICKET)["decision"] == "allow"


def test_an_unreadable_ledger_counts_no_ticket_as_zero(org, monkeypatch):
    org(entry("ticket", "bounded", max_per_day=3))

    def broken(*a, **k):
        raise OSError("locked")
    monkeypatch.setattr(guard_ledger, "recent", broken)
    v = lv(TICKET)
    assert (v["decision"], v["within_bound"]) == ("ask", False)
    assert "could not be counted" in v["reason"]


@pytest.mark.parametrize("command,within", [
    ("gh issue create -R Acme/Payments --title x", True),
    ("gh issue create --repo=acme/payments --title x", True),
    ("gh issue create --repo acme/infra --title x", False),
    ("glab issue create -R acme/payments --title x", True),
    ("jira issue create -pPAY -tBug -s x", False),
])
def test_a_ticket_bound_with_repos_reads_the_repo(org, command, within):
    org(entry("ticket", "bounded", max_per_day=10, repos=["acme/payments"]))
    v = lv(command)
    assert v["authority_class"] == "ticket"
    assert v["within_bound"] is within, (command, v.get("reason"))


def test_a_ticket_repo_is_read_from_the_working_directory_remote(org, tmp_path):
    org(entry("ticket", "bounded", max_per_day=10, repos=["acme/payments"]))
    repo = tmp_path / "work"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:Acme/Payments.git\n')
    assert lv("gh issue create --title x", cwd=str(repo))["within_bound"] is True
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/other.git\n')
    assert lv("gh issue create --title x", cwd=str(repo))["within_bound"] is False


@pytest.mark.parametrize("tool,args", [
    ("mcp__github__create_issue", {"owner": "acme", "repo": "payments", "title": "x"}),
    ("mcp__atlassian__createJiraIssue", {"projectKey": "PAY", "summary": "x"}),
    ("mcp__github__issue_write", {"method": "create", "owner": "acme", "repo": "payments"}),
    ("create_ticket", {"title": "x"}),
])
def test_an_mcp_create_issue_at_approve_each_asks(org, tool, args):
    org(entry("ticket"))
    v = lv("mcp call", mcp=(tool, args))
    assert (v["authority_class"], v["decision"]) == ("ticket", "ask")


@pytest.mark.parametrize("tool,args", [
    ("mcp__github__issue_write", {"method": "update", "issue_number": 1}),
    ("mcp__github__get_issue", {"issue_number": 1}),
    ("mcp__github__create_pull_request", {"title": "x"}),
])
def test_an_mcp_call_that_creates_no_issue_is_in_no_class(org, tool, args):
    org(entry("ticket", "off"))
    assert lv("mcp call", mcp=(tool, args)) is None


# ── more than one class, and the guard's own asks ───────────────────────────

def test_a_command_with_two_classes_answers_with_the_stricter(org):
    org(entry("tag", "bounded", tag_keys=["team"]), entry("ticket", "off"))
    v = lv(f"{TAG} && {TICKET}")
    assert (v["authority_class"], v["decision"]) == ("ticket", "deny")
    org(entry("tag", "bounded", tag_keys=["team"]), entry("ticket", "bounded", max_per_day=5))
    v = lv(f"{TAG} && {TICKET}")
    assert (v["decision"], v["within_bound"]) == ("ask", False)


def test_bounded_never_removes_an_ask_the_guard_raises_without_the_file(org, tmp_path):
    org(entry("tag", "bounded", tag_keys=["team"]))
    off = guard_ledger.ledger_path().parent / "guard-ledger.jsonl"
    command = f"{TAG} ; rm -f {off}"
    lad = lv(command)
    assert lad["decision"] == "allow"
    assert _merged(guard.gate_command(command, record=False), lad)["decision"] == "ask"


# ── never money ──────────────────────────────────────────────────────────────

def _purchase_forms():
    out = []
    for p in guard.COMMITMENT_PURCHASES:
        form = f"aws {p.service} {p.verb} --x 1"
        out.append(form + (f" {p.when} 31536000" if p.when else ""))
    return out


_ONE_WAY = ["terraform destroy -auto-approve", "tofu destroy", "terragrunt run-all destroy",
            "terraform apply -destroy", "terraform workspace delete old", "pulumi destroy",
            "cdk destroy", "sam delete", "doctl compute droplet delete web", ("eksctl delete "
            "cluster x"), "aws s3 rb s3://b --force", "aws s3 rm s3://b --recursive",
            "aws s3 sync . s3://b --delete", "helm uninstall web", "kubectl delete pod web",
            "kubectl drain node-1", "aws ec2 terminate-instances --instance-ids i-1",
            "aws ec2 release-address --allocation-id a", ("aws ec2 delete-snapshot "
            "--snapshot-id s"), "aws kms schedule-key-deletion --key-id k",
            "aws ec2 deregister-image --image-id ami-1", ("aws dynamodb delete-table "
            "--table-name t"), "aws s3api delete-bucket-tagging --bucket b",
            "gcloud compute instances delete web", "az vm delete -n web"]
_PRICED = ["aws ec2 run-instances --instance-type m5.large --count 2",
           ("aws rds create-db-instance --db-instance-class db.m5.large --engine mysql "
           "--db-instance-identifier x"), ("aws elasticache create-cache-cluster "
           "--cache-node-type cache.m5.large --num-cache-nodes 1 --cache-cluster-id x"),
           ("aws autoscaling set-desired-capacity --auto-scaling-group-name web "
           "--desired-capacity 50"), "kubectl scale deploy/web --replicas 40",
           "terraform apply -auto-approve", ("aws ec2 modify-instance-attribute --instance-id "
           "i-1 --instance-type m5.24xlarge")]


@pytest.mark.parametrize("money", _purchase_forms() + _ONE_WAY + _PRICED)
def test_nothing_money_moving_one_way_or_priced_is_ever_in_a_class(org, money):
    """The ratchet: whatever the file says, a command any purchase, one-way or
    priced rule matches is never classified, alone or beside a class call,
    so bounded can never let it through."""
    org(entry("tag", "bounded", tag_keys=["team"]),
        entry("offhours_schedule", "bounded", environments=["nonprod"]),
        entry("ticket", "bounded", max_per_day=50))
    assert lv(money, scope=_scope(("nonprod",))) is None
    for cls_call in (TAG, STOP, TICKET):
        assert lv(f"{cls_call} && {money}", scope=_scope(("nonprod",))) is None, money
        assert lv(f"{money} ; {cls_call}", scope=_scope(("nonprod",))) is None, money


def test_no_purchase_kind_is_a_class_and_none_can_reach_bounded():
    assert set(guard_authority.CLASSES) == set(model.NO_MONEY_CLASSES)
    assert not set(guard_authority.CLASSES) & set(model.AUTHORITY_CLASSES)
    assert "bounded" not in model.RUNGS


def test_removals_the_guard_judges_one_way_still_ask_there(org):
    """A removal is in its class and the guard's own one-way ask stands: at
    rung none the ladder adds nothing, and the merge keeps the guard's ask."""
    for command in ("aws ec2 delete-tags --resources i-1 --tags Key=team",
                    ("aws autoscaling delete-scheduled-action --auto-scaling-group-name web "
                    "--scheduled-action-name night"), "aws scheduler delete-schedule --name n"):
        lad = lv(command)
        assert lad["rung"] == "none"
        assert _merged(guard.gate_command(command, record=False), lad)["decision"] == "ask"


# ── the box only narrows ─────────────────────────────────────────────────────

def _standing(**classes):
    cloud_standing.bind(BOX, workspace=WS, at=NOW)
    block = {"v": 1, "read": True, "workspace_ref": WS, "as_of": "2026-10-22T11:00:00+00:00",
             "classes": {c: {"rung_in_force": None, "demoted": False, "since": None,
                             "line": None, **classes.get(c, {})}
                         for c in ("tag", "ticket", "offhours_schedule")}}
    got = authority_standing.keep(block, box=BOX, workspace_ref=WS, fetched_at=NOW)
    assert got["kept"], got


def test_a_demotion_on_the_box_asks_even_where_the_file_says_bounded(org):
    org(entry("tag", "bounded", tag_keys=["team"]))
    _standing(tag={"demoted": True, "since": "2026-10-20", "line": "d" * 64,
                   "rung_in_force": "bounded"})
    v = lv(TAG)
    assert (v["decision"], v["rung"]) == ("ask", "approve_each")
    assert "2026-10-20" in v["reason"] and "lift" in v["reason"]
    assert v["authority"]["file_rung"] == "bounded"


def test_a_block_claiming_bounded_where_the_file_says_approve_each_still_asks(org):
    org(entry("tag"))
    _standing(tag={"rung_in_force": "bounded"})
    v = lv(TAG)
    assert (v["decision"], v["rung"]) == ("ask", "approve_each")


def test_a_lower_rung_in_force_on_the_box_answers(org):
    org(entry("tag", "bounded", tag_keys=["team"]))
    _standing(tag={"rung_in_force": "off"})
    assert lv(TAG)["decision"] == "deny"


def test_a_standing_that_is_not_read_holds_bounded_at_approve_each(org):
    org(entry("tag", "bounded", tag_keys=["team"]), entry("ticket", "off"))
    cloud_standing.bind(BOX, workspace=WS, at=NOW)
    authority_standing.keep({"v": 1, "read": False, "reason": "the ladder pass failed",
                             "workspace_ref": WS, "as_of": "2026-10-22T11:00:00+00:00"},
                            box=BOX, workspace_ref=WS, fetched_at=NOW)
    assert lv(TAG)["decision"] == "ask"
    assert lv(TICKET)["decision"] == "deny"


def test_a_fifo_in_place_of_the_standing_returns_inside_the_hook_budget(org, tmp_path):
    """In a subprocess with a timeout: a read that waited would hang it."""
    path = org(entry("tag", "bounded", tag_keys=["team"]))
    standing = authority_standing.path()
    standing.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(standing)
    code = (
        "import json, sys\n"
        "from datetime import datetime, UTC\n"
        "from finops import guard_ledger, guard_authority\n"
        f"guard_ledger._path_override = __import__('pathlib').Path({str(guard_ledger.ledger_path())!r})\n"
        f"guard_authority._now = lambda: datetime({NOW.year}, {NOW.month}, {NOW.day}, tzinfo=UTC)\n"
        f"v = guard_authority.ladder_verdict({TAG!r}, cwd=None, harness='claude-code',\n"
        "    scope=lambda: (None, None))\n"
        "print(json.dumps(v))\n")
    env = {**os.environ, "FINOPS_ORG_DIR": str(path.parent),
           "PYTHONPATH": str(Path(guard_authority.__file__).resolve().parents[1])}
    started = time.perf_counter()
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=20, check=True)
    assert time.perf_counter() - started < 20
    v = json.loads(out.stdout.strip().splitlines()[-1])
    assert v["decision"] == "ask" and v["rung"] == "approve_each"


# ── cheap, and never raises ──────────────────────────────────────────────────

def test_ten_thousand_calls_in_no_class_return_none_in_under_a_millisecond_each(org):
    commands = ["ls -la", "git status", "git commit -m 'fix the stage'", "pytest -q tests",
                "npm run build", "cat README.md", "python -m pip install -e .",
                "aws s3 ls s3://bucket", "terraform plan -out tf.plan", "kubectl get pods -A",
                "docker build -t app .", "make test", "rg -n TODO src", "cd src && ls",
                "go test ./...", "cargo build --release", "echo done", "head -50 log.txt",
                "gh pr view 12", "aws sts get-caller-identity"]
    calls = [commands[i % len(commands)] for i in range(10_000)]
    started = time.perf_counter()
    for c in calls:
        assert lv(c) is None
    per_call_ms = (time.perf_counter() - started) * 1000 / len(calls)
    assert per_call_ms < 1.0, per_call_ms


def test_it_never_raises(org, monkeypatch):
    org(entry("tag", "bounded", tag_keys=["team"]))
    for junk in ("", "'unbalanced", "aws ec2 create-tags --resources '", "x" * 300_000,
                 "aws ec2 create-tags " + "--resources i-1 " * 5000):
        lv(junk)
    monkeypatch.setattr(guard_authority, "_ladder", lambda *a, **k: 1 / 0)
    v = lv(TAG)
    assert v["decision"] == "ask" and "could not be judged" in v["reason"]


def test_an_unreadable_authority_yaml_holds_the_class_at_approve_each(org):
    path = org(entry("tag", "bounded", tag_keys=["team"]))
    path.write_text("- fact: authority\n  subject: {kind: action_class\n")
    v = lv(TAG)
    assert (v["decision"], v["rung"]) == ("ask", "approve_each")


def test_a_fifo_in_place_of_authority_yaml_is_unreadable_never_waited_on(org):
    path = org()
    path.unlink()
    os.mkfifo(path)
    v = lv(TAG)
    assert (v["decision"], v["rung"]) == ("ask", "approve_each")


def test_an_expired_bounded_entry_asks(org):
    org(entry("tag", "bounded", tag_keys=["team"]).replace("2027-06-30", "2026-10-20"))
    v = lv(TAG)
    assert (v["decision"], v["rung"]) == ("ask", "approve_each")


def test_an_untrusted_repo_bounded_entry_asks(org, tmp_path, monkeypatch):
    repo = tmp_path / "cloned"
    (repo / ".git").mkdir(parents=True)
    (repo / "nable.org").mkdir()
    (repo / "nable.org" / "authority.yaml").write_text(
        "# nable org model v1\n" + entry("tag", "bounded", tag_keys=["team"]))
    monkeypatch.delenv("FINOPS_ORG_DIR")
    from finops.org import store
    monkeypatch.setattr(store, "_data_dir", lambda: tmp_path / "data")
    v = lv(TAG, cwd=str(repo))
    assert (v["decision"], v["rung"]) == ("ask", "approve_each")


# ── doctor ───────────────────────────────────────────────────────────────────

def test_doctor_says_each_class_and_a_demotion(org):
    org(entry("tag", "bounded", tag_keys=["team"]), entry("ticket"))
    _standing(tag={"demoted": True, "since": "2026-10-20", "line": "d" * 64})
    covered, gaps, fixes = [], [], []
    got = guard_authority.doctor(covered, gaps, lambda cmd, why: fixes.append((cmd, why)))
    assert got["present"] and got["classes"]["tag"]["rung"] == "approve_each"
    assert got["classes"]["tag"]["file_rung"] == "bounded"
    assert any("tag" in c for c in covered + gaps)
    assert any("demoted" in g for g in gaps)


def test_doctor_with_no_file_says_nothing_changed(org):
    covered, gaps = [], []
    got = guard_authority.doctor(covered, gaps, lambda *a: None)
    assert got["present"] is False and not gaps
