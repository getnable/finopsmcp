"""
Amazon DocumentDB cost analyzer.

Pulls DocumentDB costs from Cost Explorer, breaks down by cluster,
and fetches CloudWatch utilization metrics to surface rightsizing opportunities.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any


def _make_ce(role_arn: str | None = None):
    import boto3

    if role_arn:
        sts = boto3.client("sts")
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-docdb")["Credentials"]
        return boto3.client(
            "ce",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name="us-east-1",
        )
    return boto3.client("ce", region_name="us-east-1")


def _make_session(region: str, role_arn: str | None = None):
    import boto3

    if role_arn:
        sts = boto3.client("sts")
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-docdb")["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
    return boto3.Session(region_name=region)


# Approximate per-hour on-demand prices for common DocumentDB instance classes
_INSTANCE_HOURLY_USD: dict[str, float] = {
    "db.t3.medium": 0.076,
    "db.r5.large": 0.277,
    "db.r5.xlarge": 0.554,
    "db.r5.2xlarge": 1.109,
    "db.r5.4xlarge": 2.218,
    "db.r5.8xlarge": 4.436,
    "db.r5.12xlarge": 6.654,
    "db.r5.16xlarge": 8.872,
    "db.r5.24xlarge": 13.308,
    "db.r6g.large": 0.249,
    "db.r6g.xlarge": 0.498,
    "db.r6g.2xlarge": 0.997,
    "db.r6g.4xlarge": 1.993,
    "db.r6g.8xlarge": 3.986,
    "db.r6g.12xlarge": 5.980,
    "db.r6g.16xlarge": 7.973,
}

# Class size ordering for downsize recommendations
_CLASS_ORDER = [
    "db.t3.medium",
    "db.r6g.large", "db.r6g.xlarge", "db.r6g.2xlarge",
    "db.r6g.4xlarge", "db.r6g.8xlarge", "db.r6g.12xlarge", "db.r6g.16xlarge",
    "db.r5.large", "db.r5.xlarge", "db.r5.2xlarge",
    "db.r5.4xlarge", "db.r5.8xlarge", "db.r5.12xlarge",
    "db.r5.16xlarge", "db.r5.24xlarge",
]


def _get_avg_metric(cw, cluster_id: str, instance_id: str, metric: str, days: int) -> float | None:
    """Return average CloudWatch metric value over the last N days, or None on error."""
    try:
        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=days)
        resp = cw.get_metric_statistics(
            Namespace="AWS/DocDB",
            MetricName=metric,
            Dimensions=[
                {"Name": "DBClusterIdentifier", "Value": cluster_id},
                {"Name": "DBInstanceIdentifier", "Value": instance_id},
            ],
            StartTime=start,
            EndTime=end,
            Period=86400,  # daily
            Statistics=["Average"],
        )
        points = resp.get("Datapoints", [])
        if not points:
            return None
        return sum(p["Average"] for p in points) / len(points)
    except Exception:
        return None


def _downsize_recommendation(instance_class: str, cpu_avg: float) -> tuple[str | None, float]:
    """
    Returns (recommended_class, estimated_monthly_savings) based on avg CPU.
    Returns (None, 0.0) if no downsize is warranted.
    """
    if cpu_avg >= 20.0:
        return None, 0.0

    steps = 2 if cpu_avg < 10.0 else 1
    current_hourly = _INSTANCE_HOURLY_USD.get(instance_class, 0.0)

    if instance_class in _CLASS_ORDER:
        idx = _CLASS_ORDER.index(instance_class)
        target_idx = max(0, idx - steps)
        target_class = _CLASS_ORDER[target_idx]
        if target_class == instance_class:
            return None, 0.0
        target_hourly = _INSTANCE_HOURLY_USD.get(target_class, 0.0)
        savings = (current_hourly - target_hourly) * 730  # 730h/month
        return target_class, max(savings, 0.0)

    return None, 0.0


class DocumentDBAnalyzer:
    def __init__(self, region: str = "us-east-1", role_arn: str | None = None) -> None:
        self.region = region
        self.role_arn = role_arn

    def get_costs(self, days: int = 30) -> str:
        end = date.today()
        start = end - timedelta(days=days)

        ce = _make_ce(self.role_arn)

        # Total DocumentDB cost, grouped by usage type
        results: list[dict] = []
        kwargs: dict[str, Any] = dict(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["Amazon DocumentDB"]}},
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
            return "No Amazon DocumentDB spend found in the selected period."

        # Separate compute vs storage costs by usage type keyword
        compute_cost = sum(v for k, v in usage_costs.items() if "InstanceUsage" in k or "instance" in k.lower())
        storage_cost = sum(v for k, v in usage_costs.items() if "StorageUsage" in k or "storage" in k.lower() or "io" in k.lower())
        other_cost = total - compute_cost - storage_cost

        lines: list[str] = [
            f"Amazon DocumentDB costs (last {days} days): ${total:,.2f}",
            "",
            "Cost breakdown:",
            f"  Compute:  ${compute_cost:>8,.2f}",
            f"  Storage:  ${storage_cost:>8,.2f}",
        ]
        if other_cost > 0.01:
            lines.append(f"  Other:    ${other_cost:>8,.2f}")

        # Pull cluster-level data and CloudWatch metrics
        session = _make_session(self.region, self.role_arn)
        docdb = session.client("docdb")
        cw = session.client("cloudwatch")

        try:
            clusters = docdb.describe_db_clusters()["DBClusters"]
        except Exception as exc:
            lines += ["", f"Could not retrieve cluster details: {exc}"]
            return "\n".join(lines)

        if not clusters:
            return "\n".join(lines)

        lines += ["", "Clusters:"]

        any_rightsizing = False
        rightsizing_lines: list[str] = []

        for cluster in clusters:
            cluster_id = cluster.get("DBClusterIdentifier", "unknown")
            engine_version = cluster.get("EngineVersion", "")
            status = cluster.get("Status", "unknown")
            members = cluster.get("DBClusterMembers", [])

            lines.append(f"\n  {cluster_id}  (status: {status}, engine: {engine_version})")
            lines.append(f"  Instances: {len(members)}")

            for member in members:
                instance_id = member.get("DBInstanceIdentifier", "")
                is_writer = member.get("IsClusterWriter", False)
                role = "writer" if is_writer else "reader"

                # Fetch instance class from describe_db_instances
                instance_class = ""
                try:
                    inst_resp = docdb.describe_db_instances(DBInstanceIdentifier=instance_id)
                    inst_detail = inst_resp["DBInstances"][0]
                    instance_class = inst_detail.get("DBInstanceClass", "")
                except Exception:
                    pass

                # CloudWatch metrics
                cpu_avg = _get_avg_metric(cw, cluster_id, instance_id, "CPUUtilization", 14)
                connections_avg = _get_avg_metric(cw, cluster_id, instance_id, "DatabaseConnections", 14)

                cpu_str = f"{cpu_avg:.1f}%" if cpu_avg is not None else "n/a"
                conn_str = f"{connections_avg:.0f}" if connections_avg is not None else "n/a"

                inst_line = f"    {instance_id}  [{role}]"
                if instance_class:
                    inst_line += f"  {instance_class}"
                inst_line += f"  CPU avg: {cpu_str}  Connections avg: {conn_str}"
                lines.append(inst_line)

                # Rightsizing check
                if cpu_avg is not None and instance_class:
                    rec_class, savings = _downsize_recommendation(instance_class, cpu_avg)
                    if rec_class:
                        any_rightsizing = True
                        rightsizing_lines.append(
                            f"  {instance_id}: avg CPU {cpu_avg:.1f}% over 14 days. "
                            f"Recommend downsizing from {instance_class} to {rec_class}. "
                            f"Estimated savings: ${savings:,.0f}/mo."
                        )

        if any_rightsizing:
            lines += ["", "Rightsizing recommendations:"]
            lines.extend(rightsizing_lines)
        else:
            lines += ["", "No rightsizing recommendations based on current CPU utilization."]

        return "\n".join(lines)
