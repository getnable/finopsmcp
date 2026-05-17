"""
Thin Python mirror of vscode-extension/src/prices.ts.

Used by the GitHub App diff analyser and any server-side code that needs
the same pricing logic as the VS Code extension — without importing the
heavier terraform_estimate module.
"""
from __future__ import annotations
from typing import Any

from .connectors.terraform_estimate import (
    _EC2_HOURLY, _RDS_HOURLY, _ELASTICACHE_HOURLY,
    _EBS_PER_GB_MONTH, _OPENSEARCH_HOURLY, _REDSHIFT_HOURLY, _MSK_BROKER_HOURLY,
    HOURS_PER_MONTH,
)


def price_resource_py(
    resource_type: str,
    attrs: dict[str, str],
) -> dict[str, Any] | None:
    """
    Return {"monthly": float, "detail": str, "note": str|None} or None.
    Mirrors priceResource() in prices.ts exactly.
    """
    def _f(v: str | None, default: float = 0.0) -> float:
        try:
            return float(v) if v is not None else default
        except (ValueError, TypeError):
            return default

    def _i(v: str | None, default: int = 1) -> int:
        try:
            return int(v) if v is not None else default
        except (ValueError, TypeError):
            return default

    def _b(v: str | None) -> bool:
        return str(v).lower() == "true"

    t = resource_type

    if t == "aws_instance":
        it = attrs.get("instance_type", "")
        h  = _EC2_HOURLY.get(it)
        if not h:
            return {"monthly": 0.0, "detail": f"Unknown instance type: {it}"}
        return {"monthly": round(h * HOURS_PER_MONTH, 2), "detail": f"{it} on-demand"}

    if t in ("aws_db_instance", "aws_rds_cluster_instance"):
        cls = attrs.get("instance_class", "")
        h   = _RDS_HOURLY.get(cls, 0.0)
        maz = _b(attrs.get("multi_az"))
        if maz: h *= 2
        note = "Multi-AZ doubles cost — wasteful in dev/staging" if maz else None
        return {"monthly": round(h * HOURS_PER_MONTH, 2), "detail": f"{cls}{'  Multi-AZ' if maz else ''}", "note": note}

    if t in ("aws_elasticache_cluster", "aws_elasticache_replication_group"):
        node  = attrs.get("node_type", "")
        count = _i(attrs.get("num_cache_nodes") or attrs.get("number_cache_clusters"), 1)
        h     = _ELASTICACHE_HOURLY.get(node, 0.0)
        return {"monthly": round(h * count * HOURS_PER_MONTH, 2), "detail": f"{count}× {node}"}

    if t == "aws_ebs_volume":
        vtype = attrs.get("type", "gp2")
        size  = _f(attrs.get("size"))
        price = _EBS_PER_GB_MONTH.get(vtype, 0.10)
        iops  = _f(attrs.get("iops"))
        m     = size * price
        if vtype in ("io1", "io2") and iops:
            m += iops * 0.065
        note = "Switch to gp3 to save 20% with same/better IOPS" if vtype == "gp2" else None
        return {"monthly": round(m, 2), "detail": f"{size:.0f} GB {vtype}", "note": note}

    if t == "aws_nat_gateway":
        return {"monthly": round(0.045 * HOURS_PER_MONTH, 2), "detail": "$0.045/hr base",
                "note": "Add VPC endpoints for S3/DynamoDB to cut data transfer charges"}

    if t in ("aws_lb", "aws_alb"):
        return {"monthly": round(0.008 * HOURS_PER_MONTH, 2), "detail": "$0.008/hr + LCU"}

    if t == "aws_elb":
        return {"monthly": round(0.025 * HOURS_PER_MONTH, 2), "detail": "$0.025/hr classic ELB"}

    if t == "aws_eks_cluster":
        return {"monthly": round(0.10 * HOURS_PER_MONTH, 2), "detail": "$0.10/hr control plane",
                "note": "Node group costs are EC2 instances billed separately"}

    if t == "aws_lambda_function":
        mem = _i(attrs.get("memory_size"), 128)
        return {"monthly": 0.0, "detail": f"{mem} MB — pay per invocation"}

    if t in ("aws_opensearch_domain", "aws_elasticsearch_domain"):
        inst  = attrs.get("instance_type", "m5.large.search")
        count = _i(attrs.get("instance_count"), 1)
        h     = _OPENSEARCH_HOURLY.get(inst, 0.142)
        return {"monthly": round(h * count * HOURS_PER_MONTH, 2), "detail": f"{count}× {inst}"}

    if t == "aws_redshift_cluster":
        node  = attrs.get("node_type", "dc2.large")
        count = _i(attrs.get("number_of_nodes"), 1)
        h     = _REDSHIFT_HOURLY.get(node, 0.25)
        return {"monthly": round(h * count * HOURS_PER_MONTH, 2), "detail": f"{count}× {node}"}

    if t == "aws_msk_cluster":
        broker = attrs.get("instance_type", "kafka.m5.large")
        count  = _i(attrs.get("number_of_broker_nodes"), 3)
        h      = _MSK_BROKER_HOURLY.get(broker, 0.142)
        return {"monthly": round(h * count * HOURS_PER_MONTH, 2), "detail": f"{count}× {broker}"}

    if t == "aws_kinesis_stream":
        shards = _i(attrs.get("shard_count"), 1)
        m = shards * 0.015 * 24 * 30
        return {"monthly": round(m, 2), "detail": f"{shards} shard(s) @ $0.015/shard-hr"}

    if t == "aws_cloudwatch_metric_alarm":
        return {"monthly": 0.10, "detail": "$0.10/alarm-month"}

    if t == "aws_s3_bucket":
        return {"monthly": 0.0, "detail": "Pay per GB stored / requests"}

    if t in ("aws_ecs_service", "aws_ecs_task_definition"):
        cpu = _f(attrs.get("cpu"), 256) / 1024
        mem = _f(attrs.get("memory"), 512) / 1024
        h   = cpu * 0.04048 + mem * 0.004445
        return {"monthly": round(h * HOURS_PER_MONTH, 2), "detail": f"{cpu:.2f} vCPU {mem:.2f} GB Fargate"}

    if t in ("aws_dynamodb_table",):
        return {"monthly": 0.0, "detail": "Pay-per-request or provisioned capacity"}

    if t in ("aws_cloudfront_distribution",):
        return {"monthly": 0.0, "detail": "Pay per request + transfer"}

    if t in ("aws_api_gateway_rest_api", "aws_apigatewayv2_api"):
        return {"monthly": 0.0, "detail": "~$3.50/M API calls"}

    return None
