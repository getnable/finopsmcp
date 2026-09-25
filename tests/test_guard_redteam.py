"""What red-team dogfooding of the guard found, as tests.

Each section is one family of findings. The payloads are the ones that got
past the hook (or stopped something harmless), so a regression shows up as
the exact command an agent could send.
"""
from __future__ import annotations

import time

import pytest

import finops.guard as g
from finops import ai_budget

MB = 1 << 20


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


def _timed(fn, *a, **k):
    t = time.perf_counter()
    out = fn(*a, **k)
    return out, time.perf_counter() - t


# ── 1. cheap misses: one-way doors the classifier did not see ─────────────────

DELETE = ("one_way", "delete_resource")
TERMINATE = ("one_way", "terminate_instance")


@pytest.mark.parametrize("cmd,want", [
    # Go's flag package takes --flag as well as -flag
    ("terraform apply --destroy", DELETE),
    ("tofu apply --destroy -auto-approve", DELETE),
    ("terraform apply --destroy=true", DELETE),
    # aws global options after the service name, not only before it
    ("aws ec2 --region us-west-2 terminate-instances --instance-ids i-1", TERMINATE),
    ("aws ec2 terminate-instances --profile prod --instance-ids i-1", TERMINATE),
    ("aws s3 --profile prod rb s3://b --force", DELETE),
    ("aws ec2 --output json --no-cli-pager terminate-instances --instance-ids i-1", TERMINATE),
    ("aws ec2 --endpoint-url http://x --debug terminate-instances", TERMINATE),
    ("aws ec2 --cli-read-timeout 5 terminate-instances", TERMINATE),
    ("aws rds --region=eu-west-1 delete-db-instance --db-instance-identifier d", DELETE),
    # helm flags before the verb, and its aliases
    ("helm -n prod uninstall api", DELETE),
    ("helm --kube-context prod del api", DELETE),
    ("helm del api", DELETE),
    ("helm un api", DELETE),
    # program names on a case-insensitive filesystem
    ("TERRAFORM destroy", DELETE),
    ("Terraform destroy -auto-approve", DELETE),
    ("/usr/local/bin/Terraform destroy", DELETE),
    ("KUBECTL delete ns payments", DELETE),
    # the rest of the IaC toolchain's destroys
    ("pulumi down -y", DELETE),
    ("cdk destroy --force", DELETE),
    ("npx cdk destroy MyStack --force", DELETE),
    ("sam delete --no-prompts", DELETE),
    ("doctl compute droplet delete 123", DELETE),
    ("doctl kubernetes cluster rm prod", DELETE),
    # aws deletes that are not spelled delete-*
    ("aws s3 sync --delete ./empty s3://prod-bucket", DELETE),
    ("aws s3 sync ./empty s3://prod-bucket --delete", DELETE),
    ("aws ecr batch-delete-image --repository-name r --image-ids imageTag=x", DELETE),
    ("aws dynamodb batch-delete-item --request-items file://x.json", DELETE),
    ("aws kms schedule-key-deletion --key-id k", DELETE),
    ("aws ec2 deregister-image --image-id ami-1", DELETE),
    ("aws organizations close-account --account-id 111122223333", DELETE),
    ("aws ec2 cancel-spot-fleet-requests --spot-fleet-request-ids s --terminate-instances",
     TERMINATE),
    ("terraform workspace delete prod", DELETE),
    ("tofu workspace delete -force prod", DELETE),
    ("kubectl drain node-1 --delete-emptydir-data", DELETE),
    ("kubectl replace --force -f x.yaml", DELETE),
    # an alias defined on the same line
    ("alias tf=terraform; tf destroy -auto-approve", DELETE),
    ("alias tf='terraform' && tf destroy", DELETE),
    ("alias k='kubectl --context prod'; k delete ns payments", DELETE),
])
def test_cheap_misses_are_classified(cmd, want):
    assert g.classify_command(cmd) == want, cmd


@pytest.mark.parametrize("cmd,want", [
    ("aws s3 sync ./site s3://bucket", None),
    ("aws s3 sync ./site s3://bucket --no-delete-marker", None),
    ("aws ec2 cancel-spot-fleet-requests --spot-fleet-request-ids s --no-terminate-instances",
     None),
    ("kubectl replace -f x.yaml", None),
    ("terraform workspace list", None),
    ("terraform workspace new prod", None),
    ("helm list -n prod", None),
    ("helm -n prod upgrade api ./chart", ("two_way", "infra_apply")),
    ("alias tf=terraform; tf plan", None),
    ("python3 -c 'import boto3; print(boto3.client(\"ec2\").describe_instances())'", None),
    ("python3 -c 'print(\"terminate_all\")'", None),
    ("base64 -d payload.b64 > out.bin", None),
    ("echo aGk= | base64 -d", None),
    ("aws ec2 describe-instances --region us-east-1 --output json", None),
])
def test_their_harmless_neighbours_are_not(cmd, want):
    assert g.classify_command(cmd) == want, cmd


@pytest.mark.parametrize("cmd", [
    "python3 -c \"import boto3; boto3.client('ec2').terminate_instances(InstanceIds=['i-1'])\"",
    "python3 -c 'import boto3;[i.terminate() for i in boto3.resource(\"ec2\").instances.all()]'",
    "python -c \"import boto3; boto3.client('s3').delete_bucket(Bucket='b')\"",
    "echo dGVycmFmb3JtIGRlc3Ryb3k= | base64 -d | sh",
    "echo dGVycmFmb3JtIGRlc3Ryb3k= | base64 --decode | bash",
    "cat x.b64 | base64 -D | /bin/zsh",
])
def test_heuristics_ask_and_never_deny(cmd):
    """A boto3 one-liner that deletes, and a decoded script piped into a shell,
    hide what they do from the classifier. The guard cannot be sure, so it
    asks; it never denies on a guess."""
    v = g.gate_command(cmd, record=False)
    assert v is not None and v["decision"] == "ask", cmd


def test_the_hook_asks_on_a_command_that_was_silent():
    v = g.gate_command("aws ec2 --region us-west-2 terminate-instances --instance-ids i-1",
                       record=False)
    assert v["decision"] == "ask" and v["action_type"] == "terminate_instance"


# Fillers aimed at the new rules: each is a program or flag the new rules
# anchor on, repeated so every repetition is a possible start.
NEW_RULE_FILLS = ["helm -n ", "doctl ", "cdk ", "sam ", "pulumi ", "kubectl replace ",
                  "aws s3 sync ", "aws ec2 --region x ", "--region x ", "alias a=b; ",
                  "alias tf=terraform; tf ", "python3 -c ", "boto3 ", "base64 -d ",
                  "| sh ", "TERRAFORM ", "Terraform ", "workspace ", "--terminate-instances ",
                  "cancel-spot-fleet-requests ", "--destroy ",
                  "aws rds modify-db-instance ", "aws autoscaling update-auto-scaling-group ",
                  "aws eks update-nodegroup-config ", "InstanceType=m5.large ",
                  "--instance-type Value=", "desiredSize=4 "]


@pytest.mark.parametrize("fill", NEW_RULE_FILLS)
def test_the_new_rules_stay_linear_on_a_padded_command(fill):
    for head in ("aws ec2 terminate-instances --instance-ids i-1 ; echo ",
                 "echo ok ; "):
        cmd = head + fill * (MB // len(fill))
        hit, took = _timed(g.classify_command, cmd)
        assert took < 1.0, f"{fill!r}: {took:.2f}s for 1 MB"
        if head.startswith("aws"):
            assert hit is not None


def test_the_new_rules_grow_linearly():
    cmd = "echo ok ; " + "alias tf=terraform; tf python3 -c boto3 base64 -d | sh " * (MB // 56)
    _, small = _timed(g.classify_command, cmd[: MB // 4])
    _, big = _timed(g.classify_command, cmd)
    assert big < 8 * max(small, 0.01), "4x the input may not cost 16x (quadratic)"


# ── 2. expensive changes the classifier did not know ──────────────────────────

from finops.aws_prices import EC2_HOURLY, RDS_HOURLY  # noqa: E402

APPLY = ("two_way", "infra_apply")


def _monthly(hourly: float, n: int = 1) -> float:
    return round(hourly * n * 730, 2)


@pytest.mark.parametrize("cmd", [
    "aws rds modify-db-instance --db-instance-identifier db --db-instance-class db.r5.2xlarge",
    "aws ec2 modify-instance-attribute --instance-id i-1 --instance-type p4d.24xlarge",
    "aws ec2 modify-instance-attribute --instance-id i-1 --instance-type Value=p4d.24xlarge",
    "aws ec2 modify-instance-attribute --instance-id i-1 --attribute instanceType --value m5.large",
    "aws ec2 request-spot-instances --instance-count 50 --launch-specification file://spec.json",
    "aws ec2 request-spot-fleet --spot-fleet-request-config file://c.json",
    "aws ec2 create-fleet --cli-input-json file://fleet.json",
    "aws autoscaling set-desired-capacity --auto-scaling-group-name gpu --desired-capacity 100",
    "aws autoscaling update-auto-scaling-group --auto-scaling-group-name g --desired-capacity 50",
    "aws autoscaling create-auto-scaling-group --auto-scaling-group-name g --max-size 9",
    "aws eks update-nodegroup-config --cluster-name c --nodegroup-name g "
    "--scaling-config desiredSize=40",
    "aws eks create-nodegroup --cluster-name c --nodegroup-name g --instance-types m5.large",
])
def test_expensive_changes_are_classified(cmd):
    assert g.classify_command(cmd) == APPLY, cmd


@pytest.mark.parametrize("cmd", [
    "aws rds modify-db-instance --db-instance-identifier db --backup-retention-period 7",
    "aws ec2 modify-instance-attribute --instance-id i-1 --disable-api-termination",
    "aws autoscaling describe-auto-scaling-groups",
    "aws eks describe-nodegroup --cluster-name c --nodegroup-name g",
])
def test_their_cheap_neighbours_are_not(cmd):
    assert g.classify_command(cmd) is None, cmd


def test_an_rds_class_change_prices_the_new_class():
    est = g.estimate_command_monthly_cost(
        "aws rds modify-db-instance --db-instance-identifier db "
        "--db-instance-class db.r5.2xlarge --apply-immediately")
    assert est["monthly_usd"] == _monthly(RDS_HOURLY["db.r5.2xlarge"])
    assert "db.r5.2xlarge" in est["line"] and "current class" in est["basis"]


def test_an_rds_class_change_to_multi_az_prices_the_standby():
    est = g.estimate_command_monthly_cost(
        "aws rds modify-db-instance --db-instance-identifier db "
        "--db-instance-class db.r5.2xlarge --multi-az")
    assert est["monthly_usd"] == _monthly(RDS_HOURLY["db.r5.2xlarge"], 2)


@pytest.mark.parametrize("flag", ["--instance-type p4d.24xlarge",
                                  "--instance-type Value=p4d.24xlarge",
                                  '--instance-type {"Value": "p4d.24xlarge"}',
                                  "--attribute instanceType --value p4d.24xlarge"])
def test_an_instance_type_change_prices_the_new_type(flag):
    est = g.estimate_command_monthly_cost(
        f"aws ec2 modify-instance-attribute --instance-id i-1 {flag}")
    assert est["monthly_usd"] == _monthly(EC2_HOURLY["p4d.24xlarge"])


def test_an_instance_type_change_to_p4d_asks():
    v = g.gate_command("aws ec2 modify-instance-attribute --instance-id i-1 "
                       "--instance-type p4d.24xlarge", record=False)
    assert v["decision"] == "ask" and v["action_type"] == "infra_apply"


@pytest.mark.parametrize("cmd,n", [
    ("aws ec2 run-instances --instance-type p4d.24xlarge --min-count 1 --max-count 8", 8),
    ("aws ec2 run-instances --instance-type p4d.24xlarge --max-count=8", 8),
    ("aws ec2 run-instances --instance-type p4d.24xlarge --min-count 3", 3),
    ("aws ec2 run-instances --instance-type p4d.24xlarge --count 2:6", 6),
])
def test_run_instances_prices_the_max_count(cmd, n):
    """It launches up to the max, so the max is what a human is authorising."""
    est = g.estimate_command_monthly_cost(cmd)
    assert est["count"] == n
    assert est["monthly_usd"] == _monthly(EC2_HOURLY["p4d.24xlarge"], n)


def test_a_spot_request_with_a_visible_type_is_a_labelled_ceiling():
    est = g.estimate_command_monthly_cost(
        'aws ec2 request-spot-instances --instance-count 4 '
        '--launch-specification {"InstanceType": "p4d.24xlarge", "ImageId": "ami-1"}')
    assert est["count"] == 4
    assert est["monthly_usd"] == _monthly(EC2_HOURLY["p4d.24xlarge"], 4)
    assert "spot prices vary" in est["line"]


def test_a_spot_request_without_a_type_gets_no_figure():
    assert g.estimate_command_monthly_cost(
        "aws ec2 request-spot-instances --instance-count 50 "
        "--launch-specification file://spec.json") is None


def test_a_fleet_with_a_visible_type_and_capacity_is_priced():
    est = g.estimate_command_monthly_cost(
        "aws ec2 create-fleet --launch-template-configs "
        "LaunchTemplateSpecification={LaunchTemplateId=lt-1},Overrides=[{InstanceType=m5.large}] "
        "--target-capacity-specification TotalTargetCapacity=20,DefaultTargetCapacityType=on-demand")
    assert est["count"] == 20
    assert est["monthly_usd"] == _monthly(EC2_HOURLY["m5.large"], 20)


def test_a_nodegroup_with_a_type_and_size_is_priced():
    est = g.estimate_command_monthly_cost(
        "aws eks create-nodegroup --cluster-name c --nodegroup-name g "
        "--instance-types m5.large --scaling-config minSize=1,maxSize=30,desiredSize=10")
    assert est["count"] == 10
    assert est["monthly_usd"] == _monthly(EC2_HOURLY["m5.large"], 10)


@pytest.mark.parametrize("cmd", [
    "aws autoscaling set-desired-capacity --auto-scaling-group-name gpu --desired-capacity 100",
    "aws eks update-nodegroup-config --cluster-name c --nodegroup-name g "
    "--scaling-config desiredSize=40",
])
def test_a_scale_without_an_instance_type_gets_no_invented_figure(cmd):
    assert g.estimate_command_monthly_cost(cmd) is None


@pytest.mark.parametrize("cmd", [
    "aws autoscaling set-desired-capacity --auto-scaling-group-name gpu --desired-capacity 100",
    "aws rds modify-db-instance --db-instance-identifier db --db-instance-class db.r5.large",
])
def test_strict_mode_and_prod_context_reach_them(cmd, monkeypatch):
    assert g.gate_command(cmd, record=False) is None
    assert g.gate_command(f"{cmd} --profile prod", record=False)["decision"] == "ask"
    monkeypatch.setenv("FINOPS_GUARD_STRICT", "1")
    assert g.gate_command(cmd, record=False)["decision"] == "ask"
