"""Tests for the scheduled watchdog job stub.

Proves the job runs the correlator, shapes an approval card, and dedups. The
push is stubbed, so no notification fires and nothing is wired to execution.
"""
import asyncio

from finops.recommendations.rightsizing import RightsizingRecommendation
from finops.watchdog import job as wd_job


def _rec(iid="i-abc", savings=120.0) -> RightsizingRecommendation:
    return RightsizingRecommendation(
        instance_id=iid,
        instance_type="m5.xlarge",
        name=f"name-{iid}",
        region="us-east-1",
        account_id="111111111111",
        resource_type="ec2",
        source="compute_optimizer",
        avg_cpu_pct=5.0,
        recommended_type="m5.large",
        monthly_savings=savings,
        confidence="high",
    )


def test_run_watchdog_prepares_cards(monkeypatch, tmp_path):
    # Point dedup state at a temp dir so the test is isolated.
    monkeypatch.setenv("FINOPS_HOME", str(tmp_path))

    # Feed the correlator fixed findings instead of a live scan. Build them once
    # from the real function, then hand back a plain lambda so patching does not
    # recurse into itself.
    from finops.watchdog import correlator

    fixed = correlator.correlate_spend_and_utilization(
        _rightsizing_recs=[_rec(savings=120.0)],
        _idle_resources=[],
    )
    monkeypatch.setattr(
        correlator, "correlate_spend_and_utilization", lambda *a, **k: fixed
    )

    pushed_cards = []
    monkeypatch.setattr(
        wd_job, "_push_one_click_card",
        lambda f: (pushed_cards.append(f.resource_id) or True),
    )

    result = asyncio.run(wd_job._run_watchdog())
    assert result["findings"] == 1
    assert result["pushed"] == ["i-abc"]
    assert pushed_cards == ["i-abc"]
    assert result["total_monthly_waste_usd"] == 120.0


def test_watchdog_dedups_within_month(monkeypatch, tmp_path):
    monkeypatch.setenv("FINOPS_HOME", str(tmp_path))
    from finops.watchdog import correlator

    fixed = correlator.correlate_spend_and_utilization(
        _rightsizing_recs=[_rec()], _idle_resources=[],
    )
    monkeypatch.setattr(
        correlator, "correlate_spend_and_utilization", lambda *a, **k: fixed
    )
    calls = []
    monkeypatch.setattr(
        wd_job, "_push_one_click_card",
        lambda f: (calls.append(f.resource_id) or True),
    )

    first = asyncio.run(wd_job._run_watchdog())
    second = asyncio.run(wd_job._run_watchdog())

    # Pushed on the first run, deduped on the second.
    assert first["pushed"] == ["i-abc"]
    assert second["pushed"] == []
    assert calls == ["i-abc"]


def test_push_card_stub_is_inert():
    # The real stub returns True and performs no network / execution. It only
    # shapes and logs a card. Calling it directly must not raise.
    findings = _run_findings()
    assert wd_job._push_one_click_card(findings[0]) is True


def _run_findings():
    from finops.watchdog.correlator import correlate_spend_and_utilization
    return correlate_spend_and_utilization(
        _rightsizing_recs=[_rec()], _idle_resources=[],
    )
