"""An "ask" should carry a dollar figure, and every figure states its basis.

The guard's question to a human is only as useful as the number in it. These
tests pin which commands agents actually run get priced, from which table, and
the refusal side just as hard: a command whose price the repo does not know
gets NO figure, never an invented one.
"""
from __future__ import annotations

import pytest

import finops.ai_budget as ai_budget
import finops.guard as g


@pytest.fixture(autouse=True)
def _clean_policy_env(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ai_budget, "status", lambda: {"verdict": ai_budget.BUDGET_OK})


# ── AWS global options must not hide a price ──────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "aws --region us-east-1 ec2 run-instances --instance-type p4d.24xlarge --count 8",
    "aws --output json --no-cli-pager ec2 run-instances --instance-type p4d.24xlarge --count 8",
    'aws ec2 run-instances --instance-type "p4d.24xlarge" --count 8',
])
def test_a_launch_with_global_options_is_still_priced(cmd):
    """The classifier stripped global options and the pricer did not, so these
    classified as a launch, found no price, and passed silently at ~$191k/mo."""
    v = g.gate_command(cmd)
    assert v is not None and v["decision"] == "ask", f"{cmd!r} passed silently"
    assert "$191,377" in v["reason"]


# ── RDS ───────────────────────────────────────────────────────────────────────

RDS = "aws rds create-db-instance --db-instance-identifier orders --engine postgres"


def test_rds_is_priced_from_the_rds_table():
    est = g.estimate_command_monthly_cost(f"{RDS} --db-instance-class db.r5.large")
    assert est["monthly_usd"] == pytest.approx(0.24 * 730)
    assert "list price" in est["basis"] and "storage" in est["basis"], \
        "the basis must say storage is not in the figure"


def test_multi_az_doubles_the_instance_hours():
    single = g.estimate_command_monthly_cost(f"{RDS} --db-instance-class db.r5.4xlarge")
    multi = g.estimate_command_monthly_cost(f"{RDS} --db-instance-class db.r5.4xlarge --multi-az")
    assert multi["monthly_usd"] == pytest.approx(2 * single["monthly_usd"])
    assert "Multi-AZ" in multi["line"] and "x2" in multi["line"]
    assert g.estimate_command_monthly_cost(
        f"{RDS} --db-instance-class db.r5.4xlarge --no-multi-az")["monthly_usd"] \
        == single["monthly_usd"], "--no-multi-az is not --multi-az"


def test_an_expensive_database_asks_with_the_number():
    v = g.gate_command(f"{RDS} --db-instance-class db.r5.4xlarge --multi-az")
    assert v and v["decision"] == "ask"
    assert "$2,803" in v["reason"]
    assert v["estimate"]["basis"] in v["reason"]


def test_a_small_database_stays_silent():
    assert g.gate_command(f"{RDS} --db-instance-class db.t4g.micro") is None


@pytest.mark.parametrize("cmd", [
    "aws rds create-db-instance --engine sqlserver-se --db-instance-class db.r5.large",
    "aws rds create-db-instance --engine oracle-ee --db-instance-class db.r5.large",
    "aws rds create-db-instance --engine aurora-postgresql --db-instance-class db.r5.large",
    "aws rds create-db-instance --db-instance-class db.r5.large",
    f"{RDS} --db-instance-class db.x9.huge",
])
def test_rds_without_a_table_price_gets_no_figure(cmd):
    """Licence-included engines and Aurora bill at rates the table does not
    hold. A MySQL rate on a SQL Server instance would be an invented figure."""
    assert g.estimate_command_monthly_cost(cmd) is None


# ── commitments: always one-way, now with the amount ─────────────────────────

SP = "aws savingsplans create-savings-plan --savings-plan-offering-id off-1"


def test_a_savings_plan_states_the_commitment_for_both_terms():
    v = g.gate_command(f"{SP} --commitment 10")
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"
    assert "$7,300/mo" in v["reason"]
    assert "$87,600 over a 1-year term" in v["reason"]
    assert "$262,800 over 3 years" in v["reason"]
    assert "offering id" in v["reason"], "the basis must say why the term is not known"
    assert "one-way door" in v["reason"]


def test_a_savings_plan_upfront_amount_is_named():
    est = g.estimate_command_monthly_cost(f"{SP} --commitment 2.5 --upfront-payment-amount 5000")
    assert "$5,000 of it up front" in est["line"]


def test_a_reserved_instance_order_ceiling_is_stated():
    v = g.gate_command("aws ec2 purchase-reserved-instances-offering "
                       "--reserved-instances-offering-id ri-1 --instance-count 4 "
                       "--limit-price Amount=12000,CurrencyCode=USD")
    assert v["decision"] == "ask"
    assert "4 Reserved Instances, the order capped at $12,000" in v["reason"]
    assert v["estimate"]["monthly_usd"] is None, "a ceiling is not a monthly rate"


def test_the_json_limit_price_form_parses_too():
    est = g.estimate_command_monthly_cost(
        "aws ec2 purchase-reserved-instances-offering --instance-count 1 "
        "--reserved-instances-offering-id ri-1 "
        "--limit-price '{\"Amount\": 900, \"CurrencyCode\": \"USD\"}'")
    assert est and est["total_usd"] == 900


def test_a_reserved_instance_without_a_ceiling_gets_no_figure():
    """The offering id fixes the price and looking it up needs the network."""
    cmd = ("aws ec2 purchase-reserved-instances-offering "
           "--reserved-instances-offering-id ri-1 --instance-count 4")
    assert g.estimate_command_monthly_cost(cmd) is None
    v = g.gate_command(cmd)
    assert v["decision"] == "ask" and "$" not in v["reason"]


# ── GCP and Azure VMs, from the tables the repo already holds ─────────────────

def test_gcloud_instances_are_priced_per_name():
    v = g.gate_command("gcloud compute instances create web-1 web-2 "
                       "--machine-type=n2-standard-32 --zone us-central1-a")
    assert v and v["decision"] == "ask"
    assert "2x n2-standard-32" in v["reason"]
    assert "Compute Engine node price table" in v["reason"]


def test_a_small_gcloud_instance_stays_silent():
    assert g.gate_command("gcloud compute instances create web-1 --machine-type e2-standard-2") is None


def test_an_unknown_machine_type_gets_no_figure():
    assert g.estimate_command_monthly_cost(
        "gcloud compute instances create web-1 --machine-type=e2-custom-4-8192") is None


def test_az_vm_create_is_priced_and_size_is_case_insensitive():
    est = g.estimate_command_monthly_cost(
        "az vm create -g rg -n vm1 --image Ubuntu2204 --size standard_d16s_v3 --count 2")
    assert est["monthly_usd"] == pytest.approx(2 * 560.64)
    assert "Azure VM price table" in est["basis"]


def test_az_vm_create_without_a_size_gets_no_figure():
    """The CLI's default size is not in the table; guessing it is inventing."""
    assert g.estimate_command_monthly_cost("az vm create -g rg -n vm1 --image Ubuntu2204") is None


@pytest.mark.parametrize("cmd", [
    "aws rds create-db-instance --engine mysql --db-instance-class db.t3.micro",
    "gcloud compute instances create vm-1 --machine-type e2-standard-2",
    "gcloud --project p compute instances create vm-1",
    "az vm create -g rg -n vm1 --size Standard_D2s_v3",
])
def test_priceable_launches_are_classified_so_the_threshold_can_reach_them(cmd):
    assert g.classify_command(cmd) == ("two_way", "infra_apply")


# ── what is deliberately not priced ───────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "kubectl scale deploy api --replicas=50",
    "kubectl scale --replicas 500 statefulset/db -n staging",
])
def test_kubectl_scale_never_gets_an_invented_figure(cmd):
    """A replica's cost is its requests on whatever node it lands on; neither
    is in the command. No figure, and the reversible default stays silent."""
    assert g.estimate_command_monthly_cost(cmd) is None
    assert g.gate_command(cmd) is None


# ── a saved Terraform plan is priced before it is applied ─────────────────────

import json as _json  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402

P4D_PLAN = {"resource_changes": [
    {"address": "aws_instance.train", "type": "aws_instance",
     "change": {"actions": ["create"], "before": None,
                "after": {"instance_type": "p4d.24xlarge"}}},
    {"address": "aws_iam_role.r", "type": "aws_iam_role",
     "change": {"actions": ["create"], "before": None, "after": {}}},
]}


@pytest.fixture
def fake_tf(tmp_path, monkeypatch):
    """A `terraform` on PATH that prints a canned plan for `show -json` and
    records how it was called. Never the real binary: no provider runs here."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    plan_json = tmp_path / "plan.json"
    plan_json.write_text(_json.dumps(P4D_PLAN))
    for name in ("terraform", "tofu"):
        exe = bindir / name
        exe.write_text("#!/bin/sh\n"
                       f'echo "$(pwd) $*" >> "{calls}"\n'
                       '[ -n "$FAKE_TF_SLEEP" ] && sleep "$FAKE_TF_SLEEP"\n'
                       f'cat "{plan_json}"\n')
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("TERRAFORM_BIN", raising=False)
    monkeypatch.delenv("FAKE_TF_SLEEP", raising=False)
    work = tmp_path / "infra"
    work.mkdir()
    return {"work": work, "calls": calls}


def test_a_saved_plan_is_priced_through_terraform_show(fake_tf):
    (fake_tf["work"] / "plan.out").write_bytes(b"binary plan")
    v = g.gate_command("terraform apply plan.out", cwd=str(fake_tf["work"]))
    assert v and v["decision"] == "ask", "a ~$24k/mo plan must not apply silently"
    assert v["monthly_delta_usd"] == pytest.approx(32.77 * 730)
    assert "plan.out changes the bill by +$23,922/mo" in v["reason"]
    assert "terraform show -json plan.out" in v["reason"], "the basis names its source"
    assert "1 resource in the plan not priced" in v["reason"]
    call = fake_tf["calls"].read_text().split()
    assert call[0] == str(fake_tf["work"]) and call[1:3] == ["show", "-json"]


def test_chdir_and_a_cd_prefix_are_followed(fake_tf):
    (fake_tf["work"] / "plan.out").write_bytes(b"x")
    root = fake_tf["work"].parent
    assert g.estimate_command_monthly_cost("terraform -chdir=infra apply plan.out", cwd=str(root))
    assert g.estimate_command_monthly_cost("cd infra && tofu apply -auto-approve plan.out",
                                           cwd=str(root))


def test_the_plan_is_found_past_flags_that_take_a_value(fake_tf):
    (fake_tf["work"] / "plan.out").write_bytes(b"x")
    est = g.estimate_command_monthly_cost("terraform apply -lock-timeout 30s -input=false plan.out",
                                          cwd=str(fake_tf["work"]))
    assert est and est["plan"] == "plan.out"


@pytest.mark.parametrize("cmd", [
    "terraform apply",                    # no saved plan: nothing to read yet
    "terraform apply -auto-approve",
    "terraform apply missing.out",        # a plan file that does not exist
])
def test_no_plan_file_means_no_figure_and_no_subprocess(fake_tf, cmd):
    assert g.estimate_command_monthly_cost(cmd, cwd=str(fake_tf["work"])) is None
    assert not fake_tf["calls"].exists(), "terraform ran with no plan to read"


def test_no_binary_means_no_figure(fake_tf, monkeypatch):
    (fake_tf["work"] / "plan.out").write_bytes(b"x")
    monkeypatch.setenv("PATH", str(fake_tf["work"]))       # nothing runnable
    assert g.estimate_command_monthly_cost("terraform apply plan.out",
                                           cwd=str(fake_tf["work"])) is None


def test_a_slow_show_times_out_to_no_figure(fake_tf, monkeypatch):
    """The hook itself times out at 10s and a timed-out hook gives no verdict
    at all. A slow plan read must cost the figure, not the guard."""
    (fake_tf["work"] / "plan.out").write_bytes(b"x")
    monkeypatch.setenv("FAKE_TF_SLEEP", "3")
    monkeypatch.setattr(g, "_PLAN_SHOW_TIMEOUT_S", 0.3)
    assert g.estimate_command_monthly_cost("terraform apply plan.out",
                                           cwd=str(fake_tf["work"])) is None


def test_the_hook_passes_the_session_cwd(fake_tf):
    import io
    (fake_tf["work"] / "plan.out").write_bytes(b"x")
    out = io.StringIO()
    g.run_hook(stdin=io.StringIO(_json.dumps({
        "tool_name": "Bash", "cwd": str(fake_tf["work"]),
        "tool_input": {"command": "terraform apply plan.out"}})), stdout=out)
    body = _json.loads(out.getvalue())["hookSpecificOutput"]
    assert "$23,922/mo" in body["permissionDecisionReason"]


def test_the_plan_read_does_not_get_the_vault(fake_tf, monkeypatch):
    """terraform loads whatever providers the directory declares."""
    seen = {}
    import finops.security.vault as vault
    real = vault.child_env
    monkeypatch.setattr(vault, "child_env",
                        lambda *a, **k: seen.setdefault("env", real(*a, **k)))
    (fake_tf["work"] / "plan.out").write_bytes(b"x")
    g.estimate_command_monthly_cost("terraform apply plan.out", cwd=str(fake_tf["work"]))
    assert "env" in seen, "terraform show ran without child_env()"
