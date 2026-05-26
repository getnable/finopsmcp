"""
AWS Textract cost analyzer.

Breaks down Textract spend by API type (sync vs async) and flags
high-cost sync usage where async alternatives exist.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def _make_ce(role_arn: str | None = None):
    import boto3

    if role_arn:
        sts = boto3.client("sts")
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-textract")["Credentials"]
        return boto3.client(
            "ce",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name="us-east-1",
        )
    return boto3.client("ce", region_name="us-east-1")


# Usage type keyword mapping
_SYNC_KEYWORDS = ["SyncDetect", "sync"]
_ASYNC_KEYWORDS = ["AsyncDetect", "async"]

# Approximate public pricing (USD per 1,000 pages)
_PRICING_PER_1K_PAGES: dict[str, float] = {
    "sync_text_detection": 1.50,
    "async_text_detection": 0.06,
    "analyze_document": 1.50,
    "analyze_expense": 0.02,
    "analyze_id": 0.01,
}

# Human labels for usage type patterns
_USAGE_LABELS: list[tuple[str, str]] = [
    ("SyncDetectDocumentText", "Sync text detection"),
    ("AsyncDetectDocumentText", "Async text detection"),
    ("AnalyzeDocument", "Analyze document"),
    ("AnalyzeExpense", "Analyze expense"),
    ("AnalyzeID", "Analyze ID"),
    ("DetectDocumentText", "Detect text"),
    ("StartDocumentTextDetection", "Start async text detection"),
]


def _label(usage_type: str) -> str:
    lower = usage_type.lower()
    for keyword, label in _USAGE_LABELS:
        if keyword.lower() in lower:
            return label
    return usage_type


def _is_sync(usage_type: str) -> bool:
    lower = usage_type.lower()
    return any(k.lower() in lower for k in _SYNC_KEYWORDS) or (
        "detect" in lower and "async" not in lower and "start" not in lower
    )


def _is_async(usage_type: str) -> bool:
    lower = usage_type.lower()
    return any(k.lower() in lower for k in _ASYNC_KEYWORDS) or "start" in lower


class TextractAnalyzer:
    def __init__(self, role_arn: str | None = None) -> None:
        self.role_arn = role_arn

    def get_costs(self, days: int = 30) -> str:
        end = date.today()
        start = end - timedelta(days=days)

        ce = _make_ce(self.role_arn)

        results: list[dict] = []
        kwargs: dict[str, Any] = dict(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Textract"]}},
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
        while True:
            resp = ce.get_cost_and_usage(**kwargs)
            results.extend(resp.get("ResultsByTime", []))
            token = resp.get("NextPageToken")
            if not token:
                break
            kwargs["NextPageToken"] = token

        usage_costs: dict[str, float] = {}
        for period in results:
            for group in period.get("Groups", []):
                usage_type = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                usage_costs[usage_type] = usage_costs.get(usage_type, 0.0) + amount

        total = sum(usage_costs.values())
        if total == 0.0:
            return "No AWS Textract spend found in the selected period."

        sync_total = sum(v for k, v in usage_costs.items() if _is_sync(k))
        async_total = sum(v for k, v in usage_costs.items() if _is_async(k))
        other_total = total - sync_total - async_total

        sync_pct = sync_total / total * 100 if total > 0 else 0.0

        lines: list[str] = [
            f"AWS Textract costs (last {days} days): ${total:,.2f}",
            "",
            "By API type:",
            f"  Sync APIs:   ${sync_total:>8,.2f}  ({sync_pct:.0f}%)",
            f"  Async APIs:  ${async_total:>8,.2f}  ({100 - sync_pct:.0f}%)",
        ]
        if other_total > 0.01:
            lines.append(f"  Other:       ${other_total:>8,.2f}")

        # Detail by usage type
        if len(usage_costs) > 1:
            lines += ["", "By usage type:"]
            for usage_type, amount in sorted(usage_costs.items(), key=lambda x: -x[1]):
                if amount < 0.01:
                    continue
                label = _label(usage_type)
                lines.append(f"  {label:<35}  ${amount:>8,.2f}")

        # Flag high sync usage
        if sync_pct > 20.0 and sync_total > 10.0:
            lines += [
                "",
                f"Sync API usage is {sync_pct:.0f}% of Textract spend.",
                "Sync DetectDocumentText costs $1.50/1k pages. Async equivalent costs $0.06/1k pages.",
                "Migrating high-volume sync calls to StartDocumentTextDetection / GetDocumentTextDetection",
                "could reduce Textract spend by up to 96%.",
            ]
        elif sync_pct > 0 and sync_total > 0:
            lines += ["", "Sync API usage is within acceptable range."]
        else:
            lines += ["", "All Textract usage is async. Pricing is optimized."]

        return "\n".join(lines)
