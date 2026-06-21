"""Tests for the pluggable verifier registry (finops.recommendations.verifiers)
and its dispatch from auto_verify_acted_on.

Cloud reads are mocked: no real boto3 calls. We cover registry resolution, the
new idle-cleanup verifier's measure logic, EC2 still routing correctly, a source
with no verifier degrading gracefully, and an AST guard that the verifier path
only reads cloud state.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from finops.recommendations import verifiers
from finops.recommendations.verifiers import (
    get_verifier,
    register,
    verify_ec2_change,
    verify_idle_cleanup,
)


# ── ledger fixture (mirrors test_learning.py) ─────────────────────────────────

@pytest.fixture
def ledger(monkeypatch):
    td = tempfile.TemporaryDirectory()
    monkeypatch.setenv("FINOPS_DB_PATH", str(Path(td.name) / "t.db"))
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    yield db_mod
    db_mod._ENGINE = None
    td.cleanup()


_seq = [0]


def _seed(source, resource_type, status="acted_on", est=100.0, region="us-east-1",
          resource_id=None, recommended_config="{}"):
    from finops.storage.db import get_engine, savings_recommendations
    now = datetime.now(timezone.utc)
    _seq[0] += 1
    rid = resource_id or f"r{_seq[0]}"
    with get_engine().begin() as conn:
        result = conn.execute(savings_recommendations.insert().values(
            source=source, provider="aws", status=status,
            resource_type=resource_type, resource_id=rid, region=region,
            estimated_monthly_savings_usd=est, recommended_config=recommended_config,
            generated_at=now, dedup_key=f"k{_seq[0]}",
        ))
    return result.inserted_primary_key[0]


def _row(**kw):
    """A stand-in for a SQLAlchemy result row, accessed by attribute."""
    base = dict(resource_type="", region="us-east-1",
                estimated_monthly_savings_usd=0.0)
    base.update(kw)
    return SimpleNamespace(**base)


# ── registry dispatch ─────────────────────────────────────────────────────────

def test_registry_resolves_ec2_to_ec2_verifier():
    assert get_verifier("rightsizing", "ec2") is verify_ec2_change


def test_registry_resolves_idle_via_source_wildcard():
    # idle is registered as (idle, None), so any resource_type under it resolves.
    for rt in ("ebs_volume", "elastic_ip", "snapshot", "stopped_ec2", "load_balancer"):
        assert get_verifier("idle", rt) is verify_idle_cleanup


def test_registry_returns_none_for_unregistered_source():
    assert get_verifier("commitment", "savings_plan") is None
    assert get_verifier("rds", "rds") is None
    assert get_verifier(None, None) is None


def test_exact_match_wins_over_wildcard():
    sentinel = lambda *a: 1.0  # noqa: E731
    try:
        register("idle", "load_balancer", sentinel)
        assert get_verifier("idle", "load_balancer") is sentinel
        # other idle types still hit the wildcard
        assert get_verifier("idle", "ebs_volume") is verify_idle_cleanup
    finally:
        verifiers._REGISTRY.pop(("idle", "load_balancer"), None)


# ── idle verifier: measure logic ──────────────────────────────────────────────

class _FakeEC2:
    """Minimal stub: each describe_* returns whatever the test wired in."""
    def __init__(self, **responses):
        self._responses = responses

    def describe_volumes(self, **_):
        return self._responses.get("volumes", {"Volumes": []})

    def describe_addresses(self, **_):
        return self._responses.get("addresses", {"Addresses": []})

    def describe_snapshots(self, **_):
        return self._responses.get("snapshots", {"Snapshots": []})

    def describe_instances(self, **_):
        return self._responses.get("instances", {"Reservations": []})


def _patch_client(monkeypatch, fake):
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake)


def test_idle_ebs_gone_realizes_estimate(monkeypatch):
    # Volume deleted -> describe returns empty -> realize the estimate.
    _patch_client(monkeypatch, _FakeEC2(volumes={"Volumes": []}))
    row = _row(resource_type="ebs_volume", estimated_monthly_savings_usd=42.5)
    assert verify_idle_cleanup("vol-123", {}, row) == 42.5


def test_idle_ebs_still_present_not_verified(monkeypatch):
    # Volume still exists -> change has not landed -> None (stays acted_on).
    _patch_client(monkeypatch, _FakeEC2(volumes={"Volumes": [{"VolumeId": "vol-123"}]}))
    row = _row(resource_type="ebs_volume", estimated_monthly_savings_usd=42.5)
    assert verify_idle_cleanup("vol-123", {}, row) is None


def test_idle_elastic_ip_released_realizes_estimate(monkeypatch):
    _patch_client(monkeypatch, _FakeEC2(addresses={"Addresses": []}))
    row = _row(resource_type="elastic_ip", estimated_monthly_savings_usd=3.6)
    assert verify_idle_cleanup("eipalloc-abc", {}, row) == 3.6


def test_idle_snapshot_present_not_verified(monkeypatch):
    _patch_client(monkeypatch, _FakeEC2(snapshots={"Snapshots": [{"SnapshotId": "snap-1"}]}))
    row = _row(resource_type="snapshot", estimated_monthly_savings_usd=10.0)
    assert verify_idle_cleanup("snap-1", {}, row) is None


def test_idle_stopped_ec2_terminated_realizes_estimate(monkeypatch):
    fake = _FakeEC2(instances={"Reservations": [
        {"Instances": [{"State": {"Name": "terminated"}}]}
    ]})
    _patch_client(monkeypatch, fake)
    row = _row(resource_type="stopped_ec2", estimated_monthly_savings_usd=8.0)
    assert verify_idle_cleanup("i-abc", {}, row) == 8.0


def test_idle_stopped_ec2_still_stopped_not_verified(monkeypatch):
    fake = _FakeEC2(instances={"Reservations": [
        {"Instances": [{"State": {"Name": "stopped"}}]}
    ]})
    _patch_client(monkeypatch, fake)
    row = _row(resource_type="stopped_ec2", estimated_monthly_savings_usd=8.0)
    assert verify_idle_cleanup("i-abc", {}, row) is None


def test_idle_notfound_error_means_resource_gone(monkeypatch):
    # boto3 raises a *NotFound ClientError when the resource is already gone.
    class _NotFound(Exception):
        def __init__(self):
            self.response = {"Error": {"Code": "InvalidVolume.NotFound"}}

    class _RaisingEC2:
        def describe_volumes(self, **_):
            raise _NotFound()

    _patch_client(monkeypatch, _RaisingEC2())
    row = _row(resource_type="ebs_volume", estimated_monthly_savings_usd=15.0)
    assert verify_idle_cleanup("vol-x", {}, row) == 15.0


def test_idle_transient_error_stays_unverified(monkeypatch):
    # A throttling / non-NotFound error must NOT record a saving we did not measure.
    class _Throttled(Exception):
        def __init__(self):
            self.response = {"Error": {"Code": "Throttling"}}

    class _RaisingEC2:
        def describe_volumes(self, **_):
            raise _Throttled()

    _patch_client(monkeypatch, _RaisingEC2())
    row = _row(resource_type="ebs_volume", estimated_monthly_savings_usd=15.0)
    assert verify_idle_cleanup("vol-x", {}, row) is None


def test_idle_unknown_resource_type_is_noop(monkeypatch):
    _patch_client(monkeypatch, _FakeEC2())
    row = _row(resource_type="load_balancer", estimated_monthly_savings_usd=20.0)
    # load_balancer needs an elbv2 client we do not model here, so it stays
    # unverified rather than guessing.
    assert verify_idle_cleanup("arn:lb", {}, row) is None


# ── EC2 verifier: still measures correctly ────────────────────────────────────

def test_ec2_verifier_measures_resize(monkeypatch):
    fake = _FakeEC2(instances={"Reservations": [
        {"Instances": [{"InstanceType": "m5.large"}]}
    ]})
    _patch_client(monkeypatch, fake)
    cfg = {"instance_type": "m5.large", "from_instance_type": "m5.xlarge"}
    saving = verify_ec2_change("i-1", cfg, _row())
    assert saving is not None and saving > 0


def test_ec2_verifier_not_resized_yet(monkeypatch):
    fake = _FakeEC2(instances={"Reservations": [
        {"Instances": [{"InstanceType": "m5.xlarge"}]}
    ]})
    _patch_client(monkeypatch, fake)
    cfg = {"instance_type": "m5.large", "from_instance_type": "m5.xlarge"}
    assert verify_ec2_change("i-1", cfg, _row()) is None


# ── auto_verify_acted_on dispatch end to end ──────────────────────────────────

def test_auto_verify_routes_idle_and_writes_measured(ledger, monkeypatch):
    from finops.recommendations.savings_tracker import auto_verify_acted_on, list_recommendations
    rec_id = _seed("idle", "ebs_volume", est=42.5)
    _patch_client(monkeypatch, _FakeEC2(volumes={"Volumes": []}))  # volume gone

    out = auto_verify_acted_on()
    ids = {r["id"] for r in out}
    assert rec_id in ids

    verified = list_recommendations(status="verified")
    row = next(r for r in verified if r["id"] == rec_id)
    assert row["verified_monthly_savings_usd"] == 42.5


def test_auto_verify_routes_ec2_through_registry(ledger, monkeypatch):
    from finops.recommendations.savings_tracker import auto_verify_acted_on, list_recommendations
    import json
    cfg = json.dumps({"instance_type": "m5.large", "from_instance_type": "m5.xlarge"})
    rec_id = _seed("rightsizing", "ec2", est=120.0, recommended_config=cfg)
    fake = _FakeEC2(instances={"Reservations": [
        {"Instances": [{"InstanceType": "m5.large"}]}
    ]})
    _patch_client(monkeypatch, fake)

    out = auto_verify_acted_on()
    assert rec_id in {r["id"] for r in out}
    verified = list_recommendations(status="verified")
    assert any(r["id"] == rec_id for r in verified)


def test_auto_verify_unregistered_source_stays_acted_on(ledger, monkeypatch):
    """A source with no verifier must not crash and must stay acted_on."""
    from finops.recommendations.savings_tracker import auto_verify_acted_on, list_recommendations
    rec_id = _seed("commitment", "savings_plan", est=500.0)
    # No boto patch needed: the dispatcher should never reach a verifier.

    out = auto_verify_acted_on()
    assert rec_id not in {r["id"] for r in out}

    acted = list_recommendations(status="acted_on")
    assert any(r["id"] == rec_id for r in acted)
    verified = list_recommendations(status="verified")
    assert all(r["id"] != rec_id for r in verified)


def test_auto_verify_idle_not_gone_stays_acted_on(ledger, monkeypatch):
    from finops.recommendations.savings_tracker import auto_verify_acted_on, list_recommendations
    rec_id = _seed("idle", "ebs_volume", est=42.5)
    _patch_client(monkeypatch, _FakeEC2(volumes={"Volumes": [{"VolumeId": "x"}]}))  # still there

    out = auto_verify_acted_on()
    assert rec_id not in {r["id"] for r in out}
    acted = list_recommendations(status="acted_on")
    assert any(r["id"] == rec_id for r in acted)


# ── propose-only / read-only structural guard ─────────────────────────────────

def test_verifiers_never_mutate_cloud():
    """Read-only guarantee for the verifier path. Verifiers must read cloud state
    (describe_*) to measure, but must never call a mutating verb. Checked via the
    AST so docstring prose that names what it avoids does not count, only real
    attribute references and calls."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(verifiers))

    referenced = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    referenced |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}

    # No EC2/cloud mutation verbs, and no learning-status writers.
    forbidden = {
        "delete_volume", "release_address", "delete_snapshot",
        "terminate_instances", "stop_instances", "start_instances",
        "modify_instance_attribute", "modify_volume", "create_volume",
        "delete_load_balancer", "run_instances",
        "call_tool", "execute_bridge_tool", "mark_acted_on", "mark_dismissed",
        "system", "popen",
    }
    leaked = referenced & forbidden
    assert not leaked, f"verifiers references forbidden mutation calls: {leaked}"

    # Every cloud call the module makes must be a read (describe_*).
    cloud_calls = {
        a for a in referenced
        if a.startswith(("describe_", "delete_", "terminate_", "stop_",
                         "start_", "modify_", "create_", "release_", "run_"))
    }
    non_read = {c for c in cloud_calls if not c.startswith("describe_")}
    assert not non_read, f"verifiers issues non-read cloud calls: {non_read}"
