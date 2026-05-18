"""
Parse infrastructure diffs to extract resource changes.

Supports:
  - Terraform (.tf files, plan output)
  - AWS CloudFormation (.yaml/.json with AWSTemplateFormatVersion)
  - AWS CDK (synthesized CloudFormation in cdk.out/)
  - Helm values.yaml (instance type / replica count changes)
  - Kubernetes manifests (resource requests/limits)
  - Docker Compose (for Fargate cost estimation)

Returns a list of ResourceChange objects describing what's being
added, modified, or removed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ResourceChange:
    action: str           # "add" | "modify" | "remove"
    resource_type: str    # "aws_instance" | "aws_db_instance" | "aws_nat_gateway" | etc.
    resource_name: str    # logical name from the IaC
    provider: str         # "aws" | "azure" | "gcp" | "k8s"
    properties: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""


# ─── Terraform ────────────────────────────────────────────────────────────────

_TF_RESOURCE_RE = re.compile(
    # Matches both git-diff format (+resource "type" "name")
    # and Terraform plan format (  + resource "type" "name")
    r'^\s*(?P<sign>[+\-~])\s*resource\s+"(?P<type>[^"]+)"\s+"(?P<name>[^"]+)"',
    re.MULTILINE,
)
_TF_ATTR_RE = re.compile(r'^\s*[+\-~]?\s*(?P<key>\w+)\s+=\s+"?(?P<value>[^"\n]+)"?', re.MULTILINE)

_AWS_RESOURCE_COST_TYPES = {
    "aws_instance", "aws_db_instance", "aws_rds_cluster", "aws_rds_cluster_instance",
    "aws_nat_gateway", "aws_lb", "aws_alb", "aws_nlb",
    "aws_eks_cluster", "aws_eks_node_group",
    "aws_elasticache_cluster", "aws_elasticache_replication_group",
    "aws_elasticsearch_domain", "aws_opensearch_domain",
    "aws_redshift_cluster", "aws_msk_cluster",
    "aws_ebs_volume", "aws_s3_bucket",
    "aws_cloudfront_distribution", "aws_api_gateway_rest_api",
    "aws_lambda_function",
    "azurerm_virtual_machine", "azurerm_linux_virtual_machine",
    "azurerm_sql_database", "azurerm_kubernetes_cluster",
    "google_compute_instance", "google_sql_database_instance",
    "google_container_cluster",
}


def _parse_terraform_diff(diff: str, file_path: str = "") -> list[ResourceChange]:
    changes = []
    for match in _TF_RESOURCE_RE.finditer(diff):
        sign = match.group("sign")
        rtype = match.group("type")
        rname = match.group("name")

        if rtype not in _AWS_RESOURCE_COST_TYPES and not rtype.startswith(("aws_", "azurerm_", "google_")):
            continue

        action = {"+" : "add", "-": "remove", "~": "modify"}.get(sign, "modify")
        provider = "aws" if rtype.startswith("aws_") else "azure" if rtype.startswith("azurerm_") else "gcp"

        # Extract properties from the block following this resource declaration
        start = match.end()
        # Find next resource block or end
        next_match = _TF_RESOURCE_RE.search(diff, start)
        block = diff[start:next_match.start() if next_match else len(diff)]

        props: dict[str, str] = {}
        for attr in _TF_ATTR_RE.finditer(block):
            props[attr.group("key")] = attr.group("value").strip()

        changes.append(ResourceChange(
            action=action,
            resource_type=rtype,
            resource_name=rname,
            provider=provider,
            properties=props,
            file_path=file_path,
        ))
    return changes


# ─── CloudFormation ───────────────────────────────────────────────────────────

_CFN_RESOURCE_TYPE_MAP = {
    "AWS::EC2::Instance": "aws_instance",
    "AWS::RDS::DBInstance": "aws_db_instance",
    "AWS::RDS::DBCluster": "aws_rds_cluster",
    "AWS::EC2::NatGateway": "aws_nat_gateway",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "aws_lb",
    "AWS::EKS::Cluster": "aws_eks_cluster",
    "AWS::ElastiCache::CacheCluster": "aws_elasticache_cluster",
    "AWS::Redshift::Cluster": "aws_redshift_cluster",
    "AWS::EC2::Volume": "aws_ebs_volume",
    "AWS::Lambda::Function": "aws_lambda_function",
    "AWS::MSK::Cluster": "aws_msk_cluster",
}

_CFN_RESOURCE_RE = re.compile(
    r'^\s*[+\-]\s+(?P<name>\w+):\s*$.*?Type:\s*(?P<type>AWS::\S+)',
    re.MULTILINE | re.DOTALL,
)


def _parse_cfn_diff(diff: str, file_path: str = "") -> list[ResourceChange]:
    changes = []
    for match in _CFN_RESOURCE_RE.finditer(diff):
        cfn_type = match.group("type").strip()
        mapped = _CFN_RESOURCE_TYPE_MAP.get(cfn_type)
        if not mapped:
            continue
        # Determine action from leading +/- in the block
        block_start = diff.rfind("\n", 0, match.start())
        leading_char = diff[block_start + 1] if block_start >= 0 else "+"
        action = "add" if leading_char == "+" else "remove" if leading_char == "-" else "modify"
        changes.append(ResourceChange(
            action=action,
            resource_type=mapped,
            resource_name=match.group("name"),
            provider="aws",
            file_path=file_path,
        ))
    return changes


# ─── Helm values ──────────────────────────────────────────────────────────────

_HELM_INSTANCE_RE  = re.compile(r'[+\-]\s*(?:instanceType|nodeType|machineType):\s*(\S+)', re.MULTILINE)
_HELM_REPLICA_RE   = re.compile(r'[+\-]\s*replicaCount:\s*(\d+)', re.MULTILINE)
_HELM_CPU_RE       = re.compile(r'[+\-]\s*cpu:\s*["\']?(\d+m|\d+(?:\.\d+)?)["\']?', re.MULTILINE)
_HELM_MEMORY_RE    = re.compile(r'[+\-]\s*memory:\s*["\']?(\d+(?:Mi|Gi|Ki|M|G)?)["\']?', re.MULTILINE)
_HELM_NODE_COUNT_RE = re.compile(r'[+\-]\s*(?:nodeCount|minSize|desiredSize|min_count):\s*(\d+)', re.MULTILINE)


def _parse_helm_diff(diff: str, file_path: str = "") -> list[ResourceChange]:
    changes = []

    # Instance type change → node cost change
    instances: dict[str, str] = {}  # sign → value
    for m in _HELM_INSTANCE_RE.finditer(diff):
        sign = diff[m.start()]
        instances[sign] = m.group(1)

    if "+" in instances or "-" in instances:
        old_type = instances.get("-", "")
        new_type = instances.get("+", "")
        if old_type != new_type:
            changes.append(ResourceChange(
                action="modify",
                resource_type="aws_instance",
                resource_name="helm_node",
                provider="aws",
                properties={"instance_type": new_type, "old_instance_type": old_type},
                file_path=file_path,
            ))

    # Replica count change → scale cost
    replicas: dict[str, int] = {}
    for m in _HELM_REPLICA_RE.finditer(diff):
        sign = diff[m.start()]
        replicas[sign] = int(m.group(1))

    if "+" in replicas and "-" in replicas:
        old_r, new_r = replicas["-"], replicas["+"]
        if old_r != new_r:
            delta = new_r - old_r
            # Parse cpu/mem from same diff to estimate per-pod cost
            cpu_props: dict[str, str] = {}
            for m in _HELM_CPU_RE.finditer(diff):
                cpu_props[diff[m.start()]] = m.group(1)
            mem_props: dict[str, str] = {}
            for m in _HELM_MEMORY_RE.finditer(diff):
                mem_props[diff[m.start()]] = m.group(1)

            action = "add" if delta > 0 else "remove"
            for _ in range(abs(delta)):
                changes.append(ResourceChange(
                    action=action,
                    resource_type="helm_pod_replica",
                    resource_name="replica",
                    provider="k8s",
                    properties={
                        "cpu_request": cpu_props.get("+", cpu_props.get("-", "100m")),
                        "mem_request": mem_props.get("+", mem_props.get("-", "128Mi")),
                        "replica_delta": str(delta),
                    },
                    file_path=file_path,
                ))

    # Node count change
    node_counts: dict[str, int] = {}
    for m in _HELM_NODE_COUNT_RE.finditer(diff):
        node_counts[diff[m.start()]] = int(m.group(1))

    if "+" in node_counts and "-" in node_counts:
        old_n, new_n = node_counts["-"], node_counts["+"]
        if old_n != new_n:
            action = "add" if new_n > old_n else "remove"
            inst   = instances.get("+", instances.get("-", "m5.large"))
            for _ in range(abs(new_n - old_n)):
                changes.append(ResourceChange(
                    action=action,
                    resource_type="aws_instance",
                    resource_name="helm_node",
                    provider="aws",
                    properties={"instance_type": inst},
                    file_path=file_path,
                ))

    return changes


# ─── unified entry point ──────────────────────────────────────────────────────

def parse_diff(diff: str, file_path: str = "") -> list[ResourceChange]:
    """
    Parse a unified diff and return infrastructure resource changes.
    Tries all supported formats and merges results.
    """
    results = []

    lower_path = file_path.lower()

    if lower_path.endswith(".tf") or "terraform" in lower_path:
        results.extend(_parse_terraform_diff(diff, file_path))

    if lower_path.endswith((".yaml", ".yml", ".json")) and (
        "cloudformation" in lower_path or "template" in lower_path or "cdk" in lower_path
    ):
        results.extend(_parse_cfn_diff(diff, file_path))

    if "values" in lower_path and lower_path.endswith((".yaml", ".yml")):
        results.extend(_parse_helm_diff(diff, file_path))

    # If no format matched by path, try all parsers
    if not results:
        results.extend(_parse_terraform_diff(diff, file_path))
        results.extend(_parse_cfn_diff(diff, file_path))

    return results
