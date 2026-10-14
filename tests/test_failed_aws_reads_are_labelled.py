"""A failed AWS read is never a clean answer, whichever tool made it.

Reproduced with every AWS call answering AccessDeniedException (an IAM role
that can call STS and nothing else, the common first-day setup):

  audit_cloudwatch_metric_cardinality  "No high-cardinality namespaces found",
                                       "$0.00"
  list_idle_resources                  total_monthly_waste_usd 0, resources []
  run_full_cost_audit                  "No savings opportunities found. Your
                                       infrastructure looks well-optimized"

Twenty scanners catch their own per-region failures and return an empty
result. The fix observes failures where every read goes through, botocore's
after-call events, so the answer says the reads failed. The clients here are
real botocore clients; only the HTTP answer is canned (tests/mcp_support.py).
"""
from __future__ import annotations

import pytest

from tests.mcp_support import aws_answers
from tests.mcp_support import call as _call


@pytest.fixture
def denied(monkeypatch, tmp_path):
    from finops import cache, demo_data
    from finops.connectors.aws import AWSConnector

    cache.clear()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.setattr(demo_data, "_real_provider_connected", lambda: True)
    monkeypatch.setattr(AWSConnector, "_credentials_resolvable", lambda self: True)
    aws_answers(monkeypatch, "AccessDeniedException")
    yield
    cache.clear()


def test_a_scanner_whose_every_read_was_denied_says_so(denied):
    out = _call("audit_cloudwatch_metric_cardinality", {"regions": ["us-east-1"]})

    assert "AWS reads failed" in out, out
    assert "AccessDeniedException" in out
    assert "not a finding of zero" in out


def test_idle_resources_denied_in_executor_threads_is_not_zero_waste(denied):
    """list_idle_resources scans regions on a ThreadPoolExecutor, which does not
    inherit the caller's context; the read log is carried into it."""
    out = _call("list_idle_resources", {"regions": ["us-east-1"]})

    assert out.get("status") == "not_evaluated", out
    failed = out["aws_reads_failed"]
    assert failed["calls_answered"] == 0
    assert failed["by_error"].get("AccessDeniedException", 0) >= 1


def test_full_audit_with_denied_reads_is_not_well_optimized(denied, monkeypatch):
    from finops.recommendations import sweep as S

    real = S.build_specs

    def _three(aws, regions):
        keep = {"cw_cardinality", "ipv4", "idle_resources"}
        return [s for s in real(aws, regions) if s[0] in keep]

    monkeypatch.setattr(S, "build_specs", _three)
    out = _call("run_full_cost_audit", {"regions": ["us-east-1"]})

    assert "well-optimized" not in out, out
    assert "3 of 3 checks could not run" in out
    assert "not a finding that the account is optimized" in out


def test_a_not_found_answer_is_an_answer_not_a_failed_read(monkeypatch):
    """S3 and friends say "this bucket has no lifecycle rule" with an error
    code. That is a read that worked."""
    import boto3
    from botocore.exceptions import ClientError

    from finops.aws_read_failures import watching

    aws_answers(monkeypatch, "NoSuchLifecycleConfiguration", status=404)
    with watching() as reads, pytest.raises(ClientError):
        boto3.client("s3").get_bucket_lifecycle_configuration(Bucket="b")
    assert reads.failed == []
    assert reads.answered == 1


# ── the sweep reads what its scanners say about themselves ────────────────────

def test_a_scanner_result_that_carries_an_error_is_a_check_that_did_not_run(monkeypatch):
    import asyncio

    from finops.recommendations import sweep as S

    monkeypatch.setattr(S, "build_specs", lambda a, r: [
        ("s3_ta", lambda **k: {"error": "list_buckets failed: AccessDenied", "findings": []}, {}),
        ("db_sp", lambda **k: {"data_incomplete": True, "error": "Could not read RDS spend"}, {}),
    ])
    result = asyncio.run(S.sweep(object()))

    assert result.errors == ["s3_ta", "db_sp"]
    assert "AccessDenied" in result.error_reasons["s3_ta"]


@pytest.mark.parametrize(("name", "data"), [
    ("cw_cardinality", {"high_cardinality_namespaces": [
        {"namespace": "App/Orders", "metric_count": 150, "estimated_monthly_cost": 45.0}]}),
    ("cw_logs_ia", {"candidates": [
        {"log_group_name": "/aws/lambda/x", "monthly_savings": 30.0}]}),
])
def test_the_sweep_keeps_findings_the_scanner_returns_as_a_dict(name, data):
    """Both scanners return a dict; the sweep read only lists, so their findings
    never reached run_full_cost_audit while the scanners' own tools showed them."""
    from finops.recommendations.sweep import normalise

    found = normalise(name, data)
    assert len(found) == 1, found
    assert found[0]["monthly_savings"] > 0
