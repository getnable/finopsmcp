"""`nable scan` says what happened, including the parts that did not.

Found by dogfooding a first run against a mock account:
- `--json` capped findings at 20 while the text promised it "lists every one".
- a $20/mo account with $4.61/mo of findings was told "no material waste found, nice".
- `--regions eu-west-9` failed 14 of 16 checks and still printed "nice", exit 0.
- repeating `--regions` kept only the last one.
- `--json` failures printed nothing on stdout.
- `--profile X` was reported as "AWS_PROFILE=X is set in your environment".
- keys exported in the environment were labelled "profile default", and the fix
  for a bad key said `aws configure`.
- Ctrl-C printed a traceback.
- `--spend` with no Cost Explorer data printed nothing about it, showed
  "AI & GPU $0.00/mo [estimated]", and ended by suggesting `--spend`.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, ProfileNotFound

from finops import cli_scan


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
              "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "FINOPS_DEMO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("finops.tool_surface.connected_families", lambda: frozenset())
    monkeypatch.setattr(cli_scan, "_spend_window",
                        lambda today: ("2026-07-01", "2026-07-15", "month-to-date"))
    monkeypatch.setattr(cli_scan, "_emit", lambda *a, **k: None)
    yield
    import os
    os.environ.pop("AWS_PROFILE", None)


def _args(**kw):
    base = {"json": False, "demo": False, "spend": False, "debug": False,
            "profile": None, "regions": None, "dry_run": False}
    base.update(kw)
    return SimpleNamespace(**base)


def _err(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "arn:aws:iam::1:x"}}, op)


def _session(sts_exc=None, catalog=None, regions_exc=None, ce=None):
    session = MagicMock()
    session.get_credentials.return_value = object()
    sts = MagicMock()
    if sts_exc:
        sts.get_caller_identity.side_effect = sts_exc
    else:
        sts.get_caller_identity.return_value = {"Account": "352112345678"}
    ec2 = MagicMock()
    if regions_exc:
        ec2.describe_regions.side_effect = regions_exc
    else:
        ec2.describe_regions.return_value = {"Regions": [
            {"RegionName": r, "OptInStatus": s} for r, s in (catalog or {
                "us-east-1": "opt-in-not-required", "eu-west-1": "opt-in-not-required",
                "af-south-1": "not-opted-in"}).items()]}
    session.client.side_effect = lambda name, **kw: {
        "sts": sts, "ec2": ec2, "ce": ce or MagicMock()}[name]
    return session


def _finding(i: int, usd: float | None = 30.0) -> dict:
    return {"waste_type": "unattached_ebs_volume", "resource_id": f"vol-{i}",
            "region": "us-east-1", "estimated_monthly_savings": usd}


def _report(findings, **kw) -> dict:
    total = sum(f["estimated_monthly_savings"] or 0 for f in findings)
    r = {"account_id": "352112345678", "regions_scanned": ["us-east-1"],
         "regions_timed_out": [], "checks_run": ["ebs"], "checks_failed": [],
         "total_findings": len(findings), "total_estimated_monthly_savings": total,
         "findings": findings, "errors": []}
    r.update(kw)
    return r


def _run(args, session, report=None, discovered=("us-east-1",)):
    with (
        patch("boto3.Session", return_value=session),
        patch("finops.analyzers.optimizer._discover_regions",
              return_value=discovered if isinstance(discovered, list) else list(discovered)),
        patch("finops.analyzers.optimizer.run_deep_audit",
              return_value=report or _report([_finding(1)])) as engine,
    ):
        code = cli_scan.run(args)
    return code, engine


# ── --json lists every finding ───────────────────────────────────────────────

def test_json_lists_every_finding_and_they_add_up_to_the_headline(capsys):
    findings = [_finding(i, 30.0 + i) for i in range(45)] + [_finding(99, None)]
    code, _ = _run(_args(json=True), _session(), _report(findings))
    doc = json.loads(capsys.readouterr().out)
    assert code == cli_scan.EXIT_OK
    assert doc["total_findings"] == 46 == len(doc["findings"])
    assert doc["unpriced_findings"] == 1
    priced = sum(f["estimated_monthly_savings"] or 0 for f in doc["findings"])
    assert round(priced, 2) == doc["recoverable"]["monthly_usd"]


# ── regions ──────────────────────────────────────────────────────────────────

def test_a_region_that_does_not_exist_is_refused_before_the_scan(capsys):
    code, engine = _run(_args(regions=["eu-west-9"]), _session())
    out = capsys.readouterr().out
    assert code == 1
    assert "not an AWS region: eu-west-9" in out
    assert "us-east-1" in out            # names the ones that do
    assert "nice" not in out
    engine.assert_not_called()


def test_a_region_the_account_has_not_enabled_is_refused(capsys):
    code, engine = _run(_args(regions=["af-south-1"]), _session())
    assert code == 1
    assert "not enabled for this account: af-south-1" in capsys.readouterr().out
    engine.assert_not_called()


def test_region_names_are_scanned_as_given_when_they_cannot_be_checked(capsys):
    code, engine = _run(_args(regions=["us-east-1"]),
                        _session(regions_exc=_err("UnauthorizedOperation")))
    assert code == cli_scan.EXIT_OK
    assert "could not check region names (ec2:DescribeRegions: UnauthorizedOperation)" \
        in capsys.readouterr().out
    assert engine.call_args.kwargs["regions"] == ["us-east-1"]


def test_most_checks_failing_is_not_a_success(capsys):
    failed = [{"check": c, "region": "us-east-1", "error_code": "EndpointConnectionError"}
              for c in ("ebs", "snapshots", "eips", "nat", "rds", "ecr")]
    rep = _report([], checks_run=["s3", "s3_multipart"], checks_failed=failed)
    code, _ = _run(_args(), _session(), rep)
    out = capsys.readouterr().out
    assert code == cli_scan.EXIT_PARTIAL_EMPTY
    assert "6 of 8 checks could not run" in out
    assert "nice" not in out


def test_an_unlisted_region_set_is_said_and_partial(capsys):
    from finops.analyzers.optimizer import _Regions
    fallback = _Regions(["us-east-1"])
    fallback.error_code = "UnauthorizedOperation"
    _run(_args(json=True), _session(), discovered=fallback)
    cap = capsys.readouterr()
    doc = json.loads(cap.out)
    assert doc["scan"]["partial"] is True
    assert doc["scan"]["regions_unlisted"] == "UnauthorizedOperation"
    assert "could not list this account's regions" in cap.err


def test_repeating_regions_adds_to_the_list():
    import argparse
    parser = argparse.ArgumentParser()
    cli_scan.add_parser(parser.add_subparsers())
    ns = parser.parse_args(["scan", "--regions", "us-east-1", "--regions", "eu-west-1,us-west-2"])
    assert cli_scan._split_regions(ns.regions) == ["us-east-1", "eu-west-1", "us-west-2"]


# ── --json failures answer on stdout ─────────────────────────────────────────

def test_a_json_failure_prints_an_error_document(capsys):
    code, _ = _run(_args(json=True),
                   _session(sts_exc=_err("InvalidClientTokenId", "GetCallerIdentity")))
    cap = capsys.readouterr()
    doc = json.loads(cap.out)
    assert doc == {"error": {"class": "bad-creds", "exit_code": code,
                             "message": "AWS rejected these credentials"}}
    assert "352112345678" not in cap.out


# ── where the credentials came from ──────────────────────────────────────────

def test_a_missing_profile_passed_as_a_flag_is_not_blamed_on_the_environment(capsys):
    with patch("boto3.Session", side_effect=ProfileNotFound(profile="prod")), \
         patch.object(cli_scan, "_available_profiles", return_value=["default"]):
        code = cli_scan.run(_args(profile="prod"))
    out = capsys.readouterr().out
    assert code == cli_scan.EXIT_CONFIG
    assert "you asked for it with --profile prod" in out
    assert "is set in your environment" not in out
    assert "unset AWS_PROFILE" not in out


def test_a_missing_profile_from_the_environment_says_so(capsys, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "prod")
    with patch("boto3.Session", side_effect=ProfileNotFound(profile="prod")), \
         patch.object(cli_scan, "_available_profiles", return_value=["default"]):
        code = cli_scan.run(_args())
    out = capsys.readouterr().out
    assert code == cli_scan.EXIT_CONFIG
    assert "AWS_PROFILE=prod is set in your environment" in out


def test_environment_keys_are_named_and_the_fix_is_about_them(capsys, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_PROFILE", "prod")      # env keys still win over this
    code, _ = _run(_args(), _session(sts_exc=_err("InvalidClientTokenId")))
    out = capsys.readouterr().out
    assert code == cli_scan.EXIT_NO_CREDS
    assert out.splitlines()[0].endswith("credentials from AWS_ACCESS_KEY_ID in your environment")
    assert "profile" not in out.splitlines()[0]
    assert "unset them" in out and "aws configure" not in out


def test_profile_flag_is_handed_to_the_session(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    with patch("boto3.Session", return_value=_session()) as S, \
         patch("finops.analyzers.optimizer._discover_regions", return_value=["us-east-1"]), \
         patch("finops.analyzers.optimizer.run_deep_audit",
               return_value=_report([_finding(1)])) as engine:
        cli_scan.run(_args(profile="prod"))
    assert any(c.kwargs.get("profile_name") == "prod" for c in S.call_args_list)
    assert engine.call_args.kwargs["session"] is not None


# ── Ctrl-C ───────────────────────────────────────────────────────────────────

def test_ctrl_c_is_a_quiet_cancel_with_exit_130(capsys, monkeypatch):
    def interrupted(args, t0):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_scan, "run", interrupted)
    assert cli_scan.main(_args()) == cli_scan.EXIT_CANCELLED == 130
    cap = capsys.readouterr()
    assert "scan cancelled" in cap.out
    assert "Traceback" not in cap.out + cap.err


def test_ctrl_c_in_json_mode_answers_on_stdout(capsys, monkeypatch):
    def interrupted(args, t0):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_scan, "run", interrupted)
    assert cli_scan.main(_args(json=True)) == 130
    assert json.loads(capsys.readouterr().out)["error"]["class"] == "cancelled"


# ── --spend with nothing to show ─────────────────────────────────────────────

def test_spend_with_no_cost_explorer_data_says_so(capsys):
    ce = MagicMock()
    ce.get_cost_and_usage.return_value = {"ResultsByTime": [{"Groups": []}]}
    code, _ = _run(_args(spend=True), _session(ce=ce))
    out = capsys.readouterr().out
    assert code == cli_scan.EXIT_OK
    assert "Cost Explorer returned no data for this period" in out
    assert "run `nable scan --spend`" not in out       # it just ran


def test_spend_with_no_data_is_null_in_json_not_zero(capsys):
    ce = MagicMock()
    ce.get_cost_and_usage.return_value = {"ResultsByTime": [{"Groups": []}]}
    _run(_args(spend=True, json=True), _session(ce=ce))
    doc = json.loads(capsys.readouterr().out)
    assert doc["spend"]["month_to_date_usd"] is None
    assert doc["spend"]["has_data"] is False


def test_a_bedrock_leg_of_zero_is_not_an_ai_provider():
    import finops.scan_assembler as sa
    with patch("finops.connectors.llm_costs.get_all_llm_costs",
               side_effect=lambda **kw: {"total_usd": 0.0, "by_provider": {"bedrock": 0.0}}):
        blocks, _ = sa.gather_extra_providers(frozenset({"llm"}), spend=True)
    assert blocks == []


def test_dry_run_spend_footer_keeps_spend():
    from finops.scan_manifest import render_dry_run
    assert "nable scan --dry-run --spend --json" in render_dry_run(include_spend=True)
    assert "nable scan --dry-run --json" in render_dry_run(include_spend=False)


def test_the_spend_disclosure_counts_the_bedrock_calls_too(capsys, monkeypatch):
    monkeypatch.setattr("finops.tool_surface.connected_families",
                        lambda: frozenset({"aws", "llm"}))
    monkeypatch.setattr("finops.scan_assembler.gather_extra_providers",
                        lambda fams, *, spend, **kw: ([], False))
    ce = MagicMock()
    ce.get_cost_and_usage.return_value = {"ResultsByTime": [{"Groups": []}]}
    _run(_args(spend=True), _session(ce=ce))
    assert "plus up to 2 for Bedrock AI spend" in capsys.readouterr().out
