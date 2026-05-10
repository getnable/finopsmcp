"""
Opt-in resource cleanup actions.

Only available when FINOPS_CLEANUP_ENABLED=true.
Every action writes to the audit log before executing.
Protected resources are silently skipped.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .idle import IdleResource, scan_idle_resources

log = logging.getLogger(__name__)

_AUDIT_LOG = Path.home() / ".finops-mcp" / "cleanup_audit.jsonl"


def cleanup_enabled() -> bool:
    return os.getenv("FINOPS_CLEANUP_ENABLED", "").lower() in ("true", "1", "yes")


def _write_audit(action: str, resource: IdleResource, result: str, dry_run: bool) -> None:
    _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "action": action,
        "resource_type": resource.resource_type,
        "resource_id": resource.resource_id,
        "region": resource.region,
        "name": resource.name,
        "monthly_cost_usd": resource.monthly_cost_usd,
        "result": result,
    }
    with open(_AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _boto3_client(service: str, region: str) -> Any:
    import boto3
    return boto3.client(service, region_name=region)


# ─── individual resource actions ─────────────────────────────────────────────

def _delete_ebs_volume(resource: IdleResource, dry_run: bool) -> dict:
    if resource.protected:
        return {"status": "skipped", "reason": "protected tag"}
    if dry_run:
        _write_audit("delete_volume", resource, "dry_run", dry_run)
        return {"status": "dry_run", "would_delete": resource.resource_id}
    try:
        ec2 = _boto3_client("ec2", resource.region)
        ec2.delete_volume(VolumeId=resource.resource_id)
        _write_audit("delete_volume", resource, "deleted", dry_run)
        return {"status": "deleted", "resource_id": resource.resource_id, "saved_monthly": resource.monthly_cost_usd}
    except Exception as e:
        _write_audit("delete_volume", resource, f"error: {e}", dry_run)
        return {"status": "error", "resource_id": resource.resource_id, "error": str(e)}


def _release_elastic_ip(resource: IdleResource, dry_run: bool) -> dict:
    if resource.protected:
        return {"status": "skipped", "reason": "protected tag"}
    allocation_id = resource.resource_id
    if dry_run:
        _write_audit("release_eip", resource, "dry_run", dry_run)
        return {"status": "dry_run", "would_release": allocation_id}
    try:
        ec2 = _boto3_client("ec2", resource.region)
        ec2.release_address(AllocationId=allocation_id)
        _write_audit("release_eip", resource, "released", dry_run)
        return {"status": "released", "resource_id": allocation_id, "saved_monthly": resource.monthly_cost_usd}
    except Exception as e:
        _write_audit("release_eip", resource, f"error: {e}", dry_run)
        return {"status": "error", "resource_id": allocation_id, "error": str(e)}


def _delete_snapshot(resource: IdleResource, dry_run: bool) -> dict:
    if resource.protected:
        return {"status": "skipped", "reason": "protected tag"}
    if dry_run:
        _write_audit("delete_snapshot", resource, "dry_run", dry_run)
        return {"status": "dry_run", "would_delete": resource.resource_id}
    try:
        ec2 = _boto3_client("ec2", resource.region)
        ec2.delete_snapshot(SnapshotId=resource.resource_id)
        _write_audit("delete_snapshot", resource, "deleted", dry_run)
        return {"status": "deleted", "resource_id": resource.resource_id, "saved_monthly": resource.monthly_cost_usd}
    except Exception as e:
        _write_audit("delete_snapshot", resource, f"error: {e}", dry_run)
        return {"status": "error", "resource_id": resource.resource_id, "error": str(e)}


def _terminate_stopped_ec2(resource: IdleResource, dry_run: bool) -> dict:
    """Terminate a stopped EC2 instance (also releases attached EBS if DeleteOnTermination=True)."""
    if resource.protected:
        return {"status": "skipped", "reason": "protected tag"}
    if dry_run:
        _write_audit("terminate_ec2", resource, "dry_run", dry_run)
        return {"status": "dry_run", "would_terminate": resource.resource_id}
    try:
        ec2 = _boto3_client("ec2", resource.region)
        ec2.terminate_instances(InstanceIds=[resource.resource_id])
        _write_audit("terminate_ec2", resource, "terminated", dry_run)
        return {"status": "terminated", "resource_id": resource.resource_id, "saved_monthly": resource.monthly_cost_usd}
    except Exception as e:
        _write_audit("terminate_ec2", resource, f"error: {e}", dry_run)
        return {"status": "error", "resource_id": resource.resource_id, "error": str(e)}


def _delete_load_balancer(resource: IdleResource, dry_run: bool) -> dict:
    if resource.protected:
        return {"status": "skipped", "reason": "protected tag"}
    lb_arn = resource.metadata.get("lb_arn")
    if not lb_arn:
        return {"status": "error", "reason": "missing lb_arn in metadata"}
    if dry_run:
        _write_audit("delete_lb", resource, "dry_run", dry_run)
        return {"status": "dry_run", "would_delete": lb_arn}
    try:
        elbv2 = _boto3_client("elbv2", resource.region)
        elbv2.delete_load_balancer(LoadBalancerArn=lb_arn)
        _write_audit("delete_lb", resource, "deleted", dry_run)
        return {"status": "deleted", "resource_id": lb_arn, "saved_monthly": resource.monthly_cost_usd}
    except Exception as e:
        _write_audit("delete_lb", resource, f"error: {e}", dry_run)
        return {"status": "error", "resource_id": lb_arn, "error": str(e)}


_ACTION_MAP = {
    "ebs_volume":   _delete_ebs_volume,
    "elastic_ip":   _release_elastic_ip,
    "snapshot":     _delete_snapshot,
    "stopped_ec2":  _terminate_stopped_ec2,
    "load_balancer": _delete_load_balancer,
}


# ─── public entry point ───────────────────────────────────────────────────────

def cleanup_resources(
    resource_ids: list[str],
    dry_run: bool = True,
    resource_types: list[str] | None = None,
    regions: list[str] | None = None,
    min_idle_days: int = 7,
) -> dict[str, Any]:
    """
    Clean up the specified idle resources.

    Always runs in dry_run=True mode unless explicitly set to False.
    Protected resources are always skipped.
    Every action is written to ~/.finops-mcp/cleanup_audit.jsonl.

    resource_ids: specific resource IDs to clean (empty = clean all found idle resources)
    dry_run: if True, only simulate — nothing is deleted
    """
    if not cleanup_enabled():
        return {
            "error": "cleanup_disabled",
            "message": (
                "Resource cleanup is not enabled. "
                "Set FINOPS_CLEANUP_ENABLED=true in your environment to enable it. "
                "Run `finops setup` to configure this safely."
            ),
        }

    # Scan to find current idle resources
    all_idle = scan_idle_resources(
        resource_types=resource_types,
        regions=regions,
        min_idle_days=min_idle_days,
    )

    # Filter to requested IDs if provided
    if resource_ids:
        id_set = set(resource_ids)
        targets = [r for r in all_idle if r.resource_id in id_set]
        not_found = id_set - {r.resource_id for r in targets}
    else:
        targets = all_idle
        not_found = set()

    results = []
    total_saved = 0.0
    skipped_protected = 0

    for resource in targets:
        if resource.protected:
            skipped_protected += 1
            results.append({
                "resource_id": resource.resource_id,
                "resource_type": resource.resource_type,
                "status": "skipped",
                "reason": "protected tag — will never be deleted",
            })
            continue

        action_fn = _ACTION_MAP.get(resource.resource_type)
        if not action_fn:
            results.append({
                "resource_id": resource.resource_id,
                "resource_type": resource.resource_type,
                "status": "skipped",
                "reason": f"no action defined for type {resource.resource_type}",
            })
            continue

        result = action_fn(resource, dry_run)
        results.append({**result, "resource_type": resource.resource_type, "name": resource.name})
        if result.get("status") in ("deleted", "released", "terminated"):
            total_saved += resource.monthly_cost_usd

    return {
        "dry_run": dry_run,
        "total_targeted": len(targets),
        "skipped_protected": skipped_protected,
        "not_found": list(not_found),
        "total_monthly_savings_usd": round(total_saved, 2) if not dry_run else 0,
        "estimated_monthly_savings_usd": round(sum(
            r.monthly_cost_usd for r in targets if not r.protected
        ), 2) if dry_run else 0,
        "results": results,
        "audit_log": str(_AUDIT_LOG),
    }
