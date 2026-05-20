"""
Kubernetes cost visibility connector.

What it does
────────────
• Connects to any cluster reachable via kubeconfig (multi-cluster aware)
• Attributes node costs to namespaces → workloads → labels (team/env/app)
• Detects wasted spend: over-provisioned pods, idle nodes, ghost workloads
• Pulls actual CPU/memory usage from metrics-server when available
• Stores granular data in kubernetes_costs for trend analysis over time

Cost model
──────────
  node_hourly_cost  = derived from instance type (EC2/GKE/AKS pricing)
  pod_cost_fraction = pod_cpu_requests / node_allocatable_cpu
                    (weighted average across CPU and memory, whichever is limiting)
  pod_monthly_cost  = pod_cost_fraction × node_hourly_cost × 730

Install:
  pip install finops-mcp[kubernetes]

Config:
  KUBECONFIG           path to kubeconfig (default: ~/.kube/config)
  K8S_CONTEXTS         comma-separated context names (default: current context)
  K8S_CLOUD_PROVIDER   aws | gcp | azure | generic (default: auto-detect)
  AWS_REGION           for EC2 pricing lookups
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

log = logging.getLogger("finops.connectors.kubernetes")

# ── EC2 on-demand monthly prices (us-east-1) — same table as estimator.py ────
_EC2_MONTHLY: dict[str, float] = {
    # t-series
    "t3.nano": 3.80,    "t3.micro": 7.59,    "t3.small": 15.18,
    "t3.medium": 30.37, "t3.large": 60.74,   "t3.xlarge": 121.47,  "t3.2xlarge": 242.94,
    "t3a.medium": 27.74,"t3a.large": 55.48,  "t3a.xlarge": 110.95,
    # m5/m6
    "m5.large": 70.08,  "m5.xlarge": 140.16, "m5.2xlarge": 280.32, "m5.4xlarge": 560.64,
    "m5.8xlarge": 1121.28,
    "m6i.large": 70.08, "m6i.xlarge": 140.16,"m6i.2xlarge": 280.32,"m6i.4xlarge": 560.64,
    "m6a.large": 63.07, "m6a.xlarge": 126.14,"m6a.2xlarge": 252.29,
    # c5/c6
    "c5.large": 62.05,  "c5.xlarge": 124.10, "c5.2xlarge": 248.20, "c5.4xlarge": 496.40,
    "c6i.large": 61.32, "c6i.xlarge": 122.64,"c6i.2xlarge": 245.28,"c6i.4xlarge": 490.56,
    "c6a.large": 55.08, "c6a.xlarge": 110.16,"c6a.2xlarge": 220.32,
    # r5/r6
    "r5.large": 91.98,  "r5.xlarge": 183.96, "r5.2xlarge": 367.92, "r5.4xlarge": 735.84,
    "r6i.large": 91.98, "r6i.xlarge": 183.96,"r6i.2xlarge": 367.92,"r6i.4xlarge": 735.84,
    # GPU
    "p3.2xlarge": 2234.00,"p3.8xlarge": 8937.00,
    "g4dn.xlarge": 526.00,"g4dn.2xlarge": 1052.00,"g4dn.4xlarge": 2104.00,
    "g5.xlarge": 1006.00, "g5.2xlarge": 1212.00,
    # EKS managed node common types
    "m5.12xlarge": 1681.92,"m5.24xlarge": 3363.84,
    "c5.9xlarge": 1116.90, "c5.18xlarge": 2233.80,
    "r5.8xlarge": 1471.68, "r5.16xlarge": 2943.36,
}

_GKE_MONTHLY: dict[str, float] = {
    "e2-standard-2": 48.91,  "e2-standard-4": 97.82,  "e2-standard-8": 195.64,
    "e2-standard-16": 391.28,"e2-standard-32": 782.56,
    "n2-standard-2": 60.49,  "n2-standard-4": 120.97,  "n2-standard-8": 241.95,
    "n2-standard-16": 483.89,"n2-standard-32": 967.78,
    "n1-standard-1": 26.73,  "n1-standard-2": 53.46,   "n1-standard-4": 106.92,
    "n1-standard-8": 213.83, "n1-standard-16": 427.67,
}

_AKS_MONTHLY: dict[str, float] = {
    "Standard_D2s_v3": 70.08, "Standard_D4s_v3": 140.16,"Standard_D8s_v3": 280.32,
    "Standard_D16s_v3": 560.64,
    "Standard_D2s_v5": 70.08, "Standard_D4s_v5": 140.16,"Standard_D8s_v5": 280.32,
    "Standard_F4s_v2": 124.10,"Standard_F8s_v2": 248.20,
    "Standard_E4s_v3": 183.96,"Standard_E8s_v3": 367.92,
}


def _node_monthly_cost(instance_type: str, provider: str = "aws") -> float:
    """Best-effort monthly cost for a node instance type."""
    if provider == "gcp":
        return _GKE_MONTHLY.get(instance_type, 0.0)
    if provider == "azure":
        return _AKS_MONTHLY.get(instance_type, 0.0)
    return _EC2_MONTHLY.get(instance_type, 0.0)


def _fargate_pod_monthly_cost(cpu_cores: float, mem_gib: float) -> float:
    """
    Compute the monthly cost of an AWS Fargate pod from its resource requests.

    AWS Fargate pricing (us-east-1):
      $0.04048 per vCPU per hour
      $0.004445 per GB per hour
    Monthly = (cpu * 0.04048 + mem_gib * 0.004445) * 730
    """
    hourly = cpu_cores * 0.04048 + mem_gib * 0.004445
    return round(hourly * 730, 4)


def _is_fargate_pod(pod_labels: dict[str, str], node_name: str) -> bool:
    """Return True if this pod runs on AWS Fargate (no regular node)."""
    if pod_labels.get("eks.amazonaws.com/compute-type") == "fargate":
        return True
    if node_name.startswith("fargate-"):
        return True
    return False


def _parse_cpu(cpu_str: str) -> float:
    """Parse k8s CPU string → float cores. '500m' → 0.5, '2' → 2.0"""
    if not cpu_str:
        return 0.0
    if cpu_str.endswith("m"):
        return int(cpu_str[:-1]) / 1000.0
    return float(cpu_str)


def _parse_mem_gib(mem_str: str) -> float:
    """Parse k8s memory string → float GiB. '512Mi' → 0.5, '4Gi' → 4.0"""
    if not mem_str:
        return 0.0
    if mem_str.endswith("Ki"):
        return int(mem_str[:-2]) / (1024 ** 2)
    if mem_str.endswith("Mi"):
        return int(mem_str[:-2]) / 1024
    if mem_str.endswith("Gi"):
        return float(mem_str[:-2])
    if mem_str.endswith("Ti"):
        return float(mem_str[:-2]) * 1024
    if mem_str.endswith("K"):
        return int(mem_str[:-1]) / (1000 ** 2)
    if mem_str.endswith("M"):
        return int(mem_str[:-1]) / 1000
    if mem_str.endswith("G"):
        return float(mem_str[:-1])
    # raw bytes
    return int(mem_str) / (1024 ** 3)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class NodeInfo:
    name: str
    instance_type: str
    zone: str
    allocatable_cpu: float   # cores
    allocatable_mem: float   # GiB
    capacity_cpu: float
    capacity_mem: float
    labels: dict[str, str]
    monthly_cost: float
    is_spot: bool = False


@dataclass
class PodInfo:
    name: str
    namespace: str
    node_name: str
    phase: str               # Running / Pending / Succeeded / Failed
    owner_kind: str          # Deployment / StatefulSet / DaemonSet / Job / bare
    owner_name: str
    cpu_request: float       # cores
    cpu_limit: float
    mem_request: float       # GiB
    mem_limit: float
    cpu_actual: float | None = None   # from metrics-server
    mem_actual: float | None = None
    labels: dict[str, str] = field(default_factory=dict)
    containers: int = 1


@dataclass
class WorkloadCost:
    cluster: str
    namespace: str
    workload_kind: str
    workload_name: str
    pod_count: int
    cpu_requested: float
    cpu_used: float | None
    mem_requested: float
    mem_used: float | None
    monthly_cost: float
    wasted_usd: float
    cpu_efficiency_pct: float | None
    mem_efficiency_pct: float | None
    labels: dict[str, str]

    def summary_line(self) -> str:
        eff = f"{self.cpu_efficiency_pct:.0f}% CPU eff" if self.cpu_efficiency_pct is not None else "no metrics"
        waste = f"  ~${self.wasted_usd:,.0f}/mo wasted" if self.wasted_usd > 5 else ""
        return (
            f"{self.namespace}/{self.workload_name} ({self.workload_kind}): "
            f"${self.monthly_cost:,.0f}/mo  [{eff}]{waste}"
        )


@dataclass
class ClusterReport:
    cluster: str
    context: str
    node_count: int
    pod_count: int
    namespaces: list[str]
    total_monthly_cost: float
    wasted_monthly_cost: float
    overall_cpu_efficiency: float | None    # %
    overall_mem_efficiency: float | None    # %
    workloads: list[WorkloadCost]
    node_utilization: list[dict[str, Any]]  # per-node breakdown
    idle_nodes: list[str]                   # nodes with <10% utilisation
    pvc_monthly_cost: float
    top_spenders: list[WorkloadCost]        # top 10 by cost
    rightsizing_opportunities: list[dict[str, Any]]
    provider: str
    generated_at: str


# ── Main connector ────────────────────────────────────────────────────────────

class KubernetesConnector:
    """
    Multi-cluster Kubernetes cost connector.

    Requires: pip install kubernetes
    Optional: pip install kubernetes[adv]  (for metrics-server support)
    """

    def __init__(self) -> None:
        self._kubeconfig = os.environ.get("KUBECONFIG", "")
        raw_contexts = os.environ.get("K8S_CONTEXTS", "")
        self._contexts: list[str] = [c.strip() for c in raw_contexts.split(",") if c.strip()]
        self._cloud_provider = os.environ.get("K8S_CLOUD_PROVIDER", "auto").lower()

    async def is_configured(self) -> bool:
        try:
            from kubernetes import config as k8s_config  # type: ignore
            k8s_config.load_kube_config(config_file=self._kubeconfig or None)
            return True
        except Exception:
            return False

    def _load_client(self, context: str | None = None):
        from kubernetes import client, config as k8s_config  # type: ignore
        k8s_config.load_kube_config(
            config_file=self._kubeconfig or None,
            context=context,
        )
        return client

    def _detect_provider(self, nodes: list[NodeInfo]) -> str:
        if self._cloud_provider != "auto":
            return self._cloud_provider
        if not nodes:
            return "generic"
        labels = nodes[0].labels
        if "eks.amazonaws.com/nodegroup" in labels or "alpha.eksctl.io/cluster-name" in labels:
            return "aws"
        if "cloud.google.com/gke-nodepool" in labels:
            return "gcp"
        if "kubernetes.azure.com/agentpool" in labels or "kubernetes.azure.com/cluster" in labels:
            return "azure"
        if any("beta.kubernetes.io/instance-type" in l for l in [labels]):
            instance = labels.get("beta.kubernetes.io/instance-type", "")
            if instance.startswith("m") or instance.startswith("c") or instance.startswith("r"):
                return "aws"
        return "generic"

    def _get_nodes(self, client, provider: str) -> list[NodeInfo]:
        v1 = client.CoreV1Api()
        nodes = []
        for node in v1.list_node().items:
            meta   = node.metadata
            labels = dict(meta.labels or {})
            alloc  = node.status.allocatable or {}
            cap    = node.status.capacity or {}

            instance_type = (
                labels.get("node.kubernetes.io/instance-type")
                or labels.get("beta.kubernetes.io/instance-type")
                or labels.get("kops.k8s.io/instancegroup", "unknown")
            )
            zone = (
                labels.get("topology.kubernetes.io/zone")
                or labels.get("failure-domain.beta.kubernetes.io/zone", "")
            )
            is_spot = (
                labels.get("kops.k8s.io/instancegroup", "").lower().find("spot") >= 0
                or labels.get("eks.amazonaws.com/capacityType", "") == "SPOT"
                or labels.get("cloud.google.com/gke-spot", "") == "true"
            )

            monthly = _node_monthly_cost(instance_type, provider)
            if is_spot:
                monthly *= 0.35  # spots ~65% cheaper on average

            nodes.append(NodeInfo(
                name=meta.name,
                instance_type=instance_type,
                zone=zone,
                allocatable_cpu=_parse_cpu(alloc.get("cpu", "0")),
                allocatable_mem=_parse_mem_gib(alloc.get("memory", "0")),
                capacity_cpu=_parse_cpu(cap.get("cpu", "0")),
                capacity_mem=_parse_mem_gib(cap.get("memory", "0")),
                labels=labels,
                monthly_cost=monthly,
                is_spot=is_spot,
            ))
        return nodes

    def _get_pods(self, client) -> list[PodInfo]:
        v1 = client.CoreV1Api()
        pods = []
        for pod in v1.list_pod_for_all_namespaces().items:
            meta  = pod.metadata
            spec  = pod.spec
            phase = pod.status.phase or "Unknown"

            # Resolve owner (Deployment / StatefulSet / DaemonSet / Job / bare)
            owner_kind, owner_name = "Pod", meta.name
            for ref in (meta.owner_references or []):
                if ref.kind in ("ReplicaSet", "StatefulSet", "DaemonSet", "Job", "CronJob"):
                    owner_kind = ref.kind
                    owner_name = ref.name
                    # ReplicaSet → parent Deployment (strip trailing -<hash>)
                    if ref.kind == "ReplicaSet":
                        owner_kind = "Deployment"
                        parts = ref.name.rsplit("-", 1)
                        owner_name = parts[0] if len(parts) == 2 else ref.name
                    break

            # Sum resource requests/limits across all containers
            cpu_req = cpu_lim = mem_req = mem_lim = 0.0
            container_count = 0
            for container in (spec.containers or []):
                container_count += 1
                res = container.resources
                if res:
                    req = res.requests or {}
                    lim = res.limits or {}
                    cpu_req += _parse_cpu(req.get("cpu", "0"))
                    cpu_lim += _parse_cpu(lim.get("cpu", "0"))
                    mem_req += _parse_mem_gib(req.get("memory", "0"))
                    mem_lim += _parse_mem_gib(lim.get("memory", "0"))

            pod_labels = dict(meta.labels or {})
            node_name = spec.node_name or ""
            # Fargate pods may have a node name like "fargate-ip-..." or the
            # compute-type label set; preserve the node_name so callers can
            # distinguish Fargate pods from unscheduled (Pending) pods.
            pods.append(PodInfo(
                name=meta.name,
                namespace=meta.namespace,
                node_name=node_name,
                phase=phase,
                owner_kind=owner_kind,
                owner_name=owner_name,
                cpu_request=cpu_req,
                cpu_limit=cpu_lim,
                mem_request=mem_req,
                mem_limit=mem_lim,
                labels=pod_labels,
                containers=container_count,
            ))
        return pods

    def _get_pod_metrics(self, client) -> dict[str, tuple[float, float]]:
        """
        Pull actual CPU/memory usage from metrics-server.
        Returns {pod_name: (cpu_cores, mem_gib)}. Empty dict if unavailable.
        """
        try:
            custom = client.CustomObjectsApi()
            metrics = custom.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="pods",
            )
            result: dict[str, tuple[float, float]] = {}
            for item in metrics.get("items", []):
                pod_name = item["metadata"]["name"]
                cpu_total = mem_total = 0.0
                for container in item.get("containers", []):
                    usage = container.get("usage", {})
                    cpu_total += _parse_cpu(usage.get("cpu", "0"))
                    mem_total += _parse_mem_gib(usage.get("memory", "0"))
                result[pod_name] = (cpu_total, mem_total)
            return result
        except Exception as e:
            log.debug("metrics-server unavailable: %s", e)
            return {}

    def _get_pvc_cost(self, client, provider: str) -> float:
        """Estimate monthly PVC storage cost."""
        v1 = client.CoreV1Api()
        total_gib = 0.0
        try:
            for pvc in v1.list_persistent_volume_claim_for_all_namespaces().items:
                spec = pvc.spec
                storage = (spec.resources.requests or {}).get("storage", "0") if spec.resources else "0"
                total_gib += _parse_mem_gib(storage)  # storage uses same units
        except Exception:
            pass
        # gp3 EBS ~$0.08/GiB/mo, GCS standard ~$0.02, Azure ~$0.10
        rate = {"aws": 0.08, "gcp": 0.02, "azure": 0.10}.get(provider, 0.08)
        return round(total_gib * rate, 2)

    def _attribute_costs(
        self,
        cluster: str,
        nodes: list[NodeInfo],
        pods: list[PodInfo],
        pod_metrics: dict[str, tuple[float, float]],
        provider: str,
    ) -> list[WorkloadCost]:
        """
        Attribute node costs to workloads using the resource-request fraction model.
        """
        node_map = {n.name: n for n in nodes}

        # Per-pod cost
        pod_costs: dict[str, float] = {}
        for pod in pods:
            if pod.phase not in ("Running", "Pending"):
                continue

            # Fargate pods: priced directly per vCPU/GB — no node allocation needed
            if _is_fargate_pod(pod.labels, pod.node_name):
                if pod.cpu_request > 0 or pod.mem_request > 0:
                    pod_costs[pod.name] = _fargate_pod_monthly_cost(
                        pod.cpu_request, pod.mem_request
                    )
                continue

            node = node_map.get(pod.node_name)
            if not node or node.monthly_cost == 0:
                continue
            if node.allocatable_cpu <= 0 or node.allocatable_mem <= 0:
                continue

            # Weighted fraction: 50% CPU weight, 50% memory weight
            cpu_frac = pod.cpu_request / node.allocatable_cpu if node.allocatable_cpu else 0
            mem_frac = pod.mem_request / node.allocatable_mem if node.allocatable_mem else 0
            # Cap fraction at 1.0 per resource to handle over-commit
            cpu_frac = min(cpu_frac, 1.0)
            mem_frac = min(mem_frac, 1.0)
            fraction = 0.5 * cpu_frac + 0.5 * mem_frac
            pod_costs[pod.name] = fraction * node.monthly_cost

        # Merge actual metrics into pods
        for pod in pods:
            if pod.name in pod_metrics:
                pod.cpu_actual, pod.mem_actual = pod_metrics[pod.name]

        # Group pods → workloads
        workload_key = lambda p: (p.namespace, p.owner_kind, p.owner_name)
        from itertools import groupby
        from operator import attrgetter

        running = [p for p in pods if p.phase in ("Running", "Pending")]
        running.sort(key=workload_key)

        workloads: list[WorkloadCost] = []
        for (ns, kind, name), group in groupby(running, key=workload_key):
            group_pods = list(group)
            total_cost = sum(pod_costs.get(p.name, 0) for p in group_pods)
            cpu_req   = sum(p.cpu_request  for p in group_pods)
            mem_req   = sum(p.mem_request  for p in group_pods)
            cpu_used  = sum(p.cpu_actual   for p in group_pods if p.cpu_actual is not None) or None
            mem_used  = sum(p.mem_actual   for p in group_pods if p.mem_actual is not None) or None

            # Has actual metrics only if ALL pods reported
            pods_with_metrics = sum(1 for p in group_pods if p.cpu_actual is not None)
            if pods_with_metrics == 0:
                cpu_used = mem_used = None

            cpu_eff = (cpu_used / cpu_req * 100) if (cpu_used is not None and cpu_req > 0) else None
            mem_eff = (mem_used / mem_req * 100) if (mem_used is not None and mem_req > 0) else None

            # Wasted = cost of unused requests (where we have metrics)
            wasted = 0.0
            if cpu_eff is not None and cpu_eff < 100:
                wasted += total_cost * (1 - cpu_used / cpu_req) * 0.5  # CPU portion
            if mem_eff is not None and mem_eff < 100:
                wasted += total_cost * (1 - mem_used / mem_req) * 0.5  # mem portion

            # Aggregate labels (team/env/app) from pods
            merged_labels: dict[str, str] = {}
            for p in group_pods:
                for k, v in p.labels.items():
                    if k in ("app", "app.kubernetes.io/name", "team", "env",
                             "environment", "service", "component", "tier"):
                        merged_labels[k] = v

            workloads.append(WorkloadCost(
                cluster=cluster,
                namespace=ns,
                workload_kind=kind,
                workload_name=name,
                pod_count=len(group_pods),
                cpu_requested=round(cpu_req, 3),
                cpu_used=round(cpu_used, 3) if cpu_used is not None else None,
                mem_requested=round(mem_req, 2),
                mem_used=round(mem_used, 2) if mem_used is not None else None,
                monthly_cost=round(total_cost, 2),
                wasted_usd=round(wasted, 2),
                cpu_efficiency_pct=round(cpu_eff, 1) if cpu_eff is not None else None,
                mem_efficiency_pct=round(mem_eff, 1) if mem_eff is not None else None,
                labels=merged_labels,
            ))

        workloads.sort(key=lambda w: w.monthly_cost, reverse=True)
        return workloads

    def _rightsizing_opportunities(self, workloads: list[WorkloadCost]) -> list[dict[str, Any]]:
        """Flag workloads with >30% wasted resources as rightsizing candidates."""
        opportunities = []
        for w in workloads:
            if w.monthly_cost < 5:
                continue

            issues = []
            if w.cpu_efficiency_pct is not None and w.cpu_efficiency_pct < 30:
                issues.append(
                    f"CPU requests {w.cpu_requested:.2f} cores but only using "
                    f"{w.cpu_used:.2f} ({w.cpu_efficiency_pct:.0f}%) — "
                    f"consider reducing requests to {w.cpu_used * 1.3:.2f} cores"
                )
            if w.mem_efficiency_pct is not None and w.mem_efficiency_pct < 30:
                issues.append(
                    f"Memory requests {w.mem_requested:.1f} GiB but only using "
                    f"{w.mem_used:.1f} GiB ({w.mem_efficiency_pct:.0f}%) — "
                    f"consider reducing to {w.mem_used * 1.3:.1f} GiB"
                )
            if not issues:
                continue

            savings = w.wasted_usd * 0.7  # realistic 70% of wasted recoverable
            opportunities.append({
                "workload": f"{w.namespace}/{w.workload_name}",
                "kind": w.workload_kind,
                "monthly_cost": w.monthly_cost,
                "potential_savings_usd": round(savings, 2),
                "issues": issues,
                "labels": w.labels,
            })

        opportunities.sort(key=lambda x: x["potential_savings_usd"], reverse=True)
        return opportunities

    def _node_utilization(
        self,
        nodes: list[NodeInfo],
        pods: list[PodInfo],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Per-node CPU/memory utilization from pod requests."""
        node_cpu_used  = {n.name: 0.0 for n in nodes}
        node_mem_used  = {n.name: 0.0 for n in nodes}

        for pod in pods:
            if pod.phase != "Running" or not pod.node_name:
                continue
            node_cpu_used[pod.node_name]  = node_cpu_used.get(pod.node_name, 0) + pod.cpu_request
            node_mem_used[pod.node_name] = node_mem_used.get(pod.node_name, 0) + pod.mem_request

        utilization = []
        idle_nodes  = []
        for node in nodes:
            cpu_pct = (node_cpu_used[node.name] / node.allocatable_cpu * 100) if node.allocatable_cpu else 0
            mem_pct = (node_mem_used[node.name] / node.allocatable_mem * 100) if node.allocatable_mem else 0
            utilization.append({
                "node": node.name,
                "instance_type": node.instance_type,
                "zone": node.zone,
                "is_spot": node.is_spot,
                "monthly_cost": node.monthly_cost,
                "cpu_requested_pct": round(cpu_pct, 1),
                "mem_requested_pct": round(mem_pct, 1),
                "cpu_allocatable_cores": node.allocatable_cpu,
                "mem_allocatable_gib": round(node.allocatable_mem, 1),
            })
            if cpu_pct < 10 and mem_pct < 10 and node.monthly_cost > 20:
                idle_nodes.append(node.name)

        utilization.sort(key=lambda n: n["cpu_requested_pct"])
        return utilization, idle_nodes

    def analyze_cluster(self, context: str | None = None) -> ClusterReport:
        """Full cost analysis for one cluster context."""
        k8s_client = self._load_client(context)
        cluster_name = context or "default"

        log.info("Analyzing cluster: %s", cluster_name)

        nodes = self._get_nodes(k8s_client, "auto")  # provider detected below
        provider = self._detect_provider(nodes)
        # Re-price nodes with detected provider
        for node in nodes:
            node.monthly_cost = _node_monthly_cost(node.instance_type, provider)
            if node.is_spot:
                node.monthly_cost *= 0.35

        pods = self._get_pods(k8s_client)
        pod_metrics = self._get_pod_metrics(k8s_client)
        pvc_cost = self._get_pvc_cost(k8s_client, provider)

        workloads = self._attribute_costs(cluster_name, nodes, pods, pod_metrics, provider)
        node_util, idle_nodes = self._node_utilization(nodes, pods)
        rightsizing = self._rightsizing_opportunities(workloads)

        total_cost = sum(n.monthly_cost for n in nodes) + pvc_cost
        total_waste = sum(w.wasted_usd for w in workloads)

        # Cluster-wide efficiency
        all_cpu_req  = sum(w.cpu_requested for w in workloads)
        all_cpu_used = sum(w.cpu_used or 0  for w in workloads if w.cpu_used is not None)
        all_mem_req  = sum(w.mem_requested  for w in workloads)
        all_mem_used = sum(w.mem_used or 0  for w in workloads if w.mem_used is not None)
        has_metrics  = any(w.cpu_used is not None for w in workloads)

        cpu_eff = (all_cpu_used / all_cpu_req * 100) if (has_metrics and all_cpu_req > 0) else None
        mem_eff = (all_mem_used / all_mem_req * 100) if (has_metrics and all_mem_req > 0) else None

        namespaces = sorted({w.namespace for w in workloads})

        return ClusterReport(
            cluster=cluster_name,
            context=context or "current-context",
            node_count=len(nodes),
            pod_count=len([p for p in pods if p.phase == "Running"]),
            namespaces=namespaces,
            total_monthly_cost=round(total_cost, 2),
            wasted_monthly_cost=round(total_waste, 2),
            overall_cpu_efficiency=round(cpu_eff, 1) if cpu_eff is not None else None,
            overall_mem_efficiency=round(mem_eff, 1) if mem_eff is not None else None,
            workloads=workloads,
            node_utilization=node_util,
            idle_nodes=idle_nodes,
            pvc_monthly_cost=pvc_cost,
            top_spenders=workloads[:10],
            rightsizing_opportunities=rightsizing,
            provider=provider,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    def analyze_all_clusters(self) -> list[ClusterReport]:
        """Analyze every configured cluster context."""
        from kubernetes import config as k8s_config  # type: ignore

        if self._contexts:
            contexts = self._contexts
        else:
            _, all_ctx = k8s_config.list_kube_config_contexts(
                config_file=self._kubeconfig or None
            )
            contexts = [c["name"] for c in (all_ctx or [])]
            if not contexts:
                contexts = [None]  # type: ignore[list-item]

        reports = []
        for ctx in contexts:
            try:
                reports.append(self.analyze_cluster(ctx))
            except Exception as e:
                log.warning("Failed to analyze cluster %s: %s", ctx, e)
        return reports

    def persist_to_db(self, report: ClusterReport) -> None:
        """Store cluster cost data in the local SQLite DB for trend analysis."""
        from ..storage.db import get_engine, kubernetes_costs
        from datetime import datetime, timezone

        today = date.today().isoformat()
        engine = get_engine()

        with engine.begin() as conn:
            # Remove today's stale data for this cluster before re-inserting
            conn.execute(
                kubernetes_costs.delete().where(
                    kubernetes_costs.c.cluster == report.cluster,
                    kubernetes_costs.c.snapshot_date == today,
                )
            )
            for w in report.workloads:
                conn.execute(kubernetes_costs.insert().values(
                    cluster=report.cluster,
                    namespace=w.namespace,
                    workload_kind=w.workload_kind,
                    workload_name=w.workload_name,
                    snapshot_date=today,
                    cpu_requested_cores=w.cpu_requested,
                    cpu_used_cores=w.cpu_used,
                    mem_requested_gib=w.mem_requested,
                    mem_used_gib=w.mem_used,
                    node_count=report.node_count,
                    pod_count=w.pod_count,
                    monthly_cost_usd=w.monthly_cost,
                    cpu_efficiency_pct=w.cpu_efficiency_pct,
                    mem_efficiency_pct=w.mem_efficiency_pct,
                    wasted_usd=w.wasted_usd,
                    labels=json.dumps(w.labels),
                    captured_at=datetime.now(timezone.utc),
                ))

        log.info(
            "Stored %d workload records for cluster %s (total: $%.0f/mo)",
            len(report.workloads), report.cluster, report.total_monthly_cost,
        )
