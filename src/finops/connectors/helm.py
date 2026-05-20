"""
Helm release cost visibility.

What it does
────────────
• Discovers all Helm releases in the cluster via release secrets
  (sh.helm.release.v1.<name>.v<n>) — no `helm` CLI required
• Groups workload costs by Helm release so you see:
    "prometheus-stack: $340/month" not "namespace/prometheus-deployment: $340"
• Tracks chart name + version per release for cost-per-version trending
• Detects orphaned releases (deployed but all pods dead/missing)
• Extends the PR cost estimator to understand helm diff output and
  values.yaml changes (replicas, CPU/memory requests, image changes)

Cost attribution hierarchy
──────────────────────────
  Helm release  →  contains  →  Deployments / StatefulSets / DaemonSets
                               →  ReplicaSets → Pods → resource requests
                                                      → node fraction → $

Helm metadata keys used
───────────────────────
  Labels:
    meta.helm.sh/release-name       release name
    meta.helm.sh/release-namespace  release namespace
    helm.sh/chart                   chart name + version  e.g. "prometheus-25.1.0"
    app.kubernetes.io/managed-by: Helm
    app.kubernetes.io/version       app version
  Annotations (on secrets):
    meta.helm.sh/release-name
    meta.helm.sh/release-namespace
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger("finops.connectors.helm")

# Labels Helm stamps on every managed resource
_RELEASE_NAME_LABEL  = "meta.helm.sh/release-name"
_RELEASE_NS_LABEL    = "meta.helm.sh/release-namespace"
_CHART_LABEL         = "helm.sh/chart"
_MANAGED_BY_LABEL    = "app.kubernetes.io/managed-by"

# Legacy Helm 2 label
_HELM2_RELEASE_LABEL = "release"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class HelmRelease:
    name: str
    namespace: str
    chart: str           # "prometheus-25.1.0"
    chart_name: str      # "prometheus"
    chart_version: str   # "25.1.0"
    app_version: str     # "v2.48.0"
    status: str          # deployed | failed | pending | superseded
    revision: int
    deployed_at: str     # ISO timestamp
    workload_names: list[str] = field(default_factory=list)   # Deployment/SS names
    monthly_cost: float = 0.0
    wasted_usd: float = 0.0
    pod_count: int = 0
    cpu_efficiency_pct: float | None = None
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def is_orphaned(self) -> bool:
        """Release deployed but no running workloads found."""
        return self.status == "deployed" and self.pod_count == 0

    def summary_line(self) -> str:
        waste = f"  ~${self.wasted_usd:,.0f}/mo wasted" if self.wasted_usd > 5 else ""
        eff = f"  {self.cpu_efficiency_pct:.0f}% CPU eff" if self.cpu_efficiency_pct else ""
        orphan = "  ⚠️ orphaned" if self.is_orphaned else ""
        return (
            f"{self.namespace}/{self.name}  ({self.chart})"
            f"  ${self.monthly_cost:,.0f}/mo{waste}{eff}{orphan}"
        )


@dataclass
class HelmCostReport:
    releases: list[HelmRelease]
    total_monthly_cost: float
    total_wasted_usd: float
    orphaned_releases: list[HelmRelease]
    unmanaged_cost: float         # workloads not under any Helm release
    by_chart: dict[str, float]    # chart_name → total cost across all releases
    generated_at: str


# ── Release discovery via cluster secrets ─────────────────────────────────────

def _decode_helm_secret(secret_data: dict) -> dict | None:
    """
    Helm stores release data as a gzip+base64 encoded JSON blob in
    the 'release' key of a k8s secret. Decode it.
    """
    try:
        raw = secret_data.get("release", "")
        if not raw:
            return None
        # Helm 3: base64(gzip(json))
        decoded = base64.b64decode(raw)
        try:
            decompressed = gzip.decompress(decoded)
        except Exception:
            decompressed = decoded  # not gzipped (some versions)
        return json.loads(decompressed)
    except Exception as e:
        log.debug("Failed to decode helm secret: %s", e)
        return None


def discover_helm_releases(k8s_client) -> list[HelmRelease]:
    """
    Discover all Helm releases by reading sh.helm.release.v1.* secrets.
    No helm CLI required — works on any cluster.
    """
    v1 = k8s_client.CoreV1Api()
    releases: dict[str, HelmRelease] = {}   # key: "{namespace}/{name}"

    try:
        secrets = v1.list_secret_for_all_namespaces(
            label_selector="owner=helm"
        )
    except Exception:
        # Fallback: list all secrets and filter
        try:
            secrets = v1.list_secret_for_all_namespaces()
        except Exception as e:
            log.warning("Cannot list secrets: %s", e)
            return []

    for secret in secrets.items:
        meta = secret.metadata
        name = meta.name or ""

        # Only process Helm release secrets
        if not name.startswith("sh.helm.release.v1."):
            continue

        raw_data = {k: v for k, v in (secret.data or {}).items()}
        release_data = _decode_helm_secret(raw_data)

        if not release_data:
            continue

        chart_meta = release_data.get("chart", {}).get("metadata", {})
        info       = release_data.get("info", {})

        release_name = release_data.get("name", meta.labels.get("name", ""))
        release_ns   = release_data.get("namespace", meta.namespace)
        chart_name   = chart_meta.get("name", "")
        chart_ver    = chart_meta.get("version", "")
        app_ver      = chart_meta.get("appVersion", "")
        status       = info.get("status", "unknown")
        revision     = release_data.get("version", 1)

        deployed_at = info.get("last_deployed", "")
        if deployed_at:
            try:
                # Normalize to ISO format
                deployed_at = datetime.fromisoformat(
                    deployed_at.replace("Z", "+00:00")
                ).isoformat()
            except Exception:
                pass

        key = f"{release_ns}/{release_name}"

        # Keep only the latest revision
        existing = releases.get(key)
        if existing and existing.revision >= revision:
            continue

        releases[key] = HelmRelease(
            name=release_name,
            namespace=release_ns,
            chart=f"{chart_name}-{chart_ver}" if chart_ver else chart_name,
            chart_name=chart_name,
            chart_version=chart_ver,
            app_version=app_ver,
            status=status,
            revision=revision,
            deployed_at=deployed_at,
        )

    return list(releases.values())


def _map_workloads_to_releases(
    k8s_client,
    releases: list[HelmRelease],
) -> dict[str, str]:
    """
    Build a map of {workload_uid: release_key} by reading labels
    on Deployments, StatefulSets, and DaemonSets.
    Returns {"{namespace}/{workload_name}": "{release_ns}/{release_name}"}
    """
    apps = k8s_client.AppsV1Api()
    mapping: dict[str, str] = {}

    release_keys = {f"{r.namespace}/{r.name}" for r in releases}

    def _check_resource(resource_list):
        for item in resource_list:
            meta   = item.metadata
            labels = dict(meta.labels or {})
            ann    = dict(meta.annotations or {})

            # Helm 3 labels
            rel_name = labels.get(_RELEASE_NAME_LABEL) or ann.get(_RELEASE_NAME_LABEL)
            rel_ns   = labels.get(_RELEASE_NS_LABEL)   or ann.get(_RELEASE_NS_LABEL) or meta.namespace

            # Helm 2 fallback
            if not rel_name:
                rel_name = labels.get(_HELM2_RELEASE_LABEL)
                rel_ns   = meta.namespace

            if not rel_name:
                continue

            key = f"{rel_ns}/{rel_name}"
            # Only map to known releases
            if key not in release_keys:
                key = f"{meta.namespace}/{rel_name}"
                if key not in release_keys:
                    continue

            workload_key = f"{meta.namespace}/{meta.name}"
            mapping[workload_key] = key

    try:
        _check_resource(apps.list_deployment_for_all_namespaces().items)
        _check_resource(apps.list_stateful_set_for_all_namespaces().items)
        _check_resource(apps.list_daemon_set_for_all_namespaces().items)
    except Exception as e:
        log.warning("Could not map workloads to releases: %s", e)

    return mapping


# ── Cost attribution ──────────────────────────────────────────────────────────

def attribute_costs_to_releases(
    releases: list[HelmRelease],
    workloads: list,   # list[WorkloadCost] from kubernetes.py
    k8s_client,
) -> tuple[list[HelmRelease], float]:
    """
    Match WorkloadCost objects to their Helm releases.
    Returns (enriched_releases, unmanaged_cost).
    """
    workload_to_release = _map_workloads_to_releases(k8s_client, releases)

    # Build release lookup
    release_map = {f"{r.namespace}/{r.name}": r for r in releases}
    for r in releases:
        r.monthly_cost = 0.0
        r.wasted_usd   = 0.0
        r.pod_count    = 0
        r.workload_names = []

    unmanaged_cost = 0.0

    for w in workloads:
        workload_key = f"{w.namespace}/{w.workload_name}"
        release_key  = workload_to_release.get(workload_key)

        if not release_key:
            unmanaged_cost += w.monthly_cost
            continue

        release = release_map.get(release_key)
        if not release:
            unmanaged_cost += w.monthly_cost
            continue

        release.monthly_cost += w.monthly_cost
        release.wasted_usd   += w.wasted_usd
        release.pod_count    += w.pod_count
        release.workload_names.append(w.workload_name)

        # Roll up CPU efficiency (weighted average)
        if w.cpu_efficiency_pct is not None:
            if release.cpu_efficiency_pct is None:
                release.cpu_efficiency_pct = w.cpu_efficiency_pct
            else:
                # Simple average — good enough for display
                release.cpu_efficiency_pct = (release.cpu_efficiency_pct + w.cpu_efficiency_pct) / 2

    # Round
    for r in releases:
        r.monthly_cost = round(r.monthly_cost, 2)
        r.wasted_usd   = round(r.wasted_usd, 2)
        if r.cpu_efficiency_pct is not None:
            r.cpu_efficiency_pct = round(r.cpu_efficiency_pct, 1)

    releases.sort(key=lambda r: r.monthly_cost, reverse=True)
    return releases, round(unmanaged_cost, 2)


# ── Helm diff cost estimation ─────────────────────────────────────────────────
# Extends pr_comments/estimator.py to understand helm diff / values.yaml changes

_REPLICA_RE    = re.compile(r'[+\-]\s*replicaCount:\s*(\d+)', re.MULTILINE)
_CPU_REQ_RE    = re.compile(r'[+\-]\s*cpu:\s*(["\']?)(\d+m|\d+(?:\.\d+)?)\1', re.MULTILINE)
_MEM_REQ_RE    = re.compile(r'[+\-]\s*memory:\s*(["\']?)(\d+(?:Mi|Gi|Ki|M|G|K)?)\1', re.MULTILINE)
_INSTANCE_RE   = re.compile(r'[+\-]\s*(?:instanceType|nodeType|machineType):\s*(["\']?)(\S+)\1', re.MULTILINE)
_NODE_COUNT_RE = re.compile(r'[+\-]\s*(?:nodeCount|minSize|desiredSize|min_count):\s*(\d+)', re.MULTILINE)


@dataclass
class HelmCostDiff:
    """Estimated cost impact of a helm diff / values.yaml change."""
    release_name: str
    delta_monthly_usd: float
    changes: list[str]          # human-readable description of what changed
    confidence: str             # high | medium | low


def estimate_helm_diff(
    diff_text: str,
    release_name: str = "unknown",
    current_replica_count: int = 1,
    current_cpu_request: str = "100m",
    current_mem_request: str = "128Mi",
) -> HelmCostDiff:
    """
    Estimate cost impact of a `helm diff upgrade` or values.yaml diff.

    Handles:
      - replicaCount changes
      - resources.requests.cpu changes
      - resources.requests.memory changes
      - instanceType / machineType changes (for node pools)
      - nodeCount / minSize changes
    """
    from .kubernetes import _parse_cpu, _parse_mem_gib, _node_monthly_cost, _EC2_MONTHLY

    changes: list[str] = []
    delta = 0.0
    confidence = "medium"

    # Parse added (+) and removed (-) values
    def _last_pair(pattern: re.Pattern) -> tuple[str | None, str | None]:
        """Return (removed_value, added_value) for a pattern."""
        removed = added = None
        for m in pattern.finditer(diff_text):
            line = diff_text[:m.start()].rsplit("\n", 1)[-1] + diff_text[m.start():]
            sign = diff_text[m.start()]
            val  = m.group(m.lastindex or 1)
            if sign == "-":
                removed = val
            elif sign == "+":
                added = val
        return removed, added

    # ── Replica count change ──────────────────────────────────────────────────
    old_replicas = new_replicas = None
    for m in _REPLICA_RE.finditer(diff_text):
        sign = diff_text[m.start()]
        val  = int(m.group(1))
        if sign == "-":
            old_replicas = val
        elif sign == "+":
            new_replicas = val

    if old_replicas is not None and new_replicas is not None and old_replicas != new_replicas:
        delta_replicas = new_replicas - old_replicas
        # Estimate cost of one pod from current CPU/mem requests
        cpu  = _parse_cpu(current_cpu_request)
        mem  = _parse_mem_gib(current_mem_request)
        # Rough: 1 core ≈ $35/mo, 1 GiB ≈ $5/mo (blended k8s node rates)
        pod_cost = cpu * 35 + mem * 5
        replica_delta = round(pod_cost * delta_replicas, 2)
        delta += replica_delta
        direction = "+" if delta_replicas > 0 else ""
        changes.append(
            f"replicaCount {old_replicas} → {new_replicas} "
            f"({direction}{delta_replicas} pods, ~{'+' if replica_delta >= 0 else ''}"
            f"${replica_delta:,.0f}/mo)"
        )

    # ── CPU request change ────────────────────────────────────────────────────
    old_cpu_str = new_cpu_str = None
    for m in _CPU_REQ_RE.finditer(diff_text):
        sign = diff_text[m.start()]
        val  = m.group(2)
        if sign == "-":
            old_cpu_str = val
        elif sign == "+":
            new_cpu_str = val

    if old_cpu_str and new_cpu_str:
        old_cpu = _parse_cpu(old_cpu_str)
        new_cpu = _parse_cpu(new_cpu_str)
        if abs(new_cpu - old_cpu) > 0.001:
            # Use replica count to scale
            replicas = new_replicas or current_replica_count
            cpu_delta = round((new_cpu - old_cpu) * replicas * 35, 2)
            delta += cpu_delta
            changes.append(
                f"CPU request {old_cpu_str} → {new_cpu_str} × {replicas} pods "
                f"(~{'+' if cpu_delta >= 0 else ''}${cpu_delta:,.0f}/mo)"
            )

    # ── Memory request change ─────────────────────────────────────────────────
    old_mem_str = new_mem_str = None
    for m in _MEM_REQ_RE.finditer(diff_text):
        sign = diff_text[m.start()]
        val  = m.group(2)
        if sign == "-":
            old_mem_str = val
        elif sign == "+":
            new_mem_str = val

    if old_mem_str and new_mem_str:
        from .kubernetes import _parse_mem_gib
        old_mem = _parse_mem_gib(old_mem_str)
        new_mem = _parse_mem_gib(new_mem_str)
        if abs(new_mem - old_mem) > 0.001:
            replicas = new_replicas or current_replica_count
            mem_delta = round((new_mem - old_mem) * replicas * 5, 2)
            delta += mem_delta
            changes.append(
                f"Memory request {old_mem_str} → {new_mem_str} × {replicas} pods "
                f"(~{'+' if mem_delta >= 0 else ''}${mem_delta:,.0f}/mo)"
            )

    # ── Instance type change (node pools) ────────────────────────────────────
    old_inst = new_inst = None
    for m in _INSTANCE_RE.finditer(diff_text):
        sign = diff_text[m.start()]
        val  = m.group(2)
        if sign == "-":
            old_inst = val
        elif sign == "+":
            new_inst = val

    if old_inst and new_inst and old_inst != new_inst:
        old_cost = _node_monthly_cost(old_inst, "aws")
        new_cost = _node_monthly_cost(new_inst, "aws")

        # How many nodes?
        node_count = 1
        for m in _NODE_COUNT_RE.finditer(diff_text):
            if diff_text[m.start()] == "+":
                node_count = int(m.group(1))
                break

        if old_cost > 0 and new_cost > 0:
            inst_delta = round((new_cost - old_cost) * node_count, 2)
            delta += inst_delta
            confidence = "high"
            changes.append(
                f"instanceType {old_inst} (${old_cost:,.0f}/mo) → "
                f"{new_inst} (${new_cost:,.0f}/mo) × {node_count} nodes "
                f"(~{'+' if inst_delta >= 0 else ''}${inst_delta:,.0f}/mo)"
            )
        else:
            changes.append(f"instanceType {old_inst} → {new_inst} (price unknown)")
            confidence = "low"

    # ── Node count change ─────────────────────────────────────────────────────
    old_nodes = new_nodes = None
    for m in _NODE_COUNT_RE.finditer(diff_text):
        sign = diff_text[m.start()]
        val  = int(m.group(1))
        if sign == "-":
            old_nodes = val
        elif sign == "+":
            new_nodes = val

    if old_nodes is not None and new_nodes is not None and old_nodes != new_nodes and not old_inst:
        # No instance type change — estimate with a generic m5.large
        node_cost  = _EC2_MONTHLY.get("m5.large", 70.0)
        node_delta = round((new_nodes - old_nodes) * node_cost, 2)
        delta += node_delta
        changes.append(
            f"nodeCount {old_nodes} → {new_nodes} "
            f"(~{'+' if node_delta >= 0 else ''}${node_delta:,.0f}/mo)"
        )
        confidence = "low"

    return HelmCostDiff(
        release_name=release_name,
        delta_monthly_usd=round(delta, 2),
        changes=changes,
        confidence=confidence,
    )


def format_helm_diff_comment(diff: HelmCostDiff, threshold_usd: float = 10.0) -> str | None:
    """Format a GitHub PR comment for a helm diff cost change."""
    if abs(diff.delta_monthly_usd) < threshold_usd or not diff.changes:
        return None

    sign = "+" if diff.delta_monthly_usd >= 0 else ""
    lines = [
        f"**💰 nable cost estimate — `{diff.release_name}`**\n",
        "| Change | Est. impact / month |",
        "|---|---|",
    ]
    for c in diff.changes:
        # Split into description and amount
        if "(~" in c:
            desc, amt = c.rsplit("(~", 1)
            amt = amt.rstrip(")")
            lines.append(f"| {desc.strip()} | {amt} |")
        else:
            lines.append(f"| {c} | — |")

    conf_note = "" if diff.confidence == "high" else " ⁽ᵉˢᵗ⁾"
    lines.append(
        f"| | **{sign}${abs(diff.delta_monthly_usd):,.0f}/month{conf_note}** |"
    )
    lines.append("")
    lines.append("*[nable](https://getnable.com)*")
    return "\n".join(lines)
