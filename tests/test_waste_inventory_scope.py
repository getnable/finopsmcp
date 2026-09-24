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


def test_multipart_waste_counts_every_page_of_parts():
    """list_parts returns at most 1,000 parts a call. A stale 1,500 part upload
    was priced on its first 1,000 parts only."""
    import boto3
    from botocore.stub import Stubber

    s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x",
                      aws_secret_access_key="x")
    stale = datetime.now(timezone.utc) - timedelta(days=30)
    gib = 1024 ** 3

    def parts(first: int, n: int) -> list[dict]:
        return [{"PartNumber": i, "Size": gib} for i in range(first, first + n)]

    upload = {"Bucket": "b", "Key": "big.bin", "UploadId": "u1"}
    with Stubber(s3) as stub:
        stub.add_response("list_buckets", {"Buckets": [{"Name": "b"}]})
        stub.add_response(
            "list_multipart_uploads",
            {"Bucket": "b", "IsTruncated": False,
             "Uploads": [{"Key": "big.bin", "UploadId": "u1", "Initiated": stale}]},
            {"Bucket": "b"})
        stub.add_response(
            "list_parts",
            {**upload, "IsTruncated": True, "NextPartNumberMarker": 1000,
             "Parts": parts(1, 1000)},
            dict(upload))
        stub.add_response(
            "list_parts",
            {**upload, "IsTruncated": False, "Parts": parts(1001, 500)},
            {**upload, "PartNumberMarker": 1000})
        findings = waste.check_s3_incomplete_multipart(s3, "us-east-1")
        stub.assert_no_pending_responses()

    assert len(findings) == 1
    assert findings[0]["wasted_gb"] == pytest.approx(1500.0)
