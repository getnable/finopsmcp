"""A rule's program and verb must sit in one shell command that runs.

Read across the whole line, `terraform plan | grep destroy` was a destroy and
`kubectl get events | grep -i delete` a delete, and a dry run (`aws ec2
terminate-instances --dry-run`, `kubectl delete --dry-run=client`, `helm
uninstall --dry-run`) or a permission check (`kubectl auth can-i delete
pods`) asked to confirm a change nobody was making. These pin both halves:
the false alarms are gone, and nothing that runs got through with them.
"""
from __future__ import annotations

import time

import pytest

import finops.guard as g

DOOR = ("one_way", "delete_resource")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("cmd", [
    "terraform plan -no-color | grep destroy",
    'terraform plan -no-color | grep -E "destroy|replace"',
    "kubectl get events -A | grep -i delete",
    "kubectl logs deploy/api -n staging | grep -i drain",
    "helm history api -n staging | grep uninstall",
    "aws ec2 terminate-instances --instance-ids i-1 --dry-run",
    "aws s3 rm s3://b/tmp/ --recursive --dryrun",
    "aws s3 sync ./dist s3://b --delete --dryrun",
    "kubectl delete ns x --dry-run=client -o yaml",
    "kubectl delete ns x --dry-run=server",
    "kubectl delete ns x --dry-run",
    "kubectl drain node-1 --ignore-daemonsets --dry-run=client",
    "helm uninstall api -n staging --dry-run",
    "kubectl auth can-i delete pods -n prod",
    "kubectl --context prod auth can-i delete namespaces",
    "helm plugin uninstall diff",
    "helm diff upgrade api ./chart",
    "kubectl apply -f - <<EOF\nkind: ConfigMap\nmetadata:\n  name: delete-me\nEOF",
])
def test_no_change_is_not_a_door(cmd):
    hit = g.classify_command(cmd)
    assert hit is None or hit[0] != "one_way", (cmd, hit)


@pytest.mark.parametrize("cmd", [
    # The real command after the dry run, in the same line or a nested shell.
    "kubectl delete ns x --dry-run=client && kubectl delete ns x",
    ("aws ec2 terminate-instances --dry-run --instance-ids i-1; "
    "aws ec2 terminate-instances --instance-ids i-1"),
    "bash -c 'kubectl delete ns x --dry-run=client; kubectl delete ns x'",
    "bash -c 'kubectl auth can-i list pods; kubectl delete ns x'",
    # A dry run switched off again, or a flag that is an argument after `--`.
    "kubectl delete ns x --dry-run=client --dry-run=none",
    "aws ec2 terminate-instances --instance-ids i-1 --dry-run --no-dry-run",
    "kubectl delete ns x -- --dry-run",
    # Two CLIs in one command: whose flag it is cannot be told.
    "kubectl delete ns x --as helm --dry-run",
    # A dry-run flag the service does not have runs nothing, but the guard
    # only trusts the documented ones.
    "aws rds delete-db-instance --db-instance-identifier x --dry-run",
    # A release named like an inert subcommand, and a flag before it.
    "helm uninstall diff",
    "helm --debug uninstall plugin",
    "helm -n prod uninstall template",
    "kubectl --insecure-skip-tls-verify delete ns prod auth can-i",
    # A substitution can carry the verb across a `;`: judged whole.
    "terraform $(echo plan; echo destroy)",
    # The heuristics that read across a pipe by design.
    "echo ZGVzdHJveQ== | base64 -d | sh",
    "python3 -c \"import boto3; boto3.client('ec2').terminate_instances(InstanceIds=['i-1'])\"",
    "TF_CLI_ARGS_apply=-destroy; terraform apply",
])
def test_a_change_that_runs_still_asks(cmd):
    hit = g.classify_command(cmd)
    assert hit is not None and hit[0] == "one_way", (cmd, hit)


def test_a_dry_run_is_not_priced_but_the_launch_beside_it_is():
    cmd = ("aws ec2 run-instances --dry-run --instance-type p4d.24xlarge --count 8 "
           "--image-id ami-1 && aws ec2 run-instances --instance-type t3.micro --image-id ami-1")
    est = g.estimate_command_monthly_cost(cmd)
    assert est["instance_type"] == "t3.micro"
    assert est["monthly_usd"] == pytest.approx(0.0104 * 730, abs=0.01)


def test_a_dry_run_launch_is_silent():
    assert g.gate_command("aws ec2 run-instances --dry-run --instance-type p4d.24xlarge "
                          "--count 8 --image-id ami-1", record=False) is None


@pytest.mark.parametrize("fill", ["kubectl ;", "kubectl get x | grep delete ; ",
                                  "helm diff upgrade x ; ", "kubectl delete x --dry-run ; ",
                                  "echo 'a;b' | "])
def test_segment_reading_stays_linear(fill):
    cmd = "aws ec2 terminate-instances --instance-ids i-1 ; " + fill * ((256 << 10) // len(fill))
    t = time.perf_counter()
    hit = g.classify_command(cmd)
    assert time.perf_counter() - t < 1.0
    assert hit == ("one_way", "terminate_instance")
