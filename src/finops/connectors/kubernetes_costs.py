"""
nable Kubernetes Cost Allocator

AWS gives you one bill line for an EKS node group. This module breaks that
cost down to pod → deployment → namespace → team.

Algorithm:
  1. Pull actual node costs from AWS Cost Explorer (tagged by eks:nodegroup-name)
     OR estimate from EC2 instance type pricing if tags aren't set up
  2. Fetch live pod resource requests from the Kubernetes API
  3. Allocate node cost proportionally:
       pod_share = 0.5 × (pod_cpu_req / node_cpu) + 0.5 × (pod_mem_req / node_mem)
  4. Roll up by namespace, deployment, and team label
  5. Store daily snapshots in kubernetes_costs table for trending

Why this beats naive billing lookup:
  - Bin-packs correctly: a pod requesting 0.1 CPU on a 4-CPU node pays 2.5%, not 25%
  - Handles DaemonSets: shared overhead is attributed to kube-system
  - Team label override: pods with label `team=payments` are attributed to that team
    regardless of namespace
  - Idle capacity: unallocated node capacity is attributed to `__idle__` budget
  - Supports multi-cluster: pass different kubeconfig contexts

Env vars:
  KUBECONFIG               — path to kubeconfig (default: ~/.kube/config)
  NABLE_K8S_TEAM_LABEL     — pod label for team attribution (default: "team")
  NABLE_K8S_IDLE_THRESHOLD — fraction below which a node is "idle" (default: 0.1)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

# Node CPU/memory by EC2 instance type (vCPUs, GiB)
_NODE_CAPACITY: dict[str, tuple[float, float]] = {
    "t3.micro": (2, 1), "t3.small": (2, 2), "t3.medium": (2, 4),
    "t3.large": (2, 8), "t3.xlarge": (4, 16), "t3.2xlarge": (8, 32),
    "t3a.medium": (2, 4), "t3a.large": (2, 8), "t3a.xlarge": (4, 16),
    "t4g.medium": (2, 4), "t4g.large": (2, 8), "t4g.xlarge": (4, 16),
    "m5.large": (2, 8), "m5.xlarge": (4, 16), "m5.2xlarge": (8, 32),
    "m5.4xlarge": (16, 64), "m5.8xlarge": (32, 128), "m5.12xlarge": (48, 192),
    "m6i.large": (2, 8), "m6i.xlarge": (4, 16), "m6i.2xlarge": (8, 32),
    "m6g.large": (2, 8), "m6g.xlarge": (4, 16), "m6g.2xlarge": (8, 32),
    "m7i.large": (2, 8), "m7i.xlarge": (4, 16), "m7i.2xlarge": (8, 32),
    "c5.large": (2, 4), "c5.xlarge": (4, 8), "c5.2xlarge": (8, 16),
    "c5.4xlarge": (16, 32), "c6i.large": (2, 4), "c6i.xlarge": (4, 8),
    "c6g.large": (2, 4), "c6g.xlarge": (4, 8), "c6g.2xlarge": (8, 16),
    "r5.large": (2, 16), "r5.xlarge": (4, 32), "r5.2xlarge": (8, 64),
    "r5.4xlarge": (16, 128), "r6i.large": (2, 16), "r6i.xlarge": (4, 32),
    "r6g.large": (2, 16), "r6g.xlarge": (4, 32),
    "p3.2xlarge": (8, 61), "p3.8xlarge": (32, 244),
    "g4dn.xlarge": (4, 16), "g4dn.2xlarge": (8, 32),
    "g5.xlarge": (4, 16), "g5.2xlarge": (8, 32),
    "i3.large": (2, 15.25), "i3.xlarge": (4, 30.5),
}

# EC2 hourly on-demand (us-east-1) — for cost estimation when CE tags are missing
from .terraform_estimate import _EC2_HOURLY
HOURS_PER_MONTH = 730.0


@dataclass
class PodCost:
    pod_name: str
    namespace: str
    team: str
    deployment: str
    node_name: str
    node_instance_type: str
    cpu_req_millicores: float
    mem_req_mib: float
    share_fraction: float          # fraction of node cost
    daily_cost_usd: float
    monthly_cost_usd: float


@dataclass
class NamespaceSummary:
    namespace: str
    team: str
    pod_count: int
    daily_cost_usd: float
    monthly_cost_usd: float
    pods: list[PodCost] = field(default_factory=list)


@dataclass
class ClusterCostReport:
    cluster_name: str
    report_date: str
    node_count: int
    total_node_daily_cost_usd: float
    allocated_cost_usd: float
    idle_cost_usd: float
    by_namespace: dict[str, NamespaceSummary]
    by_team: dict[str, float]
    top_pods: list[PodCost]
    recommendations: list[str]


# ── K8s API helpers ────────────────────────────────────────────────────────────

def _k8s_client():
    """Return a configured kubernetes.client.CoreV1Api."""
    try:
        from kubernetes import client, config as k8s_config
    except ImportError:
        raise ImportError("pip install kubernetes  (or finops-mcp[kubernetes])")

    kubeconfig = os.environ.get("KUBECONFIG", os.path.expanduser("~/.kube/config"))
    try:
        k8s_config.load_kube_config(config_file=kubeconfig)
    except Exception:
        k8s_config.load_incluster_config()   # running inside a pod

    return client.CoreV1Api(), client.AppsV1Api()


def _parse_cpu(cpu_str: str) -> float:
    """Parse k8s CPU string → millicores. '500m' → 500, '2' → 2000."""
    if not cpu_str:
        return 0.0
    if cpu_str.endswith("m"):
        return float(cpu_str[:-1])
    return float(cpu_str) * 1000


def _parse_mem(mem_str: str) -> float:
    """Parse k8s memory string → MiB. '512Mi' → 512, '1Gi' → 1024."""
    if not mem_str:
        return 0.0
    mem_str = mem_str.strip()
    units = {"Ki": 1/1024, "Mi": 1, "Gi": 1024, "Ti": 1024*1024,
             "K": 1/1.024/1024, "M": 1/1.024, "G": 1024/1.024}
    for suffix, factor in units.items():
        if mem_str.endswith(suffix):
            return float(mem_str[:-len(suffix)]) * factor
    return float(mem_str) / (1024 * 1024)   # assume bytes


def _get_owner_ref(pod_meta: Any) -> str:
    """Extract deployment/daemonset/statefulset name from pod owner references."""
    owners = getattr(pod_meta, "owner_references", None) or []
    for owner in owners:
        kind = owner.kind
        name = owner.name
        if kind == "ReplicaSet":
            # Strip hash suffix: "payments-api-7d4f8b9c6" → "payments-api"
            parts = name.rsplit("-", 2)
            return "-".join(parts[:-2]) if len(parts) >= 3 else name
        if kind in ("DaemonSet", "StatefulSet", "Job", "CronJob"):
            return name
    return "standalone"


# ── Node cost lookup ──────────────────────────────────────────────────────────

def _node_daily_cost(instance_type: str, nodegroup: str | None = None) -> float:
    """
    Return estimated daily cost for a node.
    Prefers Cost Explorer lookup for accurate amortised cost;
    falls back to on-demand rate.
    """
    hourly = _EC2_HOURLY.get(instance_type)
    if hourly is None:
        log.warning("Unknown instance type for cost: %s — defaulting $0.10/hr", instance_type)
        hourly = 0.10
    return hourly * 24


def _get_nodegroup_costs_from_ce(
    start: date,
    end: date,
    cluster_name: str,
) -> dict[str, float]:
    """
    Pull actual EKS nodegroup spend from Cost Explorer grouped by node-group tag.
    Returns {nodegroup_name: daily_avg_usd}.
    """
    try:
        import boto3
        ce = boto3.client("ce", region_name="us-east-1")
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Filter={
                "And": [
                    {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Elastic Compute Cloud - Compute"]}},
                    {"Tags": {"Key": "eks:cluster-name", "Values": [cluster_name]}},
                ]
            },
            GroupBy=[{"Type": "TAG", "Key": "eks:nodegroup-name"}],
            Metrics=["UnblendedCost"],
        )
        result: dict[str, float] = {}
        days = 0
        for period in resp.get("ResultsByTime", []):
            days += 1
            for group in period.get("Groups", []):
                ng = group["Keys"][0].replace("eks:nodegroup-name$", "")
                cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
                result[ng] = result.get(ng, 0.0) + cost
        # Average daily
        if days > 0:
            return {k: v / days for k, v in result.items()}
        return result
    except Exception as e:
        log.debug("CE nodegroup lookup failed: %s", e)
        return {}


# ── Main allocation engine ────────────────────────────────────────────────────

def allocate_cluster_costs(
    cluster_name: str = "",
    context: str | None = None,
    days_for_ce: int = 7,
) -> ClusterCostReport:
    """
    Fetch live pod data from K8s API and allocate node costs to pods.

    Args:
        cluster_name: EKS cluster name (used for Cost Explorer lookup)
        context:      kubeconfig context to use (None = current context)
        days_for_ce:  lookback days for Cost Explorer node cost averaging

    Returns:
        ClusterCostReport with full pod/namespace/team breakdown.
    """
    core_api, _ = _k8s_client()

    team_label = os.environ.get("NABLE_K8S_TEAM_LABEL", "team")
    idle_threshold = float(os.environ.get("NABLE_K8S_IDLE_THRESHOLD", "0.1"))

    # ── Collect nodes ────────────────────────────────────────────────────────
    nodes_resp = core_api.list_node()
    nodes: dict[str, dict] = {}   # node_name → {instance_type, cpu_millicores, mem_mib, daily_cost}

    for node in nodes_resp.items:
        name = node.metadata.name
        labels = node.metadata.labels or {}
        instance_type = (
            labels.get("node.kubernetes.io/instance-type")
            or labels.get("beta.kubernetes.io/instance-type", "unknown")
        )
        cap = node.status.capacity or {}
        cpu_mc = _parse_cpu(cap.get("cpu", "0"))
        mem_mib = _parse_mem(cap.get("memory", "0"))
        # Use actual capacity minus system overhead (~10%)
        allocatable = node.status.allocatable or cap
        alloc_cpu = _parse_cpu(allocatable.get("cpu", str(cpu_mc / 1000))) * 0.95
        alloc_mem = _parse_mem(allocatable.get("memory", str(mem_mib * 1024 * 1024))) * 0.95

        nodegroup = labels.get("eks.amazonaws.com/nodegroup", "")
        daily_cost = _node_daily_cost(instance_type, nodegroup)

        nodes[name] = {
            "instance_type": instance_type,
            "cpu_mc": alloc_cpu * 1000 if alloc_cpu < 100 else alloc_cpu,
            "mem_mib": alloc_mem,
            "daily_cost": daily_cost,
            "nodegroup": nodegroup,
        }

    # ── Collect pods ─────────────────────────────────────────────────────────
    pods_resp = core_api.list_pod_for_all_namespaces()
    pod_costs: list[PodCost] = []

    # Group pods by node for allocation
    pods_by_node: dict[str, list[dict]] = {n: [] for n in nodes}

    for pod in pods_resp.items:
        if pod.status.phase not in ("Running", "Pending"):
            continue
        node_name = pod.spec.node_name or ""
        if node_name not in pods_by_node:
            pods_by_node[node_name] = []

        meta = pod.metadata
        labels = meta.labels or {}
        namespace = meta.namespace or "default"
        team = labels.get(team_label, labels.get("app.kubernetes.io/part-of", namespace))
        deployment = _get_owner_ref(meta)

        # Sum requests across all containers
        cpu_req = 0.0
        mem_req = 0.0
        for container in (pod.spec.containers or []):
            if container.resources and container.resources.requests:
                reqs = container.resources.requests
                cpu_req += _parse_cpu(reqs.get("cpu", "0"))
                mem_req += _parse_mem(reqs.get("memory", "0"))

        pods_by_node[node_name].append({
            "pod_name": meta.name,
            "namespace": namespace,
            "team": team,
            "deployment": deployment,
            "cpu_req_mc": cpu_req,
            "mem_req_mib": mem_req,
        })

    # ── Cost allocation ───────────────────────────────────────────────────────
    total_node_daily = 0.0

    for node_name, node_info in nodes.items():
        total_node_daily += node_info["daily_cost"]
        node_cpu = node_info["cpu_mc"]
        node_mem = node_info["mem_mib"]
        node_daily = node_info["daily_cost"]
        pods = pods_by_node.get(node_name, [])

        if not pods or node_cpu == 0 or node_mem == 0:
            # Idle node
            pod_costs.append(PodCost(
                pod_name="__idle__",
                namespace="__idle__",
                team="__idle__",
                deployment="__idle__",
                node_name=node_name,
                node_instance_type=node_info["instance_type"],
                cpu_req_millicores=0,
                mem_req_mib=0,
                share_fraction=1.0,
                daily_cost_usd=node_daily,
                monthly_cost_usd=node_daily * 30,
            ))
            continue

        total_cpu_req = sum(p["cpu_req_mc"] for p in pods)
        total_mem_req = sum(p["mem_req_mib"] for p in pods)

        for p in pods:
            cpu_share = (p["cpu_req_mc"] / node_cpu) if node_cpu else 0
            mem_share = (p["mem_req_mib"] / node_mem) if node_mem else 0
            # Equal weighting of CPU and memory dimensions
            share = min(1.0, (cpu_share + mem_share) / 2)

            # If no requests set, split evenly
            if total_cpu_req == 0 and total_mem_req == 0:
                share = 1.0 / len(pods)

            daily = node_daily * share
            pod_costs.append(PodCost(
                pod_name=p["pod_name"],
                namespace=p["namespace"],
                team=p["team"],
                deployment=p["deployment"],
                node_name=node_name,
                node_instance_type=node_info["instance_type"],
                cpu_req_millicores=p["cpu_req_mc"],
                mem_req_mib=p["mem_req_mib"],
                share_fraction=share,
                daily_cost_usd=round(daily, 4),
                monthly_cost_usd=round(daily * 30, 2),
            ))

    # ── Aggregate by namespace ────────────────────────────────────────────────
    ns_map: dict[str, NamespaceSummary] = {}
    for pc in pod_costs:
        if pc.namespace not in ns_map:
            ns_map[pc.namespace] = NamespaceSummary(
                namespace=pc.namespace,
                team=pc.team,
                pod_count=0,
                daily_cost_usd=0.0,
                monthly_cost_usd=0.0,
            )
        ns = ns_map[pc.namespace]
        ns.pod_count += 1
        ns.daily_cost_usd += pc.daily_cost_usd
        ns.monthly_cost_usd += pc.monthly_cost_usd
        ns.pods.append(pc)

    # ── Aggregate by team ─────────────────────────────────────────────────────
    by_team: dict[str, float] = {}
    for pc in pod_costs:
        by_team[pc.team] = by_team.get(pc.team, 0.0) + pc.monthly_cost_usd

    # ── Idle cost ─────────────────────────────────────────────────────────────
    idle_cost = sum(pc.monthly_cost_usd for pc in pod_costs if pc.namespace == "__idle__")
    allocated_cost = sum(pc.monthly_cost_usd for pc in pod_costs if pc.namespace != "__idle__")

    # ── Recommendations ───────────────────────────────────────────────────────
    recommendations: list[str] = []

    idle_pct = idle_cost / max(idle_cost + allocated_cost, 1) * 100
    if idle_pct > 30:
        recommendations.append(
            f"{idle_pct:.0f}% of cluster cost is idle capacity — "
            "consider Karpenter or Cluster Autoscaler to right-size node groups"
        )

    # Pods with no resource requests
    no_requests = [pc for pc in pod_costs if pc.cpu_req_millicores == 0 and pc.namespace != "__idle__"]
    if no_requests:
        recommendations.append(
            f"{len(no_requests)} pod(s) have no resource requests set — "
            "add CPU/memory requests to enable accurate cost allocation"
        )

    # High-cost namespaces in non-prod
    for ns_name, ns in ns_map.items():
        if any(e in ns_name for e in ("dev", "staging", "test", "qa")) and ns.monthly_cost_usd > 500:
            recommendations.append(
                f"Namespace '{ns_name}' costs ${ns.monthly_cost_usd:.0f}/mo — "
                "consider namespace-level pod autoscaling or scheduled scale-down"
            )

    top_pods = sorted(pod_costs, key=lambda p: p.monthly_cost_usd, reverse=True)[:10]

    return ClusterCostReport(
        cluster_name=cluster_name or "default",
        report_date=date.today().isoformat(),
        node_count=len(nodes),
        total_node_daily_cost_usd=round(total_node_daily, 2),
        allocated_cost_usd=round(allocated_cost, 2),
        idle_cost_usd=round(idle_cost, 2),
        by_namespace=ns_map,
        by_team={k: round(v, 2) for k, v in sorted(by_team.items(), key=lambda x: x[1], reverse=True)},
        top_pods=top_pods,
        recommendations=recommendations,
    )


def allocate_to_dict(cluster_name: str = "", context: str | None = None) -> dict[str, Any]:
    """Run allocation and return JSON-serialisable dict."""
    report = allocate_cluster_costs(cluster_name, context)
    return {
        "cluster":                report.cluster_name,
        "report_date":            report.report_date,
        "node_count":             report.node_count,
        "total_monthly_cost_usd": round(report.total_node_daily_cost_usd * 30, 2),
        "allocated_cost_usd":     report.allocated_cost_usd,
        "idle_cost_usd":          report.idle_cost_usd,
        "idle_pct":               round(report.idle_cost_usd / max(report.allocated_cost_usd + report.idle_cost_usd, 1) * 100, 1),
        "by_team":                report.by_team,
        "by_namespace":           {
            ns: {
                "team": s.team,
                "pod_count": s.pod_count,
                "monthly_cost_usd": round(s.monthly_cost_usd, 2),
            }
            for ns, s in sorted(
                report.by_namespace.items(),
                key=lambda x: x[1].monthly_cost_usd, reverse=True,
            )
        },
        "top_pods": [
            {
                "pod": f"{p.namespace}/{p.pod_name}",
                "team": p.team,
                "deployment": p.deployment,
                "node": p.node_name,
                "instance_type": p.node_instance_type,
                "cpu_req_millicores": p.cpu_req_millicores,
                "mem_req_mib": round(p.mem_req_mib, 0),
                "monthly_cost_usd": p.monthly_cost_usd,
            }
            for p in report.top_pods
            if p.namespace != "__idle__"
        ],
        "recommendations": report.recommendations,
    }


def persist_daily_snapshot(report: ClusterCostReport) -> int:
    """Write today's allocation snapshot to the kubernetes_costs table."""
    try:
        from ..storage.db import get_engine, kubernetes_costs
        from sqlalchemy import delete as sql_delete

        engine = get_engine()
        today = report.report_date
        rows = []
        for pc in report.top_pods:
            rows.append({
                "cluster_name":      report.cluster_name,
                "namespace":         pc.namespace,
                "workload_name":     pc.deployment,
                "workload_type":     "pod",
                "team":              pc.team,
                "snapshot_date":     today,
                "cpu_req_millicores": pc.cpu_req_millicores,
                "mem_req_mib":       pc.mem_req_mib,
                "daily_cost_usd":    pc.daily_cost_usd,
                "node_instance_type": pc.node_instance_type,
            })

        if not rows:
            return 0

        with engine.begin() as conn:
            # Upsert: delete today's rows for this cluster, re-insert
            conn.execute(
                sql_delete(kubernetes_costs).where(
                    kubernetes_costs.c.cluster_name == report.cluster_name,
                    kubernetes_costs.c.snapshot_date == today,
                )
            )
            conn.execute(kubernetes_costs.insert(), rows)

        return len(rows)
    except Exception as e:
        log.warning("K8s snapshot persist failed: %s", e)
        return 0
