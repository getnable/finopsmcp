"""T2 agent cost controls: the pure helpers behind the pre-action gate.

These cover the parts that must stay honest: the cheaper-path heuristic only fires
for a real compute add and always labels its number an estimate; remediation is
propose by default and never claims to have applied anything; the age helper is
robust to junk input.
"""
from datetime import datetime, timedelta, timezone

from finops.agent_controls import suggest_cheaper_path, remediation_status, data_age_hours


def test_cheaper_path_offers_spot_for_a_compute_add():
    breakdown = [
        {"address": "aws_instance.api", "resource_type": "aws_instance",
         "action": "add", "monthly_delta": 1000.0, "detail": "r6g.4xlarge @ $2.00/hr"},
    ]
    out = suggest_cheaper_path(breakdown, 1000.0)
    assert out is not None
    assert out["is_estimate"] is True
    assert 0 < out["estimated_monthly_usd"] < 1000.0
    assert out["estimated_saving_usd"] > 0
    assert "aws_instance.api" in out["applies_to"]


def test_cheaper_path_none_for_non_compute():
    breakdown = [
        {"address": "aws_s3_bucket.data", "resource_type": "aws_s3_bucket",
         "action": "add", "monthly_delta": 50.0, "detail": "standard storage"},
    ]
    assert suggest_cheaper_path(breakdown, 50.0) is None


def test_cheaper_path_none_for_a_saving_or_empty():
    removal = [
        {"address": "aws_instance.api", "resource_type": "aws_instance",
         "action": "remove", "monthly_delta": -1000.0, "detail": "r6g.4xlarge"},
    ]
    assert suggest_cheaper_path(removal, -1000.0) is None
    assert suggest_cheaper_path(None, 100.0) is None
    assert suggest_cheaper_path([], 100.0) is None


def test_remediation_defaults_to_propose(monkeypatch):
    monkeypatch.delenv("FINOPS_REMEDIATION_MODE", raising=False)
    r = remediation_status()
    assert r["mode"] == "propose"
    assert r["applied"] is False


def test_remediation_auto_is_recognized_but_never_applied(monkeypatch):
    monkeypatch.setenv("FINOPS_REMEDIATION_MODE", "auto")
    r = remediation_status()
    assert r["mode"] == "auto"
    assert r["applied"] is False  # auto path not built yet; must never claim it applied
    assert "note" in r


def test_remediation_junk_falls_back_to_propose(monkeypatch):
    monkeypatch.setenv("FINOPS_REMEDIATION_MODE", "yolo")
    assert remediation_status()["mode"] == "propose"


def test_data_age_hours_is_robust():
    two_h_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    age = data_age_hours(two_h_ago)
    assert age is not None and 1.5 <= age <= 2.5
    assert data_age_hours(None) is None
    assert data_age_hours("not-a-date") is None


def test_cheaper_path_ignores_storage_and_version_tokens():
    # Regression: the old regex matched s3.standard / v2.0 / a1.b2 and fabricated a saving.
    for detail, rtype in [("s3.standard tier", "aws_s3_bucket"),
                          ("engine v2.0", "aws_rds_cluster"),
                          ("a1.b2 config", "some_resource")]:
        bd = [{"action": "add", "monthly_delta": 100.0, "resource_type": rtype, "detail": detail}]
        assert suggest_cheaper_path(bd, 100.0) is None, detail


def test_cheaper_path_excludes_gpu_and_metal():
    for detail in ["p4d.24xlarge @ $32/hr", "g5.xlarge", "c5.metal"]:
        bd = [{"action": "add", "monthly_delta": 2000.0, "resource_type": "aws_instance",
               "detail": detail}]
        assert suggest_cheaper_path(bd, 2000.0) is None, detail


def test_cheaper_path_mixed_gpu_and_general_sizes_to_general_only():
    bd = [
        {"action": "add", "monthly_delta": 2000.0, "resource_type": "aws_instance",
         "detail": "p4d.24xlarge @ $32/hr"},
        {"action": "add", "monthly_delta": 1000.0, "resource_type": "aws_instance",
         "detail": "m5.large @ $0.10/hr"},
    ]
    out = suggest_cheaper_path(bd, 3000.0)
    assert out is not None
    assert out["estimated_monthly_usd"] == 300.0   # only the $1000 m5 counts; p4d excluded
    assert out["estimated_saving_usd"] == 700.0


def test_cheaper_path_real_instances_still_match():
    for detail in ["m5.16xlarge", "r6g.4xlarge @ $1/hr", "t3.micro", "db.r6g.xlarge"]:
        bd = [{"action": "add", "monthly_delta": 500.0, "resource_type": "aws_instance",
               "detail": detail}]
        assert suggest_cheaper_path(bd, 500.0) is not None, detail


def test_data_age_hours_clamps_future_to_zero():
    future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    assert data_age_hours(future) == 0.0
