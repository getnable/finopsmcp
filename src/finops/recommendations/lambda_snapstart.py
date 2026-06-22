"""
Lambda SnapStart recommender.

SnapStart eliminates cold starts for Java Lambda functions at no extra cost.
It replaces expensive provisioned concurrency for cold-start reduction.
This scanner finds Java functions without SnapStart enabled and flags those
that are paying for provisioned concurrency when SnapStart would do the same job free.
"""
from __future__ import annotations

import logging
from typing import Any

from .envelope import INFERRED, Finding

log = logging.getLogger(__name__)

JAVA_RUNTIMES = {"java8", "java8.al2", "java11", "java17", "java21"}

# Provisioned concurrency keep-warm rate: $0.0000041667 per GB-second
# (not the $0.0000097222/GB-s duration rate for PC-enabled execution).
PC_COST_PER_GB_SECOND: float = 0.0000041667
SECONDS_PER_MONTH: int = 30 * 24 * 3600

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


def _snapstart_enabled(fn: dict) -> bool:
    """Return True if the function has SnapStart enabled on published versions."""
    snap = fn.get("SnapStart", {})
    return snap.get("ApplyOn") == "PublishedVersions"


def _get_pc_monthly_cost(lambda_client: Any, function_name: str, memory_mb: int) -> float:
    """
    Return estimated monthly provisioned concurrency cost for a function.
    Returns 0.0 if the function has no provisioned concurrency configs.
    """
    try:
        resp = lambda_client.list_provisioned_concurrency_configs(
            FunctionName=function_name
        )
    except Exception as exc:
        log.debug("list_provisioned_concurrency_configs failed for %s: %s", function_name, exc)
        return 0.0

    configs = resp.get("ProvisionedConcurrencyConfigs", [])
    if not configs:
        return 0.0

    total_allocated = sum(
        int(c.get("AllocatedProvisionedConcurrentExecutions", 0))
        for c in configs
    )
    if total_allocated == 0:
        return 0.0

    memory_gb = memory_mb / 1024.0
    return total_allocated * memory_gb * SECONDS_PER_MONTH * PC_COST_PER_GB_SECOND


async def recommend_lambda_snapstart(
    aws_client: Any,
    regions: list[str] | None = None,
) -> list[dict]:
    """
    Scan Java Lambda functions and recommend SnapStart where not yet enabled.

    Flags functions that are paying for provisioned concurrency when SnapStart
    would eliminate cold starts at no cost.

    Args:
        aws_client: AWSConnector instance (provides boto3 session).
        regions:    AWS regions to scan. Defaults to common regions.

    Returns:
        List of dicts with findings, sorted by monthly_pc_cost descending.
    """
    target_regions = regions or _DEFAULT_REGIONS
    session = _make_boto_session(aws_client)

    findings: list[dict] = []

    for region in target_regions:
        try:
            lambda_client = session.client("lambda", region_name=region)
        except Exception as exc:
            log.debug("Could not create lambda client for region %s: %s", region, exc)
            continue

        paginator = lambda_client.get_paginator("list_functions")
        try:
            pages = paginator.paginate()
        except Exception as exc:
            log.debug("list_functions failed in %s: %s", region, exc)
            continue

        for page in pages:
            for fn in page.get("Functions", []):
                runtime = fn.get("Runtime", "")
                if runtime not in JAVA_RUNTIMES:
                    continue

                function_name = fn["FunctionName"]
                memory_mb = int(fn.get("MemorySize", 128))
                snap_enabled = _snapstart_enabled(fn)

                monthly_pc_cost = _get_pc_monthly_cost(lambda_client, function_name, memory_mb)
                has_pc = monthly_pc_cost > 0.0

                if snap_enabled and not has_pc:
                    # Already good: SnapStart on, no wasted PC spend
                    recommendation = "no_action_snapstart_enabled"
                elif snap_enabled and has_pc:
                    # SnapStart on but still paying for PC, remove PC to save money
                    recommendation = "remove_provisioned_concurrency_snapstart_already_enabled"
                elif not snap_enabled and has_pc:
                    # Best opportunity: enable SnapStart and remove PC
                    recommendation = "enable_snapstart_replace_provisioned_concurrency"
                else:
                    # Java function, no PC, no SnapStart, enable SnapStart proactively
                    recommendation = "enable_snapstart_eliminate_cold_starts_free"

                findings.append({
                    "function_name": function_name,
                    "runtime": runtime,
                    "region": region,
                    "snapstart_enabled": snap_enabled,
                    "has_provisioned_concurrency": has_pc,
                    "monthly_pc_cost": round(monthly_pc_cost, 4),
                    "recommendation": recommendation,
                })

    findings.sort(key=lambda f: f["monthly_pc_cost"], reverse=True)

    # Classify the finding by the STRENGTH OF EVIDENCE behind it. We can MEASURE the
    # runtime, the SnapStart flag, and the provisioned-concurrency cost directly. But
    # the SAVING depends on a change the customer has to make and validate: enabling
    # SnapStart and then removing provisioned concurrency. SnapStart restores from a
    # snapshot rather than a cold init, so it is not a guaranteed drop-in for every
    # function (init code that opens connections or seeds caches can misbehave on
    # restore, and tail latency must be re-checked). Because the dollar figure rests
    # on "enable SnapStart and it works for you", this is an INFERRED investigation
    # with a magnitude band, not a precise recommendation.
    # Attached to the top finding so the returned list shape and keys are unchanged.
    replaceable = [f for f in findings if f["has_provisioned_concurrency"] and not f["snapstart_enabled"]]
    if replaceable:
        top = replaceable[0]
        rough = sum(f["monthly_pc_cost"] for f in replaceable)
        finding = Finding(
            source="lambda_snapstart",
            title="Let's check whether SnapStart can replace this Lambda's provisioned concurrency",
            why=(
                f"Function '{top['function_name']}' runs a Java runtime "
                f"({top['runtime']}), has provisioned concurrency enabled, and does not "
                "use SnapStart. SnapStart removes cold starts for Java at no charge, so "
                "if it covers this function's latency needs the provisioned concurrency "
                "spend may be removable. I count "
                f"{len(replaceable)} such function(s)."
            ),
            evidence=INFERRED,
            confidence="medium",
            why_unsure=(
                "I can see the provisioned-concurrency cost, but I cannot tell from "
                "config alone whether SnapStart will hold your cold-start latency. "
                "Functions that open DB connections or warm caches during init can "
                "behave differently when restored from a snapshot, so the saving is "
                "only real once SnapStart is enabled and validated. That is why I am "
                "not putting a precise number on it yet."
            ),
            assumptions=[
                "SnapStart fully replaces provisioned concurrency for these functions "
                "(true only if post-restore latency is acceptable).",
                "The provisioned concurrency exists for cold-start reduction, not for a "
                "hard concurrency floor.",
            ],
            rough_monthly=round(rough, 2),
            confirm_steps=[
                "Enable SnapStart on a published version in a non-prod stage and compare "
                "init duration and p99 latency against the provisioned-concurrency alias.",
                "Check the function's init code for connection or cache warm-up that may "
                "need a SnapStart runtime hook (beforeCheckpoint/afterRestore).",
                "Once latency holds, remove the provisioned concurrency and confirm the "
                "line item drops on the next bill.",
            ],
            pro_can_confirm=True,
            pro_unlock=(
                "On Pro, give nable read-only CloudWatch access (plus CUR for line-item "
                "precision) and it tracks each function's cold-start latency and "
                "Init/RestoreDuration before and after, then confirms the provisioned "
                "concurrency is safe to remove and reports the exact saving."
            ),
            remediation=[
                "Validate SnapStart latency first, then enable SnapStart and remove the "
                "provisioned concurrency. Do not remove provisioned concurrency before "
                "confirming SnapStart holds latency: that can reintroduce cold starts.",
            ],
            resource_id=top["function_name"],
            metadata={
                "region": top["region"],
                "runtime": top["runtime"],
                "monthly_pc_cost": top["monthly_pc_cost"],
                "replaceable_functions": [f["function_name"] for f in replaceable[:8]],
            },
        )
        top["finding"] = finding.to_dict()

    return findings
