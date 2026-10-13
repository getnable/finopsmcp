"""What cleanup/idle.py may call idle, and what it may say about how long.

list_idle_resources and cleanup_idle_resources (which drafts a delete command
for every row) both read this scanner, so a false positive here is a delete
proposal for a resource that is in use.

- Snapshots: when describe_images failed the scan carried on with an empty AMI
  set and flagged every snapshot behind a registered AMI. analyzers/waste.py
  fixed the same bug; this copy still had it.
- Load balancers: "idle" meant no target reported "healthy". A Lambda target
  with health checks off reports "unavailable", so every ALB in front of a
  Lambda read as idle, and so did an ALB whose listeners only redirect or
  return a fixed response (no target group at all). A Gateway Load Balancer was
  priced at the ALB rate.
- resource_types: the tool documents ["ebs", "eip", "nat"]. Those names
  matched nothing, the scan ran no detector and reported $0 waste.
- idle_days: a stopped instance's idle time was its LaunchTime, the last
  start. EC2 reports the stop time in StateTransitionReason.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

from finops.aws_prices import ALB_PER_MONTH, HOURS_PER_MONTH, lb_hourly
from finops.cleanup import idle

_NOW = datetime.now(timezone.utc)


def _denied(op: str) -> ClientError:
    return ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": "no"}}, op)


class _Pages:
    def __init__(self, by_op: dict):
        self.by_op = by_op

    def get_paginator(self, name):
        pages = self.by_op[name]

        class _P:
            def paginate(self, **_kw):
                return iter(pages)
        return _P()


# ── snapshots ────────────────────────────────────────────────────────────────

class _EC2Snaps(_Pages):
    def describe_images(self, **_kw):
        raise _denied("DescribeImages")


def test_an_unread_ami_list_flags_no_snapshot():
    snaps = [{"SnapshotId": "snap-ami-root", "VolumeId": "vol-1", "VolumeSize": 100,
              "StartTime": _NOW - timedelta(days=200)}]
    ec2 = _EC2Snaps({"describe_snapshots": [{"Snapshots": snaps}]})
    assert idle._scan_old_snapshots(ec2, "111122223333", "us-east-1", 7) == []


# ── load balancers ───────────────────────────────────────────────────────────

def _lb(name: str, lb_type: str = "application") -> dict:
    kind = {"application": "app", "network": "net", "gateway": "gwy"}[lb_type]
    return {"LoadBalancerArn": f"arn:aws:elasticloadbalancing:us-east-1:111122223333:"
                               f"loadbalancer/{kind}/{name}/abc",
            "LoadBalancerName": name, "Type": lb_type,
            "CreatedTime": _NOW - timedelta(days=400)}


class _ELBv2:
    def __init__(self, lbs, tgs: dict, health: dict, listeners: dict | None = None):
        self.lbs, self.tgs, self.health = lbs, tgs, health
        self.listeners = listeners or {}

    def get_paginator(self, name):
        lbs = self.lbs

        class _P:
            def paginate(self, **_kw):
                return iter([{"LoadBalancers": lbs}])
        return _P()

    def describe_tags(self, ResourceArns):
        return {"TagDescriptions": []}

    def describe_target_groups(self, LoadBalancerArn):
        return {"TargetGroups": [{"TargetGroupArn": t} for t in self.tgs.get(LoadBalancerArn, [])]}

    def describe_target_health(self, TargetGroupArn):
        return {"TargetHealthDescriptions": [
            {"TargetHealth": {"State": s}} for s in self.health[TargetGroupArn]]}

    def describe_listeners(self, LoadBalancerArn):
        return {"Listeners": [{"ListenerArn": x} for x in self.listeners.get(LoadBalancerArn, [])]}


def test_an_alb_in_front_of_a_lambda_is_not_idle():
    """Health checks are off by default for a Lambda target group, and the
    target reports "unavailable" (ELBv2 API: Target.HealthCheckDisabled)."""
    lb = _lb("lambda-api")
    elb = _ELBv2([lb], {lb["LoadBalancerArn"]: ["tg-lambda"]}, {"tg-lambda": ["unavailable"]})
    assert idle._scan_idle_load_balancers(elb, "111122223333", "us-east-1") == []


def test_a_redirect_only_alb_is_not_idle_but_one_with_no_listener_is():
    serving, dead = _lb("http-to-https"), _lb("left-behind")
    elb = _ELBv2([serving, dead], {}, {},
                 listeners={serving["LoadBalancerArn"]: ["listener-80"]})
    got = idle._scan_idle_load_balancers(elb, "111122223333", "us-east-1")
    assert [r.name for r in got] == ["left-behind"]
    assert got[0].monthly_cost_usd == ALB_PER_MONTH


def test_a_gateway_load_balancer_is_priced_at_its_own_rate():
    lb = _lb("inspection", "gateway")
    elb = _ELBv2([lb], {lb["LoadBalancerArn"]: ["tg-appliance"]}, {"tg-appliance": ["unhealthy"]})
    [got] = idle._scan_idle_load_balancers(elb, "111122223333", "us-east-1")
    # $0.0125/hr (AWS Price List, AWSELB us-east-1), not the ALB's $0.0225.
    assert got.monthly_cost_usd == round(lb_hourly("gateway") * HOURS_PER_MONTH, 2)
    assert got.monthly_cost_usd < 9.2 < ALB_PER_MONTH


# ── resource types ───────────────────────────────────────────────────────────

class _FakeEC2:
    def __init__(self):
        self.calls: list[str] = []

    def get_paginator(self, name):
        self.calls.append(name)
        vols = [{"VolumeId": "vol-1", "Size": 100, "VolumeType": "gp3",
                 "CreateTime": _NOW - timedelta(days=60)}]

        class _P:
            def paginate(self, **_kw):
                return iter([{"Volumes": vols}])
        return _P()

    def describe_addresses(self):
        self.calls.append("describe_addresses")
        return {"Addresses": [{"AllocationId": "eipalloc-1", "PublicIp": "203.0.113.1"}]}


def test_the_documented_resource_type_names_scan_something(monkeypatch):
    ec2 = _FakeEC2()
    fake = types.ModuleType("boto3")
    fake.client = lambda service, **_kw: (
        ec2 if service == "ec2" else types.SimpleNamespace(
            get_caller_identity=lambda: {"Account": "111122223333"}))
    monkeypatch.setitem(sys.modules, "boto3", fake)

    got = idle.scan_idle_resources(resource_types=["ebs", "eip"], regions=["us-east-1"])
    assert {r.resource_type for r in got} == {"ebs_volume", "elastic_ip"}


def test_an_unsupported_resource_type_is_an_error_not_an_empty_scan():
    with pytest.raises(ValueError, match="nat"):
        idle.normalize_resource_types(["ebs", "nat"])


# ── how long a stopped instance has been stopped ─────────────────────────────

class _EC2Stopped:
    def __init__(self, inst: dict):
        self.inst = inst

    def get_paginator(self, name):
        inst = self.inst

        class _P:
            def paginate(self, **_kw):
                return iter([{"Reservations": [{"Instances": [inst]}]}])
        return _P()

    def describe_volumes(self, VolumeIds):
        return {"Volumes": []}


def _stopped(launched_days: int, reason: str) -> dict:
    return {"InstanceId": "i-1", "InstanceType": "m5.large",
            "LaunchTime": _NOW - timedelta(days=launched_days),
            "StateTransitionReason": reason, "BlockDeviceMappings": []}


def test_stopped_time_comes_from_the_state_transition_not_the_launch():
    stopped_at = (_NOW - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    inst = _stopped(300, f"User initiated ({stopped_at} GMT)")
    # Launched 300 days ago, stopped 2 days ago: not idle for 7 days.
    assert idle._scan_stopped_ec2(_EC2Stopped(inst), "1", "us-east-1", 7) == []
    [got] = idle._scan_stopped_ec2(_EC2Stopped(inst), "1", "us-east-1", 1)
    assert got.idle_days == 2
    assert got.idle_basis == "stopped"


def test_an_unparsable_stop_time_says_the_figure_is_the_launch():
    [got] = idle._scan_stopped_ec2(_EC2Stopped(_stopped(300, "")), "1", "us-east-1", 7)
    assert got.idle_days == 300
    assert got.idle_basis == "launched"
    assert "launch" in got.reason.lower()
