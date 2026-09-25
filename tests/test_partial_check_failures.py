"""A read denied inside a check is a partial check, never a clean one.

Each check reads an inventory and then follow-up details. The inventory failing
already surfaced as a failed check; the follow-ups were swallowed with `pass`
or `continue`, so a least-privilege identity missing one action got a scan that
said nothing and reported the rest as the whole answer. Worse, the snapshot
check turned its AMI filter off when the AMI list was denied, and then flagged
the snapshots behind registered AMIs as waste.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from finops.analyzers import optimizer, waste


def _deny(op: str, code: str = "AccessDenied") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "arn:aws:iam::123456789012:x"}}, op)


class _Pages:
    def __init__(self, pages=None, exc=None):
        self.pages, self.exc = pages or [], exc

    def paginate(self, **kw):
        if self.exc:
            def boom():
                raise self.exc
                yield  # pragma: no cover
            return boom()
        return iter(self.pages)


def _client(paginators: dict, **methods) -> MagicMock:
    c = MagicMock()
    c.get_paginator.side_effect = lambda op: paginators[op]
    for name, fn in methods.items():
        setattr(c, name, fn)
    return c


_OLD = datetime.now(UTC) - timedelta(days=400)


# ── snapshots: the AMI filter ────────────────────────────────────────────────

def _snapshot_client(images: _Pages) -> MagicMock:
    snaps = [{"SnapshotId": "snap-behind-ami", "StartTime": _OLD, "VolumeSize": 100},
             {"SnapshotId": "snap-other", "StartTime": _OLD, "VolumeSize": 50}]
    return _client({"describe_snapshots": _Pages([{"Snapshots": snaps}]),
                    "describe_images": images})


def test_snapshots_behind_an_ami_are_never_flagged_when_the_ami_list_is_denied():
    out = waste.check_ebs_snapshots(
        _snapshot_client(_Pages(exc=_deny("DescribeImages", "UnauthorizedOperation"))),
        "us-east-1")
    assert list(out) == [], "flagged snapshots that may back an AMI"
    assert out.partial_failures == [{
        "call": "ec2.describe_images", "error_code": "UnauthorizedOperation",
        "count": 2, "unit": "snapshots",
        "effect": "old snapshots not assessed: the AMI list that protects them could not be read",
    }]


def test_snapshots_are_still_flagged_when_the_ami_list_reads():
    images = _Pages([{"Images": [{"BlockDeviceMappings": [
        {"Ebs": {"SnapshotId": "snap-behind-ami"}}]}]}])
    out = waste.check_ebs_snapshots(_snapshot_client(images), "us-east-1")
    assert [f["resource_id"] for f in out] == ["snap-other"]
    assert out.partial_failures == []


# ── ECS: denied ListServices / DescribeTaskDefinition ────────────────────────

def test_a_denied_list_services_is_recorded_not_skipped():
    ecs = _client({"list_clusters": _Pages([{"clusterArns": ["arn:c/prod"]}]),
                   "list_services": _Pages(exc=_deny("ListServices"))})
    out = waste.check_ecs_task_rightsizing(ecs, MagicMock(), "us-east-1")
    assert list(out) == []
    assert [(f["call"], f["error_code"], f["count"]) for f in out.partial_failures] == [
        ("ecs.list_services", "AccessDenied", 1)]


def test_a_denied_task_definition_read_is_recorded():
    def describe_task_definition(**kw):
        raise _deny("DescribeTaskDefinition")

    ecs = _client(
        {"list_clusters": _Pages([{"clusterArns": ["arn:c/prod"]}]),
         "list_services": _Pages([{"serviceArns": ["arn:s/a", "arn:s/b"]}])},
        describe_services=lambda **kw: {"services": [
            {"serviceName": n, "launchType": "FARGATE", "taskDefinition": "td"}
            for n in ("a", "b")]},
        describe_task_definition=describe_task_definition)
    out = waste.check_ecs_task_rightsizing(ecs, MagicMock(), "us-east-1")
    assert [(f["call"], f["count"]) for f in out.partial_failures] == [
        ("ecs.describe_task_definition", 2)]


# ── S3 multipart: a size that could not be read is not 0 bytes ──────────────

def test_an_unreadable_upload_size_is_unpriced_not_zero():
    s3 = _client(
        {"list_multipart_uploads": _Pages([{"Uploads": [
            {"Key": "big.bin", "UploadId": "u1", "Initiated": _OLD}]}]),
         "list_parts": _Pages(exc=_deny("ListParts"))},
        list_buckets=lambda: {"Buckets": [{"Name": "logs"}]})
    out = waste.check_s3_incomplete_multipart(s3, "us-east-1")
    assert len(out) == 1
    assert out[0]["estimated_monthly_savings"] is None
    assert "could not be read" in out[0]["detail"]
    assert [(f["call"], f["error_code"]) for f in out.partial_failures] == [
        ("s3.list_parts", "AccessDenied")]


def test_a_denied_bucket_upload_listing_is_recorded():
    s3 = _client({"list_multipart_uploads": _Pages(exc=_deny("ListMultipartUploads"))},
                 list_buckets=lambda: {"Buckets": [{"Name": "a"}, {"Name": "b"}]})
    out = waste.check_s3_incomplete_multipart(s3, "us-east-1")
    assert [(f["call"], f["count"], f["unit"]) for f in out.partial_failures] == [
        ("s3.list_multipart_uploads", 2, "buckets")]


# ── CloudTrail: trail status ─────────────────────────────────────────────────

def test_a_denied_trail_status_is_recorded():
    def status(**kw):
        raise _deny("GetTrailStatus")

    ct = MagicMock()
    ct.describe_trails.return_value = {"trailList": [{"TrailARN": "arn:t", "Name": "t"}]}
    ct.get_event_selectors.return_value = {"EventSelectors": []}
    ct.get_trail_status.side_effect = status
    out = waste.check_cloudtrail_waste(ct, "us-east-1")
    assert [(f["call"], f["error_code"]) for f in out.partial_failures] == [
        ("cloudtrail.get_trail_status", "AccessDenied")]


# ── the audit carries them to checks_failed ──────────────────────────────────

def test_the_audit_reports_a_partial_check_as_failed_but_run():
    def snapshots(ec2, region):
        out = waste.CheckFindings()
        out.note_failure("ec2.describe_images", _deny("DescribeImages", "UnauthorizedOperation"),
                         count=3, unit="snapshots")
        return out

    session = MagicMock()
    with (patch.object(optimizer, "_get_boto3_session", return_value=session),
          patch("finops.analyzers.waste.check_ebs_snapshots", snapshots),
          patch.object(optimizer, "_fetch_compute_optimizer_recommendations", return_value=[])):
        report = optimizer.run_deep_audit(account_id="1", regions=["us-east-1"],
                                          checks=["snapshots"])
    assert report["checks_run"] == ["snapshots"]
    [entry] = report["checks_failed"]
    assert entry["check"] == "snapshots" and entry["partial"] is True
    assert entry["error_code"] == "UnauthorizedOperation"
    assert entry["call"] == "ec2.describe_images" and entry["count"] == 3
    assert "123456789012" not in str(report["errors"])
    assert "could not fully run (ec2.describe_images: UnauthorizedOperation)" in report["errors"][0]


def test_the_cli_names_a_partial_check_and_does_not_call_it_not_run():
    from finops.cli_scan import _failed_check_lines

    lines = _failed_check_lines({
        "checks_run": ["snapshots", "ebs"],
        "checks_failed": [
            {"check": "snapshots", "region": "us-east-1", "error_code": "UnauthorizedOperation",
             "partial": True, "call": "ec2.describe_images", "count": 3, "unit": "snapshots"},
            {"check": "lambda", "region": "us-east-1", "error_code": "AccessDenied"},
        ]})
    assert lines[0].startswith("1 check(s) could not run and were not counted: lambda")
    assert "--dry-run --json" in lines[0]
    assert lines[1].startswith("1 check(s) could not fully run")
    assert "snapshots (3 snapshots unread, UnauthorizedOperation)" in lines[1]
