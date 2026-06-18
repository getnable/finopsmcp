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

import statistics
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
    # Both suppress and boost require WARM (>= WARM_FLOOR resolved). Below that a
    # source stays neutral (blanket behavior), so sparse data never flips a verdict.
    if coverage != "WARM":
        return "neutral"
    if shrunk < SUPPRESS_ACT_RATE:
        return "suppress"
    if shrunk >= BOOST_ACT_RATE and (
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
    # Floor so a sparse/low-accuracy source is ranked low, never erased to 0.
    return max(0.001, round(shrunk * acc_factor, 3))


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


def _new_counts() -> dict:
    return {"open": 0, "acted_on": 0, "verified": 0, "dismissed": 0, "expired": 0, "realized": 0.0}


def _entry(source: str, bucket: str | None, c: dict, acc_list: list[float]) -> dict:
    """Build one signal entry (the same math for a per-source or a per-(source,bucket)
    grouping). accuracy is the median per-rec ratio; act-rate is Bayesian-shrunk."""
    acted = c["acted_on"] + c["verified"]
    resolved = acted + c["dismissed"] + c["expired"]
    accuracy = round(statistics.median(acc_list), 3) if acc_list else None
    shrunk = round(_shrunk_act_rate(acted, resolved), 3)
    coverage = _coverage(resolved)
    verdict = _verdict(coverage, shrunk, resolved, accuracy)
    e = {
        "source": source,
        "open": c["open"], "acted": acted, "verified": c["verified"],
        "dismissed": c["dismissed"], "expired": c["expired"], "resolved": resolved,
        "act_rate": shrunk,
        "act_rate_raw": round(acted / resolved, 3) if resolved else None,
        "accuracy": accuracy, "coverage": coverage, "verdict": verdict,
        "confidence_multiplier": _confidence_multiplier(shrunk, accuracy),
        "why": _why(source, coverage, verdict, acted, resolved, accuracy),
        "realized_monthly_usd": round(c["realized"], 2),
    }
    if bucket is not None:
        e["bucket"] = bucket
    return e


def customer_signal() -> dict[str, Any]:
    """Per-source AND per-(source, bucket) learning signal for this install. The bucket
    breakdown lets the loop learn e.g. spot is fine for nonprod-batch but not prod-steady.
    See module docstring."""
    sr = savings_recommendations
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                sr.c.source, sr.c.environment_bucket, sr.c.status,
                func.count().label("cnt"),
                func.sum(sr.c.verified_monthly_savings_usd).label("sum_ver"),
            ).group_by(sr.c.source, sr.c.environment_bucket, sr.c.status)
        ).fetchall()
        # Per-rec accuracy among verified recs; median (not a sum-ratio) so one big
        # miss can't dominate; negatives clamp to 0 (a "verified" loss = a failed rec).
        vrows = conn.execute(
            select(sr.c.source, sr.c.environment_bucket,
                   sr.c.estimated_monthly_savings_usd,
                   sr.c.verified_monthly_savings_usd).where(sr.c.status == "verified")
        ).fetchall()

    source_counts: dict[str, dict] = {}
    bucket_counts: dict[tuple, dict] = {}
    for r in rows:
        src = r.source or "unknown"
        bkt = r.environment_bucket or "unknown|other"
        cnt = int(r.cnt or 0)
        ver = max(0.0, float(r.sum_ver or 0.0))
        for store, key in ((source_counts, src), (bucket_counts, (src, bkt))):
            d = store.setdefault(key, _new_counts())
            if r.status in d:
                d[r.status] += cnt
            if r.status == "verified":
                d["realized"] += ver

    source_acc: dict[str, list[float]] = {}
    bucket_acc: dict[tuple, list[float]] = {}
    for r in vrows:
        est = float(r.estimated_monthly_savings_usd or 0.0)
        if est <= 0:
            continue
        ratio = max(0.0, float(r.verified_monthly_savings_usd or 0.0)) / est
        src = r.source or "unknown"
        bkt = r.environment_bucket or "unknown|other"
        source_acc.setdefault(src, []).append(ratio)
        bucket_acc.setdefault((src, bkt), []).append(ratio)

    by_source = [_entry(src, None, c, source_acc.get(src, [])) for src, c in sorted(source_counts.items())]
    by_source.sort(key=lambda s: s["realized_monthly_usd"], reverse=True)
    by_bucket = [_entry(src, bkt, c, bucket_acc.get((src, bkt), []))
                 for (src, bkt), c in sorted(bucket_counts.items())]
    total_realized = sum(c["realized"] for c in source_counts.values())

    return {
        "by_source": by_source,
        "by_bucket": by_bucket,
        "verified_monthly_usd": round(total_realized, 2),
        "verified_annual_run_rate_usd": round(total_realized * 12, 2),
        "params": {
            "prior_act_rate": PRIOR_ACT_RATE, "prior_strength": PRIOR_STRENGTH,
            "warm_floor": WARM_FLOOR, "suppress_act_rate": SUPPRESS_ACT_RATE,
            "boost_act_rate": BOOST_ACT_RATE, "accuracy_ok": list(ACCURACY_OK),
        },
        "note": ("Per recommendation source (and per environment bucket): act-rate "
                 "(acted vs all you decided on, Bayesian-shrunk) and accuracy (measured "
                 "vs predicted savings among verified recs). A source/bucket stays on "
                 f"blanket behavior until it has >= {WARM_FLOOR} resolved recs; only "
                 "then can it be suppressed for you."),
    }


def signal_for(signal: dict, source: str, bucket: str | None = None) -> dict:
    """Look up the signal for a source (and bucket if given). Prefers a bucket-level
    entry with real signal, falls back to the source aggregate, then a COLD default."""
    if bucket:
        for s in signal.get("by_bucket", []):
            if s["source"] == source and s.get("bucket") == bucket and s["resolved"] > 0:
                return s
    for s in signal.get("by_source", []):
        if s["source"] == source and s["resolved"] > 0:
            return s
    return {
        "source": source, "bucket": bucket, "resolved": 0, "acted": 0,
        "act_rate": PRIOR_ACT_RATE, "accuracy": None, "coverage": "COLD", "verdict": "neutral",
        "confidence_multiplier": _confidence_multiplier(PRIOR_ACT_RATE, None),
        "why": f"No decisions on {source} recs yet, using the standard ranking (global default).",
    }
