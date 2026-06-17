"""Tests for finops.recommendations.gcp_waste."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from finops.recommendations import gcp_waste
from finops.recommendations.gcp_waste import (
    IDLE_IP_MONTHLY,
    SNAPSHOT_GB_MONTHLY,
    VM_VCPU_MONTHLY,
    _disk_rate,
    _region_from_zone,
    _vcpus_from_machine_type,
    audit_gcp_waste,
)

MOD = "finops.recommendations.gcp_waste"


# ── helpers ───────────────────────────────────────────────────────────────────


def _client(projects=("proj-1",)):
    return SimpleNamespace(project_ids=lambda: list(projects))


def _disk(name, size_gb, users=(), type_="pd-ssd", zone="us-central1-a", status="READY"):
    return SimpleNamespace(name=name, size_gb=size_gb, users=list(users),
                           type_=type_, zone=zone, status=status)


def _addr(name, status="RESERVED", address_type="EXTERNAL", region="us-central1", address="1.2.3.4"):
    return SimpleNamespace(name=name, status=status, address_type=address_type,
                           region=region, address=address)


def _snap(name, disk_size_gb, creation_timestamp):
    return SimpleNamespace(name=name, disk_size_gb=disk_size_gb,
                           creation_timestamp=creation_timestamp)


def _inst(name, machine_type="e2-standard-4", zone="us-central1-a", iid="111"):
    return SimpleNamespace(name=name, machine_type=machine_type, zone=zone,
                           status="RUNNING", id=iid)


def _run(client=None, **kw):
    return asyncio.run(audit_gcp_waste(client or _client(), **kw))


def _patch_all(disks=(), addrs=(), snaps=(), insts=(), cpu=None):
    """Patch every GCP fetch seam at once. cpu = avg utilization for idle check."""
    return [
        patch(f"{MOD}._list_disks", return_value=list(disks)),
        patch(f"{MOD}._list_addresses", return_value=list(addrs)),
        patch(f"{MOD}._list_snapshots", return_value=list(snaps)),
        patch(f"{MOD}._list_running_instances", return_value=list(insts)),
        patch(f"{MOD}._instance_cpu_utilization", return_value=cpu),
    ]


def _with(patches, fn):
    if not patches:
        return fn()
    with patches[0]:
        return _with(patches[1:], fn)


# ── pure parsers ──────────────────────────────────────────────────────────────


def test_region_from_zone():
    assert _region_from_zone("us-central1-a") == "us-central1"
    assert _region_from_zone("https://www.googleapis.com/.../zones/europe-west1-b") == "europe-west1"
    assert _region_from_zone("us-east1") == "us-east1"  # already a region


def test_vcpus_from_machine_type():
    assert _vcpus_from_machine_type("e2-standard-4") == 4
    assert _vcpus_from_machine_type(".../machineTypes/n1-highmem-8") == 8
    assert _vcpus_from_machine_type("e2-micro") is None


def test_disk_rate_by_type():
    assert _disk_rate("pd-ssd") == 0.17
    assert _disk_rate(".../diskTypes/pd-standard") == 0.04
    assert _disk_rate("something-unknown") == gcp_waste.DISK_RATE_DEFAULT


# ── structure + no-projects ───────────────────────────────────────────────────


def test_no_projects_returns_error():
    out = _run(SimpleNamespace(project_ids=lambda: []))
    assert "error" in out and "project" in out["error"].lower()


def test_structure_and_empty():
    out = _with(_patch_all(), lambda: _run(projects=["proj-1"]))
    for k in ("provider", "projects_scanned", "checks_run", "findings", "total_findings",
              "total_estimated_monthly_savings", "total_estimated_annual_savings",
              "by_category", "by_severity", "by_project", "errors"):
        assert k in out
    assert out["provider"] == "gcp"
    assert out["total_findings"] == 0
    assert out["total_estimated_monthly_savings"] == 0.0
    assert out["errors"] == []


# ── disks ─────────────────────────────────────────────────────────────────────


def test_unattached_disk_flagged_with_savings():
    out = _with(_patch_all(disks=[_disk("d1", 100, users=())]),
                lambda: _run(projects=["proj-1"], checks=["disks"]))
    assert out["total_findings"] == 1
    f = out["findings"][0]
    assert f["category"] == "unattached_disk"
    assert f["resource_id"] == "d1"
    assert f["region"] == "us-central1"
    assert f["estimated_monthly_savings"] == round(100 * 0.17, 2)  # pd-ssd
    assert f["severity"] == "medium"  # $17 -> medium


def test_attached_disk_not_flagged():
    out = _with(_patch_all(disks=[_disk("d2", 100, users=["inst-x"])]),
                lambda: _run(projects=["proj-1"], checks=["disks"]))
    assert out["total_findings"] == 0


def test_large_standard_disk_high_severity():
    out = _with(_patch_all(disks=[_disk("d3", 500, type_="pd-standard")]),
                lambda: _run(projects=["proj-1"], checks=["disks"]))
    f = out["findings"][0]
    assert f["estimated_monthly_savings"] == round(500 * 0.04, 2)  # $20
    assert f["severity"] == "high"


# ── idle IPs ──────────────────────────────────────────────────────────────────


def test_reserved_ip_flagged():
    out = _with(_patch_all(addrs=[_addr("ip1", status="RESERVED")]),
                lambda: _run(projects=["proj-1"], checks=["ips"]))
    assert out["total_findings"] == 1
    f = out["findings"][0]
    assert f["category"] == "idle_ip"
    assert f["estimated_monthly_savings"] == round(IDLE_IP_MONTHLY, 2)


def test_in_use_ip_not_flagged():
    out = _with(_patch_all(addrs=[_addr("ip2", status="IN_USE")]),
                lambda: _run(projects=["proj-1"], checks=["ips"]))
    assert out["total_findings"] == 0


# ── snapshots ─────────────────────────────────────────────────────────────────


def test_old_snapshot_flagged():
    out = _with(_patch_all(snaps=[_snap("s1", 200, "2020-01-01T00:00:00Z")]),
                lambda: _run(projects=["proj-1"], checks=["snapshots"]))
    assert out["total_findings"] == 1
    f = out["findings"][0]
    assert f["category"] == "old_snapshot"
    assert f["severity"] == "low"
    assert f["estimated_monthly_savings"] == round(200 * SNAPSHOT_GB_MONTHLY, 2)


def test_recent_snapshot_not_flagged():
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    out = _with(_patch_all(snaps=[_snap("s2", 200, recent)]),
                lambda: _run(projects=["proj-1"], checks=["snapshots"]))
    assert out["total_findings"] == 0


# ── idle VMs ──────────────────────────────────────────────────────────────────


def test_idle_vm_flagged():
    out = _with(_patch_all(insts=[_inst("vm1", "e2-standard-4")], cpu=0.02),
                lambda: _run(projects=["proj-1"], checks=["idle_vms"]))
    assert out["total_findings"] == 1
    f = out["findings"][0]
    assert f["category"] == "idle_vm"
    assert f["estimated_monthly_savings"] == round(4 * VM_VCPU_MONTHLY, 2)
    assert f["detail"]["avg_cpu_pct"] == 2.0


def test_busy_vm_not_flagged():
    out = _with(_patch_all(insts=[_inst("vm2")], cpu=0.55),
                lambda: _run(projects=["proj-1"], checks=["idle_vms"]))
    assert out["total_findings"] == 0


def test_vm_no_metric_data_not_flagged():
    out = _with(_patch_all(insts=[_inst("vm3")], cpu=None),
                lambda: _run(projects=["proj-1"], checks=["idle_vms"]))
    assert out["total_findings"] == 0


# ── aggregation ───────────────────────────────────────────────────────────────


def test_aggregates_and_sorting():
    out = _with(
        _patch_all(
            disks=[_disk("d1", 100), _disk("d2", 500, type_="pd-standard")],
            addrs=[_addr("ip1")],
            snaps=[_snap("s1", 200, "2020-01-01T00:00:00Z")],
            insts=[_inst("vm1", "e2-standard-4")],
            cpu=0.01,
        ),
        lambda: _run(projects=["proj-1"]),
    )
    assert out["total_findings"] == 5
    # sorted by monthly savings desc: idle vm ($96) first, snapshot ($5.2) last
    savings = [f["estimated_monthly_savings"] for f in out["findings"]]
    assert savings == sorted(savings, reverse=True)
    assert out["findings"][0]["category"] == "idle_vm"
    expected = round(96 + 20 + 17 + 7.30 + 5.2, 2)
    assert out["total_estimated_monthly_savings"] == expected
    assert out["total_estimated_annual_savings"] == round(expected * 12, 2)
    assert out["by_category"]["unattached_disk"]["count"] == 2
    assert set(out["by_project"].keys()) == {"proj-1"}


def test_checks_subset_only_runs_requested():
    # Only disks requested: the IP fixture must be ignored.
    out = _with(_patch_all(disks=[_disk("d1", 100)], addrs=[_addr("ip1")]),
                lambda: _run(projects=["proj-1"], checks=["disks"]))
    assert out["checks_run"] == ["disks"]
    assert out["total_findings"] == 1
    assert out["findings"][0]["category"] == "unattached_disk"


# ── GCPConnector.project_ids() (feeds the waste audit) ────────────────────────


def test_gcp_connector_project_ids_from_env(monkeypatch):
    """project_ids() parses GCP_PROJECT_IDS comma-separated and trims blanks."""
    from finops.connectors.gcp import GCPConnector
    monkeypatch.setenv("GCP_PROJECT_IDS", " proj-a , proj-b ,")
    assert GCPConnector().project_ids() == ["proj-a", "proj-b"]


def test_gcp_connector_project_ids_no_env_returns_list(monkeypatch):
    """With no env var, it falls back to ADC and never raises; always a list."""
    monkeypatch.delenv("GCP_PROJECT_IDS", raising=False)
    from finops.connectors.gcp import GCPConnector
    assert isinstance(GCPConnector().project_ids(), list)
