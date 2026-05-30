"""
Public IPv4 audit.

Since Feb 1 2024, AWS charges $0.005/hr per public IPv4 address.
That is $3.60/month or $43.20/year for every address, including ones
on stopped instances and unassociated Elastic IPs.

Categories:
  - Unattached EIPs: pure waste, release immediately
  - EIPs on stopped instances: paying for both the IP and the dead instance
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

# AWS pricing since Feb 1 2024
IPV4_HOURLY_RATE: float = 0.005
IPV4_MONTHLY_RATE: float = IPV4_HOURLY_RATE * 24 * 30  # $3.60/mo


def _make_ec2(session_or_none, region: str):
    """Return a boto3 EC2 client for the given region."""
    import boto3

    if session_or_none is not None:
        return session_or_none.client("ec2", region_name=region)
    return boto3.client("ec2", region_name=region)


def _get_opted_in_regions(session_or_none) -> list[str]:
    """Return all regions the account has opted in to."""
    import boto3

    client = boto3.client("ec2", region_name="us-east-1") if session_or_none is None else session_or_none.client("ec2", region_name="us-east-1")
    resp = client.describe_regions(Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}])
    return [r["RegionName"] for r in resp.get("Regions", [])]


def _audit_region_sync(session_or_none, region: str) -> dict:
    """
    Scan a single region for public IPv4 waste.
    Returns raw data for the region.
    """
    ec2 = _make_ec2(session_or_none, region)

    # --- Elastic IPs ---
    eip_resp = ec2.describe_addresses()
    addresses = eip_resp.get("Addresses", [])

    # --- Instance states (for EIPs associated with instances) ---
    # Collect all instance IDs referenced by EIPs
    instance_ids = [
        a["InstanceId"]
        for a in addresses
        if a.get("InstanceId")
    ]

    instance_states: dict[str, str] = {}
    if instance_ids:
        inst_resp = ec2.describe_instances(InstanceIds=instance_ids)
        for reservation in inst_resp.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                instance_states[inst["InstanceId"]] = inst["State"]["Name"]

    unattached: list[dict] = []
    stopped_instance: list[dict] = []

    for addr in addresses:
        alloc_id = addr.get("AllocationId", "")
        public_ip = addr.get("PublicIp", "")
        assoc_id = addr.get("AssociationId", "")
        instance_id = addr.get("InstanceId", "")
        network_iface = addr.get("NetworkInterfaceId", "")

        item: dict[str, Any] = {
            "allocation_id": alloc_id,
            "public_ip": public_ip,
            "region": region,
            "monthly_cost": IPV4_MONTHLY_RATE,
            "association_id": assoc_id,
            "instance_id": instance_id,
            "state": "",
        }

        if not assoc_id and not instance_id and not network_iface:
            # Completely unassociated
            item["state"] = "unattached"
            unattached.append(item)
        elif instance_id:
            state = instance_states.get(instance_id, "unknown")
            item["state"] = state
            if state == "stopped":
                stopped_instance.append(item)

    return {
        "region": region,
        "unattached": unattached,
        "stopped_instance": stopped_instance,
        "total_eips": len(addresses),
    }


async def audit_public_ipv4(
    aws_client: Any,
    regions: list[str] | None = None,
) -> dict:
    """
    Audit all public IPv4 addresses across regions.
    Returns categorized findings with cost impact.

    aws_client: AWSConnector instance (or anything with ._session).
    regions: explicit list of regions to scan; defaults to all opted-in regions.
    """
    loop = asyncio.get_event_loop()
    session = getattr(aws_client, "_session", None)

    # Resolve region list
    if not regions:
        try:
            regions = await loop.run_in_executor(None, _get_opted_in_regions, session)
        except Exception as exc:
            log.warning("Could not list regions, falling back to us-east-1: %s", exc)
            regions = ["us-east-1"]

    # Scan each region concurrently
    tasks = [
        loop.run_in_executor(None, _audit_region_sync, session, region)
        for region in regions
    ]
    region_results = await asyncio.gather(*tasks, return_exceptions=True)

    unattached_eips: list[dict] = []
    stopped_instance_eips: list[dict] = []
    by_region: dict[str, dict] = {}
    total_ips = 0

    for result in region_results:
        if isinstance(result, Exception):
            log.warning("Region scan failed: %s", result)
            continue

        region_name = result["region"]
        unattached_eips.extend(result["unattached"])
        stopped_instance_eips.extend(result["stopped_instance"])
        total_ips += result["total_eips"]

        waste_count = len(result["unattached"]) + len(result["stopped_instance"])
        by_region[region_name] = {
            "total_eips": result["total_eips"],
            "unattached": len(result["unattached"]),
            "stopped_instance": len(result["stopped_instance"]),
            "monthly_waste": waste_count * IPV4_MONTHLY_RATE,
        }

    waste_count_total = len(unattached_eips) + len(stopped_instance_eips)
    total_monthly_waste = waste_count_total * IPV4_MONTHLY_RATE

    return {
        "unattached_eips": unattached_eips,
        "stopped_instance_eips": stopped_instance_eips,
        "total_monthly_waste": round(total_monthly_waste, 2),
        "total_ips_found": total_ips,
        "by_region": by_region,
    }
