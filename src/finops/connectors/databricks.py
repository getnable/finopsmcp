"""
Databricks connector for nable FinOps.

Pulls DBU consumption and cost data from the Databricks REST API (v2.0 / v2.1).
Surfaces cluster-level spend, job costs, workspace totals, and efficiency signals
(idle clusters, autoscale utilisation, auto-termination missing).

Required env vars:
    DATABRICKS_HOST    -- workspace URL, e.g. https://adb-1234567890.1.azuredatabricks.net
    DATABRICKS_TOKEN   -- personal access token or service-principal OAuth token

Optional env vars:
    DATABRICKS_ACCOUNT_ID  -- account-level billing API (for multi-workspace orgs)
    DATABRICKS_ACCOUNT_TOKEN -- service-principal token for account console
    DATABRICKS_DBU_PRICE   -- override $/DBU for cost estimates (default 0.40)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from .base import BaseConnector, CostEntry, CostSummary

# ── Typing helpers ─────────────────────────────────────────────────────────────

@dataclass
class ClusterEfficiency:
    cluster_id: str
    cluster_name: str
    state: str                    # "RUNNING" | "TERMINATED" | "TERMINATING" | ...
    cluster_type: str             # "ALL_PURPOSE" | "JOB" | "SQL" | "PIPELINE"
    autotermination_minutes: int  # 0 = disabled (BAD)
    autoscale_enabled: bool
    autoscale_min: int
    autoscale_max: int
    num_workers: int
    dbu_per_hour: float
    uptime_hours: float
    estimated_cost_usd: float
    driver_node_type: str
    worker_node_type: str
    creator: str
    tags: dict[str, str]
    issues: list[str]             # human-readable efficiency warnings

@dataclass
class JobCost:
    job_id: int
    job_name: str
    run_id: int
    state: str
    cluster_type: str
    dbu_consumed: float
    estimated_cost_usd: float
    duration_seconds: int
    start_time: str
    tags: dict[str, str]

@dataclass
class DatabricksWorkspaceSummary:
    workspace_id: str
    workspace_name: str
    host: str
    total_dbu: float
    estimated_cost_usd: float
    by_cluster_type: dict[str, float]    # cluster_type -> USD
    by_cluster: dict[str, float]         # cluster_name -> USD
    by_job: dict[str, float]             # job_name -> USD
    cluster_efficiencies: list[ClusterEfficiency]
    job_costs: list[JobCost]
    idle_clusters: list[ClusterEfficiency]
    missing_autotermination: list[ClusterEfficiency]


# ── DBU pricing (list prices — customers override via DATABRICKS_DBU_PRICE) ───

# Approximate Databricks list pricing in USD/DBU
# Real deployments vary by cloud, region, SKU, and commit tier.
_DEFAULT_DBU_PRICE = 0.40   # conservative midpoint across AWS/Azure/GCP

_CLUSTER_TYPE_DBU_MULTIPLIER: dict[str, float] = {
    "ALL_PURPOSE":  1.0,   # interactive/all-purpose clusters (most expensive)
    "JOB":          0.15,  # job clusters (cheapest per DBU)
    "SQL":          0.22,  # Databricks SQL warehouse (Serverless slightly higher)
    "PIPELINE":     0.20,  # Delta Live Tables
    "UNKNOWN":      1.0,
}

# Rough DBU/hour per instance family (used when the API doesn't give DBU rate)
_NODE_DBU_MAP: dict[str, float] = {
    "m5.xlarge": 0.75,   "m5.2xlarge": 1.5,   "m5.4xlarge": 3.0,
    "m5.8xlarge": 6.0,   "m5.12xlarge": 9.0,  "m5.24xlarge": 18.0,
    "m6i.xlarge": 0.75,  "m6i.2xlarge": 1.5,  "m6i.4xlarge": 3.0,
    "i3.xlarge": 0.75,   "i3.2xlarge": 1.5,   "i3.4xlarge": 3.0,
    "r5.xlarge": 0.75,   "r5.2xlarge": 1.5,   "r5.4xlarge": 3.0,
    # Azure
    "Standard_DS3_v2": 0.75,  "Standard_DS4_v2": 1.5,  "Standard_DS5_v2": 3.0,
    "Standard_D8s_v3": 1.5,   "Standard_D16s_v3": 3.0, "Standard_D32s_v3": 6.0,
    # GCP
    "n1-standard-4": 0.75,  "n1-standard-8": 1.5,  "n1-standard-16": 3.0,
    "n2-standard-4": 0.75,  "n2-standard-8": 1.5,  "n2-standard-16": 3.0,
}


def _estimate_dbu_per_hour(node_type: str, num_workers: int) -> float:
    """Rough DBU/hour estimate from node type * worker count."""
    node_dbu = _NODE_DBU_MAP.get(node_type, 1.0)
    # +1 for driver
    return node_dbu * (num_workers + 1)


def _ms_to_hours(ms: int) -> float:
    return ms / 3_600_000


class DatabricksConnector(BaseConnector):
    """
    Connects to one Databricks workspace via REST API.

    Exposes:
    - get_costs()                 — workspace-level CostSummary (BaseConnector contract)
    - get_workspace_summary()     — full DatabricksWorkspaceSummary with cluster details
    - get_cluster_efficiency()    — list of ClusterEfficiency objects
    - get_job_costs()             — recent job run costs
    - list_accounts()             — [{id, name}] for the workspace
    """

    provider = "databricks"

    def __init__(self) -> None:
        self._host = os.getenv("DATABRICKS_HOST", "").rstrip("/")
        self._token = os.getenv("DATABRICKS_TOKEN", "")
        self._account_id = os.getenv("DATABRICKS_ACCOUNT_ID", "")
        self._account_token = os.getenv("DATABRICKS_ACCOUNT_TOKEN", self._token)
        self._dbu_price = float(os.getenv("DATABRICKS_DBU_PRICE", str(_DEFAULT_DBU_PRICE)))

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _account_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._account_token}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self._host}/api/{path}"

    # ── BaseConnector ─────────────────────────────────────────────────────────

    async def is_configured(self) -> bool:
        return bool(self._host and self._token)

    async def list_accounts(self) -> list[dict[str, str]]:
        """Return the workspace as a single pseudo-account."""
        try:
            workspace_id = self._host.split("adb-")[-1].split(".")[0] if "adb-" in self._host else "workspace"
            return [{"id": workspace_id, "name": self._host.replace("https://", "")}]
        except Exception:
            return [{"id": "databricks", "name": "Databricks Workspace"}]

    async def get_costs(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
        group_by: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> CostSummary:
        """
        Returns a CostSummary rolled up from cluster and job run data.

        Uses the cluster list + run history to estimate costs when the
        account-level Billable Usage API is not available.
        """
        if not await self.is_configured():
            return CostSummary(
                provider="databricks",
                start_date=start_date,
                end_date=end_date,
                total_usd=0.0,
                by_service={},
                by_account={},
                by_region={},
                entries=[],
            )

        try:
            summary = await self._try_billable_usage_api(start_date, end_date)
            if summary:
                return summary
        except Exception:
            pass

        # Fall back to estimating from cluster/job data
        return await self._estimated_costs_from_clusters(start_date, end_date)

    # ── Billable Usage API (account-level, requires account admin) ────────────

    async def _try_billable_usage_api(
        self, start_date: date, end_date: date
    ) -> CostSummary | None:
        """
        Use the account-level Billable Usage Download API if an account ID is set.
        Returns None if not available (falls back to estimation).
        """
        if not self._account_id:
            return None

        base = f"https://accounts.azuredatabricks.net/api/2.0/accounts/{self._account_id}"
        params = {
            "start_month": start_date.strftime("%Y-%m"),
            "end_month": end_date.strftime("%Y-%m"),
            "personal_data": "false",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{base}/usage/download",
                headers=self._account_headers(),
                params=params,
            )
            if r.status_code != 200:
                return None

        # Response is CSV: workspace_id, sku_name, cloud, usage_start_time,
        # usage_end_time, usage_quantity, usage_unit, usage_metadata.*
        lines = r.text.strip().split("\n")
        if len(lines) < 2:
            return None

        headers = [h.strip() for h in lines[0].split(",")]

        def col(row: list[str], name: str) -> str:
            try:
                return row[headers.index(name)].strip().strip('"')
            except (ValueError, IndexError):
                return ""

        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        by_account: dict[str, float] = {}
        total = 0.0

        for line in lines[1:]:
            if not line.strip():
                continue
            row = line.split(",")
            sku = col(row, "sku_name") or "DATABRICKS_UNKNOWN"
            quantity = float(col(row, "usage_quantity") or 0)
            cost = quantity * self._dbu_price
            workspace = col(row, "workspace_id") or "workspace"
            total += cost
            by_service[sku] = by_service.get(sku, 0.0) + cost
            by_account[workspace] = by_account.get(workspace, 0.0) + cost
            entries.append(CostEntry(
                provider="databricks",
                account_id=workspace,
                account_name=workspace,
                service=sku,
                region=col(row, "cloud") or "",
                amount=cost,
                metadata={"dbu": quantity, "sku": sku},
            ))

        return CostSummary(
            provider="databricks",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account=by_account,
            by_region={},
            entries=entries,
        )

    # ── Cluster-based cost estimation ─────────────────────────────────────────

    async def _estimated_costs_from_clusters(
        self, start_date: date, end_date: date
    ) -> CostSummary:
        """Estimate costs from cluster list and recent runs (no account API needed)."""
        async with httpx.AsyncClient(timeout=30) as client:
            clusters = await self._list_clusters(client)
            runs = await self._list_recent_runs(client, start_date, end_date)

        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        by_account: dict[str, float] = {}
        total = 0.0

        cluster_map = {c["cluster_id"]: c for c in clusters}

        # Cost from job runs (more precise — uses actual run duration)
        for run in runs:
            cluster_type = run.get("cluster_spec", {}).get("job_cluster_key", "JOB")
            node_type = (
                run.get("cluster_spec", {})
                   .get("new_cluster", {})
                   .get("node_type_id", "")
            )
            num_workers = (
                run.get("cluster_spec", {})
                   .get("new_cluster", {})
                   .get("num_workers", 1)
            )
            duration_ms = run.get("execution_duration", 0) or run.get("run_duration", 0)
            hours = _ms_to_hours(duration_ms)
            dbu_hr = _estimate_dbu_per_hour(node_type, num_workers)
            dbu = dbu_hr * hours
            mult = _CLUSTER_TYPE_DBU_MULTIPLIER.get("JOB", 0.15)
            cost = dbu * mult * self._dbu_price
            job_name = run.get("run_name", str(run.get("job_id", "unknown")))
            total += cost
            by_service["Jobs"] = by_service.get("Jobs", 0.0) + cost
            by_account["workspace"] = by_account.get("workspace", 0.0) + cost
            entries.append(CostEntry(
                provider="databricks",
                account_id="workspace",
                account_name=self._host.replace("https://", ""),
                service="Jobs",
                region="",
                amount=cost,
                tags=run.get("run_tags", {}),
                metadata={"job_id": run.get("job_id"), "run_id": run.get("run_id"), "dbu": dbu},
            ))

        # Cost from all-purpose / interactive clusters (estimate from uptime)
        for cluster in clusters:
            ctype = cluster.get("cluster_source", "UNKNOWN")
            if ctype in ("JOB_LAUNCHER", "JOB"):
                continue  # already counted via runs
            node_type = cluster.get("node_type_id", "")
            num_workers = cluster.get("num_workers", 1)
            # uptime from cluster state transitions — use start_time if available
            created_ms = cluster.get("start_time", 0)
            last_activity = cluster.get("last_activity_time", created_ms)
            # Bound uptime to requested date range
            range_start_ms = int(datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc).timestamp() * 1000)
            range_end_ms = int(datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)
            effective_start = max(created_ms, range_start_ms)
            effective_end = min(last_activity or range_end_ms, range_end_ms)
            hours = max(0.0, _ms_to_hours(effective_end - effective_start))
            dbu_hr = _estimate_dbu_per_hour(node_type, num_workers)
            dbu = dbu_hr * hours
            mult = _CLUSTER_TYPE_DBU_MULTIPLIER.get("ALL_PURPOSE", 1.0)
            cost = dbu * mult * self._dbu_price
            if cost < 0.01:
                continue
            svc = "All-Purpose Compute"
            total += cost
            by_service[svc] = by_service.get(svc, 0.0) + cost
            by_account["workspace"] = by_account.get("workspace", 0.0) + cost
            entries.append(CostEntry(
                provider="databricks",
                account_id="workspace",
                account_name=self._host.replace("https://", ""),
                service=svc,
                region="",
                amount=cost,
                tags=cluster.get("custom_tags", {}),
                metadata={
                    "cluster_id": cluster["cluster_id"],
                    "cluster_name": cluster.get("cluster_name", ""),
                    "dbu": dbu,
                },
            ))

        return CostSummary(
            provider="databricks",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account=by_account,
            by_region={},
            entries=entries,
        )

    # ── Public extras (called from server.py tools) ───────────────────────────

    async def get_workspace_summary(
        self, start_date: date, end_date: date
    ) -> DatabricksWorkspaceSummary:
        """Full workspace cost + efficiency rollup."""
        async with httpx.AsyncClient(timeout=30) as client:
            clusters = await self._list_clusters(client)
            runs = await self._list_recent_runs(client, start_date, end_date)
            jobs = await self._list_jobs(client)

        job_id_to_name: dict[int, str] = {j["job_id"]: j.get("settings", {}).get("name", str(j["job_id"])) for j in jobs}

        efficiencies = self._compute_cluster_efficiency(clusters)
        job_costs = self._compute_job_costs(runs, job_id_to_name)

        by_cluster_type: dict[str, float] = {}
        by_cluster: dict[str, float] = {}
        by_job: dict[str, float] = {}
        total_dbu = 0.0
        total_cost = 0.0

        for e in efficiencies:
            by_cluster_type[e.cluster_type] = by_cluster_type.get(e.cluster_type, 0.0) + e.estimated_cost_usd
            by_cluster[e.cluster_name] = by_cluster.get(e.cluster_name, 0.0) + e.estimated_cost_usd
            total_dbu += e.dbu_per_hour * e.uptime_hours
            total_cost += e.estimated_cost_usd

        for jc in job_costs:
            by_job[jc.job_name] = by_job.get(jc.job_name, 0.0) + jc.estimated_cost_usd
            total_dbu += jc.dbu_consumed
            total_cost += jc.estimated_cost_usd

        idle = [e for e in efficiencies if "idle" in " ".join(e.issues).lower() or e.state == "RUNNING" and e.autotermination_minutes == 0]
        missing_at = [e for e in efficiencies if e.autotermination_minutes == 0 and e.cluster_type == "ALL_PURPOSE"]

        workspace_id = self._host.split("adb-")[-1].split(".")[0] if "adb-" in self._host else "workspace"

        return DatabricksWorkspaceSummary(
            workspace_id=workspace_id,
            workspace_name=self._host.replace("https://", ""),
            host=self._host,
            total_dbu=round(total_dbu, 2),
            estimated_cost_usd=round(total_cost, 2),
            by_cluster_type={k: round(v, 2) for k, v in by_cluster_type.items()},
            by_cluster={k: round(v, 2) for k, v in sorted(by_cluster.items(), key=lambda x: -x[1])[:20]},
            by_job={k: round(v, 2) for k, v in sorted(by_job.items(), key=lambda x: -x[1])[:20]},
            cluster_efficiencies=efficiencies,
            job_costs=job_costs,
            idle_clusters=idle,
            missing_autotermination=missing_at,
        )

    async def get_cluster_efficiency(self) -> list[ClusterEfficiency]:
        async with httpx.AsyncClient(timeout=30) as client:
            clusters = await self._list_clusters(client)
        return self._compute_cluster_efficiency(clusters)

    async def get_job_costs(
        self, start_date: date, end_date: date
    ) -> list[JobCost]:
        async with httpx.AsyncClient(timeout=30) as client:
            runs = await self._list_recent_runs(client, start_date, end_date)
            jobs = await self._list_jobs(client)
        job_id_to_name = {j["job_id"]: j.get("settings", {}).get("name", str(j["job_id"])) for j in jobs}
        return self._compute_job_costs(runs, job_id_to_name)

    # ── API calls ─────────────────────────────────────────────────────────────

    async def _list_clusters(self, client: httpx.AsyncClient) -> list[dict]:
        r = await client.get(self._url("2.0/clusters/list"), headers=self._headers())
        if r.status_code == 200:
            return r.json().get("clusters", [])
        return []

    async def _list_jobs(self, client: httpx.AsyncClient) -> list[dict]:
        jobs = []
        offset = 0
        limit = 100
        while True:
            r = await client.get(
                self._url("2.1/jobs/list"),
                headers=self._headers(),
                params={"limit": limit, "offset": offset, "expand_tasks": "false"},
            )
            if r.status_code != 200:
                break
            data = r.json()
            batch = data.get("jobs", [])
            jobs.extend(batch)
            if not data.get("has_more"):
                break
            offset += limit
        return jobs

    async def _list_recent_runs(
        self, client: httpx.AsyncClient, start_date: date, end_date: date
    ) -> list[dict]:
        """Fetch completed job runs in the date window (last 60 days max)."""
        start_ms = int(datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc).timestamp() * 1000)
        end_ms = int(datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)

        runs = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "completed_only": "true",
                "limit": 100,
                "start_time_from": start_ms,
                "start_time_to": end_ms,
            }
            if page_token:
                params["page_token"] = page_token

            r = await client.get(
                self._url("2.1/jobs/runs/list"),
                headers=self._headers(),
                params=params,
            )
            if r.status_code != 200:
                break
            data = r.json()
            runs.extend(data.get("runs", []))
            page_token = data.get("next_page_token")
            if not page_token or not data.get("has_more"):
                break

        return runs

    # ── Efficiency computation ─────────────────────────────────────────────────

    def _compute_cluster_efficiency(self, clusters: list[dict]) -> list[ClusterEfficiency]:
        results = []
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        for c in clusters:
            cid = c.get("cluster_id", "")
            name = c.get("cluster_name", cid)
            state = c.get("state", "UNKNOWN")
            source = c.get("cluster_source", "UNKNOWN")
            auto_min = c.get("autotermination_minutes", 0)
            autoscale = c.get("autoscale", {})
            auto_enabled = bool(autoscale)
            auto_min_w = autoscale.get("min_workers", 0)
            auto_max_w = autoscale.get("max_workers", 0)
            num_workers = c.get("num_workers", auto_min_w)
            node_type = c.get("node_type_id", "")
            driver_type = c.get("driver_node_type_id", node_type)
            creator = c.get("creator_user_name", "unknown")
            tags: dict[str, str] = c.get("custom_tags", {})

            # Cluster type classification
            if source in ("JOB_LAUNCHER", "JOB"):
                cluster_type = "JOB"
            elif "sql" in name.lower() or source == "SQL":
                cluster_type = "SQL"
            elif source == "PIPELINE":
                cluster_type = "PIPELINE"
            else:
                cluster_type = "ALL_PURPOSE"

            # Uptime estimate
            start_ms = c.get("start_time", now_ms)
            last_ms = c.get("last_activity_time") or now_ms
            uptime_h = max(0.0, _ms_to_hours(last_ms - start_ms))

            dbu_hr = _estimate_dbu_per_hour(node_type, num_workers)
            mult = _CLUSTER_TYPE_DBU_MULTIPLIER.get(cluster_type, 1.0)
            cost = dbu_hr * mult * self._dbu_price * uptime_h

            # Efficiency issues
            issues: list[str] = []
            if state == "RUNNING":
                last_active_h = _ms_to_hours(now_ms - (c.get("last_activity_time") or now_ms))
                if last_active_h > 2 and cluster_type == "ALL_PURPOSE":
                    issues.append(f"Cluster idle for {last_active_h:.1f}h — consider terminating")
            if cluster_type == "ALL_PURPOSE" and auto_min == 0:
                issues.append("No auto-termination set — cluster can run indefinitely")
            if cluster_type == "ALL_PURPOSE" and not auto_enabled and num_workers > 4:
                issues.append(f"Autoscaling disabled with {num_workers} fixed workers — enable autoscale to reduce waste")
            if auto_enabled and auto_max_w > 0 and auto_min_w / max(auto_max_w, 1) < 0.2:
                issues.append(f"Autoscale range is very wide ({auto_min_w}-{auto_max_w}). Consider tightening for predictable jobs")
            if not tags:
                issues.append("No custom tags — hard to attribute cost to a team or project")

            results.append(ClusterEfficiency(
                cluster_id=cid,
                cluster_name=name,
                state=state,
                cluster_type=cluster_type,
                autotermination_minutes=auto_min,
                autoscale_enabled=auto_enabled,
                autoscale_min=auto_min_w,
                autoscale_max=auto_max_w,
                num_workers=num_workers,
                dbu_per_hour=round(dbu_hr * mult, 3),
                uptime_hours=round(uptime_h, 2),
                estimated_cost_usd=round(cost, 2),
                driver_node_type=driver_type,
                worker_node_type=node_type,
                creator=creator,
                tags=tags,
                issues=issues,
            ))

        return sorted(results, key=lambda x: -x.estimated_cost_usd)

    def _compute_job_costs(
        self,
        runs: list[dict],
        job_id_to_name: dict[int, str],
    ) -> list[JobCost]:
        results = []
        for run in runs:
            job_id = run.get("job_id", 0)
            run_id = run.get("run_id", 0)
            state_obj = run.get("state", {})
            state = state_obj.get("life_cycle_state", "UNKNOWN")
            job_name = job_id_to_name.get(job_id, run.get("run_name", str(job_id)))

            new_cluster = run.get("cluster_spec", {}).get("new_cluster", {})
            node_type = new_cluster.get("node_type_id", "")
            num_workers = new_cluster.get("num_workers", 1)
            duration_ms = run.get("execution_duration", 0) or run.get("run_duration", 0)
            hours = _ms_to_hours(duration_ms)

            dbu_hr = _estimate_dbu_per_hour(node_type, num_workers)
            mult = _CLUSTER_TYPE_DBU_MULTIPLIER.get("JOB", 0.15)
            dbu = dbu_hr * hours
            cost = dbu * mult * self._dbu_price

            start_ts = run.get("start_time", 0)
            start_str = (
                datetime.fromtimestamp(start_ts / 1000, tz=timezone.utc).isoformat()
                if start_ts else ""
            )

            results.append(JobCost(
                job_id=job_id,
                job_name=job_name,
                run_id=run_id,
                state=state,
                cluster_type="JOB",
                dbu_consumed=round(dbu, 3),
                estimated_cost_usd=round(cost, 4),
                duration_seconds=duration_ms // 1000,
                start_time=start_str,
                tags=run.get("run_tags", {}),
            ))

        return sorted(results, key=lambda x: -x.estimated_cost_usd)
