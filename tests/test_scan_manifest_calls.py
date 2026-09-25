"""The dry-run policy is derived from the code, not trusted to match it.

A security reviewer decides whether to hand nable credentials on the strength of
`nable scan --dry-run --json`. It claimed to be exactly what a scan calls, and a
real scan against an IAM user holding only that policy was denied five calls
the manifest did not list (ecs:ListServices, ecs:DescribeTaskDefinition,
ec2:DescribeImages, s3:ListMultipartUploadParts, cloudtrail:GetTrailStatus).
Each denial was swallowed, so the scan reported a clean answer it never read.

These tests read the AWS calls out of the check functions themselves (every
`client.op(...)` and `get_paginator("op")`, followed through the helpers they
call), resolve each one against botocore's own service model, and hold the
manifest to them in both directions: nothing the scan calls is missing, and
nothing the manifest grants goes uncalled.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import botocore.session
import pytest
from botocore import xform_name

from finops.analyzers import cloudwatch, optimizer, waste
from finops.scan_manifest import (
    BASE_ACTIONS,
    COMPUTE_OPTIMIZER_ACTIONS,
    GET_METRIC_DATA_ACTIONS,
    SCAN_CHECKS,
    iam_actions,
)

# The variable a check holds its client in, and the boto3 service behind it.
_CLIENT_SERVICE = {
    "ec2_client": "ec2", "ec2": "ec2",
    "cw_client": "cloudwatch", "cw": "cloudwatch",
    "rds_client": "rds",
    "cloudtrail_client": "cloudtrail",
    "logs_client": "logs",
    "s3_client": "s3",
    "lambda_client": "lambda",
    "elbv2_client": "elbv2",
    "elb_client": "elb",
    "ecr_client": "ecr",
    "ecs_client": "ecs",
    "dynamodb_client": "dynamodb", "ddb_client": "dynamodb",
    "co": "compute-optimizer",
    "sts": "sts", "sts_client": "sts",
}

# IAM names the service differently from boto3.
_IAM_PREFIX = {"elbv2": "elasticloadbalancing", "elb": "elasticloadbalancing"}

# IAM names these operations differently from the API. From the AWS Service
# Authorization Reference; each is an action a reviewer would otherwise look
# for and not find.
_IAM_ACTION = {
    ("s3", "ListBuckets"): "s3:ListAllMyBuckets",
    ("s3", "ListMultipartUploads"): "s3:ListBucketMultipartUploads",
    ("s3", "ListParts"): "s3:ListMultipartUploadParts",
    ("s3", "GetBucketLifecycleConfiguration"): "s3:GetLifecycleConfiguration",
    ("lambda", "ListFunctions"): "lambda:ListFunctions",
}

_BOTO = botocore.session.get_session()


def _iam_action(service: str, snake_op: str) -> str:
    model = _BOTO.get_service_model(service)
    by_snake = {xform_name(op): op for op in model.operation_names}
    assert snake_op in by_snake, f"{service}.{snake_op} is not a real API operation"
    op = by_snake[snake_op]
    if (service, op) in _IAM_ACTION:
        return _IAM_ACTION[(service, op)]
    return f"{_IAM_PREFIX.get(service, service)}:{op}"


def _functions(*modules) -> dict[str, ast.FunctionDef]:
    out: dict[str, ast.FunctionDef] = {}
    for mod in modules:
        tree = ast.parse(Path(inspect.getsourcefile(mod)).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                out.setdefault(node.name, node)
    return out


_FUNCS = _functions(waste, cloudwatch, optimizer)


def _direct_calls(fn: ast.AST) -> tuple[set[tuple[str, str]], set[str]]:
    """(service, snake_op) pairs a function body calls, and the names of other
    functions it calls or hands to a pool."""
    ops: set[tuple[str, str]] = set()
    refs: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id in _FUNCS:
            refs.add(node.id)
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # _co_pages(co, "get_ec2_instance_recommendations")
        if isinstance(func, ast.Name) and func.id == "_co_pages":
            client, op = node.args[0], node.args[1]
            ops.add((_CLIENT_SERVICE[client.id], op.value))
            continue
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        var = func.value.id
        if var not in _CLIENT_SERVICE:
            continue
        if func.attr == "get_paginator":
            ops.add((_CLIENT_SERVICE[var], node.args[0].value))
        else:
            ops.add((_CLIENT_SERVICE[var], func.attr))
    return ops, refs


def _reachable_ops(root: str) -> set[tuple[str, str]]:
    seen: set[str] = set()
    todo = [root]
    ops: set[tuple[str, str]] = set()
    while todo:
        name = todo.pop()
        if name in seen or name not in _FUNCS:
            continue
        seen.add(name)
        direct, refs = _direct_calls(_FUNCS[name])
        ops |= direct
        todo.extend(refs - seen)
    return ops


def _checks_in_audit_region() -> dict[str, str]:
    """check key -> the waste.py function _audit_region runs for it."""
    out: dict[str, str] = {}
    for node in ast.walk(_FUNCS["_audit_region"]):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_run"):
            key = node.args[0].value
            target = node.args[1]
            if isinstance(target, ast.Call):        # partial(check_x, ...)
                target = target.args[0]
            out[key] = target.id
    return out


_CHECK_FUNCS = _checks_in_audit_region()
_OPT_IN = {a for _, a in GET_METRIC_DATA_ACTIONS}


def test_every_check_the_audit_runs_was_found():
    assert set(_CHECK_FUNCS) == set(SCAN_CHECKS)


@pytest.mark.parametrize("check", sorted(SCAN_CHECKS))
def test_every_call_a_check_makes_is_in_its_manifest_entry(check):
    """The five that were missing: a policy built from the manifest was denied
    them, and the scan then reported what it could not read as clean."""
    called = {_iam_action(s, op) for s, op in _reachable_ops(_CHECK_FUNCS[check])}
    listed = {a for _, a in SCAN_CHECKS[check][1]}
    missing = called - listed - _OPT_IN
    assert not missing, f"{check} calls {sorted(missing)} and the manifest does not list them"


@pytest.mark.parametrize("check", sorted(SCAN_CHECKS))
def test_nothing_in_a_manifest_entry_goes_uncalled(check):
    """Over-granting is the other half of the lie. The s3 entry listed
    s3:GetLifecycleConfiguration and the load balancer entry
    elasticloadbalancing:DescribeTargetHealth, and neither check calls them."""
    called = {_iam_action(s, op) for s, op in _reachable_ops(_CHECK_FUNCS[check])}
    listed = {a for _, a in SCAN_CHECKS[check][1]}
    assert listed <= called, f"{check} lists {sorted(listed - called)} but never calls them"


def test_each_manifest_call_name_is_the_call_it_grants():
    """The printed `service.op` beside each action is what the dry run shows a
    reviewer, so it has to resolve to that very action."""
    for _, calls in SCAN_CHECKS.values():
        for call, action in calls + BASE_ACTIONS + COMPUTE_OPTIMIZER_ACTIONS:
            service, op = call.split(".", 1)
            assert _iam_action(service, op) == action, call


def test_compute_optimizer_calls_match_the_manifest():
    called = {_iam_action(s, op)
              for s, op in _reachable_ops("_fetch_compute_optimizer_recommendations")}
    assert called == {a for _, a in COMPUTE_OPTIMIZER_ACTIONS}


def test_the_calls_outside_the_checks_are_base_actions():
    """Region discovery and the identity read the CLI makes before any check."""
    import finops.cli_scan as cs

    called = {_iam_action(s, op) for s, op in _reachable_ops("_discover_regions")}
    tree = ast.parse(inspect.getsource(cs))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "sts"):
            called.add(_iam_action("sts", node.func.attr))
    assert called <= {a for _, a in BASE_ACTIONS}


def test_the_policy_covers_every_call_the_scan_makes():
    everything = set()
    for fn in list(_CHECK_FUNCS.values()) + ["_fetch_compute_optimizer_recommendations",
                                             "_discover_regions"]:
        everything |= {_iam_action(s, op) for s, op in _reachable_ops(fn)}
    assert everything - _OPT_IN <= set(iam_actions(include_get_metric_data=False))


# ── the same claim, on a real boto3 call stream ──────────────────────────────

def test_a_moto_backed_scan_calls_nothing_the_policy_does_not_grant(monkeypatch):
    """The static read above could miss a call made some way it does not
    recognise. This records every operation boto3 actually sends during a
    one-region audit of a seeded account (Fargate service, trail, snapshot,
    multipart upload) and checks each against the policy."""
    moto = pytest.importorskip("moto")
    import boto3

    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
              "AWS_PROFILE", "AWS_ENDPOINT_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    seen: set[str] = set()

    def record(model, **_kw):
        service = model.service_model.service_name
        seen.add(_iam_action(service, xform_name(model.name)))

    with moto.mock_aws():
        r = "us-east-1"
        ec2 = boto3.client("ec2", region_name=r)
        vol = ec2.create_volume(AvailabilityZone="us-east-1a", Size=10)["VolumeId"]
        ec2.create_snapshot(VolumeId=vol)
        s3 = boto3.client("s3", region_name=r)
        s3.create_bucket(Bucket="trail-bucket")
        s3.create_multipart_upload(Bucket="trail-bucket", Key="big.bin")
        boto3.client("cloudtrail", region_name=r).create_trail(
            Name="trail", S3BucketName="trail-bucket")
        ecs = boto3.client("ecs", region_name=r)
        ecs.create_cluster(clusterName="c")
        ecs.register_task_definition(
            family="f", requiresCompatibilities=["FARGATE"], cpu="1024", memory="2048",
            networkMode="awsvpc",
            containerDefinitions=[{"name": "app", "image": "nginx", "memory": 512}])
        ecs.create_service(cluster="c", serviceName="svc", taskDefinition="f",
                           desiredCount=1, launchType="FARGATE")
        boto3.client("dynamodb", region_name=r).create_table(
            TableName="orders", BillingMode="PROVISIONED",
            AttributeDefinitions=[{"AttributeName": "k", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "k", "KeyType": "HASH"}],
            ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5})

        session = boto3.Session(region_name=r)
        session.events.register("before-call", record)
        optimizer._audit_region(session, r, frozenset(SCAN_CHECKS))
        optimizer._fetch_compute_optimizer_recommendations(session)

    # The calls the static test depends on actually happened here.
    for action in ("ec2:DescribeImages", "ecs:ListServices", "ecs:DescribeTaskDefinition",
                   "cloudtrail:GetTrailStatus", "s3:ListBucketMultipartUploads",
                   "dynamodb:ListTables", "dynamodb:DescribeTable"):
        assert action in seen, action
    assert seen - set(iam_actions(include_get_metric_data=False)) == set()
