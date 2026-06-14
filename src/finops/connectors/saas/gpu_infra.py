"""
GPU / serverless-inference infra connectors: Modal, Together, Replicate.

These platforms hold the single largest variable cost for the model-builder
slice of AI-native startups (per-second GPU time billed inside each vendor's own
dashboard, invisible to any cloud bill). nable needs to see them to be credible
with that buyer.

Honest reality, and why this module is deliberately conservative: none of the
three exposes a clean public per-range cost API on the free/standard tier.
  - Modal: billing is on the `modal billing` CLI and a billing API gated to
    Team/Enterprise plans.
  - Together: per-account usage/cost is not on the public API; it lives on the
    dashboard.
  - Replicate: account is reachable by API, but per-prediction cost is not
    returned, so spend is not summable from the public API.

So each connector confirms the credential and reports a clear, honest status
rather than inventing spend. Where a vendor later ships a usable usage endpoint,
the fetch slots in here. Until then the practical path for these bills is the
invoice email parser (connectors/invoice/parser.py).
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

log = logging.getLogger(__name__)


def _env(name: str) -> str | None:
    from ...security.env import get_env
    return get_env(name)


# ── Modal ──────────────────────────────────────────────────────────────────

def modal_configured() -> bool:
    return bool(_env("MODAL_TOKEN_ID") and _env("MODAL_TOKEN_SECRET"))


def modal_get_costs(start_date: date, end_date: date) -> dict[str, Any]:
    if not modal_configured():
        return _empty("modal", "not_configured")
    return _limited(
        "modal",
        "Modal exposes billing via the `modal billing` CLI and a billing API on "
        "Team/Enterprise plans only. Import Modal invoices via the invoice email "
        "parser, or upgrade the workspace for live GPU-second cost.",
    )


# ── Together ───────────────────────────────────────────────────────────────

def together_configured() -> bool:
    return bool(_env("TOGETHER_API_KEY"))


def together_get_costs(start_date: date, end_date: date) -> dict[str, Any]:
    if not together_configured():
        return _empty("together", "not_configured")
    # Confirm the key is live (models endpoint is public to any valid key).
    reachable = _probe("https://api.together.xyz/v1/models",
                       {"Authorization": f"Bearer {_env('TOGETHER_API_KEY')}"})
    note = (
        "Together does not expose per-range account spend on the public API; "
        "usage lives on the dashboard. Import Together invoices via the invoice "
        "email parser for cost tracking."
    )
    return _limited("together", note, reachable=reachable)


# ── Replicate ──────────────────────────────────────────────────────────────

def replicate_configured() -> bool:
    return bool(_env("REPLICATE_API_TOKEN"))


def replicate_get_costs(start_date: date, end_date: date) -> dict[str, Any]:
    if not replicate_configured():
        return _empty("replicate", "not_configured")
    reachable = _probe("https://api.replicate.com/v1/account",
                       {"Authorization": f"Token {_env('REPLICATE_API_TOKEN')}"})
    note = (
        "Replicate's API returns account details but not per-prediction cost, so "
        "spend is not summable from the public API. Import Replicate invoices via "
        "the invoice email parser for cost tracking."
    )
    return _limited("replicate", note, reachable=reachable)


# ── Aggregate ──────────────────────────────────────────────────────────────

_PROVIDERS = {
    "modal":     (modal_configured,     modal_get_costs),
    "together":  (together_configured,  together_get_costs),
    "replicate": (replicate_configured, replicate_get_costs),
}


def get_all_gpu_infra_costs(start_date: date, end_date: date) -> dict[str, Any]:
    """Report status across all configured GPU/inference-infra providers."""
    providers: dict[str, dict] = {}
    for name, (configured, fetch) in _PROVIDERS.items():
        if configured():
            providers[name] = fetch(start_date, end_date)
    return {
        "providers": providers,
        "configured_count": len(providers),
        "note": (
            "Modal/Together/Replicate gate per-range cost behind paid plans or "
            "omit it from the public API. Use the invoice email parser to track "
            "these bills until a usable usage endpoint is available."
        ) if providers else "No GPU/inference-infra providers configured.",
    }


# ── helpers ────────────────────────────────────────────────────────────────

def _probe(url: str, headers: dict[str, str]) -> bool:
    """Best-effort liveness check; never raises."""
    try:
        import httpx
        r = httpx.get(url, headers=headers, timeout=15)
        return r.status_code < 400
    except Exception as e:
        log.debug("GPU-infra probe failed for %s: %s", url, e)
        return False


def _limited(provider: str, note: str, reachable: bool = True) -> dict[str, Any]:
    return {
        "provider": provider,
        "total_usd": 0.0,
        "by_model": {},
        "by_model_tokens": {},
        "daily": [],
        "source": "limited",
        "credential_reachable": reachable,
        "note": note,
    }


def _empty(provider: str, reason: str) -> dict[str, Any]:
    return {"provider": provider, "total_usd": 0.0, "by_model": {},
            "by_model_tokens": {}, "daily": [], "source": "none", "reason": reason}
