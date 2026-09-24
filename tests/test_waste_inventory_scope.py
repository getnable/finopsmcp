"""A waste check must read this account's inventory, and all of it.

Two ways a check reads the wrong amount: too much (another account's public
snapshots flagged as this account's waste) and too little (only the first page
of a paginated answer, so the dollar figure is quietly short). The seam is the
boto3 client; everything above it is the real check.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from finops.analyzers import waste


class _SnapshotEC2:
    """EC2 shaped client that answers describe_snapshots the way AWS does:
    without OwnerIds, the answer includes every public snapshot in the region."""

    def __init__(self):
        self.snapshot_kwargs: dict | None = None
        old = datetime.now(timezone.utc) - timedelta(days=400)
        self._mine = {"SnapshotId": "snap-mine", "StartTime": old, "VolumeSize": 100}
        self._public = {"SnapshotId": "snap-someone-elses", "StartTime": old,
                        "VolumeSize": 8000}

    def get_paginator(self, op):
        client = self

        class _P:
            def paginate(self, **kw):
                if op == "describe_images":
                    return [{"Images": []}]
                client.snapshot_kwargs = kw
                owned = kw.get("OwnerIds") == ["self"]
                snaps = [client._mine] if owned else [client._mine, client._public]
                return [{"Snapshots": snaps}]
        return _P()


def test_snapshots_are_always_filtered_to_this_account(monkeypatch):
    """STS failing used to drop the OwnerIds filter entirely."""
    import boto3

    def sts_down(*a, **k):
        raise RuntimeError("sts unreachable")

    monkeypatch.setattr(boto3, "client", sts_down)
    ec2 = _SnapshotEC2()
    findings = waste.check_ebs_snapshots(ec2, "us-east-1")
    assert ec2.snapshot_kwargs["OwnerIds"] == ["self"]
    assert [f["resource_id"] for f in findings] == ["snap-mine"]


def test_the_snapshot_check_does_not_call_sts_on_the_default_chain(monkeypatch):
    """The account id came from boto3.client("sts"), the default chain, not the
    audit's session: a role_arn audit filtered on the caller's account."""
    import boto3

    reached: list = []

    def default_chain(*a, **k):
        # Recorded, not asserted here: the old code swallowed any exception
        # from this call, an AssertionError included.
        reached.append(a)
        raise RuntimeError("default chain")

    monkeypatch.setattr(boto3, "client", default_chain)
    waste.check_ebs_snapshots(_SnapshotEC2(), "us-east-1")
    assert reached == [], "check_ebs_snapshots reached the default boto3 chain"
