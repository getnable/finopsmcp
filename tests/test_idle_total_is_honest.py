"""list_idle_resources' total: what can be acted on, from what could be read.

Two defects in the figure the welcome screen prints as "Idle / wasted $X/mo,
doing nothing" and the MCP tool returns as total_monthly_waste_usd.

1. Protected resources (env=prod, protected=true, do-not-delete, finops-skip)
   are flagged "never acted on", yet their cost was in the waste total. The cost
   sweep skips them, so one account read two idle totals. Fixture: an
   unprotected unattached gp3 volume, 100 GB at $0.08 = $8.00/mo, and a
   prod-tagged one, 500 GB = $40.00/mo. The total was $48.00; what nable will
   propose acting on is $8.00.

2. A region whose scan raised (AccessDenied, throttling) was logged and
   dropped. The total came back with nothing to say it was partial, and with
   every region failed it was $0, which reads as an account with no idle
   resources.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from finops.cleanup import idle

_OLD = datetime.now(UTC) - timedelta(days=60)


def _vol(vid, size, tags=()):
    return {"VolumeId": vid, "Size": size, "VolumeType": "gp3", "CreateTime": _OLD,
            "Tags": [{"Key": k, "Value": v} for k, v in tags]}


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kw):
        return iter(self._pages)


class _EC2:
    def __init__(self, volumes, fail=False):
        self._volumes, self._fail = volumes, fail

    def get_paginator(self, op):
        assert op == "describe_volumes", op
        if self._fail:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": "no"}},
                              "DescribeVolumes")
        return _Paginator([{"Volumes": self._volumes}])


class _STS:
    def get_caller_identity(self):
        return {"Account": "123456789012"}


@pytest.fixture
def fake_aws(monkeypatch):
    import boto3

    per_region: dict = {}

    def _client(service, region_name=None, **kw):
        if service == "sts":
            return _STS()
        return per_region[region_name]

    monkeypatch.setattr(boto3, "client", _client)
    return per_region


def test_protected_resources_are_not_in_the_waste_total(fake_aws):
    fake_aws["us-east-1"] = _EC2([_vol("vol-free", 100), _vol("vol-prod", 500, [("env", "prod")])])
    found = idle.scan_idle_resources(resource_types=["ebs_volume"], regions=["us-east-1"])
    out = idle.idle_resources_summary(found)
    assert out["total_monthly_waste_usd"] == 8.0, out
    assert out["protected_monthly_usd"] == 40.0, out
    # Still listed, so nothing is hidden: it is flagged, not counted.
    assert any(r["resource_id"] == "vol-prod" and r["protected"] for r in out["resources"])


def test_a_region_that_could_not_be_read_makes_the_total_partial(fake_aws):
    fake_aws["us-east-1"] = _EC2([_vol("vol-free", 100)])
    fake_aws["eu-west-1"] = _EC2([], fail=True)
    found = idle.scan_idle_resources(resource_types=["ebs_volume"],
                                     regions=["us-east-1", "eu-west-1"])
    out = idle.idle_resources_summary(found)
    assert out["total_monthly_waste_usd"] == 8.0
    assert out["partial"] is True, out
    assert out["regions_not_read"] == {"eu-west-1": "UnauthorizedOperation"}, out
    assert "not a finding" in out["partial_note"], out


def test_a_full_read_is_not_marked_partial(fake_aws):
    fake_aws["us-east-1"] = _EC2([_vol("vol-free", 100)])
    out = idle.idle_resources_summary(
        idle.scan_idle_resources(resource_types=["ebs_volume"], regions=["us-east-1"]))
    assert "partial" not in out
