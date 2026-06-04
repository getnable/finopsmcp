"""
APScheduler jobs that run on a schedule to:
  1. Take daily cost snapshots from all active connectors
  2. Detect anomalies and send alerts
  3. Send a daily digest to Slack/Teams
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("finops.scheduler")

_scheduler: BackgroundScheduler | None = None

# Holds the single-owner lock (a DB connection or a file handle) for the life of
# the process so two processes pointing at the same database do not both run the
# digest/anomaly jobs and double-send. Fixed 64-bit key for the PG advisory lock.
_scheduler_lock_handle = None
_SCHED_LOCK_KEY = 0x6E61626C  # 'nabl'


def _acquire_scheduler_lock() -> bool:
    """Best-effort single-owner guard across processes sharing one database.

    Postgres (shared team mode): a session-level advisory lock, so only one of
    several hosts pointing at the same Postgres owns the schedule. SQLite / local:
    a non-blocking file lock keyed on the DB path, so `finops serve` and a separate
    `finops-mcp` on the same host do not both fire. Fails OPEN (returns True) on any
    error or unsupported platform: better to run than to silently never send.
    """
    global _scheduler_lock_handle
    try:
        from ..storage.db import get_engine, _is_postgres
        url = os.environ.get("DATABASE_URL", "")
        if url and _is_postgres(url):
            conn = get_engine().raw_connection()
            cur = conn.cursor()
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_SCHED_LOCK_KEY,))
            if cur.fetchone()[0]:
                _scheduler_lock_handle = (conn, cur)  # hold for process lifetime
                return True
            cur.close()
            conn.close()
            return False
        import fcntl  # unix-only; Windows raises ImportError -> fail open below
        import hashlib
        import tempfile
        ident = url or os.environ.get("FINOPS_DB_PATH", "default")
        key = hashlib.sha256(ident.encode()).hexdigest()[:16]
        path = os.path.join(tempfile.gettempdir(), f"nable-sched-{key}.lock")
        fh = open(path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        _scheduler_lock_handle = fh  # keep fd open so the lock is held
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Scheduler single-owner lock unavailable (%s); proceeding.", exc)
        return True


# ── Core job functions ────────────────────────────────────────────────────────

async def _snapshot_all() -> dict:
    """Fetch today's costs from all configured providers and persist snapshots."""
    from ..connectors.aws import AWSConnector
    from ..connectors.azure import AzureConnector
    from ..connectors.gcp import GCPConnector
    from ..connectors.saas.datadog import DatadogConnector
    from ..connectors.saas.mongodb_atlas import MongoDBAtlasConnector
    from ..connectors.saas.stripe import StripeConnector
    from ..connectors.saas.twilio import TwilioConnector
    from ..storage.snapshots import store_snapshot

    today = date.today()
    yesterday = today - timedelta(days=1)

    connectors = {
        "aws": AWSConnector(),
        "azure": AzureConnector(),
        "gcp": GCPConnector(),
        "datadog": DatadogConnector(),
        "mongodb_atlas": MongoDBAtlasConnector(),
        "stripe": StripeConnector(),
        "twilio": TwilioConnector(),
    }

    results: dict[str, str] = {}
    for name, connector in connectors.items():
        if not await connector.is_configured():
            continue
        try:
            summary = await connector.get_costs(yesterday, today, granularity="DAILY")
            for entry in summary.entries:
                if entry.amount > 0:
                    store_snapshot(
                        provider=entry.provider,
                        service=entry.service,
                        account_id=entry.account_id,
                        region=entry.region,
                        snapshot_date=yesterday,
                        amount_usd=entry.amount,
                        granularity="DAILY",
                    )
            results[name] = f"ok — {len(summary.entries)} entries"
            log.info("Snapshot: %s — %d entries, $%.2f", name, len(summary.entries), summary.total_usd)
        except Exception as exc:
            results[name] = f"error: {exc}"
            log.exception("Snapshot failed for %s", name)

    return results


async def _detect_and_alert() -> list[dict]:
    """Run anomaly detection on yesterday's snapshot and send alerts for new ones."""
    from ..anomaly.seasonality import detect_with_seasonality
    from ..anomaly.detector import (
        AnomalyResult, get_active_anomalies,
        mark_notified, persist_anomaly,
    )
    from ..integrations.ticketing import create_ticket
    from ..notifications import slack, teams
    from ..storage.db import cost_snapshots, get_engine
    from sqlalchemy import select, and_

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    engine = get_engine()

    with engine.connect() as conn:
        rows = conn.execute(
            select(cost_snapshots)
            .where(cost_snapshots.c.snapshot_date == yesterday)
        ).fetchall()

    alerted: list[dict] = []
    for row in rows:
        r = dict(row._mapping)
        anomaly = detect_with_seasonality(
            provider=r["provider"],
            service=r["service"],
            account_id=r["account_id"],
            snapshot_date=date.fromisoformat(r["snapshot_date"]),
            current_amount=r["amount_usd"],
        )
        if anomaly is None:
            continue
        anomaly_id = persist_anomaly(anomaly)
        anomaly_dict = {
            "id": anomaly_id,
            "provider": anomaly.provider,
            "service": anomaly.service,
            "account_id": anomaly.account_id,
            "severity": anomaly.severity,
            "direction": anomaly.direction,
            "pct_change": anomaly.pct_change,
            "z_score": anomaly.z_score,
            "baseline_mean": anomaly.baseline_mean,
            "current_amount": anomaly.current_amount,
            "detected_at": str(date.today()),
        }
        # Send alerts (fire-and-forget, don't crash on failure)
        notified = False
        if slack.is_configured():
            try:
                ok = await slack.send_anomaly_alert(anomaly_dict)
                notified = notified or ok
            except Exception:
                log.exception("Slack alert failed for anomaly %d", anomaly_id)
        if teams.is_configured():
            try:
                ok = await teams.send_anomaly_alert(anomaly_dict)
                notified = notified or ok
            except Exception:
                log.exception("Teams alert failed for anomaly %d", anomaly_id)
        try:
            from ..connectors.saas.n8n import N8nConnector
            _n8n = N8nConnector()
            if await _n8n.is_configured():
                await _n8n.send_anomaly(anomaly_dict)
        except Exception:
            log.exception("n8n alert failed for anomaly %d", anomaly_id)
        if notified:
            mark_notified(anomaly_id)

        # Auto-create ticket for high/medium severity
        if anomaly.severity in ("high", "medium"):
            try:
                ticket_url = create_ticket(anomaly_dict)
                if ticket_url:
                    anomaly_dict["ticket_url"] = ticket_url
                    log.info("Ticket created: %s", ticket_url)
            except Exception:
                log.exception("Ticket creation failed for anomaly %d", anomaly_id)

        alerted.append(anomaly_dict)
        log.info("Anomaly: %s", anomaly.summary())

    return alerted


async def _send_daily_digest() -> bool:
    from ..anomaly.detector import get_active_anomalies
    from ..notifications import slack, teams
    from ..storage.db import cost_snapshots, get_engine
    from sqlalchemy import func, select

    if not slack.is_configured() and not teams.is_configured():
        return False

    today = date.today()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)

    engine = get_engine()
    with engine.connect() as conn:
        def day_total(d: date) -> float:
            row = conn.execute(
                select(func.sum(cost_snapshots.c.amount_usd))
                .where(cost_snapshots.c.snapshot_date == d.isoformat())
            ).scalar()
            return float(row or 0)

        grand_total = day_total(yesterday)
        prev_total = day_total(two_days_ago)

        # by provider
        rows = conn.execute(
            select(
                cost_snapshots.c.provider,
                func.sum(cost_snapshots.c.amount_usd).label("total"),
            )
            .where(cost_snapshots.c.snapshot_date == yesterday.isoformat())
            .group_by(cost_snapshots.c.provider)
        ).fetchall()
        by_provider = {r.provider: float(r.total) for r in rows}

        # top services
        svc_rows = conn.execute(
            select(
                cost_snapshots.c.service,
                func.sum(cost_snapshots.c.amount_usd).label("total"),
            )
            .where(cost_snapshots.c.snapshot_date == yesterday.isoformat())
            .group_by(cost_snapshots.c.service)
            .order_by(func.sum(cost_snapshots.c.amount_usd).desc())
            .limit(5)
        ).fetchall()
        top_services = [
            {
                "service": r.service,
                "amount_usd": float(r.total),
                "pct": float(r.total) / grand_total * 100 if grand_total else 0,
            }
            for r in svc_rows
        ]

    active = get_active_anomalies()

    sent = False
    if slack.is_configured():
        try:
            sent = await slack.send_daily_digest(yesterday, grand_total, prev_total, by_provider, top_services, len(active))
        except Exception:
            log.exception("Slack daily digest failed")
    if teams.is_configured():
        try:
            sent = await teams.send_daily_digest(yesterday, grand_total, prev_total, by_provider, top_services, len(active))
        except Exception:
            log.exception("Teams daily digest failed")

    return sent


# ── Sync wrappers for APScheduler ─────────────────────────────────────────────

def _run(coro) -> None:
    try:
        asyncio.run(coro)
    except Exception:
        log.exception("Scheduled job failed")


def job_snapshot() -> None:
    _run(_snapshot_all())


def job_detect_and_alert() -> None:
    _run(_detect_and_alert())


def job_daily_digest() -> None:
    _run(_send_daily_digest())


def job_invoice_fetch() -> None:
    """Fetch and parse invoice emails from the configured IMAP mailbox."""
    try:
        from ..connectors.invoice.parser import fetch_and_store_invoices
        stored = fetch_and_store_invoices()
        if stored:
            log.info("Invoice fetch: stored %d invoices", len(stored))
    except Exception:
        log.exception("Invoice fetch job failed")


def job_weekly_slack_insight() -> None:
    """Send the weekly Slack insight (top movers, savings, anomalies, budget alerts)."""
    try:
        _run(run_weekly_insight_now())
        log.info("Weekly Slack insight sent")
    except Exception:
        log.exception("Weekly Slack insight failed")


def job_weekly_email_digest() -> None:
    """Send the standalone weekly email digest (no AI client required)."""
    try:
        from ..notifications.email_digest import send_weekly_digest
        from ..anomaly.detector import get_active_anomalies
        from ..storage.db import cost_snapshots, get_engine
        from ..recommendations.rightsizing import analyze_rightsizing, rightsizing_summary
        from sqlalchemy import func, select
        from datetime import date, timedelta

        today = date.today()
        week_start = (today - timedelta(days=7)).isoformat()
        prev_week_start = (today - timedelta(days=14)).isoformat()
        prev_week_end = (today - timedelta(days=7)).isoformat()

        engine = get_engine()
        with engine.connect() as conn:
            def week_total(start: str, end: str) -> float:
                row = conn.execute(
                    select(func.sum(cost_snapshots.c.amount_usd))
                    .where(cost_snapshots.c.snapshot_date >= start)
                    .where(cost_snapshots.c.snapshot_date < end)
                ).scalar()
                return float(row or 0)

            current_week_total = week_total(week_start, today.isoformat())
            prev_week_total = week_total(prev_week_start, prev_week_end)

            rows = conn.execute(
                select(
                    cost_snapshots.c.provider,
                    func.sum(cost_snapshots.c.amount_usd).label("total"),
                )
                .where(cost_snapshots.c.snapshot_date >= week_start)
                .group_by(cost_snapshots.c.provider)
                .order_by(func.sum(cost_snapshots.c.amount_usd).desc())
            ).fetchall()

            top_providers = [
                {
                    "provider": r.provider,
                    "amount": float(r.total),
                    "pct": float(r.total) / current_week_total * 100 if current_week_total else 0,
                }
                for r in rows
            ]

        anomalies = get_active_anomalies(limit=10)

        try:
            rs = analyze_rightsizing()
            recs = rightsizing_summary(rs)["recommendations"][:5]
            rec_list = [
                {
                    "title": r["title"],
                    "description": r["description"],
                    "monthly_savings": r["monthly_savings"],
                }
                for r in recs
            ]
        except Exception:
            rec_list = []

        send_weekly_digest(
            total_spend=current_week_total,
            prev_total=prev_week_total,
            top_providers=top_providers,
            anomalies=anomalies,
            recommendations=rec_list,
        )
    except Exception:
        log.exception("Weekly email digest job failed")


# ── Scheduler lifecycle ───────────────────────────────────────────────────────

def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    if not _acquire_scheduler_lock():
        log.info(
            "Another process already owns the nable scheduler for this database; "
            "not starting digest/anomaly jobs here (prevents double-sends)."
        )
        return None

    _scheduler = BackgroundScheduler(timezone="UTC")

    # Daily snapshot at 01:00 UTC
    snapshot_cron = os.environ.get("FINOPS_SNAPSHOT_CRON", "0 1 * * *")
    _scheduler.add_job(job_snapshot, CronTrigger.from_crontab(snapshot_cron), id="snapshot", replace_existing=True)

    # Anomaly check at 02:00 UTC (after snapshot)
    anomaly_cron = os.environ.get("FINOPS_ANOMALY_CRON", "0 2 * * *")
    _scheduler.add_job(job_detect_and_alert, CronTrigger.from_crontab(anomaly_cron), id="anomaly", replace_existing=True)

    # Daily digest at 09:00 UTC
    digest_cron = os.environ.get("FINOPS_DIGEST_CRON", "0 9 * * *")
    _scheduler.add_job(job_daily_digest, CronTrigger.from_crontab(digest_cron), id="digest", replace_existing=True)

    # Invoice email fetch every 6 hours
    invoice_cron = os.environ.get("FINOPS_INVOICE_CRON", "0 */6 * * *")
    _scheduler.add_job(job_invoice_fetch, CronTrigger.from_crontab(invoice_cron), id="invoice_fetch", replace_existing=True)

    # Weekly email digest every Monday at 09:00 UTC
    weekly_cron = os.environ.get("FINOPS_WEEKLY_CRON", "0 9 * * 1")
    _scheduler.add_job(job_weekly_email_digest, CronTrigger.from_crontab(weekly_cron), id="weekly_digest", replace_existing=True)

    # Weekly Slack insight every Monday at 09:30 UTC (30 min after email)
    weekly_slack_cron = os.environ.get("FINOPS_WEEKLY_SLACK_CRON", "30 9 * * 1")
    _scheduler.add_job(job_weekly_slack_insight, CronTrigger.from_crontab(weekly_slack_cron), id="weekly_slack_insight", replace_existing=True)

    _scheduler.start()
    log.info("Scheduler started (snapshot=%s, anomaly=%s, digest=%s)", snapshot_cron, anomaly_cron, digest_cron)
    return _scheduler


def _release_scheduler_lock() -> None:
    """Release the cross-process single-owner lock so another host can take over
    and we do not leak the DB connection / file descriptor."""
    global _scheduler_lock_handle
    h = _scheduler_lock_handle
    _scheduler_lock_handle = None
    if h is None:
        return
    try:
        if isinstance(h, tuple):  # (conn, cur) for the Postgres advisory lock
            conn, cur = h
            try:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_SCHED_LOCK_KEY,))
            except Exception:
                pass
            cur.close()
            conn.close()
        else:  # a file handle holding an fcntl flock
            import fcntl
            try:
                fcntl.flock(h.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            h.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("Scheduler lock release failed: %s", exc)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
    _release_scheduler_lock()


# ── Manual triggers (used by MCP tools) ──────────────────────────────────────

async def run_snapshot_now() -> dict:
    return await _snapshot_all()


async def run_anomaly_check_now() -> list[dict]:
    return await _detect_and_alert()


async def run_digest_now() -> bool:
    return await _send_daily_digest()


async def run_weekly_insight_now() -> bool:
    """Trigger the weekly Slack insight immediately (used by push_weekly_insight tool)."""
    from ..notifications import slack
    if not slack.is_configured():
        return False
    from datetime import date, timedelta
    from ..storage.db import get_engine, cost_snapshots
    from sqlalchemy import select, func

    today = date.today()
    this_start = today - timedelta(days=7)
    last_start = today - timedelta(days=14)
    last_end = today - timedelta(days=8)

    def _week(start: date, end: date) -> tuple[float, dict]:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    cost_snapshots.c.provider,
                    cost_snapshots.c.service,
                    func.sum(cost_snapshots.c.amount_usd).label("t"),
                )
                .where(
                    cost_snapshots.c.snapshot_date >= start.isoformat(),
                    cost_snapshots.c.snapshot_date <= end.isoformat(),
                )
                .group_by(cost_snapshots.c.provider, cost_snapshots.c.service)
            ).fetchall()
        by_key = {}
        total = 0.0
        for r in rows:
            by_key[f"{r.provider}::{r.service}"] = {"provider": r.provider, "service": r.service, "total": r.t or 0}
            total += r.t or 0
        return total, by_key

    try:
        grand_total, this_week = _week(this_start, today)
        prev_total, last_week = _week(last_start, last_end)
    except Exception:
        grand_total, prev_total, this_week, last_week = 0.0, 0.0, {}, {}

    movers = []
    for key in set(this_week) | set(last_week):
        tw = this_week.get(key, {}).get("total", 0.0)
        lw = last_week.get(key, {}).get("total", 0.0)
        if tw < 5 and lw < 5:
            continue
        rec = (this_week.get(key) or last_week.get(key) or {})
        pct = ((tw - lw) / lw * 100) if lw else 100.0
        movers.append({"provider": rec.get("provider", ""), "service": rec.get("service", ""),
                       "this_week": tw, "last_week": lw, "pct_change": pct})
    movers.sort(key=lambda m: -abs(m["pct_change"]))

    try:
        from ..recommendations.savings_tracker import get_summary
        s = get_summary()
        open_savings = s.get("potential_monthly_usd", 0)
        verified_savings = s.get("verified_monthly_usd", 0)
    except Exception:
        open_savings = verified_savings = 0.0

    try:
        from ..anomaly.detector import get_active_anomalies
        active = len(get_active_anomalies(limit=100) or [])
    except Exception:
        active = 0

    period_label = f"{this_start.strftime('%b %d')} – {today.strftime('%b %d')}"
    return await slack.send_weekly_insight(
        period_label=period_label,
        grand_total=grand_total,
        prev_total=prev_total,
        top_movers=movers[:5],
        open_savings_usd=open_savings,
        verified_savings_usd=verified_savings,
        active_anomalies=active,
    )
