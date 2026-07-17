"""
run_deep_audit progress_callback + deadline_seconds tests.

Both kwargs are additive: default None must keep the MCP tool path
byte-identical, which the default-behavior test pins. The deadline test uses
one artificially slow region to prove partial results come back with the
timed-out region named.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from finops.analyzers import optimizer


def _fake_session():
    session = MagicMock()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    session.client.return_value = sts
    return session


def _finding(region: str, savings: float = 10.0) -> dict:
    return {
        "waste_type": "unattached_ebs",
        "severity": "medium",
        "region": region,
        "estimated_monthly_savings": savings,
        "account_id": None,
        "resource_id": f"vol-{region}",
    }


def _run(**kwargs):
    with (
        patch.object(optimizer, "_get_boto3_session", return_value=_fake_session()),
        patch.object(
            optimizer,
            "_fetch_compute_optimizer_recommendations",
            return_value=[],
        ),
    ):
        return optimizer.run_deep_audit(**kwargs)


def test_default_behavior_unchanged():
    # No callback, no deadline: the report shape and totals match the
    # pre-kwargs contract, and the new key is present but empty.
    with patch.object(
        optimizer, "_audit_region", side_effect=lambda s, r, c: [_finding(r)]
    ):
        report = _run(regions=["us-east-1", "us-west-2"], checks=["ebs"])
    assert report["total_findings"] == 2
    assert report["regions_scanned"] == ["us-east-1", "us-west-2"]
    assert report["regions_timed_out"] == []
    assert report["errors"] == []


def test_progress_callback_fires_per_region():
    calls: list[tuple] = []
    with patch.object(
        optimizer, "_audit_region", side_effect=lambda s, r, c: [_finding(r)]
    ):
        _run(
            regions=["us-east-1", "us-west-2", "eu-west-1"],
            checks=["ebs"],
            progress_callback=lambda *a: calls.append(a),
        )
    assert len(calls) == 3
    regions_seen = {c[0] for c in calls}
    assert regions_seen == {"us-east-1", "us-west-2", "eu-west-1"}
    # done counts are monotonically increasing 1..3, total is always 3
    assert sorted(c[2] for c in calls) == [1, 2, 3]
    assert all(c[3] == 3 for c in calls)


def test_progress_callback_exception_never_kills_scan():
    def boom(*a):
        raise RuntimeError("progress renderer crashed")

    with patch.object(
        optimizer, "_audit_region", side_effect=lambda s, r, c: [_finding(r)]
    ):
        report = _run(
            regions=["us-east-1"], checks=["ebs"], progress_callback=boom
        )
    assert report["total_findings"] == 1


def test_deadline_returns_partials_and_names_timed_out_regions():
    def slow_or_fast(session, region, checks):
        if region == "slow-region":
            time.sleep(3)
        return [_finding(region)]

    with patch.object(optimizer, "_audit_region", side_effect=slow_or_fast):
        start = time.monotonic()
        report = _run(
            regions=["us-east-1", "slow-region"],
            checks=["ebs"],
            max_workers=1,  # force the slow region to queue behind the fast one
            deadline_seconds=1.0,
        )
        elapsed = time.monotonic() - start

    # Returned promptly, did not wait the full 3s sleep
    assert elapsed < 2.5
    # The fast region's findings survived as partial results
    assert report["total_findings"] == 1
    assert report["findings"][0]["region"] == "us-east-1"
    assert report["regions_scanned"] == ["us-east-1"]
    assert report["regions_timed_out"] == ["slow-region"]


def test_deadline_skips_compute_optimizer_when_expired():
    co = MagicMock(return_value=[])

    def slow(session, region, checks):
        time.sleep(2)
        return []

    with (
        patch.object(optimizer, "_get_boto3_session", return_value=_fake_session()),
        patch.object(optimizer, "_fetch_compute_optimizer_recommendations", co),
        patch.object(optimizer, "_audit_region", side_effect=slow),
    ):
        optimizer.run_deep_audit(
            regions=["us-east-1"], checks=["ec2"], deadline_seconds=0.5
        )
    co.assert_not_called()


# ── regressions caught by the first live scan (0.8.181) ───────────────────────

def test_check_ebs_snapshots_has_no_clientmeta_bug():
    # A real boto3 client's `.meta` is a ClientMeta with no `.client` attr, so
    # `ec2_client.meta.client` raised in every region and killed the snapshot
    # check. The line was also dead (account id is fetched via boto3.client).
    # Mocks can't reproduce it (MagicMock.meta.client is happy), so guard the
    # exact broken construct in source.
    import inspect

    from finops.analyzers import waste

    src = inspect.getsource(waste.check_ebs_snapshots)
    assert ".meta.client" not in src


def test_deadline_marks_threads_abandoned_for_hard_exit():
    # When a region is still running at the deadline, run_deep_audit must flag
    # _threads_abandoned so the CLI hard-exits instead of hanging on the join.
    def _slow_region(session, region, checks):
        if region == "slow":
            time.sleep(5)
        return []

    with patch.object(optimizer, "_audit_region", side_effect=_slow_region):
        report = optimizer.run_deep_audit(
            account_id="123456789012",
            regions=["fast", "slow"],
            deadline_seconds=0.3,
        )
    assert report["regions_timed_out"] == ["slow"]
    assert report["_threads_abandoned"] is True
