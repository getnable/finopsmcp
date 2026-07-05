"""
GCP native Recommender API pull.

The waste audit (``gcp_waste``) enumerates resources ourselves and applies
list-price estimates. This module is the deeper, GCP-native counterpart: it asks
Google's own Recommender API, which runs ML on 8+ days of real usage and returns
cost projections priced against your actual SKU rates (including any committed-use
discounts already in effect). It covers recommenders we cannot approximate by
scanning: machine-type rightsizing, committed-use-discount purchases, Cloud SQL
idle/overprovisioned, and Cloud Run cost tuning.

Why keep both? The scanner works with only Compute + Monitoring read scope and
returns instantly; the Recommender API needs the Recommender role and only lights
up once Google has enough usage history, but when it does it is more precise and
wider. Run the scanner for coverage-day-one, the Recommender for depth.

Each recommender maps to a trust envelope:
  - idle_* recommenders  : Google measured near-zero usage over its window ->
                           MEASURED -> recommendation (still confirm-first, deletes
                           are destructive).
  - machine_type / sql   : measured utilization, but the resize depends on the
                           workload's headroom -> MEASURED, medium confidence.
  - committed_use        : projected savings assume usage stays flat for the
                           commitment term -> INFERRED -> investigation.

The single seam the tests patch is ``_list_recommendations(project, recommender)``,
so the suite never needs the google-cloud-recommender SDK installed.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .envelope import INFERRED, MEASURED, Finding

log = logging.getLogger(__name__)

# GCP project-id format (lowercase, 6-30 chars). Validated before a project id
# goes into any API path.
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")

# Recommender ids -> how we present them. evidence/confidence drive whether the
# finding is a recommendation or an investigation (see envelope.classify).
#   category   : stable slug for bucketing
#   display    : human label for the report
#   evidence   : MEASURED | INFERRED
#   confidence : high | medium | low
#   title/why  : filled per finding; here we hold the reusable why-suffix
RECOMMENDERS: dict[str, dict[str, str]] = {
    "google.compute.instance.IdleResourceRecommender": {
        "category": "idle_vm",
        "display": "Idle VM",
        "evidence": MEASURED,
        "confidence": "high",
        "why": (
            "Google measured near-zero utilization on this VM over its analysis "
            "window and projects you are paying for compute that does no work."
        ),
    },
    "google.compute.disk.IdleResourceRecommender": {
        "category": "idle_disk",
        "display": "Idle persistent disk",
        "evidence": MEASURED,
        "confidence": "high",
        "why": (
            "Google flags this persistent disk as unused. GCP bills provisioned "
            "disk size whether or not anything reads or writes it."
        ),
    },
    "google.compute.address.IdleResourceRecommender": {
        "category": "idle_ip",
        "display": "Idle external IP",
        "evidence": MEASURED,
        "confidence": "high",
        "why": (
            "Google flags this reserved external IP as not in use. A reserved IP "
            "that is attached to nothing is billed for buying nothing."
        ),
    },
    "google.compute.image.IdleResourceRecommender": {
        "category": "idle_image",
        "display": "Idle image",
        "evidence": MEASURED,
        "confidence": "medium",
        "why": (
            "Google flags this custom image as unused. Image storage is billed per "
            "GB-month for something nothing is deploying from."
        ),
    },
    "google.compute.instance.MachineTypeRecommender": {
        "category": "vm_rightsizing",
        "display": "VM rightsizing",
        "evidence": MEASURED,
        "confidence": "medium",
        "why": (
            "Google measured this VM's CPU and memory headroom and recommends a "
            "smaller machine type that still covers observed peaks."
        ),
    },
    "google.cloudsql.instance.IdleRecommender": {
        "category": "idle_cloudsql",
        "display": "Idle Cloud SQL instance",
        "evidence": MEASURED,
        "confidence": "high",
        "why": (
            "Google flags this Cloud SQL instance as idle: no connections or "
            "queries over its window while you pay for the provisioned instance."
        ),
    },
    "google.cloudsql.instance.OverprovisionedRecommender": {
        "category": "overprovisioned_cloudsql",
        "display": "Overprovisioned Cloud SQL",
        "evidence": MEASURED,
        "confidence": "medium",
        "why": (
            "Google measured this Cloud SQL instance's CPU and memory usage and "
            "recommends a smaller tier that still fits the workload."
        ),
    },
    "google.run.service.CostRecommender": {
        "category": "cloud_run_cost",
        "display": "Cloud Run cost tuning",
        "evidence": MEASURED,
        "confidence": "medium",
        "why": (
            "Google recommends a CPU/memory or concurrency change on this Cloud Run "
            "service to cut cost while holding the observed load."
        ),
    },
    "google.compute.commitment.UsageCommitmentRecommender": {
        "category": "committed_use",
        "display": "Committed-use discount",
        "evidence": INFERRED,
        "confidence": "medium",
        "why": (
            "Google projects savings from buying a committed-use discount to cover "
            "steady compute usage. The savings hold only if that usage stays flat "
            "for the full commitment term, so this is a call, not a cleanup."
        ),
    },
}

# Recommendation lifecycle states. We only surface ACTIVE ones; CLAIMED/SUCCEEDED/
# DISMISSED are already handled and would be noise.
_STATE_ACTIVE = "ACTIVE"

# GCP projects committed-use savings over the commitment term (1 or 3 years). Idle
# and rightsizing recommendations project over ~30 days. We normalize every dollar
# figure to a monthly number using the projection duration so findings are
# comparable in one list.
_MONTH_SECONDS = 30.0 * 86400.0

# Per-recommender remediation. Every one is confirm-first: the Recommender API is
# advice, and deletes/resizes are the user's to make, never ours.
_REMEDIATION: dict[str, list[str]] = {
    "idle_vm": [
        "Confirm the VM is not a warm standby or a rarely-triggered job, then stop "
        "it (keeps the disk) or delete it.",
        "Stopping halts compute billing immediately while preserving the disk for a "
        "later restart.",
    ],
    "idle_disk": [
        "Confirm no one is holding the disk for a planned restore, then snapshot it "
        "and delete the disk.",
        "Snapshot storage is a fraction of a live disk, so snapshot-then-delete keeps "
        "the data recoverable while stopping the disk charge.",
    ],
    "idle_ip": [
        "Confirm the address is not reserved for an imminent launch, then release it. "
        "The charge stops as soon as it is released.",
    ],
    "idle_image": [
        "Confirm nothing deploys from this image (no instance template or pipeline "
        "references it), then delete it.",
    ],
    "vm_rightsizing": [
        "Review Google's suggested machine type against your own headroom needs, then "
        "resize during a maintenance window.",
        "Rightsizing changes the machine type in place; validate the workload holds "
        "at the smaller size before making it permanent.",
    ],
    "idle_cloudsql": [
        "Confirm the instance is not a low-traffic-but-required database, then stop or "
        "delete it after taking a final backup.",
    ],
    "overprovisioned_cloudsql": [
        "Review the recommended tier against your peak load, then change the instance "
        "tier during a maintenance window.",
    ],
    "cloud_run_cost": [
        "Apply Google's suggested CPU/memory or concurrency setting to the service and "
        "watch latency and error rate after the change.",
    ],
    "committed_use": [
        "Model the commitment against your forecast: only buy the discount for usage "
        "you are confident stays on for the full term.",
        "A committed-use discount is a one-way financial commitment; over-buying locks "
        "in spend for capacity you may not use.",
    ],
}

PRICING_BASIS = (
    "Figures come from Google's Recommender API cost projections, priced against "
    "your account's actual SKU rates and normalized to a 30-day month."
)


# ── money + duration parsing ───────────────────────────────────────────────────

def _money_units(cost: Any) -> float:
    """A google.type.Money is units (int) + nanos (billionths). Cost-savings
    projections are negative; the caller decides sign handling."""
    if cost is None:
        return 0.0
    units = float(getattr(cost, "units", 0) or 0)
    nanos = float(getattr(cost, "nanos", 0) or 0)
    return units + nanos / 1e9


def _duration_seconds(duration: Any) -> float:
    """Read a google.protobuf.Duration (or a timedelta) as seconds. Defaults to a
    30-day month when absent so a missing duration never divides by zero."""
    if duration is None:
        return _MONTH_SECONDS
    total = getattr(duration, "total_seconds", None)
    if callable(total):
        try:
            secs = float(total())
            return secs if secs > 0 else _MONTH_SECONDS
        except Exception:
            return _MONTH_SECONDS
    secs = float(getattr(duration, "seconds", 0) or 0)
    secs += float(getattr(duration, "nanos", 0) or 0) / 1e9
    return secs if secs > 0 else _MONTH_SECONDS


def _monthly_savings(rec: Any) -> tuple[float, str]:
    """Return (monthly_savings_usd, currency) for one recommendation.

    Cost-savings recommendations carry a negative cost over a projection duration.
    We negate to a positive saving and normalize to a 30-day month so a 3-year CUD
    and a 30-day idle disk are comparable in the same list. Non-savings (e.g. a
    reliability recommendation with zero/positive cost) yield 0.0.
    """
    impact = getattr(rec, "primary_impact", None)
    proj = getattr(impact, "cost_projection", None) if impact is not None else None
    if proj is None:
        return 0.0, "USD"
    cost = getattr(proj, "cost", None)
    amount = _money_units(cost)
    currency = getattr(cost, "currency_code", "") or "USD"
    if amount >= 0:  # not a saving (or nothing to save)
        return 0.0, currency
    secs = _duration_seconds(getattr(proj, "duration", None))
    monthly = (-amount) * (_MONTH_SECONDS / secs)
    return round(monthly, 2), currency


def _state_name(rec: Any) -> str:
    """Recommendation state as an upper-case string across proto/enum/plain shapes."""
    info = getattr(rec, "state_info", None)
    state = getattr(info, "state", None) if info is not None else None
    if state is None:
        return _STATE_ACTIVE  # be permissive: unlabelled stand-ins count as active
    name = getattr(state, "name", None)
    return str(name if name is not None else state).upper()


# ── envelope mapping ───────────────────────────────────────────────────────────

def _target_resource(rec: Any) -> str:
    """Best-effort resource id for the finding: the recommendation name's tail is
    a stable id; Google's description names the resource in prose."""
    name = str(getattr(rec, "name", "") or "")
    return name.rsplit("/", 1)[-1] if name else ""


def _finding_for(project: str, recommender: str, rec: Any) -> dict | None:
    meta = RECOMMENDERS[recommender]
    monthly, currency = _monthly_savings(rec)
    category = meta["category"]
    rid = _target_resource(rec)
    description = str(getattr(rec, "description", "") or "").strip()

    # Idle/rightsizing findings with no positive saving carry no dollar signal; we
    # drop them rather than show a $0 recommendation. Committed-use is only ever
    # worth surfacing when it saves money too.
    if monthly <= 0:
        return None

    display = meta["display"]
    title = f"{display}: {description}" if description else f"{display} recommendation"
    if len(title) > 160:
        title = title[:157] + "..."

    env = Finding(
        source="gcp_recommender",
        title=title,
        why=meta["why"],
        evidence=meta["evidence"],
        confidence=meta["confidence"],
        why_unsure=(
            "Google projects this saving from past usage; it holds only if the "
            "workload's pattern stays the same."
            if meta["evidence"] == INFERRED else ""
        ),
        est_monthly_savings=monthly,
        remediation=list(_REMEDIATION.get(category, [])),
        resource_id=rid,
        metadata={
            "project": project,
            "recommender": recommender,
            "recommender_subtype": str(getattr(rec, "recommender_subtype", "") or ""),
            "google_description": description,
            "currency": currency,
        },
    )

    return {
        "project": project,
        "category": category,
        "display": display,
        "resource_id": rid,
        "recommender": recommender,
        "description": description,
        "estimated_monthly_savings": monthly,
        "severity": _severity_for_savings(monthly),
        "currency": currency,
        "finding": env.to_dict(),
    }


def _severity_for_savings(monthly: float) -> str:
    if monthly >= 500:
        return "high"
    if monthly >= 50:
        return "medium"
    return "low"


# ── SDK seam (patched in tests) ────────────────────────────────────────────────

def _list_recommendations(project: str, recommender: str) -> list[Any]:
    """List ACTIVE cost recommendations for one recommender across all locations.

    Uses the ``-`` location wildcard so we do not enumerate every zone/region. This
    is the single function the tests patch; it is the only place the
    google-cloud-recommender SDK is imported.
    """
    from google.cloud import recommender_v1

    client = recommender_v1.RecommenderClient()
    parent = f"projects/{project}/locations/-/recommenders/{recommender}"
    return list(client.list_recommendations(parent=parent))


# ── public API ─────────────────────────────────────────────────────────────────

async def get_gcp_recommendations(
    gcp_client: Any,
    projects: list[str] | None = None,
    recommenders: list[str] | None = None,
) -> dict:
    """
    Pull Google's native Recommender API cost recommendations for GCP projects.

    gcp_client:   a GCPConnector (used for project_ids() when projects is None).
    projects:     explicit project IDs; defaults to gcp_client.project_ids().
    recommenders: subset of the RECOMMENDERS keys; defaults to all cost recommenders.
    """
    if not projects:
        getter = getattr(gcp_client, "project_ids", None)
        projects = list(getter() if callable(getter) else [])
    projects = [p for p in projects if _PROJECT_RE.match(p or "")]
    if not projects:
        return {
            "error": "No valid GCP project IDs found. Set GCP_PROJECT_IDS "
                     "(comma-separated) or configure Application Default Credentials "
                     "with a default project.",
        }

    run = [r for r in (recommenders or list(RECOMMENDERS)) if r in RECOMMENDERS]
    if not run:
        run = list(RECOMMENDERS)

    findings: list[dict] = []
    errors: list[dict] = []

    async def _scan(project: str, recommender: str) -> None:
        try:
            recs = await asyncio.to_thread(_list_recommendations, project, recommender)
        except Exception as e:  # per-recommender: a missing API/role must not sink the rest
            errors.append({
                "project": project,
                "recommender": recommender,
                "error": str(e),
            })
            return
        for rec in recs:
            if _state_name(rec) != _STATE_ACTIVE:
                continue
            f = _finding_for(project, recommender, rec)
            if f:
                findings.append(f)

    await asyncio.gather(*[
        _scan(p, r) for p in projects for r in run
    ])

    findings.sort(key=lambda f: f.get("estimated_monthly_savings", 0), reverse=True)

    by_category: dict[str, dict] = {}
    by_severity: dict[str, dict] = {}
    by_project: dict[str, dict] = {}
    total_monthly = 0.0
    for f in findings:
        m = f.get("estimated_monthly_savings", 0) or 0
        total_monthly += m
        for bucket, key in ((by_category, f["category"]),
                            (by_severity, f["severity"]),
                            (by_project, f["project"])):
            slot = bucket.setdefault(key, {"count": 0, "monthly_savings": 0.0})
            slot["count"] += 1
            slot["monthly_savings"] = round(slot["monthly_savings"] + m, 2)

    # If every recommender errored the same way (API disabled, role missing), say so
    # plainly instead of returning an empty, healthy-looking report.
    hint = None
    if not findings and errors and len(errors) == len(projects) * len(run):
        hint = (
            "No recommendations returned and every call errored. The Recommender API "
            "is likely not enabled (recommender.googleapis.com) or the credentials "
            "lack the Recommender Viewer role (roles/recommender.viewer). Enable both, "
            "then retry. Note the API only surfaces recommendations after ~8 days of "
            "usage history."
        )

    return {
        "provider": "gcp",
        "source": "recommender_api",
        "projects_scanned": projects,
        "recommenders_run": run,
        "pricing_basis": PRICING_BASIS,
        "findings": findings,
        "total_findings": len(findings),
        "total_estimated_monthly_savings": round(total_monthly, 2),
        "total_estimated_annual_savings": round(total_monthly * 12, 2),
        "by_category": by_category,
        "by_severity": by_severity,
        "by_project": by_project,
        "errors": errors,
        **({"setup_hint": hint} if hint else {}),
    }
