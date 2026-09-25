"""
Budget alerting engine.

Budgets are stored in the DB (budgets table) and checked against actual spend
from cost_snapshots / attributed_costs. Supports:

  - Total account budget
  - Per-provider budget (aws, azure, gcp, etc.)
  - Per-account budget (the account_id on cost_snapshots)
  - Per-team budget (via attributed_costs)
  - Per-service budget

Two-tier alerting:
  alert_at_pct    (default 80%)  → warning notification
  critical_at_pct (default 100%) → critical notification, and a breach

What a breach stops is the team's choice, and off by default:
  - `nable budget ci-gate --fail-on-breach` fails a pipeline step (exit 1)
    when a budget is breached; without the flag it only reports.
  - The guard hook asks before an agent's priced change that would take a
    budget over its limit, or denies it when the policy says
    `on_budget_breach: deny` (guard.budget_lens reads the summary that
    check_all_budgets writes, budget/summary.py).

budget.yml format (committed alongside infra code):
────────────────────────────────────────────────────
budgets:
  - name: Platform Team Monthly
    scope_type: team
    scope_value: platform
    period: monthly
    limit_usd: 15000
    alert_at_pct: 80

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

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── Budget CRUD ───────────────────────────────────────────────────────────────

def create_budget(
    name: str,
    scope_type: str,        # "total" | "provider" | "account" | "team" | "service"
    limit_usd: float,
    scope_value: str = "*",
    period: str = "monthly",
    alert_at_pct: float = 80.0,
    critical_at_pct: float = 100.0,
    created_by: str = "mcp",
    block_at_pct: float | None = None,
) -> dict[str, Any]:
    from ..storage.db import budgets, get_engine
    from sqlalchemy import insert

    # block_at_pct is the set_budget tool's name for critical_at_pct (and the
    # old configs' one). Refusing it made that tool fail on every call.
    if block_at_pct is not None:
        critical_at_pct = float(block_at_pct)
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        result = conn.execute(insert(budgets).values(
            name=name,
            scope_type=scope_type,
            scope_value=scope_value,
            period=period,
            limit_usd=limit_usd,
            alert_at_pct=alert_at_pct,
            critical_at_pct=critical_at_pct,
            created_at=now,
            updated_at=now,
            created_by=created_by,
            is_active=True,
        ))
        budget_id = result.inserted_primary_key[0]

    refresh_summary()
    return {
        "id": budget_id,
        "name": name,
        "scope_type": scope_type,
        "scope_value": scope_value,
        "limit_usd": limit_usd,
        "period": period,
        "alert_at_pct": alert_at_pct,
        "critical_at_pct": critical_at_pct,
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
    refresh_summary()
    return result.rowcount > 0


# ── Spend fetchers ────────────────────────────────────────────────────────────

def _period_dates(period: str) -> tuple[str, str]:
    today = date.today()
    if period == "monthly":
        start = today.replace(day=1)
        if today.month == 12:
            end = date(today.year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(today.year, today.month + 1, 1) - timedelta(days=1)
        return start.isoformat(), end.isoformat()
    elif period == "weekly":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        return start.isoformat(), end.isoformat()
    else:
        return (today - timedelta(days=30)).isoformat(), today.isoformat()


def _fetch_spend(budget: dict[str, Any], start: str, end: str, conn: Any) -> float:
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
    elif scope_type == "account":
        q = select(func.sum(cost_snapshots.c.amount_usd)).where(
            cost_snapshots.c.snapshot_date >= start,
            cost_snapshots.c.snapshot_date <= end,
            cost_snapshots.c.account_id == scope_value,
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

    Status levels (all informational — none block execution):
      ok       → under alert threshold
      warning  → past alert_at_pct, approaching limit
      exceeded → past critical_at_pct (over budget — alert only)
    """
    from ..storage.db import get_engine
    start, end = _period_dates(budget["period"])
    if conn is None:
        with get_engine().connect() as _conn:
            spent = _fetch_spend(budget, start, end, _conn)
    else:
        spent = _fetch_spend(budget, start, end, conn)

    limit        = budget["limit_usd"]
    pct_used     = (spent / limit * 100) if limit else 0.0
    alert_pct    = budget.get("alert_at_pct", 80.0)
    # back-compat: honour block_at_pct from old configs, treat as critical_at_pct
    critical_pct = budget.get("critical_at_pct", budget.get("block_at_pct", 100.0))

    if pct_used >= critical_pct:
        status = "exceeded"   # alert only, never blocks
    elif pct_used >= alert_pct:
        status = "warning"
    else:
        status = "ok"

    days_elapsed   = (date.today() - date.fromisoformat(start)).days + 1
    days_in_period = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    run_rate       = (spent / days_elapsed * days_in_period) if days_elapsed > 0 else 0

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
        "run_rate_monthly": round(run_rate, 2),
        "projected_overage": round(max(0, run_rate - limit), 2),
    }


def _spend_through(conn: Any) -> str | None:
    """The newest cost snapshot date up to today: how current the spend is."""
    from sqlalchemy import func, select

    from ..storage.db import cost_snapshots
    today = datetime.now().astimezone().date()
    q = select(func.max(cost_snapshots.c.snapshot_date)).where(
        cost_snapshots.c.snapshot_date <= today.isoformat())
    got = conn.execute(q).scalar()
    return str(got) if got else None


def check_all_budgets() -> list[dict[str, Any]]:
    """Check all active budgets. Returns list sorted by % used descending.

    Also writes the figures to the budget summary the guard hook reads
    (budget/summary.py), so every budget check keeps the guard's spend
    figure current."""
    from ..storage.db import get_engine
    budget_list = list_budgets(active_only=True)
    results: list[dict[str, Any]] = []
    through = None
    if budget_list:
        with get_engine().connect() as conn:
            for b in budget_list:
                try:
                    results.append(check_budget(b, conn=conn))
                except Exception as e:
                    log.warning("Budget check failed for %s: %s", b.get("name"), e)
            try:
                through = _spend_through(conn)
            except Exception:  # noqa: BLE001 - a missing date must not fail the check
                through = None
    results.sort(key=lambda x: x["pct_used"], reverse=True)
    from .summary import write_summary
    write_summary(results, spend_through=through)
    return results


def refresh_summary() -> dict[str, Any]:
    """Recompute every budget and rewrite the guard's summary. Never raises:
    {"ok": True, "budgets": n, "path": ...} or {"ok": False, "error": ...}."""
    from .summary import summary_path
    try:
        results = check_all_budgets()
    except Exception as e:  # noqa: BLE001 - callers are budget writes that already succeeded
        log.warning("Budget summary refresh failed: %s", e)
        return {"ok": False, "error": str(e)}
    return {"ok": True, "budgets": len(results), "path": str(summary_path())}


# ── budget.yml sync ───────────────────────────────────────────────────────────

def sync_from_yaml(yaml_path: str) -> dict[str, Any]:
    """
    Read a budget.yml file and upsert budgets into the DB. Idempotent.

    budget.yml format:
        budgets:
          - name: Platform Team Monthly
            scope_type: team
            scope_value: platform
            period: monthly
            limit_usd: 15000
            alert_at_pct: 80       # warning alert (default 80%)
            critical_at_pct: 100   # critical alert (default 100%)
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
    now    = datetime.now(timezone.utc)

    with engine.connect() as conn:
        existing_names: set[str] = {
            r.name for r in conn.execute(select(budgets_table.c.name)).fetchall()
        }

    valid = [(b, b.get("name", "")) for b in raw_budgets if b.get("name")]

    to_insert = [
        dict(
            name=name,
            scope_type=b.get("scope_type", "total"),
            scope_value=b.get("scope_value", "*"),
            period=b.get("period", "monthly"),
            limit_usd=float(b.get("limit_usd", 0)),
            alert_at_pct=float(b.get("alert_at_pct", 80)),
            critical_at_pct=float(b.get("critical_at_pct", b.get("block_at_pct", 100))),
            created_at=now,
            updated_at=now,
            created_by="budget.yml",
            is_active=True,
        )
        for b, name in valid if name not in existing_names
    ]
    created = []
    if to_insert:
        with engine.begin() as conn:
            conn.execute(insert(budgets_table), to_insert)
        created = [r["name"] for r in to_insert]

    to_update    = [(b, name) for b, name in valid if name in existing_names]
    updated_list = []
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
                        critical_at_pct=float(b.get("critical_at_pct", b.get("block_at_pct", 100))),
                        updated_at=now,
                        is_active=True,
                    )
                )
        updated_list = [name for _, name in to_update]

    refresh_summary()
    return {
        "source": str(path),
        "created": created,
        "updated": updated_list,
        "total": len(created) + len(updated_list),
    }


# ── CI gate: a report by default, a failing step when asked ────────────────────

def ci_gate(
    budget_yaml: str | None = None,
    fail_on_exceeded: bool = False,
    *,
    fail_on_breach: bool = False,
    as_json: bool = False,
) -> int:
    """
    CI budget gate: prints budget status and returns the step's exit code.

    By default it reports and returns 0, whatever the budgets say, as it
    always has. With fail_on_breach (`nable budget ci-gate --fail-on-breach`)
    it returns:

      0  no budget breached (warnings do not fail the step)
      1  at least one budget breached: spend at or past its critical_at_pct
      2  the budgets could not be checked (a gate that cannot see must not
         pass); without fail_on_breach this is reported and returns 0

    fail_on_exceeded is the old name for fail_on_breach and does the same.
    as_json prints one JSON document on stdout instead of the report:
    {ok, exit_code, fail_on_breach, breached, warnings, budgets, as_of,
    spend_through, [sync], [error]}. The check also refreshes the spend
    summary the guard hook reads.
    """
    import json

    fail = bool(fail_on_breach or fail_on_exceeded)
    doc: dict[str, Any] = {"ok": True, "exit_code": 0, "fail_on_breach": fail,
                           "breached": [], "warnings": [], "budgets": []}

    def done(code: int) -> int:
        doc["exit_code"] = code
        doc["ok"] = code == 0 and not doc.get("error") and not doc["breached"]
        if as_json:
            print(json.dumps(doc, default=str))
        return code

    def failed(msg: str) -> int:
        doc["error"] = msg
        if not as_json:
            print(f"Budget check failed: {msg}")
            print("Failing the step (--fail-on-breach)." if fail
                  else "Not failing the step; pass --fail-on-breach to fail it.")
        return done(2 if fail else 0)

    if budget_yaml:
        if not Path(budget_yaml).exists():
            return failed(f"File not found: {budget_yaml}")
        synced = sync_from_yaml(budget_yaml)
        doc["sync"] = synced
        if synced.get("error"):
            return failed(f"{budget_yaml}: {synced['error']}")

    try:
        results = check_all_budgets()
    except Exception as e:  # noqa: BLE001 - reported, and fails the step when asked
        return failed(str(e))

    from .summary import freshness, read_summary
    fresh = freshness(read_summary())
    exceeded = [b for b in results if b["status"] == "exceeded"]
    warnings = [b for b in results if b["status"] == "warning"]
    doc.update(budgets=results, breached=[b["name"] for b in exceeded],
               warnings=[b["name"] for b in warnings], as_of=fresh["as_of"],
               spend_through=fresh["spend_through"])
    code = 1 if (fail and exceeded) else 0
    if as_json:
        return done(code)

    if not results:
        print("No budgets configured.")
        return done(code)

    ok = len(results) - len(exceeded) - len(warnings)
    print(f"\n{'─'*60}")
    print(f"  nable budget report, {date.today().isoformat()}")
    print(f"{'─'*60}")
    for b in results:
        print(f"  [{b['status']}] {b['name']}: ${b['spent']:,.0f} / ${b['limit']:,.0f} "
              f"({b['pct_used']:.0f}%)")
        if b["projected_overage"] > 0:
            print(f"      projected overage: ${b['projected_overage']:,.0f} by end of period")
    print(f"{'─'*60}")
    print(f"  {ok} OK · {len(warnings)} warnings · {len(exceeded)} exceeded")
    print(f"{'─'*60}\n")

    if exceeded:
        print("Budget exceeded:")
        for b in exceeded:
            print(f"   - {b['name']}: ${b['spent']:,.0f} spent, "
                  f"${b['spent'] - b['limit']:,.0f} over its ${b['limit']:,.0f} limit")
        if fail:
            print("   Failing the step (--fail-on-breach).\n")
        else:
            print("   Not failing the step; pass --fail-on-breach to fail it.\n")
    elif warnings:
        print("Budget warning: approaching the limit on some budgets.\n")
    return done(code)
