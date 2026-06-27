"""
Fetches cost data broken down by tags from each cloud provider.
Returns (service, tags, amount) tuples ready for the mapper.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any

from .mapper import tags_to_attribution


# ── AWS ──────────────────────────────────────────────────────────────────────

def fetch_aws_tagged_costs(
    start_date: date,
    end_date: date,
    tag_keys: list[str],
    role_arns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Uses Cost Explorer GroupBy DIMENSION+TAG to get real tagged spend.
    Returns list of {service, tags, amount_usd, account_id}.
    """
    import boto3

    targets = role_arns or [None]
    results = []

    for role_arn in targets:
        if role_arn:
            sts = boto3.client("sts")
            creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-attribution")["Credentials"]
            ce = boto3.client(
                "ce",
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name="us-east-1",
            )
            account_id = role_arn.split(":")[4]
        else:
            ce = boto3.client("ce", region_name="us-east-1")
            sts = boto3.client("sts")
            account_id = sts.get_caller_identity()["Account"]

        # Group by SERVICE + each tag key
        group_by = [{"Type": "DIMENSION", "Key": "SERVICE"}]
        for key in tag_keys:
            group_by.append({"Type": "TAG", "Key": key})

        kwargs: dict[str, Any] = dict(
            TimePeriod={"Start": start_date.isoformat(), "End": end_date.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=group_by,
        )

        while True:
            resp = ce.get_cost_and_usage(**kwargs)
            for period in resp.get("ResultsByTime", []):
                for group in period.get("Groups", []):
                    keys = group.get("Keys", [])
                    service = keys[0] if keys else "Unknown"
                    tags: dict[str, str] = {}
                    for i, tag_key in enumerate(tag_keys, start=1):
                        raw = keys[i] if i < len(keys) else ""
                        # AWS prefixes tag values with "tag_key$"
                        val = raw.split("$", 1)[-1] if "$" in raw else raw
                        if val:
                            tags[tag_key] = val
                    amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                    if amount > 0:
                        results.append({
                            "account_id": account_id,
                            "service": service,
                            "tags": tags,
                            "amount_usd": amount,
                            "attribution": tags_to_attribution(tags),
                        })
            token = resp.get("NextPageToken")
            if not token:
                break
            kwargs["NextPageToken"] = token

    return results


# ── Azure ─────────────────────────────────────────────────────────────────────

def fetch_azure_tagged_costs(
    subscription_ids: list[str],
    start_date: date,
    end_date: date,
    tag_keys: list[str],
) -> list[dict[str, Any]]:
    from azure.identity import ClientSecretCredential
    from azure.mgmt.costmanagement import CostManagementClient
    from azure.mgmt.costmanagement.models import (
        QueryDataset, QueryDefinition, QueryGrouping, QueryTimePeriod,
    )

    cred = ClientSecretCredential(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )
    client = CostManagementClient(cred)
    results = []

    for sub_id in subscription_ids:
        scope = f"/subscriptions/{sub_id}"
        grouping = [QueryGrouping(type="Dimension", name="ServiceName")]
        for key in tag_keys:
            grouping.append(QueryGrouping(type="TagKey", name=key))

        query = QueryDefinition(
            type="ActualCost",
            timeframe="Custom",
            time_period=QueryTimePeriod(
                from_property=f"{start_date.isoformat()}T00:00:00Z",
                to=f"{end_date.isoformat()}T00:00:00Z",
            ),
            dataset=QueryDataset(granularity="Monthly", grouping=grouping),
        )
        result = client.query.usage(scope=scope, parameters=query)
        columns = {col.name: i for i, col in enumerate(result.columns)}
        cost_idx = columns.get("Cost", 0)
        service_idx = columns.get("ServiceName", 1)

        for row in result.rows or []:
            amount = float(row[cost_idx])
            if amount <= 0:
                continue
            service = str(row[service_idx])
            tags: dict[str, str] = {}
            for j, key in enumerate(tag_keys, start=2):
                val = str(row[j]) if j < len(row) else ""
                if val and val.lower() not in ("", "none", "null"):
                    tags[key] = val
            results.append({
                "account_id": sub_id,
                "service": service,
                "tags": tags,
                "amount_usd": amount,
                "attribution": tags_to_attribution(tags),
            })

    return results


# ── GCP ───────────────────────────────────────────────────────────────────────

def fetch_gcp_tagged_costs(
    billing_account_ids: list[str],
    start_date: date,
    end_date: date,
    label_keys: list[str],
) -> list[dict[str, Any]]:
    bq_table = os.getenv("GCP_BQ_BILLING_TABLE")
    if not bq_table:
        return []

    from google.cloud import bigquery

    client = bigquery.Client()
    results = []

    # Attribution label keys come from local tag-rule config and get
    # interpolated into BigQuery SQL. Allowlist them so a poisoned/shared
    # tag_rules.yaml can't inject (defense-in-depth: this path isn't wired to
    # an MCP tool yet, but it must be injection-safe the moment it is).
    _allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:@./-")
    for _k in label_keys:
        if not _k or len(_k) > 128 or any(_c not in _allowed for _c in _k):
            raise ValueError(f"Unsafe attribution label key {_k!r}: allowed chars are [A-Za-z0-9_:@./-]")

    def _label_alias(k: str) -> str:
        return "label_" + "".join(c if (c.isalnum() or c == "_") else "_" for c in k)

    label_selects = ", ".join(
        f"(SELECT value FROM UNNEST(labels) WHERE key = '{k}' LIMIT 1) AS {_label_alias(k)}"
        for k in label_keys
    )
    label_cols = [_label_alias(k) for k in label_keys]

    for billing_account_id in billing_account_ids:
        query = f"""
            SELECT
                service.description AS service,
                SUM(cost) AS total_cost,
                {label_selects}
            FROM `{bq_table}`
            WHERE
                billing_account_id = @billing_account_id
                AND DATE(usage_start_time) >= @start_date
                AND DATE(usage_start_time) <= @end_date
            GROUP BY service, {', '.join(label_cols)}
            HAVING total_cost > 0
            ORDER BY total_cost DESC
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("billing_account_id", "STRING", billing_account_id),
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date.isoformat()),
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date.isoformat()),
        ])
        for row in client.query(query, job_config=job_config).result():
            row_dict = dict(row)
            tags: dict[str, str] = {}
            for k, col in zip(label_keys, label_cols):
                val = row_dict.get(col) or ""
                if val:
                    tags[k] = val
            results.append({
                "account_id": billing_account_id,
                "service": row_dict.get("service", "Unknown"),
                "tags": tags,
                "amount_usd": float(row_dict.get("total_cost", 0)),
                "attribution": tags_to_attribution(tags),
            })

    return results
