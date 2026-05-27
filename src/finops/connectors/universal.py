"""
Universal service cost connector.

Handles cost queries for ANY service across AWS, Azure, and GCP without
needing a hardcoded connector per service. Works by passing the service
name through to the provider's native cost API dimension filter.

AWS:   Cost Explorer SERVICE dimension (covers all 200+ AWS services)
Azure: Cost Management ServiceName/ResourceType dimension
GCP:   Cloud Billing service.description filter
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

# ── AWS service name aliases ──────────────────────────────────────────────────
# CE uses long names like "Amazon Elastic Compute Cloud - Compute".
# Map short/common names to the prefix CE uses so fuzzy matching works.
_AWS_ALIASES: dict[str, str] = {
    "ec2":              "Amazon Elastic Compute Cloud",
    "rds":              "Amazon Relational Database Service",
    "s3":               "Amazon Simple Storage Service",
    "lambda":           "AWS Lambda",
    "cloudfront":       "Amazon CloudFront",
    "dynamodb":         "Amazon DynamoDB",
    "elasticache":      "Amazon ElastiCache",
    "eks":              "Amazon Elastic Kubernetes Service",
    "ecs":              "Amazon Elastic Container Service",
    "ecr":              "Amazon Elastic Container Registry",
    "sqs":              "Amazon Simple Queue Service",
    "sns":              "Amazon Simple Notification Service",
    "kinesis":          "Amazon Kinesis",
    "glue":             "AWS Glue",
    "athena":           "Amazon Athena",
    "redshift":         "Amazon Redshift",
    "emr":              "Amazon Elastic MapReduce",
    "sagemaker":        "Amazon SageMaker",
    "bedrock":          "Amazon Bedrock",
    "opensearch":       "Amazon OpenSearch Service",
    "elasticsearch":    "Amazon OpenSearch Service",
    "msk":              "Amazon Managed Streaming for Apache Kafka",
    "kafka":            "Amazon Managed Streaming for Apache Kafka",
    "stepfunctions":    "AWS Step Functions",
    "apigateway":       "Amazon API Gateway",
    "appsync":          "AWS AppSync",
    "amplify":          "AWS Amplify",
    "cognito":          "Amazon Cognito",
    "iam":              "AWS Identity and Access Management",
    "kms":              "AWS Key Management Service",
    "secrets":          "AWS Secrets Manager",
    "ssm":              "AWS Systems Manager",
    "cloudwatch":       "Amazon CloudWatch",
    "cloudtrail":       "AWS CloudTrail",
    "config":           "AWS Config",
    "guardduty":        "Amazon GuardDuty",
    "inspector":        "Amazon Inspector",
    "macie":            "Amazon Macie",
    "securityhub":      "AWS Security Hub",
    "waf":              "AWS WAF",
    "shield":           "AWS Shield",
    "route53":          "Amazon Route 53",
    "vpc":              "Amazon Virtual Private Cloud",
    "direct connect":   "AWS Direct Connect",
    "transfer":         "AWS Transfer Family",
    "fsx":              "Amazon FSx",
    "efs":              "Amazon Elastic File System",
    "backup":           "AWS Backup",
    "storage gateway":  "AWS Storage Gateway",
    "codecommit":       "AWS CodeCommit",
    "codebuild":        "AWS CodeBuild",
    "codedeploy":       "AWS CodeDeploy",
    "codepipeline":     "AWS CodePipeline",
    "documentdb":       "Amazon DocumentDB",
    "neptune":          "Amazon Neptune",
    "timestream":       "Amazon Timestream",
    "qldb":             "Amazon QLDB",
    "keyspaces":        "Amazon Keyspaces",
    "location":         "Amazon Location Service",
    "iot":              "AWS IoT Core",
    "greengrass":       "AWS IoT Greengrass",
    "rekognition":      "Amazon Rekognition",
    "textract":         "Amazon Textract",
    "comprehend":       "Amazon Comprehend",
    "translate":        "Amazon Translate",
    "polly":            "Amazon Polly",
    "transcribe":       "Amazon Transcribe",
    "lex":              "Amazon Lex",
    "personalize":      "Amazon Personalize",
    "forecast":         "Amazon Forecast",
    "kendra":           "Amazon Kendra",
    "connect":          "Amazon Connect",
    "chime":            "Amazon Chime",
    "workspaces":       "Amazon WorkSpaces",
    "appstream":        "Amazon AppStream",
    "workmail":         "Amazon WorkMail",
    "ses":              "Amazon Simple Email Service",
    "pinpoint":         "Amazon Pinpoint",
    "data transfer":    "AWS Data Transfer",
    "support":          "AWS Support",
    "marketplace":      "AWS Marketplace",
    "lightsail":        "Amazon Lightsail",
    "batch":            "AWS Batch",
    "fargate":          "AWS Fargate",
    "app runner":       "AWS App Runner",
    "elastic beanstalk":"AWS Elastic Beanstalk",
    "cloudformation":   "AWS CloudFormation",
    "cdk":              "AWS CloudFormation",
    "service catalog":  "AWS Service Catalog",
    "control tower":    "AWS Control Tower",
    "organizations":    "AWS Organizations",
}


def _resolve_aws_service(name: str, known_services: list[str]) -> str | None:
    """
    Resolve a user-supplied service name to the exact CE service name.
    Priority: exact match > alias lookup > prefix match > substring match.
    Returns None if no match found.
    """
    lower = name.lower().strip()

    # 1. Exact match (case-insensitive)
    for s in known_services:
        if s.lower() == lower:
            return s

    # 2. Alias map
    alias_target = _AWS_ALIASES.get(lower)
    if alias_target:
        for s in known_services:
            if s.lower().startswith(alias_target.lower()):
                return s

    # 3. Prefix match on the canonical name
    for s in known_services:
        if s.lower().startswith(lower) or lower.startswith(s.lower()):
            return s

    # 4. Substring match
    matches = [s for s in known_services if lower in s.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Return all matches so the caller can present options
        return None

    return None


# ── AWS ───────────────────────────────────────────────────────────────────────

def list_aws_services(
    start_date: date | None = None,
    end_date: date | None = None,
    min_spend: float = 0.01,
) -> list[dict]:
    """
    Return every AWS service that has spend in the period, sorted by cost desc.
    Works for any service CE knows about (200+).
    """
    import boto3

    end = end_date or date.today()
    start = start_date or (end - timedelta(days=30))

    ce = boto3.client("ce", region_name="us-east-1")
    results: list[dict] = []
    kwargs: dict[str, Any] = dict(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    while True:
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                svc = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                if amount >= min_spend:
                    existing = next((r for r in results if r["service"] == svc), None)
                    if existing:
                        existing["cost_usd"] += amount
                    else:
                        results.append({"service": svc, "cost_usd": amount, "provider": "aws"})
        token = resp.get("NextPageToken")
        if not token:
            break
        kwargs["NextPageToken"] = token

    results.sort(key=lambda r: r["cost_usd"], reverse=True)
    return results


def get_aws_service_cost(
    service_name: str,
    start_date: date | None = None,
    end_date: date | None = None,
    granularity: str = "DAILY",
) -> dict:
    """
    Return cost breakdown for any AWS service by name.
    Resolves short names (e.g. "ElastiCache", "MSK", "AppSync") automatically.
    """
    import boto3

    end = end_date or date.today()
    start = start_date or (end - timedelta(days=30))

    # First get all active services so we can fuzzy-match the name
    all_services = list_aws_services(start, end, min_spend=0.0)
    known_names = [s["service"] for s in all_services]

    resolved = _resolve_aws_service(service_name, known_names)
    if resolved is None:
        # Find close matches for a helpful error
        lower = service_name.lower()
        suggestions = [s for s in known_names if lower in s.lower() or s.lower() in lower][:5]
        return {
            "error": f"No AWS service matching '{service_name}' found with spend in this period.",
            "suggestions": suggestions,
            "all_active_services": known_names,
        }

    ce = boto3.client("ce", region_name="us-east-1")
    kwargs: dict[str, Any] = dict(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity=granularity,
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": [resolved]}},
    )

    daily: list[dict] = []
    by_usage_type: dict[str, float] = {}
    total = 0.0

    while True:
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp.get("ResultsByTime", []):
            period_total = 0.0
            for group in period.get("Groups", []):
                usage_type = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                period_total += amount
                total += amount
                by_usage_type[usage_type] = by_usage_type.get(usage_type, 0.0) + amount
            daily.append({"date": period["TimePeriod"]["Start"], "cost_usd": round(period_total, 4)})
        token = resp.get("NextPageToken")
        if not token:
            break
        kwargs["NextPageToken"] = token

    # Sort usage types by cost
    top_usage = sorted(by_usage_type.items(), key=lambda x: x[1], reverse=True)

    return {
        "service": resolved,
        "provider": "aws",
        "period": f"{start.isoformat()} to {end.isoformat()}",
        "total_usd": round(total, 2),
        "daily": daily,
        "by_usage_type": {k: round(v, 4) for k, v in top_usage[:20]},
    }


# ── Azure ─────────────────────────────────────────────────────────────────────

def list_azure_services(
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict]:
    """Return every Azure service/meter category with spend, sorted by cost desc."""
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.costmanagement import CostManagementClient

    end = end_date or date.today()
    start = start_date or (end - timedelta(days=30))

    sub_ids = [s.strip() for s in os.getenv("AZURE_SUBSCRIPTION_IDS", "").split(",") if s.strip()]
    if not sub_ids:
        return [{"error": "AZURE_SUBSCRIPTION_IDS not configured"}]

    credential = DefaultAzureCredential()
    client = CostManagementClient(credential)

    results: list[dict] = []
    for sub_id in sub_ids:
        scope = f"/subscriptions/{sub_id}"
        try:
            resp = client.query.usage(
                scope=scope,
                parameters={
                    "type": "ActualCost",
                    "timeframe": "Custom",
                    "timePeriod": {"from": f"{start.isoformat()}T00:00:00Z", "to": f"{end.isoformat()}T23:59:59Z"},
                    "dataset": {
                        "granularity": "None",
                        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                        "grouping": [{"type": "Dimension", "name": "ServiceName"}],
                    },
                },
            )
            cols = [c["name"].lower() for c in resp.columns]
            cost_idx = next((i for i, c in enumerate(cols) if "cost" in c), 0)
            name_idx = next((i for i, c in enumerate(cols) if "service" in c or "name" in c), 1)
            for row in resp.rows:
                svc = str(row[name_idx])
                amount = float(row[cost_idx])
                if amount > 0.001:
                    existing = next((r for r in results if r["service"] == svc), None)
                    if existing:
                        existing["cost_usd"] += amount
                    else:
                        results.append({"service": svc, "cost_usd": amount, "provider": "azure"})
        except Exception as e:
            results.append({"error": str(e), "subscription": sub_id})

    results.sort(key=lambda r: r.get("cost_usd", 0), reverse=True)
    return results


def get_azure_service_cost(
    service_name: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    """Return cost breakdown for any Azure service by name (ServiceName dimension)."""
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.costmanagement import CostManagementClient

    end = end_date or date.today()
    start = start_date or (end - timedelta(days=30))

    sub_ids = [s.strip() for s in os.getenv("AZURE_SUBSCRIPTION_IDS", "").split(",") if s.strip()]
    if not sub_ids:
        return {"error": "AZURE_SUBSCRIPTION_IDS not configured"}

    # Resolve service name via listing
    all_services = list_azure_services(start, end)
    known = [s["service"] for s in all_services if "service" in s]
    lower = service_name.lower()
    resolved = next((s for s in known if s.lower() == lower), None)
    if not resolved:
        resolved = next((s for s in known if lower in s.lower()), None)
    if not resolved:
        suggestions = [s for s in known if any(w in s.lower() for w in lower.split())][:5]
        return {
            "error": f"No Azure service matching '{service_name}' found.",
            "suggestions": suggestions,
            "all_active_services": known,
        }

    credential = DefaultAzureCredential()
    client = CostManagementClient(credential)
    daily: list[dict] = []
    total = 0.0

    for sub_id in sub_ids:
        scope = f"/subscriptions/{sub_id}"
        try:
            resp = client.query.usage(
                scope=scope,
                parameters={
                    "type": "ActualCost",
                    "timeframe": "Custom",
                    "timePeriod": {"from": f"{start.isoformat()}T00:00:00Z", "to": f"{end.isoformat()}T23:59:59Z"},
                    "dataset": {
                        "granularity": "Daily",
                        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                        "grouping": [{"type": "Dimension", "name": "ServiceName"}],
                        "filter": {"dimensions": {"name": "ServiceName", "operator": "In", "values": [resolved]}},
                    },
                },
            )
            cols = [c["name"].lower() for c in resp.columns]
            cost_idx = next((i for i, c in enumerate(cols) if "cost" in c), 0)
            date_idx = next((i for i, c in enumerate(cols) if "date" in c or "usage" in c), 2)
            for row in resp.rows:
                amount = float(row[cost_idx])
                dt = str(row[date_idx])[:10]
                total += amount
                daily.append({"date": dt, "cost_usd": round(amount, 4), "subscription": sub_id})
        except Exception as e:
            daily.append({"error": str(e), "subscription": sub_id})

    return {
        "service": resolved,
        "provider": "azure",
        "period": f"{start.isoformat()} to {end.isoformat()}",
        "total_usd": round(total, 2),
        "daily": daily,
    }


# ── GCP ───────────────────────────────────────────────────────────────────────

def list_gcp_services(
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict]:
    """Return every GCP service with spend in the period, sorted by cost desc."""
    from google.cloud import bigquery

    end = end_date or date.today()
    start = start_date or (end - timedelta(days=30))

    bq_table = os.getenv("GCP_BILLING_BQ_TABLE")
    if not bq_table:
        return [{"error": "GCP_BILLING_BQ_TABLE not configured (needed for per-service detail)"}]

    client = bigquery.Client()
    query = f"""
        SELECT
            service.description AS service,
            SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS net_cost
        FROM `{bq_table}`
        WHERE DATE(usage_start_time) BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'
        GROUP BY service
        HAVING net_cost > 0.001
        ORDER BY net_cost DESC
    """
    results = []
    for row in client.query(query).result():
        results.append({"service": row["service"], "cost_usd": round(float(row["net_cost"]), 4), "provider": "gcp"})
    return results


def get_gcp_service_cost(
    service_name: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    """Return daily cost breakdown for any GCP service by name."""
    from google.cloud import bigquery

    end = end_date or date.today()
    start = start_date or (end - timedelta(days=30))

    bq_table = os.getenv("GCP_BILLING_BQ_TABLE")
    if not bq_table:
        return {"error": "GCP_BILLING_BQ_TABLE not configured"}

    # Resolve via listing
    all_services = list_gcp_services(start, end)
    known = [s["service"] for s in all_services if "service" in s]
    lower = service_name.lower()
    resolved = next((s for s in known if s.lower() == lower), None)
    if not resolved:
        resolved = next((s for s in known if lower in s.lower()), None)
    if not resolved:
        suggestions = [s for s in known if any(w in s.lower() for w in lower.split())][:5]
        return {
            "error": f"No GCP service matching '{service_name}' found.",
            "suggestions": suggestions,
            "all_active_services": known,
        }

    safe_service = resolved.replace("'", "\\'")
    client = bigquery.Client()
    query = f"""
        SELECT
            DATE(usage_start_time) AS day,
            sku.description AS sku,
            SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS net_cost
        FROM `{bq_table}`
        WHERE DATE(usage_start_time) BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'
          AND service.description = '{safe_service}'
        GROUP BY day, sku
        ORDER BY day, net_cost DESC
    """

    daily: dict[str, float] = {}
    by_sku: dict[str, float] = {}
    for row in client.query(query).result():
        d = str(row["day"])
        amount = float(row["net_cost"])
        sku = row["sku"]
        daily[d] = daily.get(d, 0.0) + amount
        by_sku[sku] = by_sku.get(sku, 0.0) + amount

    total = sum(daily.values())
    top_skus = sorted(by_sku.items(), key=lambda x: x[1], reverse=True)

    return {
        "service": resolved,
        "provider": "gcp",
        "period": f"{start.isoformat()} to {end.isoformat()}",
        "total_usd": round(total, 2),
        "daily": [{"date": d, "cost_usd": round(v, 4)} for d, v in sorted(daily.items())],
        "by_sku": {k: round(v, 4) for k, v in top_skus[:20]},
    }


# ── Unified entry points ──────────────────────────────────────────────────────

def list_all_services(
    provider: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    """
    List every service with spend across one or all providers.
    Provider: "aws" | "azure" | "gcp" | None (all).
    """
    out: dict[str, list] = {}
    errors: list[str] = []

    targets = [provider] if provider else ["aws", "azure", "gcp"]

    for p in targets:
        try:
            if p == "aws":
                out["aws"] = list_aws_services(start_date, end_date)
            elif p == "azure":
                out["azure"] = list_azure_services(start_date, end_date)
            elif p == "gcp":
                out["gcp"] = list_gcp_services(start_date, end_date)
        except Exception as e:
            errors.append(f"{p}: {e}")

    if errors:
        out["errors"] = errors  # type: ignore[assignment]
    return out


def get_any_service_cost(
    service_name: str,
    provider: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    granularity: str = "DAILY",
) -> dict:
    """
    Get cost for any named service on any provider.
    If provider is not specified, tries all three and returns the first match.
    """
    end = end_date or date.today()
    start = start_date or (end - timedelta(days=30))

    # If provider is given, route directly
    if provider:
        p = provider.lower()
        if p == "aws":
            return get_aws_service_cost(service_name, start, end, granularity)
        elif p == "azure":
            return get_azure_service_cost(service_name, start, end)
        elif p == "gcp":
            return get_gcp_service_cost(service_name, start, end)
        return {"error": f"Unknown provider '{provider}'. Use aws, azure, or gcp."}

    # Try all providers, return first successful non-error result
    results = {}
    for p, fn in [("aws", get_aws_service_cost), ("azure", get_azure_service_cost), ("gcp", get_gcp_service_cost)]:
        try:
            r = fn(service_name, start, end)  # type: ignore[call-arg]
            if "error" not in r:
                results[p] = r
        except Exception:
            pass

    if not results:
        return {
            "error": f"No service matching '{service_name}' found on any connected provider.",
            "tip": "Use list_active_services to see what's available.",
        }

    if len(results) == 1:
        return list(results.values())[0]

    # Found on multiple providers - return all
    total = sum(r.get("total_usd", 0) for r in results.values())
    return {
        "service_name": service_name,
        "matched_on": list(results.keys()),
        "total_usd": round(total, 2),
        "by_provider": results,
    }
