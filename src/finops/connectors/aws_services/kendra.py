"""
Amazon Kendra cost analyzer.

Lists all Kendra indexes, determines edition and monthly cost,
fetches query volume from CloudWatch, and flags oversized or unused indexes.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any


# Fixed monthly costs per edition (us-east-1 on-demand pricing)
_EDITION_MONTHLY_USD = {
    "DEVELOPER_EDITION": 810.0,
    "ENTERPRISE_EDITION": 1400.0,
}

_EDITION_LABEL = {
    "DEVELOPER_EDITION": "DEVELOPER",
    "ENTERPRISE_EDITION": "ENTERPRISE",
}


def _make_session(region: str, role_arn: str | None = None):
    import boto3

    if role_arn:
        sts = boto3.client("sts")
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-kendra")["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
    return boto3.Session(region_name=region)


def _get_query_count(cw, index_id: str, days: int = 30) -> int | None:
    """Return total query count for a Kendra index over the past N days."""
    try:
        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=days)
        resp = cw.get_metric_statistics(
            Namespace="AWS/Kendra",
            MetricName="IndexQueryCount",
            Dimensions=[{"Name": "IndexId", "Value": index_id}],
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=["Sum"],
        )
        points = resp.get("Datapoints", [])
        if not points:
            return None
        return int(sum(p["Sum"] for p in points))
    except Exception:
        return None


def _get_document_count(kendra, index_id: str) -> int | None:
    try:
        resp = kendra.describe_index(Id=index_id)
        stats = resp.get("IndexStatistics", {})
        # FaqStatistics + TextDocumentStatistics
        faq = stats.get("FaqStatistics", {}).get("IndexedQuestionAnswersCount", 0)
        text = stats.get("TextDocumentStatistics", {}).get("IndexedTextDocumentsCount", 0)
        return faq + text
    except Exception:
        return None


class KendraAnalyzer:
    def __init__(self, region: str = "us-east-1", role_arn: str | None = None) -> None:
        self.region = region
        self.role_arn = role_arn

    def get_costs(self) -> str:
        session = _make_session(self.region, self.role_arn)
        kendra = session.client("kendra")
        cw = session.client("cloudwatch")

        # List all indexes
        indexes = []
        try:
            paginator = kendra.get_paginator("list_indices") if hasattr(kendra, "get_paginator") else None
            if paginator:
                for page in paginator.paginate():
                    indexes.extend(page.get("IndexConfigurationSummaryItems", []))
            else:
                resp = kendra.list_indices()
                indexes.extend(resp.get("IndexConfigurationSummaryItems", []))
        except Exception as exc:
            return f"Could not list Kendra indexes: {exc}"

        if not indexes:
            return "No Amazon Kendra indexes found in this account/region."

        total_monthly = 0.0
        lines: list[str] = []
        index_lines: list[str] = []

        for idx in indexes:
            index_id = idx.get("Id", "")
            index_name = idx.get("Name", index_id)
            edition_raw = idx.get("Edition", "DEVELOPER_EDITION")
            status = idx.get("Status", "UNKNOWN")

            edition_label = _EDITION_LABEL.get(edition_raw, edition_raw)
            monthly_cost = _EDITION_MONTHLY_USD.get(edition_raw, 0.0)
            total_monthly += monthly_cost

            query_count = _get_query_count(cw, index_id, days=30)
            doc_count = _get_document_count(kendra, index_id)

            # Flags
            flags: list[str] = []
            savings_line = ""

            if query_count is not None:
                if query_count < 100:
                    flags.append("POSSIBLY UNUSED")
                elif (
                    query_count < 5000
                    and edition_raw == "ENTERPRISE_EDITION"
                    and monthly_cost > 0
                ):
                    cost_per_query = monthly_cost / query_count
                    if cost_per_query > 1.00:
                        flags.append(f"HIGH cost per query (${cost_per_query:.2f}) - consider DEVELOPER edition")
                        dev_cost = _EDITION_MONTHLY_USD["DEVELOPER_EDITION"]
                        savings = monthly_cost - dev_cost
                        if savings > 0:
                            savings_line = f"  Estimated savings if switched to DEVELOPER: ${savings:,.0f}/mo"

            block: list[str] = [
                f"\nIndex: {index_name} ({edition_label}, ${monthly_cost:,.0f}/mo)  [{status}]",
            ]
            if doc_count is not None:
                block.append(f"  Documents: {doc_count:,}")
            if query_count is not None:
                block.append(f"  Queries last 30d: {query_count:,}")
                if query_count > 0:
                    cq = monthly_cost / query_count
                    flag_str = f"  [HIGH - consider DEVELOPER edition]" if cq > 1.00 and edition_raw == "ENTERPRISE_EDITION" else ""
                    block.append(f"  Cost per query: ${cq:.2f}{flag_str}")
            else:
                block.append("  Queries last 30d: no CloudWatch data")

            for f in flags:
                block.append(f"  Status: {f}")
            if savings_line:
                block.append(savings_line)

            index_lines.extend(block)

        header = f"Amazon Kendra: ${total_monthly:,.0f}/month ({len(indexes)} index{'es' if len(indexes) != 1 else ''})"
        return header + "\n".join(index_lines)
