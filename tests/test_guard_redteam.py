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


# ── 3. MCP calls that passed silently ─────────────────────────────────────────

import finops.guard_mcp as gm  # noqa: E402

P4D_X8 = _monthly(EC2_HOURLY["p4d.24xlarge"], 8)


@pytest.mark.parametrize("params", [
    {"InstanceType": "p4d.24xlarge", "MinCount": 8, "MaxCount": 8, "ImageId": "ami-1"},
    {"InstanceType": "p4d.24xlarge", "MinCount": 1, "MaxCount": 8},
    {"instance_type": "p4d.24xlarge", "max_count": 8},
])
def test_use_aws_api_style_parameters_are_priced(params):
    v = g.gate_mcp_call("mcp__q__use_aws", {"service_name": "ec2",
                                           "operation_name": "RunInstances",
                                           "parameters": params, "region": "us-east-1"},
                        record=False)
    assert v is not None and v["decision"] == "ask"
    assert v["monthly_delta_usd"] == P4D_X8


def test_use_aws_parameters_are_kebab_case_flags():
    [act] = gm.translate("mcp__q__use_aws", {
        "service_name": "ec2", "operation_name": "TerminateInstances",
        "parameters": {"InstanceIds": ["i-1"], "DryRun": True}})
    assert act.command == "aws ec2 terminate-instances --instance-ids i-1 --dry-run"


@pytest.mark.parametrize("state", [
    {"InstanceType": "p4d.24xlarge", "ImageId": "ami-1"},
    '{"InstanceType": "p4d.24xlarge", "ImageId": "ami-1"}',
])
def test_a_cloud_control_ec2_instance_is_priced(state):
    v = g.gate_mcp_call("mcp__ccapi__create_resource",
                        {"resource_type": "AWS::EC2::Instance", "desired_state": state},
                        record=False)
    assert v is not None and v["action_type"] == "infra_apply"
    assert v["monthly_delta_usd"] == _monthly(EC2_HOURLY["p4d.24xlarge"])
    assert "AWS::EC2::Instance" in v["reason"] and "Cloud Control" in v["reason"]


def test_a_small_cloud_control_instance_stays_silent():
    assert g.gate_mcp_call("mcp__ccapi__create_resource",
                           {"resource_type": "AWS::EC2::Instance",
                            "desired_state": {"InstanceType": "t3.micro"}},
                           record=False) is None


def test_a_cloud_control_database_is_priced():
    v = g.gate_mcp_call("mcp__ccapi__create_resource", {
        "resource_type": "AWS::RDS::DBInstance",
        "desired_state": {"DBInstanceClass": "db.r5.8xlarge", "Engine": "postgres",
                          "MultiAZ": True}}, record=False)
    assert v is not None and v["decision"] == "ask"
    assert v["estimate"]["count"] == 2


@pytest.mark.parametrize("tool,args,action", [
    ("mcp__k8s-mcp__execute_kubectl", {"command": "kubectl delete namespace prod"},
     "delete_resource"),
    ("mcp__shell__run_command", {"command": "terraform destroy -auto-approve"},
     "delete_resource"),
    ("mcp__desktop-commander__start_process",
     {"command": "aws ec2 terminate-instances --instance-ids i-1", "timeout_ms": 5000},
     "terminate_instance"),
    ("mcp__desktop-commander__start_process", {"command": "helm -n prod uninstall api"},
     "delete_resource"),
    ("mcp__shell__run", {"argv": ["pulumi", "destroy", "--yes"]}, "delete_resource"),
    ("mcp__shell__run", {"command": "cd infra && terraform destroy"}, "delete_resource"),
    ("mcp__shell__run", {"command": "AWS_PROFILE=prod aws s3 rb s3://b --force"},
     "delete_resource"),
    ("mcp__shell__run", {"command": "/usr/local/bin/gcloud projects delete p"},
     "delete_resource"),
    ("mcp__aks__call_kubectl", {"args": "delete deployment api -n prod"}, "delete_resource"),
    ("mcp__shell__run", {"opts": {"cmd": "tofu destroy"}}, "delete_resource"),
])
def test_command_strings_in_any_mcp_tool_are_judged(tool, args, action):
    v = g.gate_mcp_call(tool, args, record=False)
    assert v is not None and v["decision"] == "ask", (tool, args)
    assert v["action_type"] == action


@pytest.mark.parametrize("tool,args", [
    ("mcp__github__create_issue", {"title": "docs", "body": "never run terraform destroy"}),
    ("mcp__shell__run_command", {"command": "ls -la"}),
    ("mcp__shell__run_command", {"command": "kubectl get pods"}),
    ("mcp__notes__save", {"text": "aws is great"}),
    ("mcp__shell__run", {"argv": ["echo", "terraform", "destroy"]}),
])
def test_prose_and_reads_in_mcp_arguments_stay_silent(tool, args):
    assert g.gate_mcp_call(tool, args, record=False) is None


def test_a_manifest_delete_says_what_it_deletes():
    v = g.gate_mcp_call("mcp__kubernetes__kubectl_delete", {
        "manifest": "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: prod\n"},
        record=False)
    assert "delete Namespace prod (from a manifest)" in v["reason"]


def test_a_multi_document_manifest_names_each_object():
    v = g.gate_mcp_call("mcp__kubernetes__kubectl_delete", {
        "manifest": "kind: Deployment\nmetadata:\n  name: api\n  namespace: shop\n---\n"
                    "kind: Service\nmetadata:\n  name: api\n"}, record=False)
    assert "Deployment api in shop" in v["reason"] and "Service api" in v["reason"]


def test_an_unreadable_manifest_still_says_it_is_a_delete():
    v = g.gate_mcp_call("mcp__kubernetes__kubectl_delete", {"manifest": "{{ not yaml"},
                        record=False)
    assert v["decision"] == "ask"
    assert "kubectl delete (from a manifest)" in v["reason"]


# ── 4. false positives: text that only mentions a destroy ─────────────────────

@pytest.mark.parametrize("cmd", [
    'git commit -m "docs: explain terraform destroy"',
    "git commit -am 'never run kubectl delete ns prod'",
    'git -C repo commit -m "why aws ec2 terminate-instances asks"',
    'git tag -a v1 -m "after terraform destroy"',
    'echo "never run aws ec2 terminate-instances"',
    "printf 'kubectl delete ns x\\n'",
    'grep -rn "kubectl delete" docs/',
    "rg 'terraform destroy' docs/",
    'ag "helm uninstall"',
    'egrep "aws s3 rb|aws s3 rm" -r scripts',
    'git commit -m "fix: pulumi destroy" && git push',
    "terraform plan -destroy -out destroy.tfplan",
    "terraform plan -destroy -out=destroy.tfplan",
    "tofu plan -destroy -out destroy",
    "terraform show destroy.tfplan",
    "kubectl apply -f delete.yaml --dry-run=client",
])
def test_text_that_only_mentions_a_destroy_does_not_classify(cmd):
    assert g.classify_command(cmd) in (None, APPLY), cmd
    v = g.gate_command(cmd, record=False)
    assert v is None or v["action_type"] == "infra_apply", (cmd, v)


@pytest.mark.parametrize("cmd,want", [
    # these ARE commands: the quoted text runs
    ('bash -c "terraform destroy -auto-approve"', DELETE),
    ("sh -c 'kubectl delete ns prod'", DELETE),
    ('eval "terraform destroy"', DELETE),
    ('printf "terraform destroy" | sh', DELETE),
    ("echo 'terraform destroy' | bash", DELETE),
    ('echo "$(terraform destroy -auto-approve)"', DELETE),
    ('echo "`kubectl delete ns prod`"', DELETE),
    ('git commit -m "wip" && terraform destroy', DELETE),
    ('echo "done"; kubectl delete ns prod', DELETE),
    ('grep -q x f || aws ec2 terminate-instances --instance-ids i-1', TERMINATE),
    # unquoted text after echo is not masked: over-matching is the safe side
    ("echo never run aws ec2 terminate-instances", TERMINATE),
    ("terraform plan -destroy -out destroy.tfplan && terraform apply -destroy", DELETE),
])
def test_commands_that_run_the_quoted_text_still_classify(cmd, want):
    assert g.classify_command(cmd) == want, cmd


def test_applying_a_plan_file_named_destroy_goes_to_the_saved_plan_reader():
    assert g.classify_command("terraform apply destroy.tfplan") == APPLY


def test_masking_stays_linear():
    for fill in ('echo "a"; ', "git commit -m 'x' ", 'grep "', "'", '"\\"'):
        cmd = "aws ec2 terminate-instances ; " + fill * (MB // len(fill))
        hit, took = _timed(g.classify_command, cmd)
        assert took < 1.0, f"{fill!r}: {took:.2f}s"
        assert hit == TERMINATE


# ── 6. the question a human reads ─────────────────────────────────────────────

@pytest.mark.parametrize("cmd,cwd,says", [
    ("terraform destroy", "/home/dev/infra",
     "This would destroy infrastructure (`terraform destroy` in infra/). "
     "It cannot be undone; confirm to proceed."),
    ("cd envs/prod && terraform destroy -auto-approve", "/home/dev/repo",
     "This would destroy infrastructure (`cd envs/prod && terraform destroy -auto-approve` "
     "in envs/prod/)."),
    ("terraform -chdir=stacks/net destroy", None, "in stacks/net/)"),
    ("aws ec2 terminate-instances --instance-ids i-1", "/x",
     "This would terminate EC2 instances (`aws ec2 terminate-instances --instance-ids i-1`). "
     "It cannot be undone; confirm to proceed."),
    ("kubectl delete ns payments", None,
     "This would delete Kubernetes resources (`kubectl delete ns payments`)."),
    ("helm -n prod uninstall api", None, "This would uninstall a Helm release"),
    ("aws s3 rb s3://b --force", None, "This would delete stored data"),
    ("aws ec2 release-address --allocation-id e", None, "This would release an Elastic IP"),
    ("aws ec2 delete-snapshot --snapshot-id s", None, "This would delete a snapshot"),
    ("aws savingsplans create-savings-plan --savings-plan-offering-id o --commitment 1", None,
     "This would buy a commitment"),
    ("echo ZA== | base64 -d | sh", None,
     "This runs a decoded script the guard cannot read"),
])
def test_an_ask_says_what_the_command_does_and_where(cmd, cwd, says):
    v = g.gate_command(cmd, cwd=cwd, record=False)
    assert says in v["reason"], v["reason"]
    assert "one-way door" not in v["reason"] and "review and apply" not in v["reason"]
    assert v["door"] == "one_way" and v["action_type"]


def test_a_commitment_says_it_cannot_be_cancelled():
    v = g.gate_command("aws savingsplans create-savings-plan --savings-plan-offering-id o "
                       "--commitment 1", record=False)
    assert "cannot be cancelled" in v["reason"]
    assert "$730/mo" in v["reason"], "the figure stays in the same breath"


def test_a_long_command_is_shortened_in_the_question():
    v = g.gate_command("kubectl delete pods " + " ".join(f"p{i}" for i in range(80)),
                       record=False)
    assert "..." in v["reason"] and len(v["reason"]) < 300


def test_an_mcp_ask_names_the_call_without_jargon():
    v = g.gate_mcp_call("mcp__terraform__create_run",
                        {"workspace_name": "net", "run_type": "is_destroy"}, record=False)
    assert "would start a destroy run on HCP Terraform workspace net" in v["reason"]
    assert "It cannot be undone; confirm to proceed." in v["reason"]
    assert "one-way door" not in v["reason"]


def test_the_policy_reason_is_plain_too():
    from finops.policy import evaluate_action_gate
    r = evaluate_action_gate("terminate_instance")
    assert r["gate"] == "escalate" and r["door"] == "one_way"
    assert "one-way door" not in r["reason"] and "cannot be undone" in r["reason"]
