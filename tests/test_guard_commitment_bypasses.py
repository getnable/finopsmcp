# SPDX-License-Identifier: Apache-2.0
"""Ways around the commitment ask, each closed.

The guard asks before a commitment purchase it can read. These are the
purchases it could not read, or read and then let through:

  - a Terraform, OpenTofu or Terragrunt apply, or a `pulumi up`, that
    creates a resource which buys a reservation (aws_rds_reserved_instance,
    aws_elasticache_reserved_cache_node, aws_ec2_capacity_block_reservation,
    and the Pulumi equivalents), from a saved plan or from the configuration
    in the directory it runs in
  - an EC2 purchase that says `--dry-run` somewhere the CLI never reads it:
    in a shell comment, inside another option's quoted value, inside a
    use_aws parameter value
  - an MCP shell tool that takes the program in `command` and the rest of
    argv in `args`
  - the AWS CLI run as a Python module, a boto3 purchase fed to Python on
    stdin or through combined flags, a botocore client, `_make_api_call`
    with the operation's own name, and an aws command whose service or verb
    is a variable or a substitution

HOME, the data directory and every directory read are under tmp_path, and
nothing here runs terraform, pulumi or any tool on PATH.
"""
from __future__ import annotations

import io
import json

import pytest

import finops.guard as g
from finops import ai_budget, guard_adapters

RDS_RI = """
resource "aws_rds_reserved_instance" "db" {
  offering_id    = "438012d3-4052-4cc7-b2e3-8d3372e0e706"
  reservation_id = "optionalCustomReservationID"
  instance_count = 3
}
"""


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_POLICY_LOOP_COUNT", "FINOPS_GUARD_TEAM", "FINOPS_GUARD_ACCOUNT",
                "FINOPS_POLICY_ON_BUDGET_BREACH", "FINOPS_POLICY_FILE", "TERRAFORM_BIN",
                *guard_adapters._COPILOT_CLOUD_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    # Nothing is run: a saved plan is whatever the test says it is.
    monkeypatch.setattr(g.shutil if hasattr(g, "shutil") else __import__("shutil"),
                        "which", lambda *_a, **_k: None)


def _asks_to_buy(v) -> bool:
    return (v is not None and v["decision"] == "ask"
            and v["action_type"] == "purchase_commitment")


def _hook(command: str, cwd=None) -> dict:
    out = io.StringIO()
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    assert g.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
    text = out.getvalue()
    return json.loads(text)["hookSpecificOutput"] if text.strip() else {}


# ── Terraform, OpenTofu, Terragrunt, Pulumi ───────────────────────────────────

@pytest.fixture
def infra(tmp_path):
    d = tmp_path / "infra"
    d.mkdir()
    return d


@pytest.mark.parametrize("resource", ["aws_rds_reserved_instance",
                                      "aws_elasticache_reserved_cache_node",
                                      "aws_ec2_capacity_block_reservation"])
@pytest.mark.parametrize("command", ["terraform apply -auto-approve", "tofu apply -auto-approve",
                                     "terraform -chdir=. apply", "terragrunt apply"])
def test_an_apply_whose_configuration_buys_a_reservation_asks(infra, resource, command):
    (infra / "main.tf").write_text(RDS_RI.replace("aws_rds_reserved_instance", resource))
    v = g.gate_command(command, cwd=str(infra), record=False)
    assert _asks_to_buy(v), v
    assert resource in v["reason"] and "main.tf" in v["reason"]


def test_the_same_apply_without_one_says_nothing(infra):
    (infra / "main.tf").write_text('resource "aws_s3_bucket" "b" { bucket = "x" }\n'
                                   'data "aws_rds_reserved_instance_offering" "o" {}\n')
    assert g.gate_command("terraform apply -auto-approve", cwd=str(infra), record=False) is None


def test_a_reservation_in_a_module_or_json_counts_and_the_cd_is_followed(tmp_path, infra):
    mod = infra / ".terraform" / "modules" / "db"
    mod.mkdir(parents=True)
    (mod / "ri.tf.json").write_text(json.dumps(
        {"resource": {"aws_rds_reserved_instance": {"db": {"offering_id": "o-1"}}}}))
    (infra / "main.tf").write_text('module "db" { source = "./db" }\n')
    v = g.gate_command("cd infra && terraform apply -auto-approve", cwd=str(tmp_path),
                       record=False)
    assert _asks_to_buy(v), v
    d = _hook("terraform apply -auto-approve", cwd=infra)
    assert d.get("permissionDecision") == "ask" and "buy a commitment" in \
        d["permissionDecisionReason"]


def test_a_saved_plan_that_creates_a_reservation_asks(monkeypatch, infra):
    doc = {"resource_changes": [
        {"address": "aws_instance.web", "type": "aws_instance",
         "change": {"actions": ["no-op"]}},
        {"address": "aws_elasticache_reserved_cache_node.cache",
         "type": "aws_elasticache_reserved_cache_node", "change": {"actions": ["create"]}}]}
    monkeypatch.setattr(g, "_plan_read", lambda cmd, cwd, start=0: ("terraform", "plan.out", doc)
                        if "apply" in cmd else None)
    v = g.gate_command("terraform apply plan.out", cwd=str(infra), record=False)
    assert _asks_to_buy(v), v
    assert "aws_elasticache_reserved_cache_node.cache" in v["reason"]


def test_a_saved_plan_that_buys_nothing_is_judged_by_the_plan_not_the_files(monkeypatch, infra):
    """The reservation is already bought (in state, no create in the plan)."""
    (infra / "main.tf").write_text(RDS_RI)
    doc = {"resource_changes": [
        {"address": "aws_rds_reserved_instance.db", "type": "aws_rds_reserved_instance",
         "change": {"actions": ["no-op"]}}]}
    monkeypatch.setattr(g, "_plan_read", lambda cmd, cwd, start=0: ("terraform", "plan.out", doc)
                        if "apply" in cmd else None)
    assert g.gate_command("terraform apply plan.out", cwd=str(infra), record=False) is None


@pytest.mark.parametrize("name,source", [
    ("index.ts", 'const ri = new aws.rds.ReservedInstance("ri", {offeringId: "o-1"});'),
    ("__main__.py", 'ri = aws.elasticache.ReservedCacheNode("ri", offering_id="o-1")'),
    ("main.go", 'ri, err := ec2.NewCapacityBlockReservation(ctx, "cb", &ec2.Args{})'),
    ("Pulumi.yaml", "resources:\n  ri:\n    type: aws:rds/reservedInstance:ReservedInstance\n"),
])
def test_pulumi_up_whose_program_buys_a_reservation_asks(infra, name, source):
    (infra / "Pulumi.yaml").write_text("name: x\nruntime: nodejs\n")
    (infra / name).write_text(source)
    v = g.gate_command("pulumi up --yes", cwd=str(infra), record=False)
    assert _asks_to_buy(v), v


def test_pulumi_reads_the_program_not_its_dependencies(infra):
    (infra / "Pulumi.yaml").write_text("name: x\nruntime: nodejs\n")
    sdk = infra / "node_modules" / "@pulumi" / "aws" / "rds"
    sdk.mkdir(parents=True)
    (sdk / "reservedInstance.ts").write_text("export class ReservedInstance {}  // rds.ReservedInstance")
    (infra / "index.ts").write_text('new aws.s3.Bucket("b");')
    assert g.gate_command("pulumi up --yes", cwd=str(infra), record=False) is None


def test_a_local_module_counts_and_a_sibling_stack_does_not(tmp_path, infra):
    (infra / "modules" / "db").mkdir(parents=True)
    (infra / "modules" / "db" / "main.tf").write_text(RDS_RI)
    (infra / "main.tf").write_text('module "db" {\n  source = "./modules/db"\n}\n')
    v = g.gate_command("terraform apply -auto-approve", cwd=str(infra), record=False)
    assert _asks_to_buy(v) and "modules/db/main.tf" in v["reason"], v
    other = tmp_path / "other"
    (other / "reservations").mkdir(parents=True)
    (other / "reservations" / "main.tf").write_text(RDS_RI)
    (other / "main.tf").write_text('resource "aws_s3_bucket" "b" {}\n')
    assert g.gate_command("terraform apply -auto-approve", cwd=str(other), record=False) is None


def test_pulumi_up_outside_a_project_reads_nothing(infra):
    (infra / "index.ts").write_text('new aws.rds.ReservedInstance("ri", {});')
    assert g.gate_command("pulumi up --yes", cwd=str(infra), record=False) is None


def test_a_directory_too_large_to_read_asks(monkeypatch, infra):
    monkeypatch.setattr(g, "_IAC_SCAN_MAX_FILES", 3)
    for i in range(5):
        (infra / f"f{i}.tf").write_text("# nothing\n")
    v = g.gate_command("terraform apply -auto-approve", cwd=str(infra), record=False)
    assert _asks_to_buy(v), v
    assert "could not read" in v["reason"]


# ── a dry run the CLI never reads ─────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    ("aws ec2 purchase-reserved-instances-offering --reserved-instances-offering-id o-1 "
     "--instance-count 2 # --dry-run"),
    ("aws ec2 purchase-host-reservation --offering-id hro-1 --host-id-set h-1 "
     "--client-token 'x --dry-run'"),
    ("aws ec2 purchase-capacity-block --capacity-block-offering-id c "
     "--instance-platform Linux/UNIX --client-token \"a --dry-run b\""),
    ("aws ec2 purchase-scheduled-instances --purchase-requests PurchaseToken=t,InstanceCount=1 "
     "--client-token=--dry-run"),
])
def test_a_dry_run_the_cli_never_reads_does_not_excuse_a_purchase(command):
    assert _asks_to_buy(g.gate_command(command, record=False)), command
    assert _hook(command).get("permissionDecision") == "ask"
    v = g.gate_mcp_call("mcp__aws-api__call_aws", {"cli_command": command}, record=False)
    assert _asks_to_buy(v), command


@pytest.mark.parametrize("command", [
    ("aws ec2 purchase-capacity-block --capacity-block-offering-id c "
     "--instance-platform Linux/UNIX --dry-run"),
    ("aws --profile prod ec2 purchase-reserved-instances-offering "
     "--reserved-instances-offering-id o-1 --instance-count 2 --dry-run # check first"),
])
def test_a_real_dry_run_still_buys_nothing(command):
    assert g.gate_command(command, record=False) is None


def test_use_aws_counts_a_dry_run_only_from_dry_run_true():
    base = {"service_name": "ec2", "operation_name": "PurchaseHostReservation",
            "region": "us-east-1"}
    for params in ({"OfferingId": "hro-1", "HostIdSet": ["h-1"], "ClientToken": "x --dry-run"},
                   {"OfferingId": "hro-1", "HostIdSet": ["h-1 --dry-run"]},
                   {"OfferingId": "hro-1", "DryRun": False}):
        v = g.gate_mcp_call("mcp__q__use_aws", {**base, "parameters": params}, record=False)
        assert _asks_to_buy(v), params
    v = g.gate_mcp_call("mcp__q__use_aws", {**base, "parameters": {
        "OfferingId": "hro-1", "HostIdSet": ["h-1"], "DryRun": True}}, record=False)
    assert v is None, v


# ── an MCP shell tool with the program and argv apart ─────────────────────────

RDS_ARGV = ["rds", "purchase-reserved-db-instances-offering",
            "--reserved-db-instances-offering-id", "o-1", "--db-instance-count", "2"]


@pytest.mark.parametrize("key", ["args", "argv", "arguments"])
def test_a_command_split_from_its_argv_is_judged(key):
    v = g.gate_mcp_call("mcp__shell__run", {"command": "aws", key: RDS_ARGV}, record=False)
    assert _asks_to_buy(v), v
    v = g.gate_mcp_call("mcp__shell__run", {"cmd": "/usr/local/bin/aws", key: RDS_ARGV},
                        record=False)
    assert _asks_to_buy(v), v


def test_the_joined_shape_still_asks_and_a_lone_name_still_says_nothing():
    v = g.gate_mcp_call("mcp__shell__run", {"command": ["aws", *RDS_ARGV]}, record=False)
    assert _asks_to_buy(v), v
    assert g.gate_mcp_call("mcp__shell__run", {"command": "aws",
                                               "args": ["s3", "ls"]}, record=False) is None


# ── the CLI and the SDK by other names ────────────────────────────────────────

RDS_BUY = ("rds purchase-reserved-db-instances-offering --reserved-db-instances-offering-id o-1 "
           "--db-instance-count 2")
BUY_CODE = ("import boto3; boto3.client('rds').purchase_reserved_db_instances_offering("
            "ReservedDBInstancesOfferingId='o-1', DBInstanceCount=1)")


@pytest.mark.parametrize("command", [
    f"python3 -m awscli {RDS_BUY}",
    f"python -m awscli --profile prod {RDS_BUY}",
    f"/usr/bin/python3.12 -m awscli {RDS_BUY}",
    f'python3 -Ic "{BUY_CODE}"',
    f'python3 -u -c "{BUY_CODE}"',
    f"python3 - <<'EOF'\nimport boto3\n{BUY_CODE.split('; ', 1)[1]}\nEOF",
    f"python3 <<EOF\n{BUY_CODE}\nEOF",
    f'echo "{BUY_CODE}" | python3',
    f'printf "%s" "{BUY_CODE}" | python3 -',
    ('python3 -c "import botocore.session; botocore.session.get_session().create_client(\'rds\')'
     '.purchase_reserved_db_instances_offering(ReservedDBInstancesOfferingId=\'o-1\')"'),
    ('python3 -c "import boto3; boto3.client(\'rds\')._make_api_call('
     '\'PurchaseReservedDBInstancesOffering\', {\'ReservedDBInstancesOfferingId\': \'o-1\'})"'),
])
def test_the_cli_and_sdk_by_other_names_ask(command):
    assert _asks_to_buy(g.gate_command(command, record=False)), command


@pytest.mark.parametrize("command", [
    "aws rds $VERB --reserved-db-instances-offering-id o-1",
    "aws $SERVICE purchase-reserved-db-instances-offering --reserved-db-instances-offering-id o",
    "aws rds $(echo purchase-reserved-db-instances-offering) --reserved-db-instances-offering-id o",
    'aws rds "${VERB}" --reserved-db-instances-offering-id o-1',
])
def test_an_aws_operation_the_guard_cannot_read_asks(command):
    v = g.gate_command(command, record=False)
    assert v is not None and v["decision"] == "ask", command
    assert "cannot read" in v["reason"]


# The operation, or what makes it a commitment, from what xargs, parallel or
# find hands it: `{}` (or the -I string) where the verb goes, a bare `aws ec2`
# that xargs ends with stdin's words, or a term-only purchase whose flags come
# from stdin. The guard never sees those words.
XARGS_FED = [
    "echo create-capacity-reservation | xargs -I{} aws ec2 {} --commitment-duration 31536000",
    "echo terminate-instances | xargs -I{} aws ec2 {} --instance-ids i-123",
    "echo terminate-instances | /usr/bin/xargs -I{} /usr/local/bin/aws ec2 {} --instance-ids i-1",
    "echo rb | xargs -I{} aws s3 {} s3://bucket --force",
    "echo purchase-reserved-db-instances-offering | xargs -I % aws rds % --db-instance-count 2",
    "echo purchase-reserved-db-instances-offering | xargs -i aws rds {} --db-instance-count 2",
    "printf 'terminate-instances\\n' | xargs -I verb aws ec2 verb --instance-ids i-1",
    "printf 'terminate-instances\\n' | xargs -0 -n 1 -I '{}' sh -c 'aws ec2 {} --instance-ids i-1'",
    "echo purchase-reserved-db-instances-offering --db-instance-count 2 | xargs aws rds",
    "echo rds purchase-reserved-db-instances-offering | xargs -n 2 aws",
    "parallel aws ec2 {} --instance-ids i-1 ::: terminate-instances",
    "find terminate-instances -maxdepth 0 -exec aws ec2 {} --instance-ids i-1 \\;",
    ("echo --commitment-duration 31536000 | xargs aws ec2 create-capacity-reservation "
     "--instance-type t3.micro --instance-platform Linux/UNIX --availability-zone us-east-1a "
     "--instance-count 1"),
    ("echo --commitment-duration=31536000 | xargs -I{} aws ec2 create-capacity-reservation "
     "--instance-type t3.micro --instance-platform Linux/UNIX --availability-zone us-east-1a "
     "--instance-count 1 {}"),
]


@pytest.mark.parametrize("command", XARGS_FED)
def test_an_aws_operation_xargs_fills_in_asks(command):
    v = g.gate_command(command, record=False)
    assert v is not None and v["decision"] == "ask", command
    assert "cannot read" in v["reason"]
    assert _hook(command).get("permissionDecision") == "ask"


@pytest.mark.parametrize("command", [
    "aws s3 cp $SRC s3://bucket/key",
    "aws ec2 describe-instances --instance-ids $ID",
    "python3 -m awscli s3 ls",
    'echo "import boto3; print(boto3.client(\'rds\').describe_reserved_db_instances())" | python3',
    "python3 -m pip install boto3",
    "aws s3 ls s3://b | xargs -I{} aws s3 cp s3://b/{} .",
    "ls | xargs -I{} aws ec2 describe-instances --instance-ids {}",
    "echo i-1 | xargs aws ec2 describe-instances --instance-ids",
    "find . -name '*.tf' | xargs grep -l aws",
    "find . -name '*.tf' | xargs -n 1 grep -c aws",
    "echo aws | xargs -I{} echo aws {}",
    "find . -name '*.json' -exec aws s3 cp {} s3://b/ \\;",
    "find . -name '*.json' -exec grep -l aws {} +",
])
def test_reads_by_those_names_still_say_nothing(command):
    assert g.gate_command(command, record=False) is None, command


@pytest.mark.parametrize("command", [
    "aws rds $VERB --reserved-db-instances-offering-id o-1",
    XARGS_FED[0],
])
def test_an_unreadable_aws_operation_asks_under_the_loosest_policy_and_says_why(monkeypatch,
                                                                                 command):
    """It may be a purchase, so the commitment floor holds for it; it may not
    be one, so the ask does not say it buys a commitment."""
    from finops import policy
    monkeypatch.setitem(policy.DEFAULT_POLICY, "escalate_one_way_doors", False)
    monkeypatch.setenv("FINOPS_POLICY_MAX_AUTO_USD", "1000000000")
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "0")
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS",
                       ",".join([*policy.TWO_WAY_DOORS, "delete_resource",
                                 "purchase_commitment"]))
    v = g.gate_command(command, record=False)
    assert v is not None and v["decision"] == "ask", v
    assert "cannot read" in v["reason"] and "would buy a commitment" not in v["reason"]
