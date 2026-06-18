"""
customer_signal(): the per-customer learning signal.

Reads this install's savings_recommendations ledger and, per recommendation source,
computes how often the customer ACTS on that rec type and how close PREDICTED
savings landed to MEASURED realized savings, then turns that into a verdict
(boost / suppress / neutral) and a confidence multiplier the rescorer uses to
re-rank proposals.

Two things keep it honest on sparse data (we have ~no ledger yet):
  - Bayesian shrinkage: act-rate is a Beta-posterior pulled toward a global prior,
    so a single dismissal can't nuke a rec type.
  - A COLD/WARMING/WARM ladder: a source is only ever SUPPRESSED once it has enough
    resolved recs (>= WARM_FLOOR); below that it keeps blanket behavior.

This is deterministic math over the ledger (like quality_signal already is). No ML
training, no model files, fully reproducible and explainable. Single-tenant: it only
ever reads this install's own DB; nothing crosses a customer boundary.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from ..savings_tracker import get_engine
from ...storage.db import savings_recommendations

# ── Tunables (conservative on purpose; the ledger is near-empty today) ────────
PRIOR_ACT_RATE = 0.4      # global prior: absent evidence, assume ~40% act rate
PRIOR_STRENGTH = 5.0      # pseudo-observations of the prior (shrinkage strength)
WARM_FLOOR = 8            # >= this many RESOLVED recs before learned weights dominate
WARMING_FLOOR = 1         # 1..WARM_FLOOR-1 = blending learned with blanket
SUPPRESS_ACT_RATE = 0.15  # below this shrunk act-rate (and WARM) -> suppress for this customer
BOOST_FLOOR = 3           # >= this many resolved before we'll actively boost
BOOST_ACT_RATE = 0.5      # at/above this shrunk act-rate (and accurate) -> boost
ACCURACY_OK = (0.8, 1.2)  # predicted/realized within this band counts as "accurate"

_RESOLVED = ("acted_on", "verified", "dismissed", "expired")
_ACTED = ("acted_on", "verified")


def _coverage(resolved: int) -> str:
    if resolved <= 0:
        return "COLD"
    if resolved < WARM_FLOOR:
        return "WARMING"
    return "WARM"


def _shrunk_act_rate(acted: int, resolved: int) -> float:
    """Beta-posterior act-rate pulled toward PRIOR_ACT_RATE; stable when resolved is small."""
    return (acted + PRIOR_ACT_RATE * PRIOR_STRENGTH) / (resolved + PRIOR_STRENGTH)


def _verdict(coverage: str, shrunk: float, resolved: int, accuracy: float | None) -> str:
    if coverage == "WARM" and shrunk < SUPPRESS_ACT_RATE:
        return "suppress"
    if resolved >= BOOST_FLOOR and shrunk >= BOOST_ACT_RATE and (
        accuracy is None or ACCURACY_OK[0] <= accuracy <= ACCURACY_OK[1]
    ):
        return "boost"
    return "neutral"


def _confidence_multiplier(shrunk: float, accuracy: float | None) -> float:
    """A ranking weight in ~[0,1]. High act-rate + accurate predictions rank higher;
    over-prediction (accuracy < 1) is penalized; under/unknown accuracy is not."""
    acc_factor = 1.0
    if accuracy is not None and accuracy < 1.0:
        acc_factor = max(0.3, accuracy)
    return round(shrunk * acc_factor, 3)


def _why(source: str, coverage: str, verdict: str, acted: int, resolved: int,
         accuracy: float | None) -> str:
    if coverage == "COLD":
        return f"No decisions on {source} recs yet, using the standard ranking (global default)."
    acc_txt = ""
    if accuracy is not None:
        if accuracy < ACCURACY_OK[0]:
            acc_txt = f" and past {source} savings landed ~{round(accuracy*100)}% of estimate (we over-predicted)"
        elif accuracy > ACCURACY_OK[1]:
            acc_txt = f" and past {source} savings beat the estimate (~{round(accuracy*100)}%)"
        else:
            acc_txt = f" and past {source} savings landed within ~{round(accuracy*100)}% of estimate"
    base = f"You acted on {acted}/{resolved} {source} recs you decided on{acc_txt}."
    if verdict == "suppress":
        return base + " So these are suppressed for you (still here if you want them)."
    if verdict == "boost":
        return base + " So these rank higher for you."
    return base


def customer_signal() -> dict[str, Any]:
    """Per-source learning signal for this install. See module docstring."""
    sr = savings_recommendations
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                sr.c.source, sr.c.status,
                func.count().label("cnt"),
                func.sum(sr.c.estimated_monthly_savings_usd).label("sum_est"),
                func.sum(sr.c.verified_monthly_savings_usd).label("sum_ver"),
            ).group_by(sr.c.source, sr.c.status)
        ).fetchall()

    agg: dict[str, dict] = {}
    for r in rows:
        d = agg.setdefault(r.source or "unknown", {
            "open": 0, "acted_on": 0, "verified": 0, "dismissed": 0, "expired": 0,
            "realized": 0.0, "predicted_of_verified": 0.0,
        })
        cnt = int(r.cnt or 0)
        if r.status in d:
            d[r.status] += cnt
        if r.status == "verified":
            d["realized"] += float(r.sum_ver or 0.0)
            d["predicted_of_verified"] += float(r.sum_est or 0.0)

    by_source = []
    total_realized = 0.0
    for src, d in sorted(agg.items()):
        acted = d["acted_on"] + d["verified"]
        resolved = acted + d["dismissed"] + d["expired"]
        pov = d["predicted_of_verified"]
        accuracy = round(d["realized"] / pov, 3) if pov > 0 else None
        shrunk = round(_shrunk_act_rate(acted, resolved), 3)
        coverage = _coverage(resolved)
        verdict = _verdict(coverage, shrunk, resolved, accuracy)
        total_realized += d["realized"]
        by_source.append({
            "source": src,
            "open": d["open"], "acted": acted, "verified": d["verified"],
            "dismissed": d["dismissed"], "expired": d["expired"], "resolved": resolved,
            "act_rate": shrunk,
            "act_rate_raw": round(acted / resolved, 3) if resolved else None,
            "accuracy": accuracy,
            "coverage": coverage,
            "verdict": verdict,
            "confidence_multiplier": _confidence_multiplier(shrunk, accuracy),
            "why": _why(src, coverage, verdict, acted, resolved, accuracy),
        })

    return {
        "by_source": by_source,
        "verified_monthly_usd": round(total_realized, 2),
        "verified_annual_run_rate_usd": round(total_realized * 12, 2),
        "params": {
            "prior_act_rate": PRIOR_ACT_RATE, "prior_strength": PRIOR_STRENGTH,
            "warm_floor": WARM_FLOOR, "suppress_act_rate": SUPPRESS_ACT_RATE,
            "boost_act_rate": BOOST_ACT_RATE, "accuracy_ok": list(ACCURACY_OK),
        },
        "note": ("Per recommendation source: act-rate (acted vs all you decided on, "
                 "Bayesian-shrunk) and accuracy (measured vs predicted savings among "
                 "verified recs). Sources stay on blanket behavior until they have "
                 f">= {WARM_FLOOR} resolved recs; only then can they be suppressed for you."),
    }


def signal_for(signal: dict, source: str) -> dict:
    """Look up one source's entry in a customer_signal() result, or a COLD default."""
    for s in signal.get("by_source", []):
        if s["source"] == source:
            return s
    return {
        "source": source, "resolved": 0, "acted": 0, "act_rate": PRIOR_ACT_RATE,
        "accuracy": None, "coverage": "COLD", "verdict": "neutral",
        "confidence_multiplier": _confidence_multiplier(PRIOR_ACT_RATE, None),
        "why": f"No decisions on {source} recs yet, using the standard ranking (global default).",
    }
