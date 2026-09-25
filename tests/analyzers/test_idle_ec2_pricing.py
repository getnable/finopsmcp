"""Idle-EC2 savings are the instance type's list price (finops.analyzers.waste).

check_idle_ec2 used to price an idle instance at vCPUs x $15/month, from a
size-suffix table. That put a p5.48xlarge ($55.04/hr, $40,179/mo) at $2,880,
a g5.xlarge at $60 against $734, and a t3.nano ($3.80/mo) at $15. The rate
now comes from aws_prices.EC2_MONTHLY, and a type the table does not hold is
reported unpriced, the way check_rds_idle reports an unknown class.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from finops.analyzers import waste
from finops.analyzers.optimizer import _monthly_savings
from finops.aws_prices import EC2_MONTHLY


class _Pages:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_kw):
        return iter(self._pages)


class _EC2:
    def __init__(self, types: dict[str, str]):
        old = datetime.now(timezone.utc) - timedelta(days=60)
        self._instances = [{"InstanceId": i, "InstanceType": t, "LaunchTime": old}
                           for i, t in types.items()]

    def get_paginator(self, _name):
        return _Pages([{"Reservations": [{"Instances": self._instances}]}])


class _IdleCW:
    """CPU at 1% and no network for every instance, read successfully."""

    def get_metric_statistics(self, **kw):
        now = datetime.now(timezone.utc)
        value = 1.0 if kw["MetricName"] == "CPUUtilization" else 0.0
        stat = kw["Statistics"][0]
        return {"Datapoints": [{"Timestamp": now - timedelta(hours=h), stat: value}
                               for h in range(48)]}


def _idle(types: dict[str, str]) -> dict[str, dict]:
    return {f["resource_id"]: f
            for f in waste.check_idle_ec2(_EC2(types), _IdleCW(), "us-east-1")}


@pytest.mark.parametrize("itype", ["t3.nano", "m5.large", "g5.xlarge", "p5.48xlarge"])
def test_idle_instance_is_priced_at_its_list_rate(itype):
    [f] = _idle({"i-1": itype}).values()
    assert f["estimated_monthly_savings"] == EC2_MONTHLY[itype]
    assert f["unpriced"] is False


def test_the_per_vcpu_guess_is_gone():
    got = _idle({"i-gpu": "p5.48xlarge", "i-nano": "t3.nano"})
    # 192 vCPUs x $15 and 1 vCPU x $15, the figures this replaced.
    assert got["i-gpu"]["estimated_monthly_savings"] == 40179.2 != 2880.0
    assert got["i-nano"]["estimated_monthly_savings"] == 3.8 != 15.0
    assert not hasattr(waste, "_vcpus_from_type")
    assert not hasattr(waste, "_SIZE_VCPU")


def test_an_unknown_type_is_unpriced_not_guessed():
    [f] = _idle({"i-new": "zz9.metal"}).values()
    assert f["estimated_monthly_savings"] is None
    assert f["unpriced"] is True
    assert f["severity"] == "unknown"
    assert "not in nable's price table" in f["detail"]
    # The audit's totals read it as unpriced, not as $0.
    assert _monthly_savings(f) is None
