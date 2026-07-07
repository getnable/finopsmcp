"""
OpenCost as a Kubernetes cost source.

OpenCost (the CNCF project, the open core Kubecost donated) runs in the cluster,
scrapes usage, and prices it against real cloud rates, including GPU, network,
and persistent-volume storage that nable's built-in list-price allocator does
not cover. When a user runs OpenCost, nable reads its Allocation API and uses
those accurate numbers; when they do not, nable falls back to its own zero-setup
estimate. OpenCost is a data SOURCE here, like Cost Explorer or Datadog: nable
adds the agent interface, propose-only remediation, and the verify-and-learn
loop on top. nable never writes to OpenCost; it only reads.

Config:
  NABLE_OPENCOST_URL (or OPENCOST_URL) = base URL of the OpenCost API, e.g.
  http://localhost:9003 when port-forwarded, or the in-cluster service URL.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 20.0
# The idle/unallocated bucket OpenCost returns as an aggregation key.
_IDLE_KEY = "__idle__"


def opencost_url() -> str:
    """Base URL of the OpenCost API, or "" if not configured."""
    return (os.environ.get("NABLE_OPENCOST_URL")
            or os.environ.get("OPENCOST_URL")
            or "").strip().rstrip("/")


def is_configured() -> bool:
    """True when an OpenCost URL is set. Reachability is checked at fetch time."""
    return bool(opencost_url())


def _window_days(window: str) -> float | None:
    """Days in a simple 'Nd' / 'Nh' window, else None (for a monthly projection)."""
    m = re.fullmatch(r"(\d+)([dh])", window.strip())
    if not m:
        return None
    n = int(m.group(1))
    return n if m.group(2) == "d" else n / 24.0


def fetch_allocation(
    window: str = "7d",
    aggregate: str = "namespace",
    url: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict] | None:
    """
    Call the OpenCost Allocation API and return the accumulated allocation set
    (a dict keyed by aggregation key), wrapped in a list, or None on any failure
    so the caller can fall back to the built-in allocator.

    GET {url}/allocation/compute?window=&aggregate=&accumulate=true
    """
    base = (url or opencost_url()).rstrip("/")
    if not base:
        return None
    try:
        import httpx
        resp = httpx.get(
            f"{base}/allocation/compute",
            params={"window": window, "aggregate": aggregate, "accumulate": "true"},
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        log.debug("OpenCost fetch failed (%s); falling back to the built-in allocator.", exc)
        return None

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        log.debug("OpenCost response had no data array; falling back.")
        return None
    return data


def allocation_report(
    window: str = "7d",
    aggregate: str = "namespace",
    url: str | None = None,
) -> dict[str, Any] | None:
    """
    Normalized nable k8s cost report from OpenCost, or None when OpenCost is not
    reachable. Numbers are OpenCost's, priced at the cluster's real rates and
    inclusive of GPU, network, and PV storage.
    """
    data = fetch_allocation(window, aggregate, url=url)
    if data is None:
        return None

    # accumulate=true returns a single allocation set; be tolerant if not.
    alloc_set: dict = {}
    for chunk in data:
        if isinstance(chunk, dict):
            alloc_set.update(chunk)

    rows: list[dict] = []
    idle_usd = 0.0
    gpu_usd = 0.0
    for key, a in alloc_set.items():
        if not isinstance(a, dict):
            continue
        total = float(a.get("totalCost", 0.0) or 0.0)
        gpu = float(a.get("gpuCost", 0.0) or 0.0)
        if key == _IDLE_KEY:
            idle_usd += total
            continue
        gpu_usd += gpu
        rows.append({
            "name": a.get("name", key),
            "cpu_usd": round(float(a.get("cpuCost", 0.0) or 0.0), 2),
            "gpu_usd": round(gpu, 2),
            "ram_usd": round(float(a.get("ramCost", 0.0) or 0.0), 2),
            "pv_usd": round(float(a.get("pvCost", 0.0) or 0.0), 2),
            "network_usd": round(float(a.get("networkCost", 0.0) or 0.0), 2),
            "total_usd": round(total, 2),
            "cpu_efficiency": a.get("cpuEfficiency"),
            "total_efficiency": a.get("totalEfficiency"),
        })

    rows.sort(key=lambda r: r["total_usd"], reverse=True)
    allocated = sum(r["total_usd"] for r in rows)
    window_total = allocated + idle_usd

    out: dict[str, Any] = {
        "source": "opencost",
        "is_estimate": False,
        "accurate": True,
        "window": window,
        "aggregate": aggregate,
        "window_total_usd": round(window_total, 2),
        "allocated_usd": round(allocated, 2),
        "idle_usd": round(idle_usd, 2),
        "gpu_usd": round(gpu_usd, 2),
        "by_key": rows,
        "note": ("Priced by OpenCost at your cluster's real rates, including GPU, "
                 "network, and PV storage. Costs are for the requested window."),
    }
    # Monthly projection only for a clean N-day/N-hour window, clearly labeled.
    days = _window_days(window)
    if days and days > 0:
        out["monthly_cost_usd"] = round(window_total / days * 30, 2)
    return out
