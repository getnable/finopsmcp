"""
Cross-cloud network-traffic classifier.

The cost of network traffic is encoded in each cloud's billing strings:
  - AWS: line_item_usage_type   (e.g. "USE1-DataTransfer-Out-Bytes")
  - GCP: sku.description         (e.g. "Network Internet Egress ...")
  - Azure: meter / meterSubCategory (e.g. "Inter-Region Egress")

This module turns those raw strings into two engineer-facing axes:

  direction:  external | internal | ingress | other
  scope:      internet_egress | cdn_egress | cross_az | cross_region |
              vpc_peering | private_endpoint | nat_processing | firewall |
              vpn_directconnect | ingress | other

`direction` is the headline split a founder/engineer asks first ("how much
leaves our network vs stays inside it"). `scope` is the actionable detail that
maps to a fix (cross_az -> topology-aware routing, nat_processing -> VPC
endpoint, internet_egress -> CDN, and so on).

The classifier is pure and deterministic so it is fully unit-testable against
real billing strings, with no AWS/GCP/Azure calls.
"""
from __future__ import annotations

# Canonical scope -> default direction. Direction can be overridden per-rule.
INTERNAL_SCOPES = {
    "cross_az", "cross_region", "vpc_peering", "private_endpoint",
    "nat_processing",
}
EXTERNAL_SCOPES = {"internet_egress", "cdn_egress"}


def _direction_for(scope: str) -> str:
    if scope in EXTERNAL_SCOPES:
        return "external"
    if scope in INTERNAL_SCOPES:
        return "internal"
    if scope == "ingress":
        return "ingress"
    return "other"


# ── AWS ───────────────────────────────────────────────────────────────────────
# Matched against the usage_type with its regional prefix stripped, lowercased.
# Order matters: first match wins, so put specific patterns before generic ones.
_AWS_RULES: list[tuple[str, str]] = [
    # (substring to find in lowercased usage_type, scope)
    ("natgateway-bytes", "nat_processing"),
    ("vpcendpoint-bytes", "private_endpoint"),
    ("privatelink", "private_endpoint"),
    ("networkfirewall", "firewall"),
    ("awsnetworkfirewall", "firewall"),
    ("vpnusage", "vpn_directconnect"),
    ("vpnconnection", "vpn_directconnect"),
    ("directconnect", "vpn_directconnect"),
    ("dataxfer-direct-connect", "vpn_directconnect"),
    # CloudFront CDN egress (CF- prefix or cloudfront in the type)
    ("cloudfront", "cdn_egress"),
    ("-cf-", "cdn_egress"),
    # Inter-region (between two AWS regions). CUR encodes this as
    # "<src>-<dst>-AWS-Out-Bytes" / "-AWS-In-Bytes" (e.g. USE1-USW2-AWS-Out-Bytes),
    # which has no "interregion" substring, so match the "-aws-out/in-bytes" form
    # BEFORE the generic internet out/in rules below.
    ("interregion", "cross_region"),
    ("inter-region", "cross_region"),
    ("aws-out-bytes", "cross_region"),
    ("aws-in-bytes", "cross_region"),
    # Intra-region cross-AZ (AWS labels this "Regional")
    ("regional-bytes", "cross_az"),
    ("datatransfer-regional", "cross_az"),
    # Inbound from the internet (free, but worth surfacing)
    ("datatransfer-in-bytes", "ingress"),
    ("-in-bytes", "ingress"),
    # Generic internet egress (keep last among DataTransfer rules)
    ("datatransfer-out-bytes", "internet_egress"),
    ("-out-bytes", "internet_egress"),
]


def classify_aws(usage_type: str) -> tuple[str, str]:
    """
    Classify an AWS usage_type into (direction, scope).

    Accepts the raw usage_type with or without the regional prefix
    (e.g. "USE1-DataTransfer-Out-Bytes" or "DataTransfer-Out-Bytes").
    Returns ("other", "other") when it is not a recognised network line item.
    """
    if not usage_type:
        return ("other", "other")
    ut = usage_type.lower()
    for needle, scope in _AWS_RULES:
        if needle in ut:
            return (_direction_for(scope), scope)
    return ("other", "other")


# ── GCP ───────────────────────────────────────────────────────────────────────
# Matched against sku.description, lowercased.
_GCP_RULES: list[tuple[str, str]] = [
    ("cloud nat", "nat_processing"),
    ("nat gateway", "nat_processing"),
    ("cloud armor", "firewall"),
    ("firewall", "firewall"),
    ("cloud vpn", "vpn_directconnect"),
    ("interconnect", "vpn_directconnect"),
    ("cdn egress", "cdn_egress"),
    ("cloud cdn", "cdn_egress"),
    ("inter region egress", "cross_region"),
    ("inter-region egress", "cross_region"),
    ("inter zone egress", "cross_az"),
    ("inter-zone egress", "cross_az"),
    ("vpc peering", "vpc_peering"),
    ("internet egress", "internet_egress"),
    ("network ingress", "ingress"),
    ("ingress", "ingress"),
]


def classify_gcp(sku_description: str) -> tuple[str, str]:
    """Classify a GCP sku.description into (direction, scope)."""
    if not sku_description:
        return ("other", "other")
    s = sku_description.lower()
    for needle, scope in _GCP_RULES:
        if needle in s:
            return (_direction_for(scope), scope)
    return ("other", "other")


# ── Azure ─────────────────────────────────────────────────────────────────────
# Matched against meter / meterSubCategory, lowercased.
_AZURE_RULES: list[tuple[str, str]] = [
    ("nat gateway", "nat_processing"),
    ("azure firewall", "firewall"),
    ("firewall", "firewall"),
    ("vpn gateway", "vpn_directconnect"),
    ("expressroute", "vpn_directconnect"),
    ("content delivery", "cdn_egress"),
    ("cdn", "cdn_egress"),
    ("inter-region", "cross_region"),
    ("inter region", "cross_region"),
    ("vnet peering", "vpc_peering"),
    # Match the full "availability zone" phrase only. A bare "zone" wrongly
    # catches geographic billing meters like "Standard Data Transfer Out - Zone 1"
    # (which is internet egress), so keep internet/egress rules able to win.
    ("availability zone", "cross_az"),
    ("data transfer out", "internet_egress"),
    ("internet egress", "internet_egress"),
    ("egress", "internet_egress"),
    ("data transfer in", "ingress"),
    ("ingress", "ingress"),
]


def classify_azure(meter: str) -> tuple[str, str]:
    """Classify an Azure meter / meterSubCategory into (direction, scope)."""
    if not meter:
        return ("other", "other")
    m = meter.lower()
    for needle, scope in _AZURE_RULES:
        if needle in m:
            return (_direction_for(scope), scope)
    return ("other", "other")


def classify(cloud: str, raw: str) -> tuple[str, str]:
    """Dispatch to the right per-cloud classifier."""
    c = (cloud or "").lower()
    if c == "aws":
        return classify_aws(raw)
    if c == "gcp":
        return classify_gcp(raw)
    if c == "azure":
        return classify_azure(raw)
    return ("other", "other")


# Human-readable, one-line fix per scope (the solve playbook seed).
SOLVE_PLAYBOOK: dict[str, str] = {
    "internet_egress": "Put a CDN in front of cacheable responses, negotiate committed egress, or use private peering for high-volume partners.",
    "cdn_egress": "Confirm cache hit ratio; raise TTLs and cache more aggressively to cut origin egress.",
    "cross_az": "Pin chatty service pairs to the same AZ, enable topology-aware routing, and disable NLB cross-zone when balanced.",
    "cross_region": "Question the replication or co-locate dependent services; compress and batch cross-region traffic.",
    "vpc_peering": "Audit whether the peered traffic needs to cross the boundary; co-locate where possible.",
    "private_endpoint": "Already on a private path; verify the endpoint is actually cheaper than the NAT route it replaced.",
    "nat_processing": "Add S3/DynamoDB/ECR gateway or interface endpoints so that traffic bypasses the NAT gateway entirely.",
    "firewall": "Right-size firewall endpoints and move stateless filtering to security groups / NACLs.",
    "vpn_directconnect": "Right-size the circuit; consolidate tunnels.",
    "ingress": "Inbound transfer is usually free; no action, surfaced for context.",
    "other": "Review the raw usage type to determine the driver.",
}
