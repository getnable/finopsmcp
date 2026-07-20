"""Connection-aware cross-provider gather for `nable scan` (v2).

`cli_scan` handles the AWS block (creds probe, `run_deep_audit` waste, optional
Cost Explorer spend under --spend). This module gathers the OTHER connected
providers, AI/LLM, GCP, and Azure, over the aggregators that already exist, and
returns structured blocks that `cli_scan` renders alongside the AWS block.

Two invariants, both load-bearing:

1. Free by default. Without --spend, the gather touches only free signals:
   usage-API AI spend (`get_all_llm_costs(exclude_cloud_native=True)` skips the
   Bedrock/Vertex metered legs), GCP recoverable via the Recommender API, and
   Azure spend + recoverable via the free Cost Management / Advisor APIs. Nothing
   here calls AWS Cost Explorer or the GCP BigQuery billing export. The cloud
   spend totals and cloud-native AI (Bedrock/Vertex/Azure-OpenAI) join only under
   --spend, in `cli_scan`.

2. One provider never sinks the scan. Each provider is gathered in its own thread
   with its own timeout inside an overall budget; a provider that times out,
   auth-fails, errors, or returns nothing degrades to a per-provider note while
   the others still render. Blocking gathers spawn their own pools/threads
   (get_all_llm_costs, the async GCP/Azure paths), so a timeout abandons a thread
   that cannot be cancelled; the caller hard-exits after rendering, exactly as
   `cli_scan._finish` already does for the AWS path.
"""
from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

# Per-provider wall-clock cap, and the overall budget for all extra providers.
# The AWS block has its own 45s budget in cli_scan; these bound the added work.
_PER_PROVIDER_TIMEOUT_S = 15.0
_EXTRA_BUDGET_S = 30.0


@dataclass
class ProviderBlock:
    """One provider's contribution to the unified scan.

    status is one of: ok, no_data, auth_failed, errored, timeout. Only `ok`
    carries trustworthy numbers; every other status renders as a note and the
    dollar fields stay None.
    """
    family: str                       # "ai" | "gcp" | "azure"
    label: str                        # "AI & GPU" | "GCP" | "Azure"
    status: str = "ok"
    spend_usd: float | None = None
    recoverable_usd: float | None = None
    detail: str = ""                  # "OpenAI $9.2k · Anthropic $3.1k"
    note: str | None = None           # failure / gating note
    estimated: bool = False           # AI spend is always [estimated] in v2
    early_recoverable: bool = False   # AI recoverable is qualitative / [early]
    # AI only: per-provider spend, so cli_scan can dedup cloud-native AI out of
    # the cloud totals under --spend using _CLOUD_NATIVE_LLM.
    by_provider: dict = field(default_factory=dict)


# ── per-provider gathers (each sync + blocking; run in a worker thread) ──────────

def _run_async(coro):
    """Run a coroutine to completion from a worker thread (no running loop here)."""
    import asyncio
    return asyncio.run(coro)


def _short(v: float) -> str:
    if v >= 1000:
        return f"${v / 1000:.1f}k".replace(".0k", "k")
    return f"${v:.0f}"


def _gather_ai(spend: bool, days: int = 30) -> ProviderBlock:
    from .connectors.llm_costs import get_all_llm_costs

    # exclude_cloud_native=True on the free path: skip Bedrock (Cost Explorer) and
    # Vertex (BigQuery export). Under --spend they are included and deduped by the
    # caller against the cloud totals.
    data = get_all_llm_costs(days=days, exclude_cloud_native=not spend)
    total = float(data.get("total_usd") or 0.0)
    by_provider = {k: float(v or 0.0) for k, v in (data.get("by_provider") or {}).items()}

    blk = ProviderBlock(family="ai", label="AI & GPU", estimated=True,
                        early_recoverable=True, by_provider=by_provider)
    if not by_provider and total == 0.0:
        # "llm" is in connected_families() for every AWS user (Bedrock rides on
        # AWS), so an empty AI result is usually that false positive, not a real
        # AI connection. Drop the block so an AWS-only scan stays byte-identical
        # to v1. A genuinely connected AI provider with spend still shows.
        blk.status = "skip"
        return blk

    top = sorted(by_provider.items(), key=lambda kv: kv[1], reverse=True)[:2]
    blk.spend_usd = total
    blk.detail = " · ".join(f"{p.capitalize()} {_short(a)}" for p, a in top if a > 0)
    if not spend:
        # The cloud-native AI legs are gated; say so, so the number is not read
        # as the whole AI bill when Bedrock/Vertex are in play.
        blk.note = "usage-API providers; Bedrock/Vertex under --spend"
    # AI recoverable stays qualitative (model routing / idle GPU), never a headline $.
    recs = data.get("recommendations") or []
    if recs:
        blk.detail += f"   routing/idle: {len(recs)} idea{'s' if len(recs) != 1 else ''} [early]"
    return blk


def _gather_gcp(spend: bool) -> ProviderBlock:
    # Engine-direct (connectors + recommendations), never tools.gcp: the tool
    # wrappers import finops.server, which the light scan path avoids and which
    # would circular-import here.
    from .connectors.gcp import GCPConnector
    from .recommendations.gcp_waste import audit_gcp_waste

    blk = ProviderBlock(family="gcp", label="GCP")
    client = GCPConnector()
    if not _run_async(client.is_configured()):
        blk.status = "auth_failed"
        blk.note = "not connected (run `nable gcp`)"
        return blk

    # Recoverable is free (the Recommender API). GCP spend lives in the metered
    # BigQuery billing export, so it stays on the --spend path in cli_scan.
    report = _run_async(audit_gcp_waste(client))
    if isinstance(report, dict) and report.get("error"):
        blk.status = "errored"
        blk.note = str(report["error"]).split(".")[0][:90]
        return blk
    rec = None
    for k in ("total_monthly_savings_usd", "total_estimated_monthly_savings_usd",
              "monthly_savings_usd", "total_savings_usd"):
        if isinstance(report, dict) and report.get(k) is not None:
            rec = float(report[k]); break
    if rec is None and isinstance(report, dict):
        rec = round(sum(float(f.get("estimated_monthly_savings_usd", 0) or 0)
                        for f in (report.get("findings") or [])), 2)
    blk.recoverable_usd = rec or 0.0
    n = len(report.get("findings") or []) if isinstance(report, dict) else 0
    blk.detail = f"{n} waste finding{'s' if n != 1 else ''}" if n else "no material waste found"
    # GCP spend lives in the BigQuery billing export (metered, per-TB); it stays on
    # the --spend path in cli_scan, not here.
    if blk.recoverable_usd == 0.0 and not n:
        blk.status = "no_data"
    return blk


def _gather_azure(spend: bool) -> ProviderBlock:
    # Engine-direct (connectors.azure_optimize is sync and imports clean); never
    # tools.azure, which imports finops.server.
    from .connectors.azure_optimize import (
        get_advisor_cost_recommendations,
        get_cost_by_dimension,
    )

    blk = ProviderBlock(family="azure", label="Azure")
    # Azure Cost Management Query + Advisor are both free, so Azure shows spend AND
    # recoverable on the default path.
    end = date.today()
    start = end - timedelta(days=30)
    try:
        cost = get_cost_by_dimension("ServiceName", start, end, limit=5)
    except Exception as exc:  # noqa: BLE001
        blk.status = "errored"
        blk.note = str(exc).split(chr(10))[0][:90]
        return blk
    if isinstance(cost, dict) and cost.get("error"):
        msg = str(cost["error"]).lower()
        blk.status = "auth_failed" if any(w in msg for w in ("connect", "subscription", "credential")) else "errored"
        blk.note = str(cost["error"]).split(".")[0][:90]
        return blk
    if isinstance(cost, dict) and cost.get("total_cost_usd") is not None:
        blk.spend_usd = float(cost["total_cost_usd"])
        top = (cost.get("by_dimension") or cost.get("breakdown") or [])[:2]
        parts = []
        for item in top:
            name = item.get("name") or item.get("dimension") or ""
            amt = float(item.get("cost_usd") or item.get("total_usd") or 0)
            if name and amt:
                parts.append(f"{name} {_short(amt)}")
        blk.detail = " · ".join(parts)

    try:
        adv = get_advisor_cost_recommendations()
        if isinstance(adv, dict) and adv.get("total_monthly_savings_usd") is not None:
            blk.recoverable_usd = float(adv["total_monthly_savings_usd"])
    except Exception:  # noqa: BLE001 - recoverable is best-effort
        pass

    if blk.spend_usd is None and blk.recoverable_usd is None:
        blk.status = "no_data"
        blk.note = "connected, no Azure cost data this period"
    return blk


_GATHERERS = {
    "llm": ("ai", _gather_ai),
    "gcp": ("gcp", _gather_gcp),
    "azure": ("azure", _gather_azure),
}


def gather_extra_providers(
    families,
    *,
    spend: bool,
    days: int = 30,
    per_provider_timeout: float = _PER_PROVIDER_TIMEOUT_S,
    overall_budget: float = _EXTRA_BUDGET_S,
) -> tuple[list[ProviderBlock], bool]:
    """Gather AI/GCP/Azure blocks for the connected families.

    Returns (blocks, threads_abandoned). threads_abandoned is True when a provider
    timed out and left an uncancellable worker thread behind, so the caller can
    hard-exit after rendering (the get_all_llm_costs / async paths spawn their own
    threads that cannot be cancelled).
    """
    jobs = [(fam, fn) for key, (fam, fn) in _GATHERERS.items() if key in families]
    if not jobs:
        return [], False

    blocks: list[ProviderBlock] = []
    abandoned = False
    labels = {"ai": "AI & GPU", "gcp": "GCP", "azure": "Azure"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futs = {pool.submit(fn, spend): fam for fam, fn in jobs}
        deadline = time.monotonic() + overall_budget
        for fut, fam in futs.items():
            remaining = max(0.5, min(per_provider_timeout, deadline - time.monotonic()))
            try:
                blocks.append(fut.result(timeout=remaining))
            except concurrent.futures.TimeoutError:
                abandoned = True
                blocks.append(ProviderBlock(
                    family=fam, label=labels.get(fam, fam.upper()), status="timeout",
                    note="timed out; partial scan (other providers still shown)"))
            except Exception as exc:  # noqa: BLE001 - one provider must never sink the scan
                blocks.append(ProviderBlock(
                    family=fam, label=labels.get(fam, fam.upper()), status="errored",
                    note=str(exc).split(chr(10))[0][:90]))

    blocks = [b for b in blocks if b.status != "skip"]
    order = {"ai": 0, "gcp": 1, "azure": 2}
    blocks.sort(key=lambda b: order.get(b.family, 9))
    return blocks, abandoned


def demo_extra_blocks(spend: bool) -> list[ProviderBlock]:
    """Fixed cross-provider blocks for `nable scan --demo` (StreamCo sample), so
    the no-account demo showcases the whole cross-provider frame deterministically.
    Mirrors the real free/--spend boundary: without --spend the AI block shows
    usage-API providers only (Bedrock waits for --spend)."""
    if spend:
        ai = ProviderBlock(
            family="ai", label="AI & GPU", status="ok", spend_usd=18400.0,
            estimated=True, early_recoverable=True,
            detail="OpenAI $9.2k · Bedrock $5.1k · Modal $4.1k",
            by_provider={"openai": 9200.0, "bedrock": 5100.0, "modal": 4100.0})
    else:
        ai = ProviderBlock(
            family="ai", label="AI & GPU", status="ok", spend_usd=13300.0,
            estimated=True, early_recoverable=True,
            detail="OpenAI $9.2k · Modal $4.1k",
            note="usage-API providers; Bedrock/Vertex under --spend",
            by_provider={"openai": 9200.0, "modal": 4100.0})
    gcp = ProviderBlock(family="gcp", label="GCP", status="ok",
                        recoverable_usd=410.0, detail="3 waste findings")
    azure = ProviderBlock(family="azure", label="Azure", status="ok",
                          spend_usd=6300.0, recoverable_usd=220.0,
                          detail="AKS $2.1k · Blob Storage $1.4k")
    return [ai, gcp, azure]
