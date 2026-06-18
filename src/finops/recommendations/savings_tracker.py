"""
Savings recommendation lifecycle tracker.

Every recommendation nable surfaces (rightsizing, idle resources, K8s waste,
commitment gaps, waste patterns) is persisted here so we can answer:

  "How much have we actually saved from recommendations we acted on?"

Lifecycle:
  open → acted_on → verified   (ideal path)
  open → dismissed              (won't fix)
  open → expired                (30+ days, never actioned)

Verification:
  For EC2/RDS: re-query the instance type after acted_on and compare cost.
  For idle resources: check the resource still exists.
  For K8s: re-run namespace cost and compare.
  For commitments: compare coverage % before/after.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select, update

from ..storage.db import get_engine, savings_recommendations

log = logging.getLogger(__name__)

# Recommendations expire (auto-closed as stale) after this many days unactioned
_EXPIRY_DAYS = 45


# ── Dedup key ─────────────────────────────────────────────────────────────────

def _dedup(source: str, resource_id: str, recommended_config: dict) -> str:
    raw = f"{source}:{resource_id}:{json.dumps(recommended_config, sort_keys=True)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _bucket(resource_type: str | None, environment: str | None) -> str | None:
    """Coarse env/workload bucket for the learning signal. Lazy import keeps the
    learning package (which imports this module) out of import-time cycles."""
    try:
        from .learning.bucket import bucket_for
        return bucket_for(resource_type, environment)
    except Exception:
        return None


# ── Upsert a recommendation ───────────────────────────────────────────────────

def record_recommendation(
    source: str,
    provider: str,
    resource_id: str,
    resource_type: str,
    resource_name: str,
    current_config: dict,
    recommended_config: dict,
    description: str,
    estimated_monthly_savings_usd: float,
    account_id: str = "",
    region: str = "",
    environment: str | None = None,
) -> int | None:
    """
    Persist a recommendation. Returns the row ID, or None if it already exists.
    Existing open/acted_on records are NOT overwritten — we only insert new ones.
    """
    key = _dedup(source, resource_id, recommended_config)
    engine = get_engine()
    with engine.begin() as conn:
        # Check if already exists
        existing = conn.execute(
            select(savings_recommendations.c.id, savings_recommendations.c.status)
            .where(savings_recommendations.c.dedup_key == key)
        ).first()

        if existing:
            # If previously dismissed/expired, re-open it (resource regressed)
            if existing.status in ("dismissed", "expired"):
                conn.execute(
                    update(savings_recommendations)
                    .where(savings_recommendations.c.id == existing.id)
                    .values(
                        status="open",
                        estimated_monthly_savings_usd=estimated_monthly_savings_usd,
                        description=description,
                        current_config=json.dumps(current_config),
                        generated_at=datetime.now(timezone.utc),
                        acted_on_at=None,
                        verified_at=None,
                        dismissed_at=None,
                        dismiss_reason=None,
                        verified_monthly_savings_usd=None,
                    )
                )
            return existing.id

        result = conn.execute(
            savings_recommendations.insert().values(
                source=source,
                provider=provider,
                account_id=account_id,
                region=region,
                resource_id=resource_id,
                resource_type=resource_type,
                resource_name=resource_name,
                current_config=json.dumps(current_config),
                recommended_config=json.dumps(recommended_config),
                description=description,
                estimated_monthly_savings_usd=estimated_monthly_savings_usd,
                status="open",
                generated_at=datetime.now(timezone.utc),
                dedup_key=key,
                environment_bucket=_bucket(resource_type, environment),
            )
        )
        return result.inserted_primary_key[0]


# ── Status transitions ────────────────────────────────────────────────────────

def mark_acted_on(rec_id: int) -> bool:
    engine = get_engine()
    with engine.begin() as conn:
        r = conn.execute(
            update(savings_recommendations)
            .where(
                savings_recommendations.c.id == rec_id,
                savings_recommendations.c.status == "open",
            )
            .values(status="acted_on", acted_on_at=datetime.now(timezone.utc))
        )
        return r.rowcount > 0


def mark_verified(rec_id: int, actual_monthly_savings_usd: float) -> bool:
    engine = get_engine()
    with engine.begin() as conn:
        r = conn.execute(
            update(savings_recommendations)
            .where(savings_recommendations.c.id == rec_id)
            .values(
                status="verified",
                verified_at=datetime.now(timezone.utc),
                verified_monthly_savings_usd=actual_monthly_savings_usd,
            )
        )
        return r.rowcount > 0


def mark_dismissed(rec_id: int, reason: str = "") -> bool:
    engine = get_engine()
    with engine.begin() as conn:
        r = conn.execute(
            update(savings_recommendations)
            .where(
                savings_recommendations.c.id == rec_id,
                savings_recommendations.c.status.in_(["open", "acted_on"]),
            )
            .values(
                status="dismissed",
                dismissed_at=datetime.now(timezone.utc),
                dismiss_reason=reason or None,
            )
        )
        return r.rowcount > 0


def expire_stale(days: int = _EXPIRY_DAYS) -> int:
    """Mark open recommendations older than `days` as expired."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    engine = get_engine()
    with engine.begin() as conn:
        r = conn.execute(
            update(savings_recommendations)
            .where(
                savings_recommendations.c.status == "open",
                savings_recommendations.c.generated_at < cutoff,
            )
            .values(status="expired")
        )
        return r.rowcount


# ── Summary queries ───────────────────────────────────────────────────────────

def get_summary() -> dict[str, Any]:
    """
    Return the realized-savings dashboard:
      - potential_monthly_usd: sum of open recommendations
      - acted_on_monthly_usd:  estimated savings from acted-on recs
      - verified_monthly_usd:  actual measured savings (verified recs)
      - counts by status and source

    Uses GROUP BY aggregation rather than a full table scan so this stays
    fast as the recommendations table grows.
    """
    sr = savings_recommendations
    engine = get_engine()

    # Aggregate counts and sums per (status, source) in one query
    with engine.connect() as conn:
        agg_rows = conn.execute(
            select(
                sr.c.status,
                sr.c.source,
                func.count().label("cnt"),
                func.sum(sr.c.estimated_monthly_savings_usd).label("sum_est"),
                func.sum(sr.c.verified_monthly_savings_usd).label("sum_verified"),
            ).group_by(sr.c.status, sr.c.source)
        ).fetchall()

        total_count = conn.execute(
            select(func.count()).select_from(sr)
        ).scalar() or 0

    # Known status buckets for by_source breakdown
    _STATUS_KEYS = ("open", "acted_on", "verified", "dismissed", "expired")

    potential = 0.0
    acted_estimated = 0.0
    verified_actual = 0.0
    by_status: dict[str, int] = {}
    by_source: dict[str, dict] = {}

    for row in agg_rows:
        s = row.status
        src = row.source
        cnt = row.cnt or 0
        sum_est = float(row.sum_est or 0.0)
        sum_ver = float(row.sum_verified or 0.0)

        by_status[s] = by_status.get(s, 0) + cnt

        if src not in by_source:
            by_source[src] = {k: 0 for k in _STATUS_KEYS}
            by_source[src]["potential_usd"] = 0.0
            by_source[src]["verified_usd"] = 0.0

        # Only bucket into known status keys; unknown statuses fall under "dismissed"
        bucket = s if s in _STATUS_KEYS else "dismissed"
        by_source[src][bucket] = by_source[src].get(bucket, 0) + cnt
        by_source[src]["potential_usd"] += sum_est if s == "open" else 0.0
        by_source[src]["verified_usd"] += sum_ver if s == "verified" else 0.0

        if s == "open":
            potential += sum_est
        elif s == "acted_on":
            acted_estimated += sum_est
        elif s == "verified":
            verified_actual += sum_ver

    return {
        "potential_monthly_usd": round(potential, 2),
        "acted_on_monthly_usd": round(acted_estimated, 2),   # estimated, not yet verified
        "verified_monthly_usd": round(verified_actual, 2),   # confirmed actual savings
        "verified_annual_usd": round(verified_actual * 12, 2),
        "total_recommendations": total_count,
        "by_status": by_status,
        "by_source": by_source,
    }


def quality_signal() -> dict[str, Any]:
    """
    The recommendation-quality flywheel: per recommendation type (source), how
    often recs actually get acted on and how close the PREDICTED savings were to
    the MEASURED realized savings.

    This is the moat's training signal (rank and suppress low-yield rec types) and
    the verified-savings proof, not just a log of what we suggested. Per source:
    total, acted (acted_on + verified), verified, predicted_monthly_usd,
    realized_monthly_usd, act_rate (acted / total), and accuracy (realized vs
    predicted among VERIFIED recs only: 1.0 = predictions landed, <1 = we
    over-predicted, >1 = under-predicted). Plus the headline verified monthly and
    annual run-rate. Sorted so the rec types that actually pay off lead.
    """
    sr = savings_recommendations
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                sr.c.source,
                sr.c.status,
                func.count().label("cnt"),
                func.sum(sr.c.estimated_monthly_savings_usd).label("sum_est"),
                func.sum(sr.c.verified_monthly_savings_usd).label("sum_ver"),
            ).group_by(sr.c.source, sr.c.status)
        ).fetchall()

    agg: dict[str, dict] = {}
    for r in rows:
        d = agg.setdefault(r.source or "unknown", {
            "total": 0, "acted": 0, "verified": 0,
            "predicted_monthly_usd": 0.0,
            "realized_monthly_usd": 0.0,
            "predicted_of_verified_usd": 0.0,
        })
        cnt = r.cnt or 0
        d["total"] += cnt
        d["predicted_monthly_usd"] += float(r.sum_est or 0.0)
        if r.status in ("acted_on", "verified"):
            d["acted"] += cnt
        if r.status == "verified":
            d["verified"] += cnt
            d["realized_monthly_usd"] += float(r.sum_ver or 0.0)
            d["predicted_of_verified_usd"] += float(r.sum_est or 0.0)

    by_source = []
    total_realized = 0.0
    for src, d in agg.items():
        total = d["total"] or 1
        pov = d["predicted_of_verified_usd"]
        realized = d["realized_monthly_usd"]
        total_realized += realized
        by_source.append({
            "source": src,
            "total": d["total"],
            "acted": d["acted"],
            "verified": d["verified"],
            "act_rate": round(d["acted"] / total, 3),
            "predicted_monthly_usd": round(d["predicted_monthly_usd"], 2),
            "realized_monthly_usd": round(realized, 2),
            # accuracy is only meaningful once a source has verified recs to measure
            "accuracy": round(realized / pov, 3) if pov > 0 else None,
        })
    by_source.sort(key=lambda s: s["realized_monthly_usd"], reverse=True)

    return {
        "verified_monthly_usd": round(total_realized, 2),
        "verified_annual_run_rate_usd": round(total_realized * 12, 2),
        "by_source": by_source,
        "note": ("accuracy compares predicted vs measured realized savings among "
                 "verified recommendations; act_rate is acted-or-verified over total. "
                 "Sources with a low act_rate or low accuracy are candidates to suppress."),
    }


def list_recommendations(
    status: str | None = None,
    source: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    engine = get_engine()
    q = select(savings_recommendations).order_by(
        savings_recommendations.c.estimated_monthly_savings_usd.desc()
    )
    if status:
        q = q.where(savings_recommendations.c.status == status)
    if source:
        q = q.where(savings_recommendations.c.source == source)
    q = q.limit(limit)

    with engine.connect() as conn:
        rows = conn.execute(q).fetchall()

    result = []
    for r in rows:
        result.append({
            "id": r.id,
            "source": r.source,
            "provider": r.provider,
            "account_id": r.account_id,
            "resource_id": r.resource_id,
            "resource_name": r.resource_name,
            "description": r.description,
            "estimated_monthly_savings_usd": r.estimated_monthly_savings_usd,
            "verified_monthly_savings_usd": r.verified_monthly_savings_usd,
            "recommended_config": r.recommended_config,
            "current_config": r.current_config,
            "status": r.status,
            "generated_at": r.generated_at.isoformat() if r.generated_at else None,
            "acted_on_at": r.acted_on_at.isoformat() if r.acted_on_at else None,
            "verified_at": r.verified_at.isoformat() if r.verified_at else None,
            "dismiss_reason": r.dismiss_reason,
            "environment_bucket": getattr(r, "environment_bucket", None),
        })
    return result


# ── Verification: check if changes were actually made ────────────────────────

def verify_ec2_change(resource_id: str, recommended_config: dict) -> float | None:
    """
    Check if an EC2 instance was actually resized.
    Returns the estimated monthly savings if changed, None if not changed yet.
    """
    try:
        import boto3
        ec2 = boto3.client("ec2")
        resp = ec2.describe_instances(InstanceIds=[resource_id])
        reservations = resp.get("Reservations", [])
        if not reservations:
            return None
        instance = reservations[0]["Instances"][0]
        current_type = instance.get("InstanceType", "")
        target_type = recommended_config.get("instance_type", "")

        if current_type == target_type:
            # Change confirmed — estimate savings from type difference
            from ..connectors.terraform_estimate import _EC2_HOURLY
            old_type = recommended_config.get("from_instance_type", "")
            old_hourly = _EC2_HOURLY.get(old_type, 0.0)
            new_hourly = _EC2_HOURLY.get(target_type, 0.0)
            from ..connectors.terraform_estimate import HOURS_PER_MONTH
            return round((old_hourly - new_hourly) * HOURS_PER_MONTH, 2)
        return None
    except Exception as e:
        log.debug("verify_ec2_change error: %s", e)
        return None


def auto_verify_acted_on() -> list[dict]:
    """
    For all acted_on recommendations, attempt to verify the change was made.
    Returns list of newly-verified records with their actual savings.
    """
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(savings_recommendations)
            .where(savings_recommendations.c.status == "acted_on")
        ).fetchall()

    newly_verified = []
    for r in rows:
        try:
            rec_config = json.loads(r.recommended_config or "{}")
            actual_savings = None

            if r.source == "rightsizing" and r.resource_type == "ec2":
                actual_savings = verify_ec2_change(r.resource_id, rec_config)
            # Future: RDS, K8s, etc.

            if actual_savings is not None:
                mark_verified(r.id, actual_savings)
                newly_verified.append({
                    "id": r.id,
                    "resource_id": r.resource_id,
                    "description": r.description,
                    "verified_monthly_savings_usd": actual_savings,
                })
        except Exception as e:
            log.debug("auto_verify row %s: %s", r.id, e)

    return newly_verified
