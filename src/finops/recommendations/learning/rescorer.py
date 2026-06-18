"""
rescore(): the propose-only enforcement point of the learning loop.

Given a list of recommendation dicts and a customer_signal(), it ONLY:
  - reorders them by a learned score (savings x the source's confidence multiplier),
  - annotates each with a `learned` block (verdict, why_ranked, rank change),
  - moves suppressed-for-you sources into a separate bucket (it never deletes them).

It does NOT change any recommendation's status, does NOT call the cloud, and imports
no boto3 / MCP / mutating module. The only thing in nable that acts is a human
clicking mark_acted_on / open_rightsizing_pr (which itself only opens a reviewable
PR). So a degenerate "save 100% -> destroy everything" is structurally impossible:
the output of this module is a ranked, explained proposal list, nothing more. A unit
test enforces that this file imports nothing that can mutate cloud state.
"""
from __future__ import annotations

from typing import Any

from .signal import signal_for


def rescore(
    recs: list[dict],
    signal: dict,
    *,
    savings_key: str = "estimated_monthly_savings_usd",
    source_key: str = "source",
    bucket_key: str = "environment_bucket",
) -> dict[str, Any]:
    """Reorder + annotate + suppress recommendations for this customer. Propose-only.

    Returns {ranked: [...], suppressed_for_you: [...], suppressed_count: int}. Input
    recs are never mutated (each output is a copy with a `learned` block added);
    statuses are preserved exactly.
    """
    ranked_candidates: list[tuple[float, int, dict]] = []
    suppressed: list[dict] = []

    for i, rec in enumerate(recs):
        src = rec.get(source_key) or "unknown"
        s = signal_for(signal, src, bucket=rec.get(bucket_key))
        try:
            savings = float(rec.get(savings_key, 0) or 0)
        except (TypeError, ValueError):
            savings = 0.0
        score = savings * float(s.get("confidence_multiplier", 0) or 0)

        annotated = dict(rec)  # copy: never mutate the caller's recommendation
        annotated["learned"] = {
            "source_verdict": s["verdict"],
            "bucket": s.get("bucket"),
            "coverage": s["coverage"],
            "act_rate": s["act_rate"],
            "accuracy": s["accuracy"],
            "confidence_multiplier": s["confidence_multiplier"],
            "why_ranked": s["why"],
            "original_rank": i,
            "rank_score": round(score, 4),
        }
        if s["verdict"] == "suppress":
            suppressed.append(annotated)
        else:
            ranked_candidates.append((score, i, annotated))

    # Highest learned score first; ties keep original order (stable).
    ranked_candidates.sort(key=lambda t: (-t[0], t[1]))
    ranked: list[dict] = []
    for new_rank, (_, _, rec) in enumerate(ranked_candidates):
        rec["learned"]["new_rank"] = new_rank
        ranked.append(rec)

    suppressed.sort(key=lambda r: float(r.get(savings_key, 0) or 0), reverse=True)
    return {
        "ranked": ranked,
        "suppressed_for_you": suppressed,
        "suppressed_count": len(suppressed),
    }
