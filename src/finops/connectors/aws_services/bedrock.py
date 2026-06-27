"""
Amazon Bedrock cost analyzer.

Breaks down Bedrock spend by model, token type (input vs output),
and compares to the prior period.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any


def _make_ce(role_arn: str | None = None):
    import boto3

    if role_arn:
        sts = boto3.client("sts")
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-bedrock")["Credentials"]
        return boto3.client(
            "ce",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name="us-east-1",
        )
    return boto3.client("ce", region_name="us-east-1")


def _make_boto_session(region: str, role_arn: str | None = None):
    import boto3

    if role_arn:
        sts = boto3.client("sts")
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-bedrock")["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
    return boto3.Session(region_name=region)


# Human-readable names for common model IDs
_MODEL_DISPLAY = {
    "anthropic.claude-3-5-sonnet": "Claude 3.5 Sonnet",
    "us.anthropic.claude-3-5-sonnet": "Claude 3.5 Sonnet",
    "anthropic.claude-3-sonnet": "Claude 3 Sonnet",
    "us.anthropic.claude-3-sonnet": "Claude 3 Sonnet",
    "anthropic.claude-3-haiku": "Claude 3 Haiku",
    "us.anthropic.claude-3-haiku": "Claude 3 Haiku",
    "anthropic.claude-3-opus": "Claude 3 Opus",
    "us.anthropic.claude-3-opus": "Claude 3 Opus",
    "anthropic.claude-instant": "Claude Instant",
    "amazon.titan-text-express": "Amazon Titan Text Express",
    "amazon.titan-text-lite": "Amazon Titan Text Lite",
    "amazon.titan-embed-text": "Amazon Titan Embed Text",
    "meta.llama2": "Meta Llama 2",
    "meta.llama3": "Meta Llama 3",
    "cohere.command": "Cohere Command",
    "cohere.embed": "Cohere Embed",
    "ai21.j2": "AI21 Jurassic-2",
    "mistral.mistral": "Mistral",
    "stability.stable-diffusion": "Stable Diffusion",
}


def _display_name(model_id: str) -> str:
    for prefix, name in _MODEL_DISPLAY.items():
        if model_id.lower().startswith(prefix):
            return name
    # Fall back: strip version suffixes, capitalize
    return model_id.replace("-", " ").replace(".", " ").title()


def _discover_bedrock_services(ce, start: str, end: str) -> list[str]:
    """Every CE service name AWS bills Bedrock under.

    Bedrock spend lands under plain "Amazon Bedrock" AND per-model SKUs like
    "Claude Sonnet 4.5 (Amazon Bedrock Edition)". Cost Explorer's SERVICE filter
    is exact-match (no contains), so we discover the names with a SERVICE-grouped
    query first, then filter to them. Filtering on just ["Amazon Bedrock"] misses
    all the per-model SKU spend and reports $0.
    """
    disc = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    return sorted({
        g["Keys"][0]
        for r in disc.get("ResultsByTime", [])
        for g in r.get("Groups", [])
        if "bedrock" in g["Keys"][0].lower()
    })


def _ce_query(ce, start: str, end: str) -> list[dict]:
    """Query CE for Bedrock spend grouped by SERVICE + USAGE_TYPE.

    Grouped by SERVICE as well as USAGE_TYPE so per-model SKU spend can be
    attributed to the model named in the service (the usage type is generic for
    those SKUs).
    """
    services = _discover_bedrock_services(ce, start, end)
    if not services:
        return []
    results = []
    kwargs: dict[str, Any] = dict(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": services}},
        GroupBy=[
            {"Type": "DIMENSION", "Key": "SERVICE"},
            {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
        ],
    )
    while True:
        resp = ce.get_cost_and_usage(**kwargs)
        results.extend(resp.get("ResultsByTime", []))
        token = resp.get("NextPageToken")
        if not token:
            break
        kwargs["NextPageToken"] = token
    return results


class BedrockAnalyzer:
    def __init__(self, region: str = "us-east-1", role_arn: str | None = None) -> None:
        self.region = region
        self.role_arn = role_arn

    def get_costs(self, days: int = 30) -> str:
        end = date.today()
        start = end - timedelta(days=days)
        prior_start = start - timedelta(days=days)

        ce = _make_ce(self.role_arn)

        current_rows = _ce_query(ce, start.isoformat(), end.isoformat())
        prior_rows = _ce_query(ce, prior_start.isoformat(), start.isoformat())

        # Aggregate by (service, usage type) across all time periods. SERVICE is
        # included so spend on per-model SKUs can be attributed to the model.
        current_by_su: dict[tuple[str, str], float] = {}
        for period in current_rows:
            for group in period.get("Groups", []):
                service, usage_type = group["Keys"][0], group["Keys"][1]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                current_by_su[(service, usage_type)] = current_by_su.get((service, usage_type), 0.0) + amount

        prior_total = sum(
            float(group["Metrics"]["UnblendedCost"]["Amount"])
            for period in prior_rows
            for group in period.get("Groups", [])
        )

        total = sum(current_by_su.values())

        if total == 0.0:
            return "No Amazon Bedrock spend found in the selected period."

        # Parse usage types into model + token type
        # CE usage type format: USE1-model-id:input-tokens or USE1-model-id:output-tokens
        model_input: dict[str, float] = {}
        model_output: dict[str, float] = {}
        model_other: dict[str, float] = {}

        for (service, usage_type), amount in current_by_su.items():
            if amount == 0.0:
                continue
            # Strip region prefix (e.g. "USE1-", "USW2-")
            parts = usage_type.split("-", 1)
            rest = parts[1] if len(parts) > 1 else usage_type

            # Extract model ID: everything before the last colon segment
            if ":" in rest:
                model_raw, token_type = rest.rsplit(":", 1)
            else:
                model_raw = rest
                token_type = ""

            model_id = model_raw.lower()
            # Per-model SKU services carry the model in the SERVICE name, not the
            # usage type, so fall back to the service when the usage type has no
            # real (vendor-dotted) model id.
            if "." not in model_id:
                model_id = (
                    service.replace(" (Amazon Bedrock Edition)", "").strip().lower()
                    or service.lower()
                )
            token_lower = token_type.lower()

            if "input" in token_lower:
                model_input[model_id] = model_input.get(model_id, 0.0) + amount
            elif "output" in token_lower:
                model_output[model_id] = model_output.get(model_id, 0.0) + amount
            else:
                model_other[model_id] = model_other.get(model_id, 0.0) + amount

        # Combine into per-model totals
        all_models = set(model_input) | set(model_output) | set(model_other)
        model_totals: dict[str, float] = {
            m: model_input.get(m, 0.0) + model_output.get(m, 0.0) + model_other.get(m, 0.0)
            for m in all_models
        }

        sorted_models = sorted(model_totals.items(), key=lambda x: -x[1])

        lines: list[str] = [
            f"Amazon Bedrock costs (last {days} days): ${total:,.2f}",
            "",
            "By model:",
        ]

        for model_id, model_cost in sorted_models:
            pct = model_cost / total * 100
            display = _display_name(model_id)
            inp = model_input.get(model_id, 0.0)
            out = model_output.get(model_id, 0.0)
            model_total_io = inp + out
            if model_total_io > 0:
                inp_pct = inp / model_total_io * 100
                out_pct = out / model_total_io * 100
                lines.append(
                    f"  {display:<28}  ${model_cost:>8,.2f}  ({inp_pct:.0f}% input, {out_pct:.0f}% output)"
                )
            else:
                lines.append(f"  {display:<28}  ${model_cost:>8,.2f}")

        # Top cost driver
        if sorted_models:
            top_model_id, top_cost = sorted_models[0]
            top_display = _display_name(top_model_id)
            daily_avg = top_cost / days
            lines += [
                "",
                f"Top cost driver: {top_display} at ${daily_avg:.2f}/day average",
            ]

        # Trend
        if prior_total > 0:
            change = (total - prior_total) / prior_total * 100
            direction = "+" if change >= 0 else ""
            lines.append(f"Trend: {direction}{change:.0f}% vs prior {days} days")
        elif total > 0:
            lines.append("Trend: no prior period data")

        return "\n".join(lines)
