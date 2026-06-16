"""
GCP idle and orphaned resource audit.

The GCP billing export tells you what you spent, never that a disk is
unattached or a VM is idle. Those signals only exist if you enumerate the
resources and, for compute, join Cloud Monitoring utilization. This module is
the GCP analog of the AWS waste audit: it scans Compute Engine across every
zone/region and returns findings sorted by estimated monthly savings.

Checks (config-only unless noted):
  - unattached_disk : persistent disks with no attached instance (users[] empty)
  - idle_ip         : reserved static external IPs not in use (GCP bills these)
  - old_snapshot    : snapshots older than a threshold (storage you keep paying for)
  - idle_vm         : RUNNING instances with near-zero CPU over a window
                      (this one joins Cloud Monitoring)

Pricing here uses documented GCP list-price estimates (commented inline). Exact
per-SKU pricing from the Cloud Billing Catalog is a later refinement; the dollar
figures are deliberately conservative so we never overstate savings.

The fetch functions (_list_disks, _list_addresses, _list_snapshots,
_list_running_instances, _instance_cpu_utilization) wrap the GCP clients and are
the seams the tests patch, so the suite never needs the google-cloud-compute /
google-cloud-monitoring SDKs installed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ── Pricing (GCP list-price estimates, us-central1-ish, USD/month) ─────────────
# Persistent disk storage, per GB-month. Picked by disk type.
DISK_RATES: dict[str, float] = {
    "pd-standard": 0.04,
    "pd-balanced": 0.10,
    "pd-ssd": 0.17,
    "pd-extreme": 0.125,
    "hyperdisk-balanced": 0.10,
}
DISK_RATE_DEFAULT: float = 0.10

# A reserved static external IP that is NOT attached is billed at ~$0.010/hr.
# GCP bills a 730-hour average month. Attached/in-use IPs are not the waste here.
IDLE_IP_MONTHLY: float = 0.010 * 730  # ~$7.30/mo

# Snapshot storage, per GB-month (multi-regional standard ~ $0.026).
SNAPSHOT_GB_MONTHLY: float = 0.026

# Idle VM: blended on-demand estimate per vCPU-month covering vCPU + memory +
# overhead (e2/n1-standard land near here). Used only when we can parse the vCPU
# count from the machine type name; otherwise the VM is still flagged with $0 so
# the idleness is visible without us inventing a number.
VM_VCPU_MONTHLY: float = 24.0

# An instance averaging below this CPU fraction over the window is "idle".
IDLE_CPU_THRESHOLD: float = 0.05  # 5%

# Safety cap so a huge fleet does not fan out into thousands of Monitoring calls.
MAX_INSTANCES_FOR_IDLE: int = 200

ALL_CHECKS: tuple[str, ...] = ("disks", "ips", "snapshots", "idle_vms")


# ── small parsers ─────────────────────────────────────────────────────────────


def _short(url_or_name: str) -> str:
    """Last path segment of a GCP resource URL (zone/region/type), or the value."""
    if not url_or_name:
        return ""
    return str(url_or_name).rstrip("/").rsplit("/", 1)[-1]


def _region_from_zone(zone: str) -> str:
    """us-central1-a -> us-central1. Leaves a region or empty value unchanged."""
    z = _short(zone)
    parts = z.rsplit("-", 1)
    # zones end in a single letter suffix (-a/-b/-c); regions do not
    if len(parts) == 2 and len(parts[1]) == 1 and parts[1].isalpha():
        return parts[0]
    return z


def _disk_rate(type_url: str) -> float:
    t = _short(type_url).lower()
    for key, rate in DISK_RATES.items():
        if key in t:
            return rate
    return DISK_RATE_DEFAULT


def _vcpus_from_machine_type(machine_type: str) -> int | None:
    """
    Parse the trailing vCPU count from a machine type name, e.g.
    e2-standard-4 -> 4, n1-highmem-8 -> 8, n2-standard-16 -> 16.
    Custom and shared-core (e2-micro/small/medium) types return None.
    """
    name = _short(machine_type)
    tail = name.rsplit("-", 1)[-1] if "-" in name else ""
    if tail.isdigit():
        return int(tail)
    return None


def _age_days(creation_timestamp: str) -> float | None:
    """Days since an RFC3339 creation timestamp, or None if unparseable."""
    if not creation_timestamp:
        return None
    ts = str(creation_timestamp).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def _severity_for_savings(monthly: float) -> str:
    if monthly >= 20:
        return "high"
    if monthly >= 5:
        return "medium"
    return "low"


# ── GCP fetch seams (patched in tests) ────────────────────────────────────────


def _list_disks(project: str) -> list[Any]:
    """All persistent disks in a project (across zones) via aggregated list."""
    from google.cloud import compute_v1

    client = compute_v1.DisksClient()
    out: list[Any] = []
    for _scope, scoped in client.aggregated_list(project=project):
        out.extend(getattr(scoped, "disks", None) or [])
    return out


def _list_addresses(project: str) -> list[Any]:
    """All regional + global addresses in a project."""
    from google.cloud import compute_v1

    out: list[Any] = []
    regional = compute_v1.AddressesClient()
    for _scope, scoped in regional.aggregated_list(project=project):
        out.extend(getattr(scoped, "addresses", None) or [])
    try:
        out.extend(list(compute_v1.GlobalAddressesClient().list(project=project)))
    except Exception as exc:  # global IPs are optional, never fail the scan on them
        log.debug("global address list failed for %s: %s", project, exc)
    return out


def _list_snapshots(project: str) -> list[Any]:
    from google.cloud import compute_v1

    return list(compute_v1.SnapshotsClient().list(project=project))


def _list_running_instances(project: str) -> list[Any]:
    from google.cloud import compute_v1

    client = compute_v1.InstancesClient()
    out: list[Any] = []
    for _scope, scoped in client.aggregated_list(project=project):
        for inst in getattr(scoped, "instances", None) or []:
            if str(getattr(inst, "status", "")).upper() == "RUNNING":
                out.append(inst)
    return out


def _instance_cpu_utilization(project: str, instance_id: str, days: int) -> float | None:
    """
    Mean CPU utilization (0..1) for an instance over the last `days`, or None if
    Monitoring has no data. Uses compute.googleapis.com/instance/cpu/utilization.
    """
    from google.cloud import monitoring_v3

    client = monitoring_v3.MetricServiceClient()
    now = datetime.now(timezone.utc)
    seconds = int(now.timestamp())
    interval = monitoring_v3.TimeInterval(
        {"end_time": {"seconds": seconds}, "start_time": {"seconds": seconds - days * 86400}}
    )
    flt = (
        'metric.type="compute.googleapis.com/instance/cpu/utilization" '
        f'AND resource.labels.instance_id="{instance_id}"'
    )
    series = client.list_time_series(
        request={
            "name": f"projects/{project}",
            "filter": flt,
            "interval": interval,
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        }
    )
    vals: list[float] = []
    for ts in series:
        for point in ts.points:
            vals.append(float(point.value.double_value))
    if not vals:
        return None
    return sum(vals) / len(vals)


# ── per-check logic (operate on already-fetched data) ─────────────────────────


def _check_disks(project: str, disks: list[Any]) -> list[dict]:
    findings: list[dict] = []
    for d in disks:
        users = getattr(d, "users", None) or []
        status = str(getattr(d, "status", "")).upper()
        if users or status not in ("", "READY"):
            continue
        size_gb = int(getattr(d, "size_gb", 0) or 0)
        rate = _disk_rate(getattr(d, "type_", "") or getattr(d, "type", ""))
        monthly = round(size_gb * rate, 2)
        zone = _short(getattr(d, "zone", ""))
        dtype = _short(getattr(d, "type_", "") or getattr(d, "type", "")) or "disk"
        findings.append({
            "category": "unattached_disk",
            "severity": _severity_for_savings(monthly),
            "resource_type": "compute_disk",
            "resource_id": getattr(d, "name", ""),
            "project": project,
            "region": _region_from_zone(zone),
            "estimated_monthly_savings": monthly,
            "description": (
                f"Unattached {dtype} disk '{getattr(d, 'name', '')}' ({size_gb} GB) "
                f"in {zone or 'unknown zone'} has no instance attached."
            ),
            "detail": {"zone": zone, "size_gb": size_gb, "disk_type": dtype},
        })
    return findings


def _check_addresses(project: str, addresses: list[Any]) -> list[dict]:
    findings: list[dict] = []
    for a in addresses:
        status = str(getattr(a, "status", "")).upper()
        atype = str(getattr(a, "address_type", "") or "EXTERNAL").upper()
        # RESERVED == reserved but not attached to anything. IN_USE is not waste.
        if status != "RESERVED" or atype != "EXTERNAL":
            continue
        region = _short(getattr(a, "region", "")) or "global"
        findings.append({
            "category": "idle_ip",
            "severity": _severity_for_savings(IDLE_IP_MONTHLY),
            "resource_type": "compute_address",
            "resource_id": getattr(a, "name", ""),
            "project": project,
            "region": region,
            "estimated_monthly_savings": round(IDLE_IP_MONTHLY, 2),
            "description": (
                f"Reserved static external IP '{getattr(a, 'name', '')}' "
                f"({getattr(a, 'address', '')}) in {region} is not in use but still billed."
            ),
            "detail": {"address": getattr(a, "address", ""), "status": status},
        })
    return findings


def _check_snapshots(project: str, snapshots: list[Any], age_days: int) -> list[dict]:
    findings: list[dict] = []
    for s in snapshots:
        age = _age_days(getattr(s, "creation_timestamp", "") or "")
        if age is None or age < age_days:
            continue
        size_gb = int(getattr(s, "disk_size_gb", 0) or 0)
        monthly = round(size_gb * SNAPSHOT_GB_MONTHLY, 2)
        findings.append({
            "category": "old_snapshot",
            "severity": "low",
            "resource_type": "compute_snapshot",
            "resource_id": getattr(s, "name", ""),
            "project": project,
            "region": "",
            "estimated_monthly_savings": monthly,
            "description": (
                f"Snapshot '{getattr(s, 'name', '')}' ({size_gb} GB) is {int(age)} days "
                f"old. Review whether it is still needed."
            ),
            "detail": {"age_days": int(age), "disk_size_gb": size_gb},
        })
    return findings


def _idle_vm_finding(project: str, inst: Any, avg_cpu: float) -> dict:
    machine = _short(getattr(inst, "machine_type", ""))
    vcpus = _vcpus_from_machine_type(getattr(inst, "machine_type", ""))
    monthly = round((vcpus or 0) * VM_VCPU_MONTHLY, 2)
    zone = _short(getattr(inst, "zone", ""))
    return {
        "category": "idle_vm",
        "severity": _severity_for_savings(monthly) if monthly else "medium",
        "resource_type": "compute_instance",
        "resource_id": getattr(inst, "name", ""),
        "project": project,
        "region": _region_from_zone(zone),
        "estimated_monthly_savings": monthly,
        "description": (
            f"Instance '{getattr(inst, 'name', '')}' ({machine}) averaged "
            f"{avg_cpu * 100:.1f}% CPU. Stop or rightsize it."
        ),
        "detail": {"zone": zone, "machine_type": machine, "avg_cpu_pct": round(avg_cpu * 100, 2),
                   "vcpus": vcpus},
    }


# ── orchestrator ──────────────────────────────────────────────────────────────


async def audit_gcp_waste(
    gcp_client: Any,
    projects: list[str] | None = None,
    checks: list[str] | None = None,
    idle_days: int = 14,
    snapshot_age_days: int = 30,
) -> dict:
    """
    Scan GCP projects for idle and orphaned resources.

    gcp_client: a GCPConnector (used for project_ids() when projects is None).
    projects:   explicit project IDs to scan; defaults to gcp_client.project_ids().
    checks:     subset of {"disks","ips","snapshots","idle_vms"}; defaults to all.
    """
    if not projects:
        getter = getattr(gcp_client, "project_ids", None)
        projects = list(getter() if callable(getter) else [])
    if not projects:
        return {
            "error": "No GCP project IDs found. Set GCP_PROJECT_IDS (comma-separated) "
                     "or configure Application Default Credentials with a default project.",
        }

    run = [c for c in (checks or ALL_CHECKS) if c in ALL_CHECKS]
    if not run:
        run = list(ALL_CHECKS)

    findings: list[dict] = []
    errors: list[dict] = []

    async def _scan_project(project: str) -> None:
        if "disks" in run:
            try:
                disks = await asyncio.to_thread(_list_disks, project)
                findings.extend(_check_disks(project, disks))
            except Exception as e:
                errors.append({"project": project, "check": "disks", "error": str(e)})
        if "ips" in run:
            try:
                addrs = await asyncio.to_thread(_list_addresses, project)
                findings.extend(_check_addresses(project, addrs))
            except Exception as e:
                errors.append({"project": project, "check": "ips", "error": str(e)})
        if "snapshots" in run:
            try:
                snaps = await asyncio.to_thread(_list_snapshots, project)
                findings.extend(_check_snapshots(project, snaps, snapshot_age_days))
            except Exception as e:
                errors.append({"project": project, "check": "snapshots", "error": str(e)})
        if "idle_vms" in run:
            try:
                instances = await asyncio.to_thread(_list_running_instances, project)
                if len(instances) > MAX_INSTANCES_FOR_IDLE:
                    errors.append({
                        "project": project, "check": "idle_vms",
                        "error": f"{len(instances)} running instances exceeds the "
                                 f"{MAX_INSTANCES_FOR_IDLE} idle-scan cap; skipped to bound "
                                 f"Monitoring calls. Narrow with the projects argument.",
                    })
                else:
                    async def _maybe_idle(inst: Any) -> dict | None:
                        iid = str(getattr(inst, "id", "") or getattr(inst, "name", ""))
                        try:
                            avg = await asyncio.to_thread(
                                _instance_cpu_utilization, project, iid, idle_days
                            )
                        except Exception as e:
                            errors.append({"project": project, "check": "idle_vms",
                                           "resource": iid, "error": str(e)})
                            return None
                        if avg is not None and avg < IDLE_CPU_THRESHOLD:
                            return _idle_vm_finding(project, inst, avg)
                        return None

                    for f in await asyncio.gather(*[_maybe_idle(i) for i in instances]):
                        if f:
                            findings.append(f)
            except Exception as e:
                errors.append({"project": project, "check": "idle_vms", "error": str(e)})

    await asyncio.gather(*[_scan_project(p) for p in projects])

    findings.sort(key=lambda f: f.get("estimated_monthly_savings", 0), reverse=True)

    by_category: dict[str, dict] = {}
    by_severity: dict[str, dict] = {}
    by_project: dict[str, dict] = {}
    total_monthly = 0.0
    for f in findings:
        m = f.get("estimated_monthly_savings", 0) or 0
        total_monthly += m
        for bucket, key in ((by_category, f["category"]), (by_severity, f["severity"]),
                            (by_project, f["project"])):
            slot = bucket.setdefault(key, {"count": 0, "monthly_savings": 0.0})
            slot["count"] += 1
            slot["monthly_savings"] = round(slot["monthly_savings"] + m, 2)

    return {
        "provider": "gcp",
        "projects_scanned": projects,
        "checks_run": run,
        "findings": findings,
        "total_findings": len(findings),
        "total_estimated_monthly_savings": round(total_monthly, 2),
        "total_estimated_annual_savings": round(total_monthly * 12, 2),
        "by_category": by_category,
        "by_severity": by_severity,
        "by_project": by_project,
        "errors": errors,
    }
