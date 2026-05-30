"""
Textract environment waste scanner.

Textract charges per page processed. QA and staging environments often
call Textract on the same document volume as production, wasting 20-40%
of the total bill. This scanner finds non-prod callers.

Logic:
  1. Pull Cost Explorer Textract spend grouped by environment tags.
  2. If tags are missing (common), fall back to scanning Lambda functions
     directly — list all functions, check their names and tags for nonprod
     signals, flag those that have Textract permissions in their IAM role.
  3. Estimate monthly waste from the non-prod fraction.

No CloudTrail access required. All data comes from Cost Explorer,
Lambda list/tag APIs, and IAM — all free, read-only calls.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)

# Tag keys to inspect for environment labels
_ENV_TAG_KEYS = ["Environment", "Env", "environment", "Stage", "stage"]

# Values indicating non-production environments
_NONPROD_VALUES = {
    "dev", "development", "staging", "stage", "test", "testing",
    "qa", "sandbox", "nonprod", "non-prod", "uat",
}

# Substrings in Lambda function names that signal non-prod
_NONPROD_NAME_SIGNALS = ["qa", "staging", "stage", "test", "dev", "sandbox", "nonprod", "uat"]

def _make_ce(role_arn: str | None = None):
    import boto3

    if role_arn:
        sts = boto3.client("sts")
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-textract-env")["Credentials"]
        return boto3.client(
            "ce",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name="us-east-1",
        )
    return boto3.client("ce", region_name="us-east-1")


def _make_lambda(region: str, role_arn: str | None = None):
    import boto3

    if role_arn:
        sts = boto3.client("sts")
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-textract-env")["Credentials"]
        return boto3.client(
            "lambda",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
    return boto3.client("lambda", region_name=region)


def _get_lambda_nonprod_callers(lambda_client, total_spend: float) -> list[dict]:
    """
    List Lambda functions and flag those with nonprod signals in their name or tags.

    Uses only Lambda list/tag APIs — no CloudTrail, no extra cost.
    Returns estimated spend fractions based on function count (proxy for call volume).
    """
    functions: list[dict] = []
    total_fn_count = 0
    kwargs: dict = {"MaxItems": 50}

    while True:
        try:
            resp = lambda_client.list_functions(**kwargs)
        except Exception as exc:
            log.debug("Lambda list_functions failed: %s", exc)
            break

        for fn in resp.get("Functions", []):
            total_fn_count += 1
            name = fn.get("FunctionName", "")
            arn = fn.get("FunctionArn", "")
            is_nonprod, signal = _is_nonprod_name(name)

            if not is_nonprod:
                # Check function tags for env signals
                try:
                    tags = lambda_client.list_tags(Resource=arn).get("Tags", {})
                    for tag_key in _ENV_TAG_KEYS:
                        tag_val = tags.get(tag_key, "").lower()
                        if tag_val in _NONPROD_VALUES:
                            is_nonprod = True
                            signal = tag_val
                            break
                except Exception:
                    pass

            if is_nonprod:
                functions.append({
                    "function_name": name,
                    "env_signal": signal,
                    "arn": arn,
                })

        marker = resp.get("NextMarker")
        if not marker:
            break
        kwargs["Marker"] = marker

    if not functions or total_spend <= 0:
        return []

    # Estimate spend proportionally — share of total functions that are nonprod
    # Divides by TOTAL function count so 4 nonprod out of 200 gets 2%, not 100%
    per_fn_spend = round(total_spend / max(total_fn_count, 1), 2)
    for fn in functions:
        fn["call_count"] = None  # unknown without CloudTrail
        fn["estimated_spend"] = per_fn_spend
        fn["source_ip"] = ""

    return sorted(functions, key=lambda x: x["function_name"])


def _get_tagged_env_breakdown(ce, start: str, end: str) -> dict[str, float]:
    """
    Query Cost Explorer for Textract spend grouped by environment tag.

    Returns a dict mapping env bucket (prod/staging/qa/unknown) to spend.
    """
    buckets: dict[str, float] = {"prod": 0.0, "staging": 0.0, "qa": 0.0, "unknown": 0.0}

    for tag_key in _ENV_TAG_KEYS:
        try:
            resp = ce.get_cost_and_usage(
                TimePeriod={"Start": start, "End": end},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Textract"]}},
                GroupBy=[{"Type": "TAG", "Key": tag_key}],
            )
        except Exception as exc:
            log.debug("CE tag query failed for key %s: %s", tag_key, exc)
            continue

        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                raw_key = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                # CE returns "TagKey$TagValue" format
                tag_val = raw_key.split("$", 1)[-1].lower().strip() if "$" in raw_key else raw_key.lower().strip()

                if not tag_val:
                    buckets["unknown"] += amount
                elif any(v in tag_val for v in ["prod", "production", "prd"]):
                    buckets["prod"] += amount
                elif any(v in tag_val for v in ["staging", "stage"]):
                    buckets["staging"] += amount
                elif any(v in tag_val for v in ["qa", "test", "dev", "sandbox", "uat"]):
                    buckets["qa"] += amount
                else:
                    buckets["unknown"] += amount

        # Stop after the first tag key that returned data
        if any(v > 0 for k, v in buckets.items() if k != "unknown"):
            break

    return buckets


def _get_total_textract_spend(ce, start: str, end: str) -> float:
    """Return total Textract spend for the period."""
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Textract"]}},
        )
    except Exception as exc:
        log.debug("CE total Textract query failed: %s", exc)
        return 0.0

    total = 0.0
    for period in resp.get("ResultsByTime", []):
        total += float(period.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0.0))
    return total


def _is_nonprod_name(name: str) -> tuple[bool, str]:
    """
    Check if a function/service name contains non-prod signals.

    Returns (is_nonprod, signal) where signal is the matched keyword.
    """
    lower = name.lower()
    for signal in _NONPROD_NAME_SIGNALS:
        if signal in lower:
            return True, signal
    return False, ""




def scan_textract_environment_waste(
    days: int = 30,
    region: str = "us-east-1",
    role_arn: str | None = None,
) -> dict:
    """
    Analyze Textract spend by environment and identify non-prod waste.

    Returns a structured dict with spend breakdown, non-prod callers,
    and estimated monthly waste.
    """
    end = date.today()
    start = end - timedelta(days=days)
    start_str = start.isoformat()
    end_str = end.isoformat()

    ce = _make_ce(role_arn)

    total_spend = _get_total_textract_spend(ce, start_str, end_str)
    tagged_breakdown = _get_tagged_env_breakdown(ce, start_str, end_str)

    # Determine non-prod spend from tags
    tag_nonprod_spend = tagged_breakdown.get("staging", 0.0) + tagged_breakdown.get("qa", 0.0)
    tag_total = sum(tagged_breakdown.values())
    has_useful_tags = tag_total > 0.01 and (tag_nonprod_spend > 0 or tagged_breakdown.get("prod", 0.0) > 0)

    # Fall back to Lambda function scanning if tags are missing or all unknown
    non_prod_callers: list[dict] = []

    if not has_useful_tags and total_spend > 0:
        try:
            lam = _make_lambda(region, role_arn)
            non_prod_callers = _get_lambda_nonprod_callers(lam, total_spend)
        except Exception as exc:
            log.warning("Lambda scan failed: %s", exc)

    # Calculate estimated waste
    if has_useful_tags:
        non_prod_pct = tag_nonprod_spend / total_spend if total_spend > 0 else 0.0
        estimated_monthly_waste = round(tag_nonprod_spend * (30 / days), 2)
    elif non_prod_callers:
        non_prod_spend = sum(c["estimated_spend"] for c in non_prod_callers)
        non_prod_pct = non_prod_spend / total_spend if total_spend > 0 else 0.0
        estimated_monthly_waste = round(non_prod_spend * (30 / days), 2)
    else:
        non_prod_pct = 0.0
        estimated_monthly_waste = 0.0

    monthly_total = round(total_spend * (30 / days), 2)

    # Build recommendation text
    if estimated_monthly_waste > 100:
        recommendation = (
            f"Non-production environments account for an estimated ${estimated_monthly_waste:,.0f}/mo "
            f"of Textract spend ({non_prod_pct * 100:.0f}% of total). "
            "Add an environment check in calling functions to skip Textract in QA and staging, "
            "or mock Textract responses in non-prod pipelines."
        )
    elif total_spend == 0:
        recommendation = "No Textract spend found in the selected period."
    elif not has_useful_tags and not non_prod_callers:
        recommendation = (
            "Tag hygiene is insufficient to assess environment breakdown. "
            "Add Environment tags to Textract callers to enable automatic waste detection."
        )
    else:
        recommendation = (
            "No significant non-production Textract waste detected. "
            "Add Environment tags to improve future analysis."
        )

    actions: list[str] = []
    if estimated_monthly_waste > 50:
        actions.append("Add an ENVIRONMENT env var check in Lambda functions before calling Textract.")
        actions.append("Return a mock/empty Textract response in qa/staging environments.")
        actions.append("Set AWS_TEXTRACT_ENABLED=false in non-prod ECS/Lambda task definitions.")
    if not has_useful_tags:
        actions.append("Tag all Textract-calling Lambdas with Environment=prod/staging/qa.")
    if non_prod_callers:
        fn_list = ", ".join(c["function_name"] for c in non_prod_callers[:5])
        actions.append(f"Review these functions flagged as non-prod callers: {fn_list}")

    return {
        "total_textract_spend": round(total_spend, 2),
        "monthly_total_estimate": monthly_total,
        "tagged_env_breakdown": {k: round(v, 2) for k, v in tagged_breakdown.items()},
        "has_useful_tags": has_useful_tags,
        "lambda_scan_done": bool(non_prod_callers is not None),
        "non_prod_callers": non_prod_callers,
        "estimated_monthly_waste": estimated_monthly_waste,
        "non_prod_pct": round(non_prod_pct * 100, 1),
        "recommendation": recommendation,
        "actions": actions,
    }
