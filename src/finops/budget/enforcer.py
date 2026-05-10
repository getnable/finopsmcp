"""
Budget enforcement engine.

Budgets are stored in the DB (budgets table) and checked against actual spend
from cost_snapshots / attributed_costs. Supports:

  - Total account budget
  - Per-provider budget (aws, azure, gcp, etc.)
  - Per-team budget (via attributed_costs)
  - Per-service budget

Two-tier alerting:
  alert_at_pct  (default 80%) → send notification, create warning ticket
  block_at_pct  (default 100%) → fail CI (budget check exits non-zero)

budget.yml format (committed alongside infra code):
────────────────────────────────────────────────────
budgets:
  - name: Platform Team Monthly
    scope_type: team
    scope_value: platform
    period: monthly
    limit_usd: 15000
    alert_at_pct: 80
    block_at_pct: 100

  - name: AWS Total
    scope_type: provider
    scope_value: aws
    period: monthly
    limit_usd: 50000
    alert_at_pct: 75

  - name: EC2 Compute
    scope_type: service
    scope_value: "Amazon Elastic Compute Cloud - Compute"
    period: monthly
    limit_usd: 20000
────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── Budget CRUD ───────────────────────────────────────────────────────────────

def create_budget(
    name: str,
    scope_type: str,        # "total" | "provider" | "team" | "service"
    limit_usd: float,
    scope_value: str = "*",
    period: str = "monthly",
    alert_at_pct: float = 80.0,
    block_at_pct: float = 100.0,
    created_by: str = "mcp",
) -> dict[str, Any]:
    from ..storage.db import budgets, get_engine
    from sqlalchemy import insert

    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        result = conn.execute(insert(budgets).values(
            name=name,
            scope_type=scope_type,
            scope_value=scope_value,
            period=period,
            limit_usd=limit_usd,
            alert_at_pct=alert_at_pct,
            block_at_pct=block_at_pct,
            created_at=now,
            updated_at=now,
            created_by=created_by,
            is_active=True,
        ))
        budget_id = result.inserted_primary_key[0]

    return {
        "id": budget_id,
        "name": name,
        "scope_type": scope_type,
        "scope_value": scope_value,
        "limit_usd": limit_usd,
        "period": period,
        "alert_at_pct": alert_at_pct,
        "block_at_pct": block_at_pct,
    }


def list_budgets(active_only: bool = True) -> list[dict[str, Any]]:
    from ..storage.db import budgets, get_engine
    from sqlalchemy import select

    q = select(budgets)
    if active_only:
        q = q.where(budgets.c.is_active == True)
    with get_engine().connect() as conn:
        rows = conn.execute(q.order_by(budgets.c.name)).fetchall()
    return [dict(r._mapping) for r in rows]


def delete_budget(budget_id: int) -> bool:
    from ..storage.db import budgets, get_engine
    from sqlalchemy import update
    with get_engine().begin() as conn:
        result = conn.execute(
            update(budgets).where(budgets.c.id == budget_id).values(is_active=False)
        )
    return result.rowcount > 0


# ── Spend fetchers ────────────────────────────────────────────────────────────

def _period_dates(period: str) -> tuple[str, str]:
    today = date.today()
    if period == "monthly":
        start = today.replace(day=1)
        # end of month
        if today.month == 12:
            end = date(today.year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(today.year, today.month + 1, 1) - timedelta(days=1)
        return start.isoformat(), end.isoformat()
    elif period == "weekly":
        start = today - timedelta(days=today.weekday())  # Monday
        end = start + timedelta(days=6)
        return start.isoformat(), end.isoformat()
    else:
        # Default: last 30 days
        return (today - timedelta(days=30)).isoformat(), today.isoformat()


def _fetch_spend(budget: dict[str, Any], start: str, end: str, conn: Any) -> float:
    """
    Fetch actual spend for a budget using a caller-provided connection.
    Accepts pre-computed (start, end) so we never call _period_dates twice.
    """
    from ..storage.db import cost_snapshots, attributed_costs
    from sqlalchemy import func, select

    scope_type  = budget["scope_type"]
    scope_value = budget["scope_value"]

    if scope_type == "total":
        q = select(func.sum(cost_snapshots.c.amount_usd)).where(
            cost_snapshots.c.snapshot_date >= start,
            cost_snapshots.c.snapshot_date <= end,
        )
    elif scope_type == "provider":
        q = select(func.sum(cost_snapshots.c.amount_usd)).where(
            cost_snapshots.c.snapshot_date >= start,
            cost_snapshots.c.snapshot_date <= end,
            cost_snapshots.c.provider == scope_value,
        )
    elif scope_type == "service":
        q = select(func.sum(cost_snapshots.c.amount_usd)).where(
            cost_snapshots.c.snapshot_date >= start,
            cost_snapshots.c.snapshot_date <= end,
            cost_snapshots.c.service == scope_value,
        )
    elif scope_type == "team":
        q = select(func.sum(attributed_costs.c.amount_usd)).where(
            attributed_costs.c.snapshot_date >= start,
            attributed_costs.c.snapshot_date <= end,
            attributed_costs.c.team == scope_value,
        )
    else:
        return 0.0

    return float(conn.execute(q).scalar() or 0.0)


# ── Budget checker ────────────────────────────────────────────────────────────

def check_budget(budget: dict[str, Any], conn: Any = None) -> dict[str, Any]:
    """Check a single budget against actual spend. Returns status dict.
    Pass an open SQLAlchemy connection to avoid opening a new one."""
    from ..storage.db import get_engine
    start, end = _period_dates(budget["period"])
    if conn is None:
        with get_engine().connect() as _conn:
            spent = _fetch_spend(budget, start, end, _conn)
    else:
        spent = _fetch_spend(budget, start, end, conn)
    limit = budget["limit_usd"]
    pct_used = (spent / limit * 100) if limit else 0.0
    alert_pct = budget.get("alert_at_pct", 80.0)
    block_pct = budget.get("block_at_pct", 100.0)

    if pct_used >= block_pct:
        status = "exceeded"
    elif pct_used >= alert_pct:
        status = "warning"
    else:
        status = "ok"

    days_elapsed = (date.today() - date.fromisoformat(start)).days + 1
    days_in_period = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    run_rate_monthly = (spent / days_elapsed * days_in_period) if days_elapsed > 0 else 0

    return {
        "id": budget.get("id"),
        "name": budget["name"],
        "scope_type": budget["scope_type"],
        "scope_value": budget["scope_value"],
        "period": budget["period"],
        "period_start": start,
        "period_end": end,
        "spent": round(spent, 2),
        "limit": round(limit, 2),
        "remaining": round(max(0, limit - spent), 2),
        "pct_used": round(pct_used, 1),
        "status": status,
        "run_rate_monthly": round(run_rate_monthly, 2),
        "projected_overage": round(max(0, run_rate_monthly - limit), 2),
    }


def check_all_budgets() -> list[dict[str, Any]]:
    """Check all active budgets using a single shared DB connection — O(n) queries
    instead of O(n) connections. Returns list sorted by % used descending."""
    from ..storage.db import get_engine
    budget_list = list_budgets(active_only=True)
    if not budget_list:
        return []
    results = []
    with get_engine().connect() as conn:          # one connection for all budgets
        for b in budget_list:
            try:
                results.append(check_budget(b, conn=conn))
            except Exception as e:
                log.warning("Budget check failed for %s: %s", b.get("name"), e)
    return sorted(results, key=lambda x: x["pct_used"], reverse=True)


# ── budget.yml sync ───────────────────────────────────────────────────────────

def sync_from_yaml(yaml_path: str) -> dict[str, Any]:
    """
    Read a budget.yml file and upsert budgets into the DB.
    Idempotent — running twice doesn't create duplicates.

    budget.yml format:
        budgets:
          - name: Platform Team Monthly
            scope_type: team
            scope_value: platform
            period: monthly
            limit_usd: 15000
            alert_at_pct: 80
            block_at_pct: 100
    """
    try:
        import yaml
    except ImportError:
        return {"error": "PyYAML not installed. Run: pip install pyyaml"}

    path = Path(yaml_path)
    if not path.exists():
        return {"error": f"File not found: {yaml_path}"}

    with open(path) as f:
        config = yaml.safe_load(f)

    raw_budgets = config.get("budgets", [])
    if not raw_budgets:
        return {"error": "No budgets found in file"}

    from ..storage.db import budgets as budgets_table, get_engine
    from sqlalchemy import select, update, insert

    engine = get_engine()
    created = []
    updated_list = []
    now = datetime.now(timezone.utc)

    # Single read to get all existing names — O(1) instead of O(n) round-trips
    with engine.connect() as conn:
        existing_names: set[str] = {
            r.name for r in conn.execute(select(budgets_table.c.name)).fetchall()
        }

    valid = [(b, b.get("name", "")) for b in raw_budgets if b.get("name")]

    # Batch INSERT all new budgets in one execute() call
    to_insert = [
        dict(
            name=name,
            scope_type=b.get("scope_type", "total"),
            scope_value=b.get("scope_value", "*"),
            period=b.get("period", "monthly"),
            limit_usd=float(b.get("limit_usd", 0)),
            alert_at_pct=float(b.get("alert_at_pct", 80)),
            block_at_pct=float(b.get("block_at_pct", 100)),
            created_at=now,
            updated_at=now,
            created_by="budget.yml",
            is_active=True,
        )
        for b, name in valid if name not in existing_names
    ]
    if to_insert:
        with engine.begin() as conn:
            conn.execute(insert(budgets_table), to_insert)
        created = [r["name"] for r in to_insert]

    # Batch UPDATE existing budgets in a single transaction
    to_update = [(b, name) for b, name in valid if name in existing_names]
    if to_update:
        with engine.begin() as conn:
            for b, name in to_update:
                conn.execute(
                    update(budgets_table).where(budgets_table.c.name == name).values(
                        scope_type=b.get("scope_type", "total"),
                        scope_value=b.get("scope_value", "*"),
                        period=b.get("period", "monthly"),
                        limit_usd=float(b.get("limit_usd", 0)),
                        alert_at_pct=float(b.get("alert_at_pct", 80)),
                        block_at_pct=float(b.get("block_at_pct", 100)),
                        updated_at=now,
                        is_active=True,
                    )
                )
        updated_list = [name for _, name in to_update]

    return {
        "source": str(path),
        "created": created,
        "updated": updated_list,
        "total": len(created) + len(updated_list),
    }


# ── CI gate — used by GitHub Actions ─────────────────────────────────────────

def ci_gate(
    budget_yaml: str | None = None,
    fail_on_exceeded: bool = True,
) -> int:
    """
    CI gate: check all budgets and exit non-zero if any are exceeded.
    Used by the GitHub Actions budget check step.

    Returns exit code: 0 = all good, 1 = budget exceeded
    """
    if budget_yaml and Path(budget_yaml).exists():
        sync_from_yaml(budget_yaml)

    results = check_all_budgets()
    if not results:
        print("✅ No budgets configured")
        return 0

    exceeded = [b for b in results if b["status"] == "exceeded"]
    warnings  = [b for b in results if b["status"] == "warning"]
    ok        = [b for b in results if b["status"] == "ok"]

    print(f"\n{'─'*60}")
    print(f"  nable Budget Check — {date.today().isoformat()}")
    print(f"{'─'*60}")
    for b in results:
        icon = "❌" if b["status"] == "exceeded" else "⚠️ " if b["status"] == "warning" else "✅"
        print(f"  {icon} {b['name']}: ${b['spent']:,.0f} / ${b['limit']:,.0f} ({b['pct_used']:.0f}%)")
    print(f"{'─'*60}")
    print(f"  {len(ok)} OK · {len(warnings)} warnings · {len(exceeded)} exceeded")
    print(f"{'─'*60}\n")

    if exceeded and fail_on_exceeded:
        print("❌ Budget gate FAILED — one or more budgets exceeded")
        return 1

    if warnings:
        print("⚠️  Budget warnings — review spend")

    return 0
