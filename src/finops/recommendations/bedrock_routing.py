"""
Bedrock model routing recommender.

Bedrock Sonnet costs ~20x more than Haiku per token. Many workloads
that use Sonnet (classification, extraction, short-context lookups)
work equally well on Haiku. This scanner identifies those workloads
and estimates the savings from routing them to cheaper models.

Logic:
  1. Get Bedrock costs from Cost Explorer grouped by USAGE_TYPE.
  2. Get CloudWatch metrics per model: InvocationCount, InputTokenCount,
     OutputTokenCount.
  3. Calculate average tokens per invocation per model.
  4. Flag short-input + short-output invocations as Haiku candidates.
  5. Estimate monthly savings from routing eligible invocations.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# Per 1M tokens pricing (input, output) as of 2026
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5":    (3.00,  15.00),
    "claude-sonnet-4-6":    (3.00,  15.00),
    "claude-haiku-3-5":     (0.80,   4.00),
    "claude-haiku-3":       (0.25,   1.25),
    "claude-opus-4":        (15.00, 75.00),
    # Also accept anthropic.* prefixes
    "anthropic.claude-sonnet-4-5": (3.00,  15.00),
    "anthropic.claude-sonnet-4-6": (3.00,  15.00),
    "anthropic.claude-haiku-3-5":  (0.80,   4.00),
    "anthropic.claude-haiku-3":    (0.25,   1.25),
    "anthropic.claude-opus-4":     (15.00, 75.00),
}

# Routing thresholds: invocations below these avg token counts
# are likely short tasks (classification, extraction, lookup).
_ROUTING_MAX_AVG_INPUT_TOKENS = 500
_ROUTING_MAX_AVG_OUTPUT_TOKENS = 200

# Models that are routing targets (cheaper alternatives)
_ROUTING_TARGETS = ["claude-haiku-3-5", "claude-haiku-3"]

# Models eligible to route FROM (expensive)
_ROUTING_SOURCES = ["claude-sonnet-4-5", "claude-sonnet-4-6", "claude-opus-4"]


def _make_ce(role_arn: str | None = None):
    import boto3

    if role_arn:
        sts = boto3.client("sts")
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-bedrock-routing")["Credentials"]
        return boto3.client(
            "ce",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name="us-east-1",
        )
    return boto3.client("ce", region_name="us-east-1")


def _make_cw(region: str, role_arn: str | None = None):
    import boto3

    if role_arn:
        sts = boto3.client("sts")
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-bedrock-routing")["Credentials"]
        return boto3.client(
            "cloudwatch",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
    return boto3.client("cloudwatch", region_name=region)


def _parse_model_from_usage_type(usage_type: str) -> str:
    """
    Extract a normalized model ID from a Cost Explorer USAGE_TYPE string.

    CE usage types look like: USE1-anthropic.claude-3-5-sonnet-20241022:input-tokens
    """
    # Strip region prefix (e.g. USE1-, USW2-)
    parts = usage_type.split("-", 1)
    rest = parts[1] if len(parts) > 1 else usage_type
    # Strip token type suffix
    if ":" in rest:
        rest = rest.rsplit(":", 1)[0]
    return rest.lower()


def _normalize_model_id(raw: str) -> str:
    """Map a raw CE model string to a canonical MODEL_PRICING key."""
    lower = raw.lower()
    for key in MODEL_PRICING:
        if key in lower:
            return key
    # Try without date suffix: claude-3-5-sonnet-20241022 -> claude-sonnet-4-5
    if "sonnet" in lower:
        if "4-5" in lower or "3-5" in lower:
            return "claude-sonnet-4-5"
        if "4-6" in lower or "claude-3-sonnet" in lower:
            return "claude-sonnet-4-6"
    if "haiku" in lower:
        if "3-5" in lower:
            return "claude-haiku-3-5"
        return "claude-haiku-3"
    if "opus" in lower:
        return "claude-opus-4"
    return raw


def _get_bedrock_ce_costs(ce, start: str, end: str) -> dict[str, dict[str, float]]:
    """
    Query Cost Explorer for Bedrock spend grouped by USAGE_TYPE.

    Returns a dict: model_id -> {input_cost, output_cost, total_cost}.
    """
    model_costs: dict[str, dict[str, float]] = {}

    kwargs: dict[str, Any] = dict(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    )
    while True:
        try:
            resp = ce.get_cost_and_usage(**kwargs)
        except Exception as exc:
            log.debug("CE Bedrock query failed: %s", exc)
            break

        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                usage_type = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                if amount == 0.0:
                    continue

                raw_model = _parse_model_from_usage_type(usage_type)
                model_id = _normalize_model_id(raw_model)

                if model_id not in model_costs:
                    model_costs[model_id] = {"input_cost": 0.0, "output_cost": 0.0, "total_cost": 0.0}

                token_type = usage_type.lower()
                if "input" in token_type:
                    model_costs[model_id]["input_cost"] += amount
                elif "output" in token_type:
                    model_costs[model_id]["output_cost"] += amount

                model_costs[model_id]["total_cost"] += amount

        token = resp.get("NextPageToken")
        if not token:
            break
        kwargs["NextPageToken"] = token

    return model_costs


def _get_cw_metrics(cw, model_id: str, start_dt: datetime, end_dt: datetime, period_seconds: int) -> dict[str, float]:
    """
    Fetch CloudWatch metrics for a Bedrock model.

    Returns {invocation_count, input_tokens, output_tokens}.
    """
    metrics = {
        "invocation_count": 0.0,
        "input_tokens": 0.0,
        "output_tokens": 0.0,
    }

    # CloudWatch Bedrock metric dimension key
    dimension = [{"Name": "ModelId", "Value": model_id}]
    namespace = "AWS/Bedrock"

    metric_map = {
        "invocation_count": "Invocations",
        "input_tokens": "InputTokenCount",
        "output_tokens": "OutputTokenCount",
    }

    for key, metric_name in metric_map.items():
        try:
            resp = cw.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric_name,
                Dimensions=dimension,
                StartTime=start_dt,
                EndTime=end_dt,
                Period=period_seconds,
                Statistics=["Sum"],
            )
            total = sum(dp.get("Sum", 0.0) for dp in resp.get("Datapoints", []))
            metrics[key] = total
        except Exception as exc:
            log.debug("CW metric %s failed for model %s: %s", metric_name, model_id, exc)

    return metrics


def _cost_per_invocation(model_id: str, avg_input_tokens: float, avg_output_tokens: float) -> float:
    """Calculate cost per invocation given average token counts."""
    pricing = MODEL_PRICING.get(model_id)
    if not pricing:
        return 0.0
    input_price, output_price = pricing
    return (avg_input_tokens * input_price + avg_output_tokens * output_price) / 1_000_000


def recommend_bedrock_model_routing(
    days: int = 30,
    region: str = "us-east-1",
    role_arn: str | None = None,
) -> dict:
    """
    Analyze Bedrock model usage and recommend routing to cheaper models.

    Returns a structured dict with models in use, routing opportunities,
    and estimated monthly savings.
    """
    end = date.today()
    start = end - timedelta(days=days)
    start_str = start.isoformat()
    end_str = end.isoformat()

    end_dt = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    period_seconds = days * 24 * 3600

    ce = _make_ce(role_arn)
    cw = _make_cw(region, role_arn)

    model_ce_costs = _get_bedrock_ce_costs(ce, start_str, end_str)

    models_in_use: list[dict] = []
    routing_opportunities: list[dict] = []
    total_monthly_savings = 0.0

    for model_id, cost_data in model_ce_costs.items():
        monthly_cost = cost_data["total_cost"] * (30 / days)

        # Get CloudWatch metrics for this model
        cw_metrics = _get_cw_metrics(cw, model_id, start_dt, end_dt, period_seconds)
        invocation_count = cw_metrics["invocation_count"]
        input_tokens = cw_metrics["input_tokens"]
        output_tokens = cw_metrics["output_tokens"]

        avg_input = input_tokens / invocation_count if invocation_count > 0 else 0.0
        avg_output = output_tokens / invocation_count if invocation_count > 0 else 0.0

        models_in_use.append({
            "model_id": model_id,
            "monthly_cost": round(monthly_cost, 2),
            "invocation_count": int(invocation_count),
            "avg_input_tokens": round(avg_input, 1),
            "avg_output_tokens": round(avg_output, 1),
        })

        # Check if this is a routing source model
        canonical = _normalize_model_id(model_id)
        if canonical not in _ROUTING_SOURCES:
            continue

        # Routing signal: short inputs + short outputs = likely classification/extraction
        is_short_task = (
            avg_input < _ROUTING_MAX_AVG_INPUT_TOKENS
            and avg_output < _ROUTING_MAX_AVG_OUTPUT_TOKENS
            and invocation_count > 0
        )

        # High invocation count + low avg tokens = batch task
        is_batch_task = (
            invocation_count > 1000
            and avg_input < 1000
            and avg_output < 500
        )

        # Low invocation count + high avg tokens = complex reasoning, keep Sonnet
        is_complex = avg_input >= 2000 or avg_output >= 1000

        if is_complex:
            continue

        if is_short_task or is_batch_task:
            # Pick cheapest routing target
            target_model = "claude-haiku-3-5"

            current_cost_per_call = _cost_per_invocation(canonical, avg_input, avg_output)
            target_cost_per_call = _cost_per_invocation(target_model, avg_input, avg_output)

            # Conservative: assume 70% of invocations are eligible for routing
            eligible_pct = 0.70
            eligible_invocations = invocation_count * eligible_pct

            monthly_invocations = invocation_count * (30 / days)
            monthly_eligible = monthly_invocations * eligible_pct

            current_monthly_cost = round(monthly_cost, 2)
            projected_monthly_cost = round(
                monthly_invocations * (
                    eligible_pct * target_cost_per_call
                    + (1 - eligible_pct) * current_cost_per_call
                ),
                2,
            )
            monthly_savings = round(max(current_monthly_cost - projected_monthly_cost, 0.0), 2)
            total_monthly_savings += monthly_savings

            if is_short_task:
                signal = (
                    f"Avg {avg_input:.0f} input tokens + {avg_output:.0f} output tokens per call "
                    "suggests classification or extraction. Haiku handles these equally well."
                )
            else:
                signal = (
                    f"High call volume ({int(invocation_count):,} calls) with low avg token counts "
                    "suggests a batch/fan-out pattern. Routing to Haiku for most calls."
                )

            routing_opportunities.append({
                "current_model": canonical,
                "recommended_model": target_model,
                "eligible_invocations_pct": round(eligible_pct * 100, 0),
                "current_monthly_cost": current_monthly_cost,
                "projected_monthly_cost": projected_monthly_cost,
                "monthly_savings": monthly_savings,
                "routing_signal": signal,
            })

    models_in_use.sort(key=lambda x: -x["monthly_cost"])
    routing_opportunities.sort(key=lambda x: -x["monthly_savings"])

    if routing_opportunities:
        implementation_note = (
            "To implement model routing: check the task type before calling Bedrock. "
            "For classification or extraction, set model_id to 'anthropic.claude-haiku-3-5-20241022-v1:0'. "
            "Keep 'anthropic.claude-sonnet-...' for multi-step reasoning, long-form generation, "
            "or any task with more than 1k input tokens. "
            "A simple approach: wrap your Bedrock call with a router function that checks "
            "estimated input length and task type before selecting the model."
        )
    else:
        implementation_note = (
            "No clear routing opportunities detected. "
            "Either usage is already on appropriate models, "
            "or CloudWatch metrics were unavailable to assess token counts."
        )

    return {
        "models_in_use": models_in_use,
        "routing_opportunities": routing_opportunities,
        "total_monthly_savings": round(total_monthly_savings, 2),
        "implementation_note": implementation_note,
    }
