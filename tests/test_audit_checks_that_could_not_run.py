"""A check that raised never looked, so it must never read as "clean".

The deep audit runs each check inside a guard so one missing permission does not
kill the scan. That guard used to log and move on: the failed check was still
listed in checks_run, nothing reached `errors`, and an identity denied every
read came back as "16 checks run, 0 findings". The brief then said "Nothing new
found." and `nable scan --json` exited 0 with partial false.

The seam is boto3: a session whose every call raises AccessDenied the way AWS
does, lazily, when a paginator is iterated. Everything above it is nable's real
code.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from finops.analyzers import optimizer

# A fake ARN on AWS's documentation account id. It rides in the exception
# message, which must never reach a gap, an error line, or --json.
_SENSITIVE = "arn:aws:iam::123456789012:role/prod-admin"


def _deny(op: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDenied", "Message": f"{_SENSITIVE} is not authorized"}}, op)


class _DeniedPages:
    def __init__(self, op):
        self.op = op

    def __iter__(self):
        raise _deny(self.op)


class _DeniedPaginator:
    def __init__(self, op):
        self.op = op

    def paginate(self, **kw):
        return _DeniedPages(self.op)


class _DeniedClient:
    def get_paginator(self, op):
        return _DeniedPaginator(op)

    def __getattr__(self, name):
        def call(*a, **k):
            raise _deny(name)
        return call


class _DeniedSession:
    def client(self, service, **kw):
        return _DeniedClient()


def _audit(**kw):
    with patch.object(optimizer, "_get_boto3_session", return_value=_DeniedSession()):
        return optimizer.run_deep_audit(account_id="123456789012", **kw)


# ── the engine ───────────────────────────────────────────────────────────────

def test_a_check_that_raised_is_recorded_and_not_counted_as_run():
    report = _audit(regions=["us-east-1"], checks=["ebs", "snapshots"])
    assert report["checks_run"] == []
    assert sorted(f["check"] for f in report["checks_failed"]) == ["ebs", "snapshots"]
    assert {f["error_code"] for f in report["checks_failed"]} == {"AccessDenied"}
    assert {f["region"] for f in report["checks_failed"]} == {"us-east-1"}
    assert report["errors"], "a denied check left `errors` empty"
    assert report["total_findings"] == 0


def test_a_check_that_worked_somewhere_still_counts_as_run():
    """Failing in one region is partial, not absent: the check did read the
    others, so it stays in checks_run and the failure is still reported."""
    def ebs(ec2, region):
        if region == "eu-west-1":
            raise _deny("DescribeVolumes")
        return []

    with (
        patch.object(optimizer, "_get_boto3_session", return_value=_DeniedSession()),
        patch("finops.analyzers.waste.check_ebs_volumes", ebs),
    ):
        report = optimizer.run_deep_audit(
            account_id="1", regions=["us-east-1", "eu-west-1"], checks=["ebs"])
    assert report["checks_run"] == ["ebs"]
    assert report["checks_failed"] == [
        {"check": "ebs", "region": "eu-west-1", "error_code": "AccessDenied"}]
    assert len(report["errors"]) == 1


def test_failure_lines_carry_the_error_code_never_the_message():
    report = _audit(regions=["us-east-1", "eu-west-1"], checks=["ebs"])
    blob = json.dumps({k: report[k] for k in ("errors", "checks_failed")})
    assert "AccessDenied" in blob
    assert _SENSITIVE not in blob and "123456789012" not in blob
    # One line per (check, code), not one per region.
    assert len(report["errors"]) == 1 and "2 region(s)" in report["errors"][0]


# ── the brief ────────────────────────────────────────────────────────────────

@pytest.fixture
def _brief_dir(tmp_path, monkeypatch):
    import finops.storage.db as _db
    monkeypatch.setattr(_db, "_DATA_DIR", None)
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))


def test_the_brief_never_calls_an_unreadable_account_clean(_brief_dir, monkeypatch):
    from finops.briefing import run as brun

    report = _audit(regions=["us-east-1"], checks=["ebs", "rds"])
    monkeypatch.setattr("finops.analyzers.optimizer.run_deep_audit", lambda *a, **k: report)
    out = brun.run_overnight(today=date(2026, 8, 3), deliver_to=(), do_persist=False,
                             now=datetime(2026, 8, 3, 6, tzinfo=timezone.utc))
    summary = out["summary"]
    assert summary["headline"] != "Nothing new found."
    assert "could not" in summary["headline"]
    assert "could not read anything" in summary["gaps"][0]
    assert summary["scanned"]["checks"] == []
    assert _SENSITIVE not in json.dumps(summary)


# ── nable scan ───────────────────────────────────────────────────────────────

def _scan(report, capsys, **args):
    from unittest.mock import MagicMock
    from finops import cli_scan

    session = MagicMock()
    session.get_credentials.return_value = object()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "352112345678"}
    session.client.side_effect = lambda name, **kw: sts
    base = dict(json=True, demo=False, spend=False, debug=False, profile=None,
                regions=["us-east-1"])
    base.update(args)
    with (
        patch.object(cli_scan, "_emit"),
        patch("finops.tool_surface.connected_families", lambda: frozenset()),
        patch("boto3.Session", return_value=session),
        patch("finops.analyzers.optimizer.run_deep_audit", return_value=report),
    ):
        code = cli_scan.run(SimpleNamespace(**base))
    return code, capsys.readouterr()


def test_scan_json_fails_when_every_check_was_denied(capsys, monkeypatch):
    from finops import cli_scan
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    report = _audit(regions=["us-east-1"], checks=["ebs", "snapshots", "rds"])
    code, cap = _scan(report, capsys)
    assert code == cli_scan.EXIT_DENIED, "an unreadable account exited as a clean scan"
    doc = json.loads(cap.out)
    assert doc["scan"]["partial"] is True
    assert doc["scan"]["errors"]
    assert doc["findings"] == []


def test_scan_json_marks_a_scan_with_some_failed_checks_partial(capsys, monkeypatch):
    from finops import cli_scan
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    report = _audit(regions=["us-east-1"], checks=["ebs"])
    report["checks_run"] = ["eips"]           # one check did read
    code, cap = _scan(report, capsys)
    assert code == cli_scan.EXIT_OK
    doc = json.loads(cap.out)
    assert doc["scan"]["partial"] is True
    assert doc["scan"]["checks_failed"][0]["check"] == "ebs"

    code, cap = _scan(report, capsys, json=False)
    assert "could not run" in cap.out and "ebs" in cap.out
