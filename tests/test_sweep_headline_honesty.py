"""The cost sweep's headline: never a verdict on what was not read, never one
saving counted twice.

Two defects pinned here.

1. Every scanner in the sweep can fail (AccessDenied on a least-privilege role,
   a region outage) and the audit then has no findings. It answered that with
   "Your infrastructure looks well-optimized", a clean bill of health for an
   account nothing had read.

2. Orphaned EBS snapshot copies were counted twice. The idle-resource scanner
   prices every snapshot older than a week with no AMI behind it, and the
   cross-region replication scanner prices the same copies again as an
   aggregate with no resource id, so the per-resource collapse could not see
   the overlap. The replication scanner's own envelope already says its figure
   is an upper bound (it bills the full volume size for incremental snapshots)
   and an INFERRED investigation, not a precise claim.

   Worked example: volume vol-1 (100 GB) is gone. Its snapshot lives in
   us-east-1 and was copied to us-west-2 and eu-west-1; a copy made by
   CopySnapshot carries the placeholder volume id vol-ffffffff. Idle snapshots:
   $5.00 in each region (100 GB x $0.05), $15.00. Replication: the two copies
   share vol-ffffffff across two regions and the volume is not live, so
   100 GB x $0.05 x 2 = $10.00 for the same two copies. The sweep total was
   $25.00 for at most $15.00 of snapshot storage.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from finops import server
from finops.recommendations import sweep as sweep_mod


def _audit_env(monkeypatch):
    monkeypatch.setattr(server, "require_role", lambda *a, **k: None)
    aws = server.CLOUD_CONNECTORS.get("aws")

    async def _configured():
        return True

    monkeypatch.setattr(aws, "is_configured", _configured)


# ── 1. a failed read is not a clean account ─────────────────────────────────

def _boom(**kw):
    raise RuntimeError("AccessDenied")


def _nothing(**kw):
    return []


def test_every_scanner_failing_is_not_reported_as_well_optimized(monkeypatch):
    _audit_env(monkeypatch)
    monkeypatch.setattr(sweep_mod, "build_specs", lambda aws, regions: [
        (n, _boom, {}) for n in ("graviton", "spot", "idle_resources", "idle_rds")])
    out = asyncio.run(server.run_full_cost_audit())
    assert "well-optimized" not in out, out
    assert "could not run" in out, out
    for name in ("graviton", "spot", "idle_resources", "idle_rds"):
        assert name in out, out


def test_a_partial_read_with_nothing_found_says_it_was_partial(monkeypatch):
    _audit_env(monkeypatch)
    monkeypatch.setattr(sweep_mod, "build_specs", lambda aws, regions: [
        ("graviton", _nothing, {}), ("spot", _boom, {})])
    out = asyncio.run(server.run_full_cost_audit())
    assert "well-optimized" not in out, out
    assert "spot" in out and "could not run" in out, out


def test_an_idle_scan_with_unread_regions_is_named_beside_the_total(monkeypatch, tmp_path):
    """The idle scanner returns what it read and drops a region that raised. The
    sweep counted that as a full read, so the audit total and both exports said
    nothing about the region that was never scanned."""
    from finops.cleanup.idle import IdleResource, IdleScan
    from finops.tools import notifications as nt

    def _partial(**kw):
        found = IdleScan([IdleResource(
            resource_type="ebs_volume", resource_id="vol-1", region="us-east-1",
            account_id="1", name="", idle_since="2026-01-01", idle_days=90,
            monthly_cost_usd=40.0, reason="Unattached gp3 volume")])
        found.regions_not_read["eu-west-1"] = "UnauthorizedOperation"
        return found

    _audit_env(monkeypatch)
    monkeypatch.setattr(sweep_mod, "build_specs", lambda aws, regions: [
        ("idle_resources", _partial, {})])
    out = asyncio.run(server.run_full_cost_audit())
    assert "$40.00" in out and "eu-west-1" in out, out

    monkeypatch.setattr(server.CLOUD_CONNECTORS.get("aws"), "_account_id",
                        lambda: "123456789012")
    dest = tmp_path / "report.csv"
    monkeypatch.setattr(nt._srv, "_resolve_safe_path", lambda p: str(p))
    msg = asyncio.run(server.export_cost_report_csv(output_path=str(dest)))
    assert "eu-west-1" in msg, msg
    assert "eu-west-1" in dest.read_text(), dest.read_text()


def test_a_full_read_with_nothing_found_still_reads_as_clean(monkeypatch):
    _audit_env(monkeypatch)
    monkeypatch.setattr(sweep_mod, "build_specs", lambda aws, regions: [
        ("graviton", _nothing, {}), ("spot", _nothing, {})])
    out = asyncio.run(server.run_full_cost_audit())
    assert "No savings opportunities found" in out, out
    assert "could not run" not in out, out


# ── 2. one snapshot, one saving ──────────────────────────────────────────────

_OLD = datetime.now(UTC) - timedelta(days=400)


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kw):
        return iter(self._pages)


class _EC2:
    """Just enough EC2 for the idle-snapshot scan of one region."""

    def __init__(self, snapshots):
        self._snaps = snapshots

    def get_paginator(self, op):
        if op == "describe_snapshots":
            return _Paginator([{"Snapshots": self._snaps}])
        if op == "describe_images":
            return _Paginator([{"Images": []}])
        raise AssertionError(op)

    def describe_images(self, **kw):
        return {"Images": []}


_SNAPSHOTS = {
    "us-east-1": [{"SnapshotId": "snap-src", "VolumeId": "vol-1", "VolumeSize": 100,
                   "StartTime": _OLD, "Description": ""}],
    "us-west-2": [{"SnapshotId": "snap-w2", "VolumeId": "vol-ffffffff", "VolumeSize": 100,
                   "StartTime": _OLD + timedelta(days=1),
                   "Description": "Copied for DestinationAmi from snap-src"}],
    "eu-west-1": [{"SnapshotId": "snap-ew1", "VolumeId": "vol-ffffffff", "VolumeSize": 100,
                   "StartTime": _OLD + timedelta(days=2),
                   "Description": "Copied for DestinationAmi from snap-src"}],
}


def _idle_snapshot_findings():
    from finops.cleanup import idle

    out = []
    for region, snaps in _SNAPSHOTS.items():
        out.extend(idle._scan_old_snapshots(_EC2(snaps), "123456789012", region, 7))
    return out


def _replication_report():
    from finops.recommendations import ebs_snapshot_replication as rep

    findings = rep._build_cross_region_findings(
        _SNAPSHOTS, {r: set() for r in _SNAPSHOTS}, datetime.now(UTC))
    saveable = sum(f["total_monthly_cost"] for f in findings if f["orphaned"])
    return {"cross_region_findings": findings, "potential_monthly_savings": round(saveable, 2)}


def test_the_fixture_reproduces_both_scanners_pricing_the_same_copies():
    idle_total = sum(r.monthly_cost_usd for r in _idle_snapshot_findings())
    assert round(idle_total, 2) == 15.0
    assert _replication_report()["potential_monthly_savings"] == 10.0


def test_orphaned_snapshot_copies_are_not_counted_twice_in_the_sweep():
    idle_rows = sweep_mod.normalise("idle_resources", _idle_snapshot_findings())
    rep_rows = sweep_mod.normalise("ebs_rep", _replication_report())
    kept, _ = sweep_mod.collapse_per_resource(idle_rows + rep_rows)
    total = sum(f.get("monthly_savings") or 0 for f in kept)
    # Every snapshot here is priced at its full 100 GB by the idle scan, an upper
    # bound already; the replication aggregate adds nothing the bill could hold.
    assert round(total, 2) == 15.0, kept
    # The replication finding is not dropped: it stays visible, with a size band
    # instead of a figure, as its own scanner classifies it.
    rep = [f for f in kept if "cross-region" in f["title"]]
    assert rep and rep[0]["monthly_savings"] is None and rep[0].get("magnitude"), rep


def test_the_notion_report_renders_a_finding_without_a_figure():
    """A finding with no figure (an investigation, or a claim the critique
    retracted) reached the Notion table as None * 12 and the publish failed with
    a TypeError. The row says what happened instead of a number."""
    from finops.connectors.saas.notion import NotionConnector

    report = {
        "findings": [
            {"title": "Migrate i-1", "category": "Compute", "monthly_savings": 40.0},
            {"title": "Review 1 cross-region EBS snapshot set(s)", "category": "Storage",
             "monthly_savings": None, "magnitude": "under ~$100/mo"},
        ],
        "total_monthly_savings": 40.0, "total_annual_savings": 480.0,
    }
    blocks = NotionConnector()._build_report_blocks(report, "2026-10-13")
    table = next(b for b in blocks if b["type"] == "table")
    cells = [[c[0]["text"]["content"] for c in row["table_row"]["cells"]]
             for row in table["table"]["children"][1:]]
    assert cells[0][2:] == ["$40.00", "$480.00"]
    assert "needs confirming" in cells[1][2] and "$" not in cells[1][3], cells
