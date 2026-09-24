from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import and_, select

from .db import attributed_costs, cost_snapshots, get_engine


def _now() -> datetime:
    return datetime.now(timezone.utc)


def store_snapshot(
    provider: str,
    service: str,
    account_id: str,
    region: str,
    snapshot_date: date,
    amount_usd: float,
    granularity: str = "DAILY",
    category: str | None = None,
) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        # Upsert: delete existing row for same key, then insert
        conn.execute(
            cost_snapshots.delete().where(
                and_(
                    cost_snapshots.c.provider == provider,
                    cost_snapshots.c.service == service,
                    cost_snapshots.c.account_id == account_id,
                    cost_snapshots.c.region == region,
                    cost_snapshots.c.snapshot_date == snapshot_date.isoformat(),
                )
            )
        )
        conn.execute(cost_snapshots.insert().values(
            provider=provider,
            service=service,
            account_id=account_id,
            region=region,
            snapshot_date=snapshot_date.isoformat(),
            amount_usd=amount_usd,
            granularity=granularity,
            captured_at=_now(),
            category=category,
        ))


def replace_provider_day(provider: str, day: date, rows: list[dict]) -> int:
    """Replace everything a provider recorded for one day. Returns rows written.

    store_snapshot upserts on (provider, service, account_id, region, date),
    which is right when one source keeps writing the same day. It is wrong the
    moment a SECOND source writes the same day under different service names.

    Cost Explorer calls it "Amazon Elastic Compute Cloud - Compute". The CUR
    calls the same spend "Amazon Elastic Compute Cloud". Those are different
    keys, so an upsert would not overwrite the CE row, it would sit beside it,
    and the day would read as twice the money. A cost tool that doubles a bill
    because it got better at reading it is the worst possible bug to ship.

    Replacing the whole provider-day is safe precisely because the CUR is
    complete for any day it covers: there is nothing left over that should have
    survived. Done in one transaction so a crash mid-write cannot leave the day
    empty.
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(cost_snapshots.delete().where(
            and_(cost_snapshots.c.provider == provider,
                 cost_snapshots.c.snapshot_date == day.isoformat())))
        if not rows:
            return 0
        now = _now()
        conn.execute(cost_snapshots.insert(), [
            {
                "provider": provider,
                "service": r.get("service") or "unknown",
                "account_id": r.get("account_id") or "",
                "region": r.get("region") or "",
                "snapshot_date": day.isoformat(),
                "amount_usd": float(r.get("amount_usd") or 0.0),
                "granularity": r.get("granularity") or "DAILY",
                "captured_at": now,
                "category": r.get("category"),
            }
            for r in rows
        ])
    return len(rows)


def store_zero_for_stopped_series(
    provider: str,
    day: date,
    seen: set[tuple[str, str, str]],
    recent_days: int = 7,
) -> int:
    """Write a $0 row for each recently billed series missing from `day`'s fetch.

    Snapshots store only what the provider billed, so a service that stopped
    billing had no row for the day at all, and anomaly detection, which walks the
    day's rows, never looked at it. A $4,000/day pipeline going to $0 is exactly
    the drop worth catching. `seen` holds the (service, account_id, region) keys
    the fetch did return. Bounded to series with spend in the last `recent_days`,
    so a service retired months ago is not zero-filled forever. Returns rows written.
    """
    from datetime import timedelta
    start = (day - timedelta(days=recent_days)).isoformat()
    engine = get_engine()
    with engine.connect() as conn:
        recent = conn.execute(
            select(cost_snapshots.c.service, cost_snapshots.c.account_id,
                   cost_snapshots.c.region)
            .where(
                and_(
                    cost_snapshots.c.provider == provider,
                    cost_snapshots.c.snapshot_date >= start,
                    cost_snapshots.c.snapshot_date < day.isoformat(),
                    cost_snapshots.c.amount_usd > 0,
                )
            )
            .distinct()
        ).fetchall()
    missing = {tuple(r) for r in recent} - seen
    for service, account_id, region in sorted(missing):
        store_snapshot(provider, service, account_id, region, day, 0.0)
    return len(missing)


def latest_captured_at() -> str | None:
    """ISO timestamp of the most recent cost snapshot, or None if there are none.

    This is the freshness of the cost data a budget's run-rate is computed from, so
    the pre-action gate can label its budget verdict with a data age. A small sorted
    read (add an index on cost_snapshots.captured_at if the table grows large); never
    a live provider call.
    """
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(cost_snapshots.c.captured_at)
            .order_by(cost_snapshots.c.captured_at.desc())
            .limit(1)
        ).first()
    if not row or row[0] is None:
        return None
    val = row[0]
    return val.isoformat() if hasattr(val, "isoformat") else str(val)


def get_history(
    provider: str,
    service: str,
    account_id: str,
    days: int = 28,
    region: str | None = None,
) -> list[dict[str, Any]]:
    """Daily rows for one series over the last `days` days, oldest first.

    Pass `region` to get that region's series only. Snapshots are stored per
    region, so without it a service billing in two regions returns both sets of
    rows interleaved, and a baseline built from them is the average of two
    unrelated series. None keeps the all-regions answer for callers that want it.
    """
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    conds = [
        cost_snapshots.c.provider == provider,
        cost_snapshots.c.service == service,
        cost_snapshots.c.account_id == account_id,
        cost_snapshots.c.snapshot_date >= cutoff,
    ]
    if region is not None:
        conds.append(cost_snapshots.c.region == region)
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(cost_snapshots)
            .where(and_(*conds))
            .order_by(cost_snapshots.c.snapshot_date)
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def get_all_provider_history(provider: str, days: int = 28) -> list[dict[str, Any]]:
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(cost_snapshots)
            .where(
                and_(
                    cost_snapshots.c.provider == provider,
                    cost_snapshots.c.snapshot_date >= cutoff,
                )
            )
            .order_by(cost_snapshots.c.snapshot_date, cost_snapshots.c.service)
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def store_attributed_cost(
    provider: str,
    service: str,
    account_id: str,
    team: str,
    environment: str,
    snapshot_date: date,
    amount_usd: float,
) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        # Environment is part of the key: teamA/prod and teamA/dev on the same
        # day are two rows, and leaving it out let the second replace the first.
        conn.execute(
            attributed_costs.delete().where(
                and_(
                    attributed_costs.c.provider == provider,
                    attributed_costs.c.service == service,
                    attributed_costs.c.account_id == account_id,
                    attributed_costs.c.team == team,
                    attributed_costs.c.environment == environment,
                    attributed_costs.c.snapshot_date == snapshot_date.isoformat(),
                )
            )
        )
        conn.execute(attributed_costs.insert().values(
            provider=provider,
            service=service,
            account_id=account_id,
            team=team,
            environment=environment,
            snapshot_date=snapshot_date.isoformat(),
            amount_usd=amount_usd,
            captured_at=_now(),
        ))


def get_costs_by_team(
    start_date: date,
    end_date: date,
    provider: str | None = None,
) -> list[dict[str, Any]]:
    from sqlalchemy import func
    engine = get_engine()
    query = (
        select(
            attributed_costs.c.team,
            attributed_costs.c.provider,
            attributed_costs.c.environment,
            func.sum(attributed_costs.c.amount_usd).label("total_usd"),
        )
        .where(
            and_(
                attributed_costs.c.snapshot_date >= start_date.isoformat(),
                attributed_costs.c.snapshot_date <= end_date.isoformat(),
            )
        )
        .group_by(attributed_costs.c.team, attributed_costs.c.provider, attributed_costs.c.environment)
        .order_by(func.sum(attributed_costs.c.amount_usd).desc())
    )
    if provider:
        query = query.where(attributed_costs.c.provider == provider)
    with get_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(query).fetchall()]
