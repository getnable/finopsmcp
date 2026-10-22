# SPDX-License-Identifier: Apache-2.0
"""The post hook names what an approved priced create made (D149).

The box reconciles an allowed ask against the bill at day 30, and it can only
do that for the resources the call created. So when a person approved a
priced create and the tool ran, the post hook's `ran` line records the
created resource ids the tool printed, as contract 2's `created`:
[{"kind": "ec2_instance" | "rds_instance" | "cache_cluster", "id": "<as printed>"}].

What has to stay true:
  - read from Claude Code's tool_response (aws ec2 run-instances JSON, aws rds
    create-db-instance JSON with DBInstanceArn, aws elasticache
    create-cache-cluster JSON with its ARN), Cursor's output, Copilot's
    toolResult, and terraform's "Creation complete ... [id=...]" lines
  - only ids in the believed forms, of the kinds the ask priced, at most 20,
    each at most 256 characters
  - nothing for a deny, a decline, an unpriced call or a call that was not
    an ask
  - guard_outcome still imports no guard module, and stays on the fast path
"""
from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import finops.guard as g
import finops.guard_ledger as gl
import finops.guard_outcome as go
import finops.guard_plugin as gp
from finops import ai_budget

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EC2 = "aws ec2 run-instances --region us-east-1 --instance-type m5.2xlarge --count 4"
RDS = ("aws rds create-db-instance --region us-east-1 --db-instance-identifier orders "
       "--engine postgres --db-instance-class db.r5.4xlarge")
CACHE = ("aws elasticache create-cache-cluster --region us-east-1 --cache-cluster-id sess "
         "--engine redis --cache-node-type cache.r6g.xlarge --num-cache-nodes 3")
I1, I2 = "i-0123456789abcdef0", "i-0fedcba9876543210"
RDS_ARN = "arn:aws:rds:us-east-1:111111111111:db:orders"
CACHE_ARN = "arn:aws:elasticache:us-east-1:111111111111:cluster:sess"


@pytest.fixture(autouse=True)
def _machine(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "FINOPS_DATA_DIR", "FINOPS_PROFILE",
                "FINOPS_GUARD", "FINOPS_GUARD_STRICT", "FINOPS_POLICY_ALLOWED_ACTIONS",
                "FINOPS_POLICY_MAX_AUTO_USD", "FINOPS_GUARD_PROD_PATTERNS", "FINOPS_GUARD_TEAM",
                "FINOPS_POLICY_VELOCITY_CAP_USD", "FINOPS_POLICY_LOOP_COUNT",
                "FINOPS_POLICY_FILE", "FINOPS_GUARD_ACCOUNT", "CURSOR_ADMIN_API_KEY",
                "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setattr(gp, "_data_root_override", None)
    monkeypatch.setattr(gp, "_user_dir_override", None)
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: None)
    monkeypatch.setattr(g, "check_budget_gate", lambda *a, **k: None)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


def _hook(payload: dict, *, post: bool = False, harness: str | None = None) -> str:
    out = io.StringIO()
    assert gp.run_hook(harness, "plugin", io.StringIO(json.dumps(payload)), out,
                       io.StringIO(), post=post) == 0
    return out.getvalue()


def _pre(command: str, tuid: str = "toolu_1") -> dict:
    out = _hook({"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash",
                 "tool_input": {"command": command}, "tool_use_id": tuid,
                 "cwd": str(Path.cwd())})
    return json.loads(out) if out else {}


def _post(command: str, response, tuid: str = "toolu_1") -> None:
    _hook({"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash",
           "tool_input": {"command": command}, "tool_use_id": tuid,
           "tool_response": response}, post=True)


def _outcomes() -> list[dict]:
    return [r for r in gl.read(outcomes=True) if gl.is_outcome(r)]


def _run_instances(*ids: str) -> str:
    return json.dumps({"Groups": [], "Instances": [
        {"AmiLaunchIndex": n, "ImageId": "ami-0abcdef1234567890", "InstanceId": i,
         "InstanceType": "m5.2xlarge", "State": {"Code": 0, "Name": "pending"},
         "NetworkInterfaces": [{"Attachment": {"AttachmentId": "eni-attach-0abc",
                                               "DeviceIndex": 0}}]}
        for n, i in enumerate(ids)], "OwnerId": "111111111111", "ReservationId": "r-0abc"},
        indent=4)


# ── what is read, from each harness ──────────────────────────────────────────

def test_an_approved_run_instances_records_its_instance_ids():
    assert _pre(EC2)["hookSpecificOutput"]["permissionDecision"] == "ask"
    _post(EC2, {"stdout": _run_instances(I1, I2), "stderr": "", "interrupted": False})
    [out] = _outcomes()
    assert out["created"] == [{"kind": "ec2_instance", "id": I1},
                              {"kind": "ec2_instance", "id": I2}]


def test_an_approved_create_db_instance_records_its_arn():
    _pre(RDS)
    body = {"DBInstance": {"DBInstanceIdentifier": "orders", "DBInstanceClass": "db.r5.4xlarge",
                           "Engine": "postgres", "DBInstanceStatus": "creating",
                           "DbiResourceId": "db-" + "A" * 26,
                           "DBInstanceArn": RDS_ARN}}
    _post(RDS, {"stdout": json.dumps(body, indent=4), "stderr": ""})
    assert _outcomes()[0]["created"] == [{"kind": "rds_instance", "id": RDS_ARN}]


def test_an_approved_create_cache_cluster_records_its_arn():
    _pre(CACHE)
    body = {"CacheCluster": {"CacheClusterId": "sess", "CacheNodeType": "cache.r6g.xlarge",
                             "Engine": "redis", "CacheClusterStatus": "creating",
                             "NumCacheNodes": 3, "ARN": CACHE_ARN}}
    _post(CACHE, {"stdout": json.dumps(body, indent=4), "stderr": ""})
    assert _outcomes()[0]["created"] == [{"kind": "cache_cluster", "id": CACHE_ARN}]


def test_a_tool_response_sent_as_text_is_read_too():
    _pre(EC2)
    _post(EC2, _run_instances(I1))
    assert _outcomes()[0]["created"] == [{"kind": "ec2_instance", "id": I1}]


def test_cursors_output_is_read():
    g.gate_command(EC2, session_id=ai_budget.NO_SESSION, harness="cursor", cwd=None,
                   tool="shell")
    _hook({"hook_event_name": "afterShellExecution", "conversation_id": "c1", "command": EC2,
           "output": _run_instances(I1), "duration": 900}, post=True)
    [out] = _outcomes()
    assert out["harness"] == "cursor" and out["created"] == [{"kind": "ec2_instance", "id": I1}]


def test_copilots_tool_result_is_read():
    g.gate_command(EC2, session_id="cop-1", harness="copilot", cwd=None, tool="bash")
    _hook({"sessionId": "cop-1", "timestamp": 1, "cwd": "/w", "toolName": "bash",
           "toolArgs": json.dumps({"command": EC2}),
           "toolResult": {"resultType": "success", "textResultForLlm": _run_instances(I2)}},
          post=True)
    [out] = _outcomes()
    assert out["harness"] == "copilot" and out["created"] == [{"kind": "ec2_instance", "id": I2}]


TF_OUT = f"""aws_instance.old: Refreshing state... [id=i-0aaaaaaaaaaaaaaaa]
aws_instance.app: Creating...
aws_instance.app: Still creating... [10s elapsed]
aws_instance.app: Creation complete after 12s [id={I1}]
aws_db_instance.db: Creation complete after 4m2s [id=db-ABCDEFGHIJKLMNOPQRSTUVWXY]
aws_instance.web: Modifications complete after 1s [id={I2}]

Apply complete! Resources: 2 added, 1 changed, 0 destroyed.
"""


def test_terraforms_creation_lines_give_the_ids_in_the_believed_forms(tmp_path, monkeypatch):
    doc = {"format_version": "1.2", "configuration": {"provider_config": {"aws": {
        "name": "aws", "expressions": {"region": {"constant_value": "us-east-1"}}}}},
        "resource_changes": [
            {"address": "aws_instance.app", "type": "aws_instance", "mode": "managed",
             "change": {"actions": ["create"], "before": None,
                        "after": {"instance_type": "p4d.24xlarge"}}}]}
    (tmp_path / "plan.out").write_bytes(b"x")
    monkeypatch.setattr(g, "_plan_read", lambda *a, **k: ("terraform", "plan.out", doc))
    cmd = "terraform apply plan.out"
    assert _pre(cmd)["hookSpecificOutput"]["permissionDecision"] == "ask"
    _post(cmd, {"stdout": TF_OUT, "stderr": ""})
    # Only the creation, and only an id in a believed form: the refresh and
    # the modification name resources that already existed, and the
    # database's DbiResourceId is not the ARN its bill lines carry.
    assert _outcomes()[0]["created"] == [{"kind": "ec2_instance", "id": I1}]


# ── only believed forms, bounded ─────────────────────────────────────────────

def test_at_most_20_ids_are_kept():
    ids = [f"i-{n:017x}" for n in range(25)]
    _pre(EC2)
    _post(EC2, {"stdout": _run_instances(*ids)})
    created = _outcomes()[0]["created"]
    assert len(created) == go.CREATED_MAX == 20 and created[0]["id"] == ids[0]


@pytest.mark.parametrize("bad", ["i-XYZ", "i-0123", "i-0123456789abcdef0; rm -rf /",
                                 "arn:aws:rds:us-east-1:1:db:orders", "x" * 300,
                                 "i-0123456789ABCDEF0"])
def test_an_id_not_in_a_believed_form_is_not_kept(bad):
    _pre(EC2)
    _post(EC2, {"stdout": json.dumps({"Instances": [{"InstanceId": bad}]})})
    [out] = _outcomes()
    assert "created" not in out


def test_an_id_of_a_kind_the_ask_did_not_price_is_not_kept():
    _pre(EC2)
    _post(EC2, {"stdout": json.dumps({"Instances": [{"InstanceId": I1}],
                                      "DBInstance": {"DBInstanceArn": RDS_ARN}})})
    assert _outcomes()[0]["created"] == [{"kind": "ec2_instance", "id": I1}]


def test_the_forms_are_bounded():
    assert all(len(x) <= go.ID_MAX for x in (RDS_ARN, CACHE_ARN, I1))
    assert go.created_ids("i-0123456789abcdef0" * 20, kinds=("ec2_instance",)) == []


# ── nothing for anything else ────────────────────────────────────────────────

def test_nothing_is_recorded_for_a_deny_a_decline_or_an_unpriced_call(monkeypatch):
    # A deny never runs: its post hook finds no ask to answer.
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "read_only")
    _pre(EC2, "toolu_d")
    _post(EC2, {"stdout": _run_instances(I1)}, "toolu_d")
    monkeypatch.delenv("FINOPS_POLICY_ALLOWED_ACTIONS")
    # A decline never runs: no post hook at all.
    _pre(EC2, "toolu_x")
    # An unpriced ask: a one-way door, approved and run.
    _pre("terraform destroy", "toolu_u")
    _post("terraform destroy", {"stdout": f"aws_instance.app: Destruction complete [id={I1}]"},
          "toolu_u")
    # A call that was never asked about.
    _post("aws ec2 describe-instances", {"stdout": _run_instances(I2)}, "toolu_n")
    assert all("created" not in o for o in _outcomes())
    assert not any(I1 in json.dumps(r) or I2 in json.dumps(r) for r in gl.read(outcomes=True))


def test_a_create_that_printed_no_id_records_none():
    _pre(EC2)
    _post(EC2, {"stdout": "", "stderr": "An error occurred (InsufficientInstanceCapacity)"})
    [out] = _outcomes()
    assert out["outcome"] == "ran" and "created" not in out


def test_the_chain_still_verifies_with_created_ids():
    _pre(EC2)
    _post(EC2, {"stdout": _run_instances(I1)})
    assert gl.verify()["ok"] is True


# ── the fast path ────────────────────────────────────────────────────────────

def test_guard_outcome_imports_no_guard_module():
    tree = ast.parse((SRC / "finops" / "guard_outcome.py").read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "guard" not in names and "guard_adapters" not in names
    assert not any(m and m.endswith(("finops.guard", ".guard")) for m in mods)


def test_the_post_hook_of_a_priced_create_never_imports_the_guard(tmp_path):
    code = ("import io, json, sys\n"
            "from finops import guard_plugin\n"
            "guard_plugin.hook_main(['--via', 'plugin', '--post'])\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith('finops'))))\n")
    env = {**os.environ, "HOME": str(tmp_path), "PYTHONPATH": str(SRC)}
    env.pop("FINOPS_DATA_DIR", None)
    r = subprocess.run([sys.executable, "-c", code], input=json.dumps(
        {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Bash",
         "tool_input": {"command": EC2}, "tool_use_id": "t1",
         "tool_response": {"stdout": _run_instances(I1)}}), capture_output=True, text=True,
        env=env, check=False, timeout=60)
    assert r.returncode == 0, r.stderr
    mods = set(json.loads(r.stdout.strip().splitlines()[-1]))
    assert "finops.guard" not in mods and "finops.guard_adapters" not in mods
