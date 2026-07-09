"""Genuine-savings judgment: the reasoning that separates 'underutilized' from
'real, safe savings'. Each test pins one of the reasons a rightsizing call is
usually wrong, plus the commitment-coverage discount and the token-cost guard.
"""
from __future__ import annotations

from finops.recommendations.genuine_savings import (
    Assessment, CommitmentContext, assess, _reset_cache_for_tests,
)
from finops.recommendations.rightsizing import RightsizingRecommendation, rightsizing_summary


def _rec(**kw):
    base = dict(
        instance_id="i-1", instance_type="m5.2xlarge", name="web-1", region="us-east-1",
        account_id="123", resource_type="ec2", source="compute_optimizer",
        monthly_savings=120.0,
    )
    base.update(kw)
    return RightsizingRecommendation(**base)


def test_very_over_provisioned_is_genuine():
    a = assess(_rec(finding="VERY_OVER_PROVISIONED", avg_cpu_pct=6.0, monthly_savings=280.0))
    assert a.verdict == "genuine_savings"
    assert a.score >= 70
    assert a.adjusted_monthly_savings == 280.0  # no commitment ctx -> undiscounted


def test_high_peak_cpu_is_not_genuine():
    # Low average but spikes to 85% -> needs headroom, not a downsize.
    a = assess(_rec(source="cloudwatch_fallback", avg_cpu_pct=8.0, max_cpu_pct=85.0,
                    finding="", monthly_savings=90.0))
    assert a.verdict == "likely_false_positive"
    assert "peak" in a.why.lower()


def test_memory_bound_is_downgraded():
    # CPU idle but memory at 82% -> CPU downsize starves RAM.
    a = assess(_rec(finding="OVER_PROVISIONED", avg_cpu_pct=10.0, avg_mem_pct=82.0,
                    monthly_savings=120.0))
    assert a.verdict != "genuine_savings"
    assert "memory" in a.why.lower()


def test_trivial_savings_penalized():
    a = assess(_rec(finding="OVER_PROVISIONED", avg_cpu_pct=9.0, monthly_savings=6.0))
    assert a.adjusted_monthly_savings == 6.0
    assert "$6" in a.why or "6/mo" in a.why


def test_compute_optimizer_zero_max_cpu_not_treated_as_peak():
    # CO doesn't populate max_cpu (0.0). That must read as "unknown", not "0% peak".
    a = assess(_rec(finding="VERY_OVER_PROVISIONED", avg_cpu_pct=5.0, max_cpu_pct=0.0,
                    monthly_savings=250.0))
    assert a.verdict == "genuine_savings"
    assert "peak" not in a.why.lower()


def test_commitment_coverage_discounts_savings():
    ctx = CommitmentContext(available=True, sp_coverage_pct=80.0, ri_coverage_pct=10.0)
    a = assess(_rec(finding="VERY_OVER_PROVISIONED", avg_cpu_pct=6.0, monthly_savings=300.0), ctx)
    # 80% covered -> real savings floored at ~20% of the on-demand estimate.
    assert a.adjusted_monthly_savings == 60.0
    assert "covered" in a.why.lower()
    # A genuinely idle box stays genuine: coverage shrinks the reward, it does not
    # make the box un-idle. $60/mo real is still worth doing.
    assert a.verdict == "genuine_savings"


def test_commitment_coverage_can_sink_a_marginal_rec():
    # Same coverage, but a small on-demand estimate: 80% off $50 = $10 real, which
    # is below the magnitude floor -> no longer genuine.
    ctx = CommitmentContext(available=True, sp_coverage_pct=80.0, ri_coverage_pct=10.0)
    a = assess(_rec(finding="OVER_PROVISIONED", avg_cpu_pct=9.0, monthly_savings=50.0), ctx)
    assert a.adjusted_monthly_savings == 10.0
    assert a.verdict != "genuine_savings"


def test_no_commitment_context_means_no_discount():
    a = assess(_rec(finding="VERY_OVER_PROVISIONED", monthly_savings=300.0), None)
    assert a.adjusted_monthly_savings == 300.0


def test_action_is_resource_specific():
    assert "stop/start" in assess(_rec(resource_type="ec2")).action
    assert "memory" in assess(_rec(resource_type="lambda")).action.lower()
    assert "maintenance" in assess(_rec(resource_type="rds")).action.lower()


def test_summary_surfaces_genuine_total_and_verdicts():
    recs = [
        _rec(instance_id="i-good", finding="VERY_OVER_PROVISIONED", avg_cpu_pct=5.0, monthly_savings=300.0),
        _rec(instance_id="i-spiky", source="cloudwatch_fallback", finding="",
             avg_cpu_pct=8.0, max_cpu_pct=90.0, monthly_savings=100.0),
    ]
    out = rightsizing_summary(recs)
    assert out["total_monthly_savings"] == 400.0
    # only the good one counts toward genuine savings
    assert out["genuine_monthly_savings"] == 300.0
    assert out["verdicts"]["genuine_savings"] == 1
    assert out["verdicts"]["likely_false_positive"] == 1
    # genuine rec is ranked first
    assert out["recommendations"][0]["instance_id"] == "i-good"
    # rows carry the judgment, not verbose prose
    row = out["recommendations"][0]
    assert set(["verdict", "score", "why", "action", "adjusted_monthly_savings"]) <= set(row)
    assert "title" not in row and "description" not in row


def test_summary_with_commitment_context_block():
    ctx = CommitmentContext(available=True, sp_coverage_pct=75.0, ri_coverage_pct=20.0)
    out = rightsizing_summary([_rec(finding="VERY_OVER_PROVISIONED", monthly_savings=200.0)], ctx)
    assert out["commitment_context"]["ec2_sp_coverage_pct"] == 75.0
    assert "genuine_monthly_savings" in out


def test_token_cost_bounded_on_large_fleet():
    # A big fleet must not dump everything into the model context.
    recs = [_rec(instance_id=f"i-{n}", finding="OVER_PROVISIONED", monthly_savings=50.0 + n)
            for n in range(400)]
    out = rightsizing_summary(recs)
    from finops.token_budget import estimate_tokens
    assert out["total_instances_flagged"] == 400
    assert out.get("recommendations_truncated") is True
    assert estimate_tokens(out["recommendations"]) < 7000


def test_fetch_context_degrades_without_ce(monkeypatch):
    # No boto3 creds / CE unreachable -> unavailable context, never raises.
    _reset_cache_for_tests()
    import finops.recommendations.genuine_savings as gs

    def boom(*a, **k):
        raise RuntimeError("no CE")
    monkeypatch.setattr("finops.recommendations.commitments._get_date_range", boom, raising=False)
    ctx = gs.fetch_commitment_context(ce_client=object())
    assert ctx.available is False
    assert ctx.adjusted if False else ctx.combined_pct == 0.0
    _reset_cache_for_tests()
