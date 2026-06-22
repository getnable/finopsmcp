"""
NLB cross-zone load balancing cost audit.

Network Load Balancers with cross-zone load balancing enabled charge $0.01/GB
for cross-AZ traffic. For high-throughput NLBs this adds up significantly.
Disabling cross-zone LB is safe when target groups have roughly equal capacity
per AZ.

This scanner flags NLBs with cross-zone enabled whose estimated cross-AZ cost
exceeds $10/month.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .envelope import INFERRED, Finding

log = logging.getLogger(__name__)

CROSS_AZ_COST_PER_GB: float = 0.01
CROSS_AZ_TRAFFIC_FRACTION: float = 0.50  # conservative: assume 50% of traffic crosses AZ
_LOOKBACK_DAYS = 30
_MIN_MONTHLY_COST_THRESHOLD: float = 10.0

_DEFAULT_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-central-1",
    "ap-southeast-1", "ap-southeast-2", "ap-northeast-1",
]


def _make_boto_session(aws_client: Any):
    """Return a boto3 session from the AWSConnector, or a fresh default session."""
    import boto3

    if hasattr(aws_client, "_session") and aws_client._session is not None:
        return aws_client._session
    return boto3.Session()


def _is_cross_zone_enabled(elbv2_client: Any, nlb_arn: str) -> bool:
    """Return True if the NLB has cross-zone load balancing enabled."""
    try:
        resp = elbv2_client.describe_load_balancer_attributes(LoadBalancerArn=nlb_arn)
        for attr in resp.get("Attributes", []):
            if attr.get("Key") == "load_balancing.cross_zone.enabled":
                return attr.get("Value", "false").lower() == "true"
    except Exception as exc:
        log.debug("describe_load_balancer_attributes failed for %s: %s", nlb_arn, exc)
    return False


def _get_processed_bytes(
    cw_client: Any,
    nlb_arn: str,
    start: datetime,
    end: datetime,
) -> float:
    """
    Return total ProcessedBytes for an NLB over the lookback window.
    The NLB dimension uses the suffix of the ARN (after the last '/').
    """
    # CloudWatch dimension value is the load balancer name portion of the ARN
    # e.g. arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/net/my-nlb/abc123
    # -> dimension = "net/my-nlb/abc123"
    try:
        lb_dim = "/".join(nlb_arn.split("/")[-3:]) if nlb_arn.count("/") >= 3 else nlb_arn
    except Exception:
        lb_dim = nlb_arn

    period = _LOOKBACK_DAYS * 86400
    total_bytes = 0.0

    try:
        resp = cw_client.get_metric_statistics(
            Namespace="AWS/NetworkELB",
            MetricName="ProcessedBytes",
            Dimensions=[{"Name": "LoadBalancer", "Value": lb_dim}],
            StartTime=start,
            EndTime=end,
            Period=period,
            Statistics=["Sum"],
        )
        for dp in resp.get("Datapoints", []):
            total_bytes += dp.get("Sum", 0.0)
    except Exception as exc:
        log.debug("ProcessedBytes metric failed for %s: %s", nlb_arn, exc)

    return total_bytes


def _build_disable_command(nlb_arn: str) -> str:
    return (
        f"aws elbv2 modify-load-balancer-attributes "
        f"--load-balancer-arn {nlb_arn} "
        f"--attributes Key=load_balancing.cross_zone.enabled,Value=false"
    )


async def audit_nlb_cross_zone_costs(
    aws_client: Any,
    regions: list[str] | None = None,
) -> list[dict]:
    """
    Audit NLBs with cross-zone load balancing enabled for unnecessary cost.

    Cross-zone LB on NLBs charges $0.01/GB for cross-AZ traffic. This scanner
    flags NLBs where the estimated monthly cross-AZ cost exceeds $10.

    Args:
        aws_client: AWSConnector instance (provides boto3 session).
        regions:    AWS regions to scan. Defaults to common regions.

    Returns:
        List of dicts with findings, sorted by estimated_cross_az_cost descending.
    """
    target_regions = regions or _DEFAULT_REGIONS
    session = _make_boto_session(aws_client)

    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(days=_LOOKBACK_DAYS)

    findings: list[dict] = []

    for region in target_regions:
        try:
            elbv2_client = session.client("elbv2", region_name=region)
            cw_client = session.client("cloudwatch", region_name=region)
        except Exception as exc:
            log.debug("Could not create clients for region %s: %s", region, exc)
            continue

        # List all NLBs
        try:
            resp = elbv2_client.describe_load_balancers()
        except Exception as exc:
            log.debug("describe_load_balancers failed in %s: %s", region, exc)
            continue

        for lb in resp.get("LoadBalancers", []):
            if lb.get("Type") != "network":
                continue

            nlb_arn = lb["LoadBalancerArn"]
            nlb_name = lb["LoadBalancerName"]

            cross_zone = _is_cross_zone_enabled(elbv2_client, nlb_arn)
            if not cross_zone:
                continue

            az_count = len(lb.get("AvailabilityZones", []))

            total_bytes = _get_processed_bytes(cw_client, nlb_arn, start_time, end_time)
            total_gb = total_bytes / (1024 ** 3)
            estimated_cross_az_cost = total_gb * CROSS_AZ_TRAFFIC_FRACTION * CROSS_AZ_COST_PER_GB

            # A single-AZ NLB has no cross-AZ traffic to charge for, so cross-zone
            # is moot. Only flag multi-AZ NLBs above the cost threshold, and never
            # assert a blind "disable": disabling cross-zone with unevenly
            # distributed targets drops traffic to under-provisioned AZs. We can't
            # confirm per-AZ target balance from describe_load_balancers, so the
            # recommendation is "review", with the availability caveat attached.
            caveat = None
            if az_count < 2:
                recommendation = "monitor_single_az_no_cross_zone_charges"
            elif estimated_cross_az_cost < _MIN_MONTHLY_COST_THRESHOLD:
                recommendation = "monitor_no_action_needed"
            else:
                recommendation = "review_disabling_cross_zone"
                caveat = ("Disabling cross-zone is only safe when targets are balanced "
                          "across all enabled AZs. Verify per-AZ target counts first; "
                          "disabling with uneven targets drops traffic to AZs that then "
                          "have no local targets.")

            # Classify by strength of evidence. We confirm cross-zone is enabled and
            # read real ProcessedBytes, but the cost rests on a flat 50% guess at how
            # much traffic actually crosses an AZ. ProcessedBytes does not tell us the
            # cross-AZ split, so the dollar figure is a heuristic, not a measurement.
            # That makes this an investigation. We only attach a finding to the
            # actionable case; the monitor cases are not a real finding.
            finding = None
            if recommendation == "review_disabling_cross_zone":
                finding = Finding(
                    source="nlb_cross_zone",
                    title="Let's confirm how much this NLB's cross-zone traffic really costs",
                    why=(f"NLB {nlb_name} has cross-zone load balancing on across {az_count} "
                         "AZs, and cross-zone traffic bills at $0.01/GB. It moves enough "
                         "volume that the cross-AZ portion is likely a meaningful line item."),
                    evidence=INFERRED,
                    confidence="low",
                    why_unsure=("My estimate assumes half of the NLB's traffic crosses an AZ "
                                "boundary. That is a placeholder, not a measurement: the real "
                                "split depends on how clients and targets are spread across "
                                "AZs, which ProcessedBytes does not reveal."),
                    assumptions=[
                        f"{CROSS_AZ_TRAFFIC_FRACTION:.0%} of processed traffic crosses an AZ "
                        "boundary (a flat default, not measured for this NLB).",
                    ],
                    rough_monthly=round(estimated_cross_az_cost, 2),
                    confirm_steps=[
                        "Check the NLB's per-AZ ProcessedBytes in CloudWatch (the LoadBalancer "
                        "and AvailabilityZone dimensions) to see the real cross-AZ share instead "
                        "of assuming 50%.",
                        "Count targets per AZ in each target group. Cross-zone only earns its "
                        "cost when AZs are unevenly loaded; if every AZ has balanced targets, "
                        "disabling it is the safe win.",
                    ],
                    pro_can_confirm=True,
                    pro_unlock=("On Pro, nable reads your CUR data-transfer line items and the "
                                "per-AZ NLB metrics to confirm the actual cross-AZ cost, replacing "
                                "the 50% assumption with a measured number."),
                    remediation=[
                        "Only after confirming targets are balanced across all enabled AZs, "
                        "disable cross-zone: " + _build_disable_command(nlb_arn) + ". "
                        "Disabling it with uneven targets starves AZs that then have no local "
                        "target, so verify per-AZ balance first.",
                    ],
                    resource_id=nlb_arn,
                    metadata={
                        "nlb_name": nlb_name,
                        "region": region,
                        "enabled_az_count": az_count,
                        "monthly_processed_gb": round(total_gb, 2),
                        "assumed_cross_az_fraction": CROSS_AZ_TRAFFIC_FRACTION,
                    },
                )

            findings.append({
                "nlb_name": nlb_name,
                "nlb_arn": nlb_arn,
                "region": region,
                "cross_zone_enabled": True,
                "enabled_az_count": az_count,
                "monthly_processed_gb": round(total_gb, 2),
                "estimated_cross_az_cost": round(estimated_cross_az_cost, 4),
                "cost_is_estimate": True,
                "recommendation": recommendation,
                "availability_caveat": caveat,
                "disable_command": _build_disable_command(nlb_arn),
                "finding": finding.to_dict() if finding else None,
            })

    findings.sort(key=lambda f: f["estimated_cross_az_cost"], reverse=True)
    return findings
