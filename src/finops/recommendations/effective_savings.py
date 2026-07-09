"""
Shared effective-savings layer: convert a list-price savings estimate into what
a customer would actually save on THEIR environment and THEIR discounts.

Every off-the-shelf recommender (Compute Optimizer, Trusted Advisor) quotes
savings at public on-demand prices. A real company pays less than list because of
three stacked discounts:

  1. Reserved Instance / Savings Plan commitments.
  2. EDP / private pricing / negotiated rates (invisible to every free tool).
  3. Credits.

This module routes every savings figure through the customer's measured rates so
the numbers are honest. The order matters and avoids a double-count:

  - The effective rate from the customer's bill (rate_detector, derived from
    amortized-vs-list cost) ALREADY blends in the commitment discount. So when we
    have it, we use it alone, per-service, and never also apply commitment
    coverage on top.
  - Only when no rate data exists (no CUR, thin Cost Explorer) do we fall back to
    the coarser commitment-coverage discount, and finally to list price, each with
    an honest confidence label.

Propose-only, and token-cheap: everything returned is scalar plus one short note.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .genuine_savings import CommitmentContext, fetch_commitment_context

log = logging.getLogger(__name__)

# resource_type -> Cost Explorer service name, so we can look up the per-service
# effective discount rather than the blended account-wide one where possible.
_SERVICE_BY_RESOURCE: dict[str, str] = {
    "ec2":    "Amazon Elastic Compute Cloud - Compute",
    "lambda": "AWS Lambda",
    "rds":    "Amazon Relational Database Service",
    "ecs":    "Amazon Elastic Container Service",
}

_CTX_TTL_SEC = 900.0
_ctx_cache: tuple[float, "SavingsContext"] | None = None


@dataclass
class SavingsContext:
    """Everything about a customer's environment needed to price savings honestly."""
    rate: Any = None                 # EffectiveRateProfile | None
    commitment: CommitmentContext | None = None

    @property
    def available(self) -> bool:
        rate_ok = bool(self.rate) and getattr(self.rate, "confidence", "low") in ("high", "medium")
        commit_ok = bool(self.commitment) and getattr(self.commitment, "available", False)
        return rate_ok or commit_ok


@dataclass
class AdjustedSavings:
    list_savings: float
    effective_savings: float
    basis: str          # "effective_rate" | "commitment_coverage" | "list_price"
    discount_pct: float  # discount applied to reach effective, as a percentage
    confidence: str      # "high" | "medium" | "low"
    note: str


def detect_savings_context() -> SavingsContext:
    """
    Build (and cache ~15 min) the customer's savings context: measured effective
    rate profile plus commitment coverage. Never raises; degrades to an empty
    context that yields list-price savings with a low-confidence label.
    """
    global _ctx_cache
    now = time.time()
    if _ctx_cache and (now - _ctx_cache[0]) < _CTX_TTL_SEC:
        return _ctx_cache[1]

    rate = None
    try:
        from .rate_detector import detect_effective_rates
        rate = detect_effective_rates()
    except Exception as e:  # pragma: no cover - defensive
        log.debug("effective rate detection unavailable: %s", e)

    commitment = None
    try:
        commitment = fetch_commitment_context()
    except Exception as e:  # pragma: no cover - defensive
        log.debug("commitment context unavailable: %s", e)

    ctx = SavingsContext(rate=rate, commitment=commitment)
    _ctx_cache = (now, ctx)
    return ctx


def _reset_cache_for_tests() -> None:
    global _ctx_cache
    _ctx_cache = None


def adjust_savings(
    list_savings: float,
    resource_type: str | None = None,
    ctx: SavingsContext | None = None,
) -> AdjustedSavings:
    """
    Convert a public on-demand savings figure into the customer's real savings.

    Prefers the measured effective rate (per-service, already includes commitment
    + EDP), falls back to commitment coverage, then to list price. Each path
    carries a confidence label so we never present a list-price guess as a real
    number.
    """
    if list_savings <= 0 or ctx is None:
        return AdjustedSavings(list_savings, list_savings, "list_price", 0.0, "low", "")

    rate = ctx.rate
    # 1. Measured effective rate: best signal. Amortized-vs-list already blends in
    #    the commitment discount, so this stands alone (no coverage stacking).
    if rate is not None and getattr(rate, "confidence", "low") in ("high", "medium"):
        overall = float(getattr(rate, "overall_discount_pct", 0.0) or 0.0)
        has_private = bool(getattr(rate, "has_private_pricing", False))
        if overall > 0 or has_private:
            svc = _SERVICE_BY_RESOURCE.get((resource_type or "").lower())
            mult = float(rate.effective_multiplier(svc))
            mult = min(1.0, max(0.0, mult))
            eff = round(list_savings * mult, 2)
            disc = round((1.0 - mult) * 100.0, 1)
            src = getattr(rate, "source", "measured")
            return AdjustedSavings(
                list_savings, eff, "effective_rate", disc,
                getattr(rate, "confidence", "medium"),
                f"on your effective rate, ~{disc:.0f}% below list ({src})",
            )

    # 2. Commitment coverage fallback (coarse, account-level).
    cc = ctx.commitment
    if cc is not None and getattr(cc, "available", False) and cc.combined_pct > 0:
        cov = cc.combined_pct
        mult = max(0.0, 1.0 - cov / 100.0)
        eff = round(list_savings * mult, 2)
        return AdjustedSavings(
            list_savings, eff, "commitment_coverage", round(cov, 1), "medium",
            f"discounted for ~{cov:.0f}% commitment coverage (connect CUR for exact rates)",
        )

    # 3. No discount data: list price, and say so.
    return AdjustedSavings(
        list_savings, list_savings, "list_price", 0.0, "low",
        "list-price estimate; connect your CUR to price this on your real rates",
    )
