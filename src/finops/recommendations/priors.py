"""
Cross-org priors: day-one judgment before an install has any history of its own.

A brand-new nable has no signal, so it can only rank findings by raw dollars. That
misses the point: the highest-dollar finding is not always the one a team like
yours actually acts on. Priors fix the cold start with expert-authored acceptance
rates per finding type and org segment ("AI-native shops fix idle GPUs first",
"graviton migrations land ~80% of the time"), so the very first ranked list already
reads like it knows how you operate.

As the install accumulates its own accept/reject history, `learning.signal` takes
over (the observed act-rate shrinks away from the prior toward reality), so priors
matter most on day one and fade as the org teaches nable directly.

Federated seam (the closed, hosted flywheel, not built here): `_PRIORS` is the
bundled default. A future control plane can aggregate per-org posteriors across the
fleet, aggregate-only (accept-rates per (segment, source), never raw bills or
resources), and ship updated priors that `_load_priors()` picks up. Keep that
function the single source so swapping bundled -> fleet-updated is a one-line change.
"""
from __future__ import annotations

from typing import Any

# Org segments we tailor priors to. Coarse on purpose; the ICP is AI-native infra
# teams, whose cost shape (GPU, spiky inference, data platforms) differs from a
# generic SaaS backend.
AI_NATIVE = "ai_native"
GENERIC = "generic"

# Neutral acceptance used when no prior exists; also the pivot the rescorer boosts
# around (p_accept > NEUTRAL boosts a cold finding, < NEUTRAL demotes it).
NEUTRAL_P_ACCEPT = 0.5

# Expert-authored priors: segment -> source -> {p_accept, rationale}. p_accept is
# "how often a team in this segment acts on this class of finding", from FinOps
# domain experience, not customer data. Sources match the ledger's `source` field
# (rightsizing | idle | commitment | kubernetes | waste).
_PRIORS: dict[str, dict[str, dict[str, Any]]] = {
    AI_NATIVE: {
        "idle": {"p_accept": 0.85,
                 "rationale": "AI-native teams kill idle GPU/compute fast; it's the "
                              "single biggest and least controversial win off-hours."},
        "rightsizing": {"p_accept": 0.6,
                        "rationale": "Accepted for stateless services; training/inference "
                                     "nodes are guarded, so it lands more often than not."},
        "commitment": {"p_accept": 0.45,
                       "rationale": "Reserved/committed spend is deferred while usage is "
                                    "still spiky; lands later, once demand settles."},
        "waste": {"p_accept": 0.8,
                  "rationale": "Orphaned volumes, old snapshots, idle endpoints: cheap to "
                               "clean up, rarely contested."},
        "kubernetes": {"p_accept": 0.55,
                       "rationale": "Namespace/request right-sizing lands once platform "
                                    "owners trust the numbers."},
    },
    GENERIC: {
        "idle": {"p_accept": 0.7,
                 "rationale": "Idle resources are the usual first cleanup."},
        "rightsizing": {"p_accept": 0.55,
                        "rationale": "Common win once the estimate is trusted."},
        "commitment": {"p_accept": 0.5,
                       "rationale": "Savings plans/RIs land when spend is steady."},
        "waste": {"p_accept": 0.75,
                  "rationale": "Low-risk cleanup of orphaned resources."},
        "kubernetes": {"p_accept": 0.5,
                       "rationale": "Depends on platform-team ownership."},
    },
}


def _load_priors() -> dict[str, dict[str, dict[str, Any]]]:
    """The single source of prior data. Bundled defaults today; the closed fleet
    updater swaps this out later without touching callers."""
    return _PRIORS


_SEGMENT_CACHE: dict[str, Any] = {"value": None}


def segment_of(*, force: bool = False) -> str:
    """Detect this org's segment from what's connected locally. AI-native if any
    LLM/GPU or data-platform provider is wired up; generic otherwise. Cached, local,
    no network."""
    if not force and _SEGMENT_CACHE["value"] is not None:
        return _SEGMENT_CACHE["value"]
    seg = GENERIC
    try:
        from ..tool_surface import connected_families
        fams = connected_families()
        if fams & {"llm", "databricks"}:
            seg = AI_NATIVE
    except Exception:
        seg = GENERIC
    _SEGMENT_CACHE["value"] = seg
    return seg


def _reset_cache_for_tests() -> None:
    _SEGMENT_CACHE["value"] = None


def prior_for(source: str, segment: str | None = None) -> dict[str, Any] | None:
    """Return {p_accept, rationale, segment} for a finding source, or None if we have
    no prior for it. p_accept is the expert-seeded acceptance rate for this segment."""
    seg = segment or segment_of()
    table = _load_priors().get(seg) or _load_priors().get(GENERIC, {})
    p = table.get(source)
    if not p:
        return None
    return {"p_accept": p["p_accept"], "rationale": p["rationale"], "segment": seg}
