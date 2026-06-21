"""
Pluggable verifier registry for realized-savings confirmation.

A recommender proposes a change. A human marks it acted_on. Then a verifier
re-reads live cloud state to confirm the change actually landed, and returns
the MEASURED monthly saving so the learning signal can score accuracy.

This module is the dispatch layer: it maps a recommendation to the right
verifier instead of hard-coding EC2 in auto_verify_acted_on(). Each verifier
is a callable with the signature:

    verify(resource_id, recommended_config, row) -> float | None

It returns the measured monthly saving once the change is confirmed, or None
if the change has not landed yet (the recommendation stays acted_on and gets
retried on the next run). A source with no registered verifier degrades to a
no-op: the row stays acted_on, nothing crashes.

HARD RULE: verifiers READ cloud state only. They must never create, modify,
delete, stop, start, or terminate anything. Verification is measurement, not
remediation. An AST test guards this module against importing or calling any
mutation path. Do not weaken it.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# A verifier reads cloud state and returns the measured monthly saving, or None
# if the change has not landed yet.
Verifier = Callable[[str, dict, Any], Optional[float]]


# ── Registry ──────────────────────────────────────────────────────────────────

# Keyed by (source, resource_type). resource_type may be None to match any
# resource type under a source. Exact (source, resource_type) wins over the
# (source, None) wildcard, so a source can have one default verifier plus
# per-type overrides.
_REGISTRY: dict[tuple[str, str | None], Verifier] = {}


def register(source: str, resource_type: str | None, verifier: Verifier) -> None:
    """Register a verifier for a (source, resource_type). resource_type=None
    registers a fallback that handles every resource type under the source."""
    _REGISTRY[(source, resource_type)] = verifier


def get_verifier(source: str | None, resource_type: str | None) -> Verifier | None:
    """Resolve the verifier for a recommendation. Exact (source, resource_type)
    match wins; otherwise fall back to the source-wide (source, None) verifier.
    Returns None when nothing is registered, which the caller treats as a no-op."""
    if source is None:
        return None
    exact = _REGISTRY.get((source, resource_type))
    if exact is not None:
        return exact
    return _REGISTRY.get((source, None))


# ── EC2 rightsizing verifier ──────────────────────────────────────────────────

def verify_ec2_change(resource_id: str, recommended_config: dict, row: Any = None) -> float | None:
    """
    Confirm an EC2 instance was actually resized to the recommended type.
    Returns the measured monthly saving once the type matches, None until then.
    Reads only: a single describe_instances call.
    """
    try:
        import boto3
        ec2 = boto3.client("ec2")
        resp = ec2.describe_instances(InstanceIds=[resource_id])
        reservations = resp.get("Reservations", [])
        if not reservations:
            return None
        instance = reservations[0]["Instances"][0]
        current_type = instance.get("InstanceType", "")
        target_type = recommended_config.get("instance_type", "")

        if current_type == target_type:
            # Change confirmed. Estimate saving from the type difference.
            from ..connectors.terraform_estimate import _EC2_HOURLY, HOURS_PER_MONTH
            old_type = recommended_config.get("from_instance_type", "")
            old_hourly = _EC2_HOURLY.get(old_type, 0.0)
            new_hourly = _EC2_HOURLY.get(target_type, 0.0)
            return round((old_hourly - new_hourly) * HOURS_PER_MONTH, 2)
        return None
    except Exception as e:
        log.debug("verify_ec2_change error: %s", e)
        return None


# ── Idle-resource verifier ────────────────────────────────────────────────────

def _idle_resource_present(ec2_client: Any, resource_type: str, resource_id: str) -> bool | None:
    """Return True if the idle resource still exists, False if it is gone,
    None if we could not determine state (treated as not-yet-verified).

    Read-only: each branch issues one describe_* call and inspects the response.
    A boto3 "NotFound" style error means the resource was deleted, which is the
    confirmation we want. Any other error returns None so we retry later rather
    than recording a saving we did not actually measure."""
    try:
        if resource_type == "ebs_volume":
            resp = ec2_client.describe_volumes(VolumeIds=[resource_id])
            return len(resp.get("Volumes", [])) > 0

        if resource_type == "elastic_ip":
            # resource_id is the AllocationId (falls back to PublicIp in the scanner).
            if str(resource_id).startswith("eipalloc-"):
                resp = ec2_client.describe_addresses(AllocationIds=[resource_id])
            else:
                resp = ec2_client.describe_addresses(PublicIps=[resource_id])
            return len(resp.get("Addresses", [])) > 0

        if resource_type == "snapshot":
            resp = ec2_client.describe_snapshots(SnapshotIds=[resource_id])
            return len(resp.get("Snapshots", [])) > 0

        if resource_type == "stopped_ec2":
            # Acting on a stopped instance means terminating it. A terminated
            # instance may still describe for a while, so treat terminated /
            # shutting-down (or absent) as gone.
            resp = ec2_client.describe_instances(InstanceIds=[resource_id])
            for res in resp.get("Reservations", []):
                for inst in res.get("Instances", []):
                    state = (inst.get("State", {}) or {}).get("Name", "")
                    if state in ("terminated", "shutting-down"):
                        return False
                    return True
            return False

        # Unknown idle resource type: we cannot measure it, so do not guess.
        return None
    except Exception as e:
        # boto3 raises ClientError with a *NotFound code when the resource is gone.
        err_code = ""
        resp = getattr(e, "response", None)
        if isinstance(resp, dict):
            err_code = (resp.get("Error", {}) or {}).get("Code", "")
        if not err_code:
            # Fall back to the exception text so we do not need botocore here.
            err_code = f"{type(e).__name__}:{e}"
        if "NotFound" in err_code:
            return False
        log.debug("_idle_resource_present(%s, %s) error: %s", resource_type, resource_id, e)
        return None


def verify_idle_cleanup(resource_id: str, recommended_config: dict, row: Any = None) -> float | None:
    """
    Confirm an idle resource was actually cleaned up (deleted / released).

    Acting on an idle recommendation removes the resource, so the realized
    saving is the full estimated monthly cost once the resource is gone. We
    read live state: present -> not verified yet (None); gone -> realized the
    estimate. This never deletes anything itself, it only describes.

    The measured saving comes from the recommendation's own
    estimated_monthly_savings_usd, which equals the resource's monthly cost.
    """
    resource_type = getattr(row, "resource_type", "") or ""
    region = getattr(row, "region", "") or None
    try:
        import boto3
        ec2 = boto3.client("ec2", region_name=region) if region else boto3.client("ec2")
    except Exception as e:
        log.debug("verify_idle_cleanup client error: %s", e)
        return None

    present = _idle_resource_present(ec2, resource_type, resource_id)
    if present is None:
        return None  # could not determine, retry next run
    if present:
        return None  # still there, change has not landed yet

    # Resource is gone: the cleanup landed. Realized saving is the estimate.
    est = getattr(row, "estimated_monthly_savings_usd", None)
    try:
        return round(float(est), 2) if est is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# ── Default registrations ─────────────────────────────────────────────────────

def _register_defaults() -> None:
    register("rightsizing", "ec2", verify_ec2_change)
    # One verifier covers every idle resource type via the source-wide fallback.
    register("idle", None, verify_idle_cleanup)


_register_defaults()
