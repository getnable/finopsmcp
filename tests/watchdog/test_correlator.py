"""Tests for the watchdog spend x utilization correlator.

Injects mocked rightsizing recs and idle resources (no live boto3) via the
underscore params, so we exercise the fusion, ranking, filtering, and prepared
remediation logic without touching a cloud.
"""
from finops.cleanup.idle import IdleResource
from finops.recommendations.rightsizing import RightsizingRecommendation
from finops.watchdog.correlator import (
    CorrelatedFinding,
    PreparedRemediation,
    correlate_spend_and_utilization,
    correlation_summary,
)


def _rightsizing_rec(iid="i-abc", cpu=5.0, savings=120.0, rtype="m5.large") -> RightsizingRecommendation:
    return RightsizingRecommendation(
        instance_id=iid,
        instance_type="m5.xlarge",
        name=f"name-{iid}",
        region="us-east-1",
        account_id="111111111111",
        resource_type="ec2",
        source="compute_optimizer",
        avg_cpu_pct=cpu,
        recommended_type=rtype,
        monthly_savings=savings,
        confidence="high",
    )


def _idle_ebs(rid="vol-1", cost=40.0) -> IdleResource:
    return IdleResource(
        resource_type="ebs_volume",
        resource_id=rid,
        region="us-west-2",
        account_id="111111111111",
        name=f"orphan-{rid}",
        idle_since="2026-01-01",
        idle_days=90,
        monthly_cost_usd=cost,
        reason="Unattached gp3 volume, 500 GB",
        metadata={"volume_type": "gp3", "size_gb": 500},
    )


def test_fuses_and_ranks_by_dollar_waste():
    findings = correlate_spend_and_utilization(
        _rightsizing_recs=[_rightsizing_rec(savings=120.0)],
        _idle_resources=[_idle_ebs(cost=40.0)],
    )
    assert len(findings) == 2
    # Ranked worst-waste first: the $120 rightsizing beats the $40 idle volume.
    assert findings[0].signal == "rightsizing"
    assert findings[0].monthly_waste_usd == 120.0
    assert findings[1].signal == "idle"
    assert findings[1].monthly_waste_usd == 40.0


def test_idle_findings_score_zero_utilization():
    findings = correlate_spend_and_utilization(
        _rightsizing_recs=[],
        _idle_resources=[_idle_ebs()],
    )
    assert findings[0].utilization_pct == 0.0
    assert findings[0].annual_waste_usd == round(40.0 * 12, 2)


def test_high_cpu_rightsizing_is_not_underutilized():
    # A rec whose CPU is above the underutilization gate is dropped: the
    # correlator surfaces underutilization, not a borderline nudge.
    findings = correlate_spend_and_utilization(
        _rightsizing_recs=[_rightsizing_rec(cpu=55.0, savings=90.0)],
        _idle_resources=[],
    )
    assert findings == []


def test_min_waste_threshold_filters_noise():
    findings = correlate_spend_and_utilization(
        _rightsizing_recs=[_rightsizing_rec(savings=2.0)],
        _idle_resources=[_idle_ebs(cost=1.0)],
        min_monthly_waste=5.0,
    )
    assert findings == []


def test_include_flags_scope_the_scan():
    idle_only = correlate_spend_and_utilization(
        _rightsizing_recs=[_rightsizing_rec()],
        _idle_resources=[_idle_ebs()],
        include_rightsizing=False,
    )
    assert {f.signal for f in idle_only} == {"idle"}

    rs_only = correlate_spend_and_utilization(
        _rightsizing_recs=[_rightsizing_rec()],
        _idle_resources=[_idle_ebs()],
        include_idle=False,
    )
    assert {f.signal for f in rs_only} == {"rightsizing"}


def test_prepared_remediation_rightsizing_reuses_pr_path():
    findings = correlate_spend_and_utilization(
        _rightsizing_recs=[_rightsizing_rec(iid="i-xyz", rtype="m5.large")],
        _idle_resources=[],
    )
    rem = findings[0].remediation
    assert isinstance(rem, PreparedRemediation)
    assert rem.kind == "rightsizing_pr"
    assert rem.requires_approval is True
    assert rem.prepare_via.endswith("open_rightsizing_pr")
    assert rem.params["instance_type"] == "m5.large"
    assert rem.params["resource_id"] == "i-xyz"


def test_prepared_remediation_idle_carries_exact_command():
    findings = correlate_spend_and_utilization(
        _rightsizing_recs=[],
        _idle_resources=[_idle_ebs(rid="vol-42")],
    )
    rem = findings[0].remediation
    assert rem.kind == "idle_cleanup"
    assert rem.requires_approval is True
    assert "delete-volume" in rem.command
    assert "vol-42" in rem.command


def test_prepared_remediation_covers_each_idle_type():
    types_and_fragments = {
        "elastic_ip": ("release-address", "eipalloc-1"),
        "snapshot": ("delete-snapshot", "snap-1"),
        "stopped_ec2": ("terminate-instances", "i-1"),
    }
    for rtype, (verb, rid) in types_and_fragments.items():
        res = IdleResource(
            resource_type=rtype,
            resource_id=rid,
            region="us-east-1",
            account_id="111111111111",
            name="x",
            idle_since="2026-01-01",
            idle_days=30,
            monthly_cost_usd=25.0,
            reason="idle",
        )
        findings = correlate_spend_and_utilization(
            _rightsizing_recs=[], _idle_resources=[res],
        )
        assert verb in findings[0].remediation.command
        assert rid in findings[0].remediation.command


def test_summary_totals_cover_all_and_flag_propose_only():
    findings = correlate_spend_and_utilization(
        _rightsizing_recs=[_rightsizing_rec(savings=120.0)],
        _idle_resources=[_idle_ebs(cost=40.0)],
    )
    out = correlation_summary(findings)
    assert out["total_findings"] == 2
    assert out["total_monthly_waste_usd"] == 160.0
    assert out["total_annual_waste_usd"] == round(160.0 * 12, 2)
    assert out["waste_by_signal"] == {"rightsizing": 120.0, "idle": 40.0}
    assert out["propose_only"] is True
    # Every surfaced finding requires approval.
    assert all(f["remediation"]["requires_approval"] for f in out["findings"])


def test_summary_caps_detail_but_keeps_totals():
    findings = [
        CorrelatedFinding(
            resource_id=f"vol-{i:06d}",
            resource_type="ebs_volume",
            name=f"orphan-volume-{i}-with-a-longish-descriptive-name",
            region="us-east-1",
            account_id="111111111111",
            provider="aws",
            monthly_waste_usd=float(1000 - i),
            utilization_pct=0.0,
            signal="idle",
            reason="Unattached for 90 days; no snapshot dependency found.",
            remediation=PreparedRemediation(
                kind="idle_cleanup",
                title=f"Clean up vol-{i}",
                command=f"aws ec2 delete-volume --volume-id vol-{i:06d}",
                prepare_via="finops.recommendations.verifiers.verify_idle_cleanup",
            ),
        )
        for i in range(800)
    ]
    out = correlation_summary(findings)
    assert out["total_findings"] == 800
    assert out["total_monthly_waste_usd"] == round(sum(f.monthly_waste_usd for f in findings), 2)
    assert len(out["findings"]) < 800
    assert out["findings_truncated"] is True
    assert out["findings"][0]["monthly_waste_usd"] == 1000.0


def test_correlator_is_read_only():
    # The correlator module must not import or call any mutation path. Guard by
    # source inspection: no boto3 write verbs appear as calls in the module.
    import inspect
    from finops.watchdog import correlator

    src = inspect.getsource(correlator)
    # The prepared COMMAND strings legitimately contain delete/terminate as data.
    # Assert the module never CALLS a mutating client method.
    for banned in (".delete_volume(", ".release_address(", ".terminate_instances(",
                   ".modify_instance_attribute(", ".delete_snapshot("):
        assert banned not in src
