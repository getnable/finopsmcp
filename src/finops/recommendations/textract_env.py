"""
Textract environment waste scanner.

Textract charges per page processed. QA and staging environments often
call Textract on the same document volume as production, wasting 20-40%
of the total bill. This scanner finds non-prod callers.

Logic:
  1. Pull Cost Explorer Textract spend grouped by environment tags.
  2. If tags are missing (common), fall back to CloudTrail to find
     which Lambda functions called Textract in the last N days.
  3. Cross-reference function names against env signals (qa, staging,
     test, dev) to identify non-prod callers.
  4. Estimate monthly waste from the non-prod fraction.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

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

# Max CloudTrail events to scan per call (avoid rate limits)
_CLOUDTRAIL_MAX_EVENTS = 1000


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


def _make_cloudtrail(region: str, role_arn: str | None = None):
    import boto3

    if role_arn:
        sts = boto3.client("sts")
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-textract-env")["Credentials"]
        return boto3.client(
            "cloudtrail",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
    return boto3.client("cloudtrail", region_name=region)


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


def _get_cloudtrail_callers(
    cloudtrail_client,
    start_time: datetime,
    end_time: datetime,
    max_events: int = _CLOUDTRAIL_MAX_EVENTS,
) -> dict[str, dict]:
    """
    Scan CloudTrail for Textract API calls and tally by caller.

    Returns a dict mapping caller identity (function name or IP) to
    {call_count, user_agent, source_ip, invoker}.
    """
    callers: dict[str, dict] = {}
    events_seen = 0
    kwargs: dict[str, Any] = dict(
        LookupAttributes=[
            {"AttributeKey": "EventSource", "AttributeValue": "textract.amazonaws.com"}
        ],
        StartTime=start_time,
        EndTime=end_time,
        MaxResults=50,
    )

    while events_seen < max_events:
        try:
            resp = cloudtrail_client.lookup_events(**kwargs)
        except Exception as exc:
            log.debug("CloudTrail lookup_events failed: %s", exc)
            break

        for event in resp.get("Events", []):
            events_seen += 1
            try:
                import json
                raw = event.get("CloudTrailEvent", "{}")
                detail = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue

            # Extract caller identity
            uid = detail.get("userIdentity", {})
            invoked_by = uid.get("sessionContext", {}).get("sessionIssuer", {}).get("userName", "")
            principal = uid.get("arn", "")
            source_ip = detail.get("sourceIPAddress", "")
            user_agent = detail.get("userAgent", "")

            # Prefer Lambda function name from ARN or principal
            caller_key = invoked_by or principal or source_ip or "unknown"

            # Extract function name from ARN like arn:aws:sts::123:assumed-role/func-name/session
            if "assumed-role" in caller_key:
                parts = caller_key.split("/")
                if len(parts) >= 2:
                    caller_key = parts[-2]  # role name, often = function name

            if caller_key not in callers:
                callers[caller_key] = {
                    "call_count": 0,
                    "user_agent": user_agent,
                    "source_ip": source_ip,
                    "invoker": invoked_by or principal,
                }
            callers[caller_key]["call_count"] += 1

        next_token = resp.get("NextToken")
        if not next_token:
            break
        kwargs["NextToken"] = next_token

    return callers


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

    # Fall back to CloudTrail if tags are missing or all unknown
    non_prod_callers: list[dict] = []
    cloudtrail_scan_done = False

    if not has_useful_tags and total_spend > 0:
        try:
            ct = _make_cloudtrail(region, role_arn)
            end_dt = datetime.now(tz=timezone.utc)
            start_dt = end_dt - timedelta(days=days)
            raw_callers = _get_cloudtrail_callers(ct, start_dt, end_dt)
            cloudtrail_scan_done = True

            total_calls = max(sum(c["call_count"] for c in raw_callers.values()), 1)

            for caller_id, info in raw_callers.items():
                is_nonprod, signal = _is_nonprod_name(caller_id)
                if is_nonprod:
                    call_fraction = info["call_count"] / total_calls
                    estimated_spend = round(total_spend * call_fraction, 2)
                    non_prod_callers.append({
                        "function_name": caller_id,
                        "call_count": info["call_count"],
                        "estimated_spend": estimated_spend,
                        "env_signal": signal,
                        "source_ip": info.get("source_ip", ""),
                    })

            non_prod_callers.sort(key=lambda x: -x["estimated_spend"])
        except Exception as exc:
            log.warning("CloudTrail scan failed: %s", exc)

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
    elif not has_useful_tags and not cloudtrail_scan_done:
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
        "cloudtrail_scan_done": cloudtrail_scan_done,
        "non_prod_callers": non_prod_callers,
        "estimated_monthly_waste": estimated_monthly_waste,
        "non_prod_pct": round(non_prod_pct * 100, 1),
        "recommendation": recommendation,
        "actions": actions,
    }
