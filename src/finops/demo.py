"""
Demo mode data for nable finops-mcp.

When FINOPS_DEMO_MODE=1 is set, cost tools return realistic sample data
instead of querying real cloud providers. Useful for screenshots, demos,
and evaluating the product without live credentials.

The sample data represents a typical Series A SaaS startup (~$8,400/mo AWS spend).
"""
from __future__ import annotations

from datetime import date, timedelta


def is_demo() -> bool:
    import os
    return os.environ.get("FINOPS_DEMO_MODE", "").strip() in ("1", "true", "yes")


def demo_cost_summary() -> dict:
    today = date.today()
    start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    end = today.replace(day=1) - timedelta(days=1)

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "grand_total_usd": 8421.63,
        "grand_total_formatted": "$8,421.63",
        "note": "Demo mode: sample data for a typical Series A SaaS startup.",
        "by_provider": {
            "aws": {
                "total_usd": 7284.17,
                "total_formatted": "$7,284.17",
                "by_service": {
                    "Amazon Elastic Compute Cloud": 3184.52,
                    "Amazon Elastic Kubernetes Service": 1821.34,
                    "Amazon Relational Database Service": 1243.80,
                    "Amazon ElastiCache": 612.40,
                    "Amazon Simple Storage Service": 184.22,
                    "Amazon CloudFront": 97.43,
                    "AWS Lambda": 48.91,
                    "Amazon Route 53": 42.10,
                    "AWS Data Transfer": 38.72,
                    "Amazon CloudWatch": 10.73,
                },
                "by_region": {
                    "us-east-1": 5841.20,
                    "us-west-2": 1124.63,
                    "eu-west-1": 318.34,
                },
            },
            "datadog": {
                "total_usd": 847.20,
                "total_formatted": "$847.20",
                "by_service": {
                    "Infrastructure Hosts": 540.00,
                    "Log Management": 198.40,
                    "APM": 108.80,
                },
            },
            "snowflake": {
                "total_usd": 290.26,
                "total_formatted": "$290.26",
                "by_service": {
                    "Compute Credits": 214.80,
                    "Storage": 75.46,
                },
            },
        },
        "grand_by_service": {
            "Amazon Elastic Compute Cloud": 3184.52,
            "Amazon Elastic Kubernetes Service": 1821.34,
            "Amazon Relational Database Service": 1243.80,
            "Infrastructure Hosts (Datadog)": 540.00,
            "Amazon ElastiCache": 612.40,
            "Log Management (Datadog)": 198.40,
            "Compute Credits (Snowflake)": 214.80,
            "Amazon Simple Storage Service": 184.22,
            "Amazon CloudFront": 97.43,
            "APM (Datadog)": 108.80,
            "AWS Lambda": 48.91,
        },
    }


def demo_cost_trends() -> dict:
    today = date.today()
    months = []
    totals = [5820.14, 6241.88, 6890.42, 7102.55, 7844.91, 8421.63]
    for i in range(5, -1, -1):
        d = (today.replace(day=1) - timedelta(days=i * 28))
        months.append(d.strftime("%Y-%m"))

    return {
        "note": "Demo mode: sample data for a typical Series A SaaS startup.",
        "trend": [
            {"month": m, "total_usd": t, "formatted": f"${t:,.2f}"}
            for m, t in zip(months, totals)
        ],
        "mom_change_pct": 7.4,
        "mom_change_usd": 576.72,
        "insight": (
            "Spend is up 7.4% month-over-month (+$576.72). "
            "EC2 and EKS account for 68% of total spend. "
            "Datadog costs have grown 22% over the last 3 months as the team scales."
        ),
    }


def demo_anomalies() -> dict:
    return {
        "note": "Demo mode: sample anomaly data.",
        "anomalies": [
            {
                "provider": "aws",
                "service": "Amazon Elastic Compute Cloud",
                "description": "EC2 spend spiked 34% on the 14th. A fleet of r6i.2xlarge instances was launched for a load test and not terminated. Estimated waste: $284/day.",
                "severity": "high",
                "detected_at": (date.today() - timedelta(days=3)).isoformat(),
                "estimated_waste_usd": 852.00,
            },
            {
                "provider": "datadog",
                "service": "Log Management",
                "description": "Log ingestion volume increased 3x after a debug logging flag was left enabled in production. Current run rate: $198/mo vs $66/mo baseline.",
                "severity": "medium",
                "detected_at": (date.today() - timedelta(days=7)).isoformat(),
                "estimated_waste_usd": 132.00,
            },
            {
                "provider": "aws",
                "service": "Amazon Relational Database Service",
                "description": "Two db.r6g.xlarge RDS instances are running at under 8% CPU utilization. Rightsizing to db.r6g.large would save ~$310/mo.",
                "severity": "low",
                "detected_at": (date.today() - timedelta(days=12)).isoformat(),
                "estimated_waste_usd": 310.00,
            },
        ],
        "total_estimated_waste_usd": 1294.00,
        "total_estimated_waste_formatted": "$1,294.00",
    }


def demo_rightsizing() -> dict:
    return {
        "note": "Demo mode: sample rightsizing recommendations.",
        "recommendations": [
            {
                "provider": "aws",
                "resource_type": "EC2 Instance",
                "resource_id": "i-0a1b2c3d4e5f6a7b8",
                "current_type": "m5.2xlarge",
                "recommended_type": "m5.xlarge",
                "current_cost_monthly": 276.48,
                "projected_cost_monthly": 138.24,
                "savings_monthly": 138.24,
                "avg_cpu_utilization_pct": 12.4,
                "avg_memory_utilization_pct": 18.7,
                "reason": "CPU and memory consistently under 20% over the last 30 days.",
            },
            {
                "provider": "aws",
                "resource_type": "RDS Instance",
                "resource_id": "db-prod-analytics",
                "current_type": "db.r6g.xlarge",
                "recommended_type": "db.r6g.large",
                "current_cost_monthly": 524.16,
                "projected_cost_monthly": 262.08,
                "savings_monthly": 262.08,
                "avg_cpu_utilization_pct": 7.8,
                "reason": "Read-heavy workload with low CPU. Smaller instance with a read replica would handle the load at half the cost.",
            },
            {
                "provider": "aws",
                "resource_type": "ElastiCache Node",
                "resource_id": "cache-prod-sessions",
                "current_type": "cache.r6g.large",
                "recommended_type": "cache.r6g.medium",
                "current_cost_monthly": 122.40,
                "projected_cost_monthly": 61.20,
                "savings_monthly": 61.20,
                "avg_memory_utilization_pct": 24.1,
                "reason": "Cache hit rate is 98.2% and memory usage is well under 30%.",
            },
        ],
        "total_monthly_savings": 461.52,
        "total_monthly_savings_formatted": "$461.52",
        "total_annual_savings_formatted": "$5,538.24",
    }
