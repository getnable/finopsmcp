"""What a review of the guard core found, as tests.

Each section is one finding; the payloads are the reviewer's repros, so a
regression shows up as the exact command an agent could send.

Invariants under test:
  - normalizing a command only ever adds classifications: the command is read
    as written, with its aliases expanded and with quoted data blanked, and
    the most severe reading wins; a hit is dropped only when it lies entirely
    inside one quoted argument of a data program the shell will not run
  - no input makes the classifier quadratic
  - a budget stop never softens a policy deny or a cloud budget hard stop,
    and the verdict on the command is still recorded
  - every shell command in a line is priced, and the figures added up
  - a saved plan is found after any `cd`, and a plan file that cannot be
    found or read asks
  - an agent cannot change its budgets or remove the guard without a human
"""
from __future__ import annotations

import io
import json
import os
import stat
import time
from datetime import UTC, date, datetime, timedelta

import pytest

import finops.guard as g
import finops.guard_ledger as gl
from finops import ai_budget
from finops.aws_prices import EC2_HOURLY
from finops.budget import summary as bs

DELETE = ("one_way", "delete_resource")
TERMINATE = ("one_way", "terminate_instance")
APPLY = ("two_way", "infra_apply")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_GUARD_TEAM", "FINOPS_GUARD_ACCOUNT", "FINOPS_POLICY_ON_BUDGET_BREACH",
                "FINOPS_POLICY_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


def _over_ai_budget(monkeypatch):
    monkeypatch.setattr(ai_budget, "status", lambda **_: {
        "verdict": ai_budget.BUDGET_OVER, "verdict_basis": "tokens", "pct_of_budget": 2.0,
        "billable_tokens_mtd": 2000, "budget": {"monthly_tokens": 1000}})


def _records():
    p = gl.ledger_path()
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def _timed(fn, *a, **k):
    t = time.perf_counter()
    out = fn(*a, **k)
    return out, time.perf_counter() - t


# ── 1. comments, escaped quotes and $'...' fooled the quote reader ────────────

@pytest.mark.parametrize("cmd,want", [
    ("echo \"Cleaning up\" # don't need these\nterraform destroy -auto-approve\necho 'done'",
     DELETE),
    ("echo \"Cleaning up\" # don't need these anymore\n"
     "aws ec2 terminate-instances --instance-ids i-1\necho 'done'", TERMINATE),
    ("echo \\' ; aws ec2 terminate-instances --instance-ids i-1 ; echo \\'", TERMINATE),
    ("echo $'\\'' ; terraform destroy -auto-approve ; echo ''", DELETE),
    ("echo \"a\\\"\" ; terraform destroy", DELETE),
])
def test_quotes_the_shell_reads_differently_do_not_hide_a_destroy(cmd, want):
    assert g.classify_command(cmd) == want, cmd
    v = g.gate_command(cmd, record=False)
    assert v is not None and v["decision"] in ("ask", "deny"), cmd


# ── 2. an alias could turn the destroy into an echo ───────────────────────────

@pytest.mark.parametrize("cmd,want", [
    ("alias terraform=echo; terraform destroy -auto-approve", DELETE),
    ("alias kubectl=true; kubectl delete ns prod", DELETE),
    ("alias aws=echo; aws ec2 terminate-instances --instance-ids i-1", TERMINATE),
    # and the expansion still counts
    ("alias tf=terraform; tf destroy -auto-approve", DELETE),
])
def test_an_alias_is_judged_both_expanded_and_as_written(cmd, want):
    assert g.classify_command(cmd) == want, cmd
    assert g.gate_command(cmd, record=False)["decision"] == "ask"


# ── 3. no quadratic input ─────────────────────────────────────────────────────

FILLS_256K = {
    "escaped quotes": lambda n: 'terraform destroy -auto-approve; echo "' + '\\"' * n,
    "pipes": lambda n: "terraform destroy; echo 'x' " + "|" * n,
    "pipe tokens": lambda n: "terraform destroy; echo 'x' " + "|a" * (n // 2),
    "base64 then pipes": lambda n: "terraform destroy; base64 -d x " + "|" * n,
}


@pytest.mark.parametrize("name", list(FILLS_256K))
def test_padding_at_the_judged_limit_stays_fast(name):
    cmd = FILLS_256K[name](g.MAX_JUDGED_CHARS)[:g.MAX_JUDGED_CHARS]
    hit, took = _timed(g.classify_command, cmd)
    assert hit == DELETE
    assert took < 1.0, f"{name}: classify_command took {took:.2f}s"
    out = io.StringIO()
    _, took = _timed(g.run_hook, io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "/tmp"})), out)
    assert took < 2.0, f"{name}: the hook took {took:.2f}s"
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("name", list(FILLS_256K))
def test_the_padding_grows_linearly(name):
    small = FILLS_256K[name](g.MAX_JUDGED_CHARS // 4)
    big = FILLS_256K[name](g.MAX_JUDGED_CHARS)
    _, t_small = _timed(g.classify_command, small)
    _, t_big = _timed(g.classify_command, big)
    assert t_big < 8 * max(t_small, 0.01), f"{name}: 4x the input may not cost 16x"


# ── 4. line continuations and backslash escapes ───────────────────────────────

@pytest.mark.parametrize("cmd,want", [
    ("terraform destroy\\\n  -auto-approve", DELETE),
    ("kubectl delete\\\n ns prod", DELETE),
    ("helm uninstall\\\n x", DELETE),
    ("t\\erraform destroy", DELETE),
    ("terraform de\\stroy -auto-approve", DELETE),
    ("a\\ws ec2 terminate-instances --instance-ids i-1", TERMINATE),
    ("aws ec2 terminate\\-instances --instance-ids i-1", TERMINATE),
    ("terraform destroy\\", DELETE),
    ("terraform destroy>/tmp/log", DELETE),
])
def test_escapes_do_not_hide_a_destroy(cmd, want):
    assert g.classify_command(cmd) == want, cmd


# ── 5. quoted text that is then run ───────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    'echo "terraform destroy -auto-approve" | env bash',
    'echo "terraform destroy -auto-approve" | sudo -E bash',
    'echo "terraform destroy -auto-approve" | command bash',
    'echo "terraform destroy -auto-approve" | nohup bash',
    'echo "terraform destroy -auto-approve" | busybox sh',
    'echo "terraform destroy -auto-approve" | tee >(sh)',
    'echo "terraform destroy -auto-approve" > x.sh; bash x.sh',
    "echo 'terraform destroy -auto-approve' |& bash",
    "echo 'terraform destroy -auto-approve' | parallel",
    'echo "terraform destroy -auto-approve" | at now',
    'echo "terraform destroy -auto-approve" | tee x.sh',
    'echo "terraform destroy -auto-approve" > x.sh && chmod +x x.sh && ./x.sh',
    'echo "terraform destroy -auto-approve" >& x.sh',
    'printf -v x "terraform destroy"; $x',
    'rg --pre "terraform destroy #" pattern',
    "git -c 'core.editor=terraform destroy #' commit",
    'GIT_EDITOR="terraform destroy #" git commit',
    'hash -p /bin/bash grep; grep -c "terraform destroy"',
    'alias echo=bash; echo "terraform destroy"',
    'alias grep=terraform; grep "destroy -auto-approve"',
    '(echo "terraform destroy"; ls) | at now',
    'echo "terraform" destroy',             # half quoted: not one argument
])
def test_quoted_text_the_shell_may_run_still_classifies(cmd):
    assert g.classify_command(cmd) == DELETE, cmd


@pytest.mark.parametrize("cmd", [
    'grep -rn "terraform destroy" docs/ | head -5',
    'echo "terraform destroy" >&2',
    'git commit -m "docs: terraform destroy" 2>/dev/null',
    'git commit -m "docs: terraform destroy" && git push',
    'kubectl get pods; echo "kubectl delete"',
    "printf 'kubectl delete ns x\\n' | wc -l",
])
def test_quoted_data_that_is_only_printed_stays_silent(cmd):
    assert g.classify_command(cmd) is None, cmd


# ── 6. a budget stop never softens a stronger verdict ─────────────────────────

P4D = "aws ec2 run-instances --instance-type p4d.24xlarge --count 8"


def test_a_policy_deny_survives_the_ai_budget_stop(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "stop_idle")
    assert g.gate_command(P4D, record=False)["decision"] == "deny"
    _over_ai_budget(monkeypatch)
    v = g.gate_command(P4D)
    assert v["decision"] == "deny" and v["action_type"] == "infra_apply"
    assert "not in your allowlist" in v["reason"] and "over its AI budget" in v["reason"]
    recs = _records()
    assert [(r["action_type"], r["decision"]) for r in recs] == \
        [("ai_budget", "ask"), ("infra_apply", "deny")]
    assert recs[1]["monthly_usd"] == pytest.approx(EC2_HOURLY["p4d.24xlarge"] * 8 * 730, abs=1)


def test_an_allowed_launch_is_recorded_as_asked_when_the_budget_asks(monkeypatch):
    _over_ai_budget(monkeypatch)
    v = g.gate_command("aws ec2 run-instances --instance-type t3.micro")
    assert v["decision"] == "ask" and v["action_type"] == "ai_budget"
    recs = _records()
    assert [(r["action_type"], r["decision"]) for r in recs] == \
        [("ai_budget", "ask"), ("infra_apply", "ask")]


def _month() -> tuple[str, str]:
    today = datetime.now().astimezone().date()
    nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    return today.replace(day=1).isoformat(), (nxt - timedelta(days=1)).isoformat()


def test_a_cloud_budget_hard_stop_survives_the_ai_budget_stop(monkeypatch, tmp_path):
    start, end = _month()
    bs.write_summary([{"name": "Total", "scope_type": "total", "scope_value": "*",
                       "period": "monthly", "period_start": start, "period_end": end,
                       "spent": 49_999.0, "limit": 50_000.0, "pct_used": 99.9,
                       "status": "ok"}],
                     spend_through=start, now=datetime.now(UTC) - timedelta(hours=1))
    policy = tmp_path / "nable.policy.yaml"
    policy.write_text("on_budget_breach: deny\n")
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(policy))
    _over_ai_budget(monkeypatch)
    v = g.gate_command(P4D, record=False)
    assert v["decision"] == "deny", v["reason"]
    assert "'Total' budget" in v["reason"]


def test_an_mcp_call_is_judged_under_the_ai_budget_stop_too(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "stop_idle")
    _over_ai_budget(monkeypatch)
    v = g.gate_mcp_call("mcp__awslabs_aws-api__call_aws",
                        {"cli_command": P4D})
    assert v["decision"] == "deny" and v["action_type"] == "infra_apply"
    assert [r["action_type"] for r in _records()] == ["ai_budget", "infra_apply"]


# ── 7. every command in the line is priced ────────────────────────────────────

def test_two_launches_are_added_up():
    cmd = ("aws ec2 run-instances --instance-type t3.micro --count 1 && "
           "aws ec2 run-instances --instance-type p4d.24xlarge --count 8")
    est = g.estimate_command_monthly_cost(cmd)
    want = (EC2_HOURLY["t3.micro"] + 8 * EC2_HOURLY["p4d.24xlarge"]) * 730
    assert est["monthly_usd"] == pytest.approx(want, abs=0.05)
    assert len(est["parts"]) == 2 and "2 changes in this command" in est["line"]
    assert g.gate_command(cmd, record=False)["decision"] == "ask"


def test_launches_on_separate_lines_are_added_up():
    cmd = ("aws ec2 run-instances --instance-type t3.micro\n"
           "aws ec2 run-instances --instance-type p4d.24xlarge --count 8")
    est = g.estimate_command_monthly_cost(cmd)
    assert est["monthly_usd"] > 8 * EC2_HOURLY["p4d.24xlarge"] * 700


def test_an_unpriceable_launch_does_not_stop_the_next_one():
    from finops.aws_prices import rds_hourly
    cmd = ("aws ec2 run-instances --launch-template LaunchTemplateId=lt-1 && "
           "aws rds create-db-instance --db-instance-identifier d "
           "--db-instance-class db.m5.12xlarge --engine postgres --multi-az")
    est = g.estimate_command_monthly_cost(cmd)
    assert est["monthly_usd"] == pytest.approx(
        rds_hourly("db.m5.12xlarge", "postgres") * 2 * 730, abs=0.05)
    assert est["unpriced_changes"] == 1 and "1 more change in this command has no figure" \
        in est["line"]


def test_an_operator_inside_a_quoted_argument_does_not_split_the_launch():
    cmd = ("aws ec2 run-instances --query 'Instances[] | [0]' "
           "--instance-type p4d.24xlarge --count 8")
    est = g.estimate_command_monthly_cost(cmd)
    assert est["monthly_usd"] == pytest.approx(8 * EC2_HOURLY["p4d.24xlarge"] * 730, abs=0.05)


def test_a_quoted_script_is_split_on_its_own_separators():
    cmd = ("sh -c 'aws ec2 run-instances --instance-type t3.micro; "
           "aws ec2 run-instances --instance-type p4d.24xlarge --count 8'")
    est = g.estimate_command_monthly_cost(cmd)
    assert est["monthly_usd"] > 8 * EC2_HOURLY["p4d.24xlarge"] * 700


def test_the_budget_scope_covers_every_command():
    scope = g._change_scope("aws ec2 run-instances --instance-type m5.large && "
                            "gcloud compute instances create vm --machine-type n2-standard-4")
    assert set(scope["provider"]) == {"aws", "gcp"}
    b = {"scope_type": "provider", "scope_value": "gcp"}
    assert g._budget_applies(b, scope)
    assert g._change_scope("aws ec2 run-instances --instance-type m5.large")["provider"] == "aws"


# ── 8. a saved plan is found after any cd ─────────────────────────────────────

@pytest.fixture
def destroy_plan(tmp_path, monkeypatch):
    """A `terraform` on PATH whose `show -json` is a plan that deletes a
    database, and a repo with infra/tfplan in it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "terraform"
    exe.write_text("#!/bin/sh\ncat <<'J'\n" + json.dumps({"resource_changes": [
        {"address": "aws_db_instance.main", "type": "aws_db_instance",
         "change": {"actions": ["delete"], "before": {"instance_class": "db.r6g.large"}}}]})
        + "\nJ\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("TERRAFORM_BIN", raising=False)
    repo = tmp_path / "proj"
    (repo / "infra").mkdir(parents=True)
    (repo / "infra" / "tfplan").write_bytes(b"x")
    return repo


@pytest.mark.parametrize("cmd", [
    "cd infra && terraform apply tfplan",
    "git pull && cd infra && terraform apply tfplan",
    "(cd infra && terraform apply tfplan)",
    "export X=1 && cd infra && terraform apply -auto-approve tfplan",
    "pushd infra && terraform apply tfplan",
    "cd infra\nterraform apply tfplan",
    "terraform -chdir=infra apply tfplan",
    "terraform apply infra/tfplan",
])
def test_a_saved_destroy_plan_is_found_after_any_cd(destroy_plan, cmd):
    v = g.gate_command(cmd, cwd=str(destroy_plan), record=False)
    assert v and v["decision"] == "ask", cmd
    assert v["action_type"] == "delete_resource", (cmd, v["reason"])
    assert "The saved plan destroys 1 resource (aws_db_instance.main)" in v["reason"]


def test_a_plan_looked_for_in_the_wrong_place_asks(destroy_plan):
    v = g.gate_command("cd elsewhere && terraform apply tfplan", cwd=str(destroy_plan),
                       record=False)
    assert v and v["decision"] == "ask"
    assert "could not read saved plan tfplan (no such file in" in v["reason"]


# ── 9. loop-key stamping is bounded ───────────────────────────────────────────

def test_a_repeated_big_directory_is_stamped_once(tmp_path):
    big = tmp_path / "manifests"
    big.mkdir()
    for i in range(3000):
        (big / f"m{i}.yaml").write_text("x")
    cmd = "kubectl --context prod apply " + f"-f {big} " * 1000
    _, took = _timed(g.loop_key, cmd, cwd=str(tmp_path))
    assert took < 0.5, f"{took:.2f}s"
    _, took = _timed(g.gate_command, cmd, cwd=str(tmp_path), record=False)
    assert took < 1.0, f"{took:.2f}s"


def test_many_distinct_files_are_capped(tmp_path, monkeypatch):
    calls = []
    real = g._file_stamp
    monkeypatch.setattr(g, "_file_stamp", lambda base, val: calls.append(val) or real(base, val))
    g.loop_key("kubectl apply " + " ".join(f"-f f{i}.yaml" for i in range(100)),
               cwd=str(tmp_path))
    assert len(calls) == g._LOOP_FILES_MAX


def test_a_new_file_in_a_manifest_directory_changes_the_key(tmp_path):
    d = tmp_path / "k8s"
    d.mkdir()
    (d / "a.yaml").write_text("x")
    cmd = f"kubectl apply -f {d}"
    before = g.loop_key(cmd, cwd=str(tmp_path))[0]
    assert g.loop_key(cmd, cwd=str(tmp_path))[0] == before
    (d / "b.yaml").write_text("y")
    os.utime(d / "b.yaml", ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert g.loop_key(cmd, cwd=str(tmp_path))[0] != before


# ── 10. an agent changing its budgets or removing the guard ──────────────────

@pytest.mark.parametrize("tool,args", [
    ("mcp__nable__set_budget", {"name": "AWS", "limit_usd": 10**9}),
    ("mcp__finops__delete_budget", {"budget_id": 3}),
    ("mcp__finops-mcp__sync_budgets_from_yaml", {"yaml_path": "budget.yml"}),
])
def test_an_agent_changing_a_cloud_budget_is_asked_about(tool, args):
    v = g.gate_mcp_call(tool, args)
    assert v and v["decision"] == "ask" and v["action_type"] == "budget_change"
    assert "a human should confirm" in v["reason"]
    [r] = _records()
    assert r["action_type"] == "budget_change" and r["command"].startswith(tool.split("__")[-1])


@pytest.mark.parametrize("cmd,action", [
    ("nable ai-budget --spend-cap 99999", "ai_budget_change"),
    ("finops ai-budget --tokens 1b", "ai_budget_change"),
    ("nable ai-budget --session-cap 0", "ai_budget_change"),
    ("nable ai-budget --plan-cost=500", "ai_budget_change"),
    ("nable ai-budget --reset", "ai_budget_change"),
    ("NABLE ai-budget --spend-cap 5", "ai_budget_change"),
    ("n\\able ai-budget --spend-cap 5", "ai_budget_change"),
    ("nable budget ci-gate --budget-file budget.yml", "budget_change"),
    ("nable guard uninstall", "guard_change"),
    ("nable guard --harness cursor uninstall", "guard_change"),
    ("uvx --from finops-mcp finops guard uninstall", "guard_change"),
    ("nable uninstall --yes", "guard_change"),
])
def test_the_same_changes_from_the_shell_ask(cmd, action):
    v = g.gate_command(cmd)
    assert v and v["decision"] == "ask" and v["action_type"] == action, cmd
    assert _records()[-1]["action_type"] == action


@pytest.mark.parametrize("cmd", [
    "nable ai-budget", "nable ai-budget --json --month", "nable budget",
    "nable budget refresh", "nable budget ci-gate --fail-on-breach", "nable guard status",
    "pip uninstall foo; nable guard status",
    'git commit -m "docs: nable guard uninstall removes the hook"',
    'echo "raise it with nable ai-budget --spend-cap 5"',
])
def test_reading_a_budget_is_not_asked_about(cmd):
    assert g.gate_command(cmd, record=False) is None, cmd


def test_an_agent_over_budget_cannot_lift_the_hard_stop_from_the_shell(monkeypatch):
    _over_ai_budget(monkeypatch)
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    v = g.gate_command("nable ai-budget --tokens 1000000000000")
    assert v["decision"] == "deny" and v["action_type"] == "ai_budget_change"
    assert "changing its own AI budget" in v["reason"] and "over its AI budget" in v["reason"]


@pytest.mark.parametrize("verdict", ["warn", "over"])
@pytest.mark.parametrize("hard", [False, True])
def test_the_budget_remedy_is_addressed_to_the_human(monkeypatch, verdict, hard):
    monkeypatch.setattr(g, "_budget_note_due", lambda _sid: True)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {
        "verdict": ai_budget.BUDGET_WARN if verdict == "warn" else ai_budget.BUDGET_OVER,
        "verdict_basis": "spend", "pct_of_budget": 0.9 if verdict == "warn" else 1.2,
        "est_usd_mtd_list_price": 180, "budget": {"spend_cap": 200}})
    if hard:
        monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    reason = g.check_budget_gate()["reason"]
    assert "in your own terminal" in reason and "`nable ai-budget --spend-cap USD`" in reason


# ── 11. lowercasing never gives up on a command ───────────────────────────────

@pytest.mark.parametrize("cmd", ["TERRAFORM destroy -auto-approve # İ",
                                 "Terraform destroy İİİ"])
def test_a_character_that_lowers_to_two_does_not_stop_the_lowering(cmd):
    assert g.classify_command(cmd) == DELETE


# ── 12. a fleet counted in anything but instances gets no figure ──────────────

FLEET = ("aws ec2 create-fleet --target-capacity-specification "
         "TotalTargetCapacity=384,DefaultTargetCapacityType=on-demand{unit} "
         "--launch-template-configs LaunchTemplateSpecification={{LaunchTemplateId=lt-1,"
         "Version=1}},Overrides=[{{InstanceType=p4d.24xlarge{weight}}}]")


@pytest.mark.parametrize("unit,weight", [(",TargetCapacityUnitType=vcpu", ""),
                                         (",TargetCapacityUnitType=memory-mib", ""),
                                         ("", ",WeightedCapacity=96")])
def test_a_fleet_in_vcpus_or_weights_is_not_priced_as_instances(unit, weight):
    assert g.estimate_command_monthly_cost(FLEET.format(unit=unit, weight=weight)) is None


def test_a_fleet_in_units_is_still_priced():
    est = g.estimate_command_monthly_cost(
        FLEET.format(unit=",TargetCapacityUnitType=units", weight="").replace("384", "2"))
    assert est["count"] == 2


# ── 13. the other bucket wipes ────────────────────────────────────────────────

@pytest.mark.parametrize("cmd,want", [
    ("gcloud storage rm -r gs://b", DELETE),
    ("gcloud storage rm --recursive gs://bucket", DELETE),
    ("gcloud --project p storage rm gs://b/o", DELETE),
    ("aws s3 mv s3://a s3://b --recursive", DELETE),
    ("aws s3 mv --recursive s3://a ./local", DELETE),
    ("aws s3 mv s3://a ./local --recursive --exclude '*.tmp'", DELETE),
    ("aws s3 mv ./local s3://b --recursive", None),
    ("gcloud storage ls gs://b", None),
])
def test_gcloud_storage_rm_and_a_move_out_of_a_bucket_are_deletes(cmd, want):
    assert g.classify_command(cmd) == want, cmd


def test_the_question_says_it_deletes_stored_data():
    v = g.gate_command("gcloud storage rm -r gs://b", record=False)
    assert "This would delete stored data" in v["reason"]


# ── latency ───────────────────────────────────────────────────────────────────

ORDINARY = ["ls -la", "git status", 'git commit -m "docs: explain terraform destroy"',
            "npm test", "pytest -q tests/", 'grep -rn "kubectl delete" docs/',
            "cat README.md | head -50", "echo 'hello world' > out.txt",
            "cd infra && terraform plan", "kubectl get pods -n prod",
            "aws s3 ls s3://bucket", "aws ec2 describe-instances --region us-east-1",
            "docker build -t app . && docker push app", "helm upgrade api ./chart",
            "python3 -m pip install -r requirements.txt", "find . -name '*.py' | xargs wc -l",
            "make build && ./bin/app --help", "kubectl apply -f deploy.yaml",
            "terraform apply -auto-approve", "aws ec2 run-instances --instance-type m5.large"]


def test_ordinary_commands_stay_cheap():
    for c in ORDINARY:
        g.classify_command(c)
    _, took = _timed(lambda: [g.classify_command(c) for _ in range(50) for c in ORDINARY])
    assert took / (50 * len(ORDINARY)) < 0.002, f"{took / 1000 * 1e6:.0f} us per command"
