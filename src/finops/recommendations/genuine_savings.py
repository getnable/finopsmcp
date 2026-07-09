"""
Genuine-savings judgment for rightsizing recommendations.

The commodity signal every cloud vendor ships for free is "this instance is
underutilized." That is not a decision. A recommendation is only genuine savings
if it survives the reasons it is usually wrong:

  1. Burst / peak      A low average with high peaks is a workload that needs
                       headroom, not an over-provisioned box.
  2. Memory-bound      Low CPU with high memory means the CPU downsize starves RAM.
  3. Commitment cover  Compute Optimizer's savings numbers assume on-demand
                       pricing. If the account is heavily covered by Reserved
                       Instances or Savings Plans, the marginal savings from
                       downsizing a covered instance are smaller than advertised,
                       and can be zero.
  4. Magnitude         A $6/mo change is noise, not a win worth a change window.

This module scores each recommendation against those, returns a verdict
(genuine_savings / review / likely_false_positive), a 0-100 score, a compact
one-line rationale, and a commitment-aware adjusted savings figure. Propose-only:
nothing here acts, it judges.

Token discipline: everything returned is scalar or a single short string. No raw
API payloads flow back into the model context.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Verdict thresholds on the 0-100 score.
_GENUINE_AT = 70
_REVIEW_AT  = 40

# Commitment-coverage cache. Account-level coverage moves slowly (commitments are
# monthly/annual), so one Cost Explorer read per ~15 min is plenty and keeps a
# chatty session from re-billing the same CE call (and the model round-trip).
_CTX_TTL_SEC = 900.0
_ctx_cache: tuple[float, "CommitmentContext"] | None = None


@dataclass
class CommitmentContext:
    """Account-level EC2 commitment coverage, used to discount on-demand savings."""
    available: bool = False
    sp_coverage_pct: float = 0.0     # Savings Plans coverage of EC2 usage
    ri_coverage_pct: float = 0.0     # Reserved Instance coverage of EC2 usage

    @property
    def combined_pct(self) -> float:
        # Coverage instruments don't stack on the same instance-hour; take the
        # stronger of the two as the conservative "how committed is this account".
        return max(self.sp_coverage_pct, self.ri_coverage_pct)


@dataclass
class Assessment:
    verdict: str            # "genuine_savings" | "review" | "likely_false_positive"
    score: int              # 0-100
    why: str                # one compact line
    action: str             # what applying it takes (blast radius / reversibility)
    adjusted_monthly_savings: float   # commitment-aware, conservative


def fetch_commitment_context(ce_client: Any = None) -> CommitmentContext:
    """
    Account-level EC2 SP + RI coverage, cached for _CTX_TTL_SEC. Degrades to an
    unavailable context (no penalty applied) whenever Cost Explorer is not
    reachable, so rightsizing never hard-depends on this.
    """
    global _ctx_cache
    now = time.time()
    if _ctx_cache and (now - _ctx_cache[0]) < _CTX_TTL_SEC:
        return _ctx_cache[1]

    ctx = CommitmentContext(available=False)
    try:
        from .commitments import _get_date_range, _savings_plan_coverage, _ri_coverage
        if ce_client is None:
            import boto3
            ce_client = boto3.client("ce", region_name="us-east-1")
        start, end = _get_date_range(months_back=1)
        sp = _savings_plan_coverage(ce_client, start, end)
        ri = _ri_coverage(ce_client, start, end)
        ctx = CommitmentContext(available=True, sp_coverage_pct=sp, ri_coverage_pct=ri)
    except Exception as e:  # pragma: no cover - defensive, CE optional
        log.debug("commitment context unavailable: %s", e)

    _ctx_cache = (now, ctx)
    return ctx


def _reset_cache_for_tests() -> None:
    global _ctx_cache
    _ctx_cache = None


def _action_for(resource_type: str) -> str:
    rt = (resource_type or "").lower()
    if rt == "lambda":
        return "Adjust the memory setting; takes effect immediately and is fully reversible."
    if rt == "rds":
        return "Modify the instance class in a maintenance window; reversible, brief failover."
    if rt == "ecs":
        return "Lower the task CPU/memory reservation and redeploy; reversible."
    return "Resize needs a stop/start (brief downtime); fully reversible."


def assess(rec: Any, ctx: CommitmentContext | None = None) -> Assessment:
    """
    Score one RightsizingRecommendation. `rec` is duck-typed: it just needs the
    fields RightsizingRecommendation carries (monthly_savings, avg_cpu_pct,
    max_cpu_pct, avg_mem_pct, source, finding, resource_type).
    """
    savings = float(getattr(rec, "monthly_savings", 0.0) or 0.0)
    avg_cpu = float(getattr(rec, "avg_cpu_pct", 0.0) or 0.0)
    max_cpu = float(getattr(rec, "max_cpu_pct", 0.0) or 0.0)
    mem = getattr(rec, "avg_mem_pct", None)
    source = getattr(rec, "source", "")
    finding = getattr(rec, "finding", "") or ""
    rtype = getattr(rec, "resource_type", "")

    score = 50
    reasons: list[str] = []

    # 1. Strength of the underutilization signal.
    if finding == "VERY_OVER_PROVISIONED":
        score += 30
        reasons.append("sustained over-provisioning (CPU+mem+net+disk)")
    elif finding == "OVER_PROVISIONED":
        score += 15
        reasons.append("over-provisioned")
    if source == "cloudwatch_fallback":
        score -= 10
        reasons.append(f"CPU-only avg {avg_cpu:.0f}%")

    # 2. Burst / peak guard. max_cpu==0 from Compute Optimizer means "unknown", so
    #    only judge peaks when we actually measured them (CloudWatch path).
    if max_cpu > 0:
        if max_cpu >= 60:
            score -= 35
            reasons.append(f"peaks to {max_cpu:.0f}% CPU, needs headroom")
        elif max_cpu >= 40:
            score -= 15
            reasons.append(f"peaks to {max_cpu:.0f}% CPU")

    # 3. Memory-bound guard: cheap CPU downsize that starves RAM is not savings.
    if mem is not None:
        if mem >= 75:
            score -= 25
            reasons.append(f"memory at {mem:.0f}%, likely memory-bound")
        elif mem >= 60:
            score -= 10
            reasons.append(f"memory at {mem:.0f}%")

    # 4. Commitment coverage: discount the on-demand estimate to the marginal
    #    saving a downsize actually yields on a committed account. This shrinks the
    #    dollars, it does not make a genuinely-idle box un-idle, so the score nudge
    #    stays small (added uncertainty only) and magnitude below judges the real
    #    number. Discounting AND heavily penalizing would double-count.
    adjusted = savings
    if ctx and ctx.available and ctx.combined_pct > 0 and savings > 0:
        cov = ctx.combined_pct
        # Conservative floor: assume the covered fraction yields no marginal
        # savings from downsizing (the commitment is already paid for).
        adjusted = round(savings * max(0.0, 1.0 - cov / 100.0), 2)
        if cov >= 40:
            score -= 8
            reasons.append(f"~{cov:.0f}% of this account's EC2 is commitment-covered, "
                           f"real saving ≈${adjusted:,.0f}/mo")

    # 5. Magnitude, judged on the REAL (commitment-adjusted) saving.
    if adjusted < 15:
        score -= 10
        reasons.append(f"only ~${adjusted:,.0f}/mo")
    elif adjusted >= 200:
        score += 10

    score = max(0, min(100, score))

    if score >= _GENUINE_AT:
        verdict = "genuine_savings"
    elif score >= _REVIEW_AT:
        verdict = "review"
    else:
        verdict = "likely_false_positive"

    why = "; ".join(reasons[:3]) if reasons else f"avg CPU {avg_cpu:.0f}%"
    # Keep it one tight line.
    why = (why[:157] + "...") if len(why) > 160 else why

    return Assessment(
        verdict=verdict,
        score=score,
        why=why,
        action=_action_for(rtype),
        adjusted_monthly_savings=adjusted,
    )
