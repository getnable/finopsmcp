"""
nable web dashboard server.

Serves a self-contained HTML dashboard that auto-refreshes every 60 seconds.
Any browser on the same network can access it -- no credentials or installation needed.

Also serves a Tableau Web Data Connector at /tableau so analysts can connect
Tableau Desktop directly to live nable cost data.

Usage:
    finops serve [--port 8080] [--host 0.0.0.0] [--open]
"""
from __future__ import annotations

import asyncio
import base64
import csv
import io
import html as _html
import json
import logging
import os
import re
import secrets
import socket
import threading
import time as _time_module
from datetime import datetime, date, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .auth import sso as _sso

# ── Dashboard authentication ─────────────────────────────────────────────────
# Password is required by default. If FINOPS_DASHBOARD_PASSWORD is not set,
# a secure random password is generated at startup and printed once.
# Set FINOPS_DASHBOARD_PASSWORD=off to explicitly disable auth (not recommended).

_DASHBOARD_PASSWORD: str = os.environ.get("FINOPS_DASHBOARD_PASSWORD", "").strip()
_AUTH_DISABLED: bool = _DASHBOARD_PASSWORD.lower() == "off"
_PASSWORD_AUTO_GENERATED: bool = False
if _AUTH_DISABLED:
    _DASHBOARD_PASSWORD = ""
elif not _DASHBOARD_PASSWORD:
    # Auto-generate a strong password rather than defaulting to open.
    # Printed clearly at startup — user can override with FINOPS_DASHBOARD_PASSWORD.
    _DASHBOARD_PASSWORD = secrets.token_urlsafe(14)
    _PASSWORD_AUTO_GENERATED = True

# Server-side session stores: token → expiry (unix timestamp). Full-access and
# read-only sessions live in SEPARATE stores so a read-only share token can never
# be replayed as a full-access session cookie (privilege escalation). Tokens are
# 32-byte URL-safe random strings — never derived from the password.
_SESSIONS: dict[str, float] = {}        # full access (password / SSO login)
_RO_SESSIONS: dict[str, float] = {}     # read-only share links (/view)
_SESSION_TTL_SECONDS: int = 8 * 3600  # 8 hours
_RO_SESSION_TTL_SECONDS: int = 24 * 3600  # 24 hours for share links
# The dashboard runs on ThreadingHTTPServer, so two requests can mint or prune a
# session at the same instant. Without this lock, _prune iterating store.items()
# while another thread inserts a token raises "dictionary changed size during
# iteration" and 500s the request. RLock so a mint can call _prune while holding it.
_SESSION_LOCK = threading.RLock()


def _prune(store: dict[str, float]) -> None:
    now = _time_module.time()
    with _SESSION_LOCK:
        for t in [t for t, exp in store.items() if now > exp]:
            store.pop(t, None)


def _create_session() -> str:
    """Mint a FULL-access session token."""
    token = secrets.token_urlsafe(32)
    with _SESSION_LOCK:
        _SESSIONS[token] = _time_module.time() + _SESSION_TTL_SECONDS
        _prune(_SESSIONS)
    return token


def _create_ro_session() -> str:
    """Mint a READ-ONLY session token (separate store, never full access)."""
    token = secrets.token_urlsafe(32)
    with _SESSION_LOCK:
        _RO_SESSIONS[token] = _time_module.time() + _RO_SESSION_TTL_SECONDS
        _prune(_RO_SESSIONS)
    return token


def _session_valid(token: str) -> bool:
    """True only for a live FULL-access token."""
    return _time_module.time() < _SESSIONS.get(token, 0)


def _ro_session_valid(token: str) -> bool:
    """True only for a live READ-ONLY token."""
    return _time_module.time() < _RO_SESSIONS.get(token, 0)

# Path to bundled Chart.js (served locally so dashboard works offline / GovCloud)
_STATIC_DIR = Path(__file__).parent / "static"
_CHARTJS_PATH = _STATIC_DIR / "chart.min.js"

log = logging.getLogger(__name__)


# ── Shared connectors (set by run_server/start_server_background) ────────────
# Using the MCP server's already-initialized connectors avoids credential
# resolution issues (env var vs keyring precedence).
_SHARED_CONNECTORS: dict[str, Any] = {}


def set_connectors(connectors: dict[str, Any]) -> None:
    """Inject the MCP server's initialized connectors into the dashboard."""
    global _SHARED_CONNECTORS
    _SHARED_CONNECTORS.update(connectors)


# ── Data fetcher ─────────────────────────────────────────────────────────────

async def _fetch_dashboard_data(days: int = 30, provider: str = "all") -> dict[str, Any]:
    """Pull live data from nable connectors. Returns zeros on any error."""
    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account_id": "",
        "total_spend_mtd": 0.0,
        "total_spend_last_month": 0.0,
        "projected_month_total": 0.0,
        "delta_pct": 0.0,
        "finops_grade": "N/A",
        "finops_score": 0.0,
        "top_services": [],
        "opportunities_count": 0,
        "opportunities_total_saving": 0.0,
        "savings_achieved_mtd": 0.0,
        "anomalies_open": 0,
        "budget_pct_used": 0.0,
        "recent_opportunities": [],
        "suppressed_opportunities": [],
        "learning_active": False,
        "recent_savings": [],
        "error": None,
        "connected_providers": [],
        "trend": [],
        "scorecard": {
            "overall_grade": "N/A",
            "overall_score": 0.0,
            "dimensions": [],
        },
    }

    try:
        from datetime import date, timedelta

        # Prefer connectors injected from the MCP server (already initialized
        # with the correct vault/keyring credentials). Fall back to fresh
        # instances only if no shared connectors are available.
        if _SHARED_CONNECTORS:
            all_connectors = _SHARED_CONNECTORS
        else:
            from .connectors.aws import AWSConnector
            from .connectors.azure import AzureConnector
            from .connectors.gcp import GCPConnector
            from .connectors.saas.datadog import DatadogConnector
            from .connectors.saas.snowflake import SnowflakeConnector
            _cloud_all = {
                "aws": AWSConnector(),
                "azure": AzureConnector(),
                "gcp": GCPConnector(),
            }
            _saas: dict[str, Any] = {}
            try:
                _saas["datadog"] = DatadogConnector()
            except Exception:
                pass
            try:
                _saas["snowflake"] = SnowflakeConnector()
            except Exception:
                pass
            all_connectors = {**_cloud_all, **_saas}

        # Find configured providers, optionally filtered by provider param
        configured: dict[str, Any] = {}
        for name, connector in all_connectors.items():
            if provider != "all" and name != provider:
                continue
            try:
                if await connector.is_configured():
                    configured[name] = connector
            except Exception:
                pass

        result["connected_providers"] = list(configured.keys())

        if not configured:
            result["error"] = "No providers configured. Run 'finops setup' to connect a provider."
            return result

        # MTD: first of this month to today
        today = date.today()
        mtd_start = today.replace(day=1)
        # Last month: full month
        last_month_end = mtd_start - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)

        # Collect per-connector fetch failures so an expired token or AccessDenied
        # surfaces as a banner instead of a silent $0 under a green "connected" badge.
        provider_errors: list[str] = []

        async def _sum_costs(start: date, end: date) -> tuple[float, dict[str, float], str]:
            """Returns (total, by_service, account_id)."""
            total = 0.0
            by_service: dict[str, float] = {}
            account_id = ""

            _t = int(os.getenv("FINOPS_PROVIDER_TIMEOUT_S", "90"))

            async def _one(pname, connector):
                try:
                    return await asyncio.wait_for(connector.get_costs(start, end), timeout=_t)
                except Exception as e:
                    provider_errors.append(f"{pname}: {e}")
                    return None

            for summary in await asyncio.gather(*[_one(n, c) for n, c in configured.items()]):
                if summary is None:
                    continue
                total += summary.total_usd
                for svc, amt in summary.by_service.items():
                    by_service[svc] = by_service.get(svc, 0.0) + amt
                if not account_id and summary.by_account:
                    account_id = next(iter(summary.by_account))
            return total, by_service, account_id

        mtd_total, mtd_services, account_id = await _sum_costs(mtd_start, today)
        last_total, _, _ = await _sum_costs(last_month_start, last_month_end)

        # Providers are connected but a fetch failed (expired token, AccessDenied):
        # surface the real reason instead of showing a green badge over $0.
        if provider_errors and not result.get("error"):
            seen: list[str] = []
            for e in provider_errors:
                if e not in seen:
                    seen.append(e)
            result["error"] = "Some connected providers returned no data: " + "; ".join(seen)

        result["account_id"] = account_id
        result["total_spend_mtd"] = round(mtd_total, 2)
        result["total_spend_last_month"] = round(last_total, 2)

        if last_total > 0:
            result["delta_pct"] = round((mtd_total - last_total) / last_total * 100, 1)

        # Projected month total. A naive run-rate (mtd / day * days_in_month)
        # craters in the first days of a month: month-to-date is tiny and Cost
        # Explorer lags ~24h, so on the 1st it reads near zero. Blend the run-rate
        # with last month's total, leaning on last month early and trusting the
        # run-rate by ~day 7, so the projection stays sensible all month.
        day_of_month = today.day
        days_in_month = (today.replace(month=today.month % 12 + 1, day=1) - timedelta(days=1)).day if today.month < 12 else 31
        run_rate = (mtd_total / day_of_month * days_in_month) if (day_of_month > 0 and mtd_total > 0) else 0.0
        if last_total > 0:
            w = min(1.0, day_of_month / 7.0)
            result["projected_month_total"] = round(run_rate * w + last_total * (1 - w), 2)
        elif run_rate > 0:
            result["projected_month_total"] = round(run_rate, 2)

        # Top services by spend in the requested window
        window_start = today - timedelta(days=days)
        _, window_services, _ = await _sum_costs(window_start, today)
        window_total = sum(window_services.values())
        sorted_svcs = sorted(window_services.items(), key=lambda x: -x[1])[:8]
        result["top_services"] = [
            {
                "service": svc,
                "amount": round(amt, 2),
                "pct": round(amt / window_total * 100, 1) if window_total > 0 else 0.0,
            }
            for svc, amt in sorted_svcs
        ]

        # 3-month cost trend: query CE directly for each calendar month.
        # This is reliable regardless of whether local snapshots exist.
        try:
            current_label = today.strftime("%B")
            trend_entries: list[dict] = []

            # Two COMPLETED calendar months only. The current month is shown as a
            # projection, not a near-zero partial actual (which makes the line look
            # like spend cratered on the 1st).
            month_windows = []
            for months_back in (2, 1):
                ref = today.replace(day=1)
                for _ in range(months_back):
                    ref = (ref - timedelta(days=1)).replace(day=1)
                m_start = ref
                m_end = (ref.replace(month=ref.month % 12 + 1, day=1) - timedelta(days=1)) if ref.month < 12 else ref.replace(month=12, day=31)
                month_windows.append((m_start.strftime("%B"), m_start, m_end))

            month_totals = await asyncio.gather(*[
                _sum_costs(s, e) for _, s, e in month_windows
            ], return_exceptions=True)

            last_completed_total = 0.0
            for i, ((label, _, _), result_or_exc) in enumerate(zip(month_windows, month_totals)):
                total = 0.0 if isinstance(result_or_exc, Exception) else result_or_exc[0]
                is_last = (i == len(month_windows) - 1)
                trend_entries.append({
                    "month": label,
                    "actual": round(total, 2),
                    # anchor the dashed projected line to the last completed month
                    "projected": round(total, 2) if is_last else None,
                })
                if is_last:
                    last_completed_total = total

            # Current month: projection only. Falls back to last month if the
            # run-rate is not yet meaningful.
            proj = result["projected_month_total"] or last_completed_total
            trend_entries.append({
                "month": f"{current_label} (projected)",
                "actual": None,
                "projected": round(proj, 2),
            })

            result["trend"] = trend_entries
        except Exception as exc:
            log.debug("Trend CE fetch failed: %s", exc)
            # Last-resort fallback with only the data already in hand
            current_label = today.strftime("%B")
            proj = result["projected_month_total"] or last_total
            result["trend"] = [
                {"month": last_month_end.strftime("%B"), "actual": round(last_total, 2), "projected": round(last_total, 2)},
                {"month": f"{current_label} (projected)", "actual": None, "projected": round(proj, 2)},
            ]

    except Exception as exc:
        log.warning("Dashboard data fetch failed: %s", exc)
        result["error"] = str(exc)

    # Savings recommendations
    try:
        from .recommendations.savings_tracker import get_summary, list_recommendations
        summary = get_summary(days=30)
        result["opportunities_count"] = summary.get("open_count", 0)
        result["opportunities_total_saving"] = round(
            summary.get("open_monthly_usd", 0.0), 2
        )
        result["savings_achieved_mtd"] = round(
            summary.get("verified_monthly_usd", 0.0) + summary.get("acted_monthly_usd", 0.0),
            2,
        )

        recs = list_recommendations(status="open", limit=5)
        result["recent_opportunities"] = [
            {
                "description": r.get("description", ""),
                "monthly_saving": round(r.get("estimated_monthly_savings_usd", 0.0), 2),
                "resource": r.get("resource_name", r.get("resource_id", "")),
            }
            for r in recs
        ]

        acted = list_recommendations(status="acted_on", limit=5)
        result["recent_savings"] = [
            {
                "description": r.get("description", ""),
                "monthly_saving": round(r.get("estimated_monthly_savings_usd", 0.0), 2),
                "resource": r.get("resource_name", r.get("resource_id", "")),
            }
            for r in acted
        ]
    except Exception as exc:
        log.debug("Savings tracker unavailable: %s", exc)

    # Anomalies
    try:
        from .anomaly.detector import get_open_anomaly_count
        result["anomalies_open"] = get_open_anomaly_count()
    except Exception:
        pass

    # Budget usage
    try:
        from .budget.enforcer import get_budget_usage_pct
        result["budget_pct_used"] = round(get_budget_usage_pct(), 1)
    except Exception:
        pass

    # Efficiency scorecard — built with real inputs, hard 8-second budget.
    # All AWS calls are optional; any timeout falls back to no-data scoring.
    try:
        from .scoring.scorecard import build_scorecard

        # idle_resources: only truly idle/stopped resources, NOT usage optimizations.
        # Passing Textract or Bedrock savings here inflates waste % and tanks the score.
        _IDLE_SOURCES = {"ec2", "ebs", "eip", "elastic", "idle", "stopped", "unused", "orphan"}
        _idle: list[dict] = []
        try:
            from .recommendations.savings_tracker import list_recommendations as _lr
            for r in _lr(status="open", limit=50):
                src = r.get("source", "").lower()
                rt = r.get("resource_type", "").lower()
                # Include only resources that are genuinely idle, not cost optimizations
                if any(kw in src or kw in rt for kw in _IDLE_SOURCES):
                    sav = r.get("estimated_monthly_savings_usd", 0) or 0
                    if sav > 0:
                        _idle.append({
                            "resource_id": r.get("resource_id", ""),
                            "monthly_cost_usd": sav,
                            "resource_type": r.get("resource_type", ""),
                        })
        except Exception:
            pass

        # Commitment data — 1-hour cache, 5s timeout.
        _commit_dict = await _get_commitment_data()

        # Tag hygiene: estimate untagged spend using CE.
        # Cached alongside commitment data (1-hour TTL).
        _untagged_usd: float = 0.0
        try:
            _untagged_usd = _COMMIT_CACHE.get("_untagged_spend_usd", 0.0) if _COMMIT_CACHE else 0.0
            if _untagged_usd == 0.0 and configured.get("aws"):
                import boto3
                ce = boto3.client("ce", region_name="us-east-1")
                today_str = date.today().isoformat()
                month_start_str = date.today().replace(day=1).isoformat()
                resp = ce.get_cost_and_usage(
                    TimePeriod={"Start": month_start_str, "End": today_str},
                    Granularity="MONTHLY",
                    Filter={"Not": {"Tags": {"Key": "team", "Values": [""]}}},
                    Metrics=["UnblendedCost"],
                )
                tagged_spend = sum(
                    float(r["Total"]["UnblendedCost"]["Amount"])
                    for r in resp.get("ResultsByTime", [])
                )
                _untagged_usd = max(0.0, (result.get("total_spend_mtd", 0) or 0) - tagged_spend)
                if _COMMIT_CACHE is not None:
                    _COMMIT_CACHE["_untagged_spend_usd"] = _untagged_usd
        except Exception:
            pass

        def _run_scorecard():
            return build_scorecard(
                scope="overall",
                label="Overall",
                idle_resources=_idle,
                commitment_data=_commit_dict,
                untagged_spend_usd=_untagged_usd,
                total_monthly_spend=result.get("total_spend_mtd", 0.0),
            )

        # Hard 8-second budget. If the scorecard DB query hangs, don't block the page.
        sc_obj = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _run_scorecard),
            timeout=8.0,
        )
        sc_dict = sc_obj.as_dict()
        sc_dict["overall_score"] = sc_dict.pop("total_score", sc_dict.get("overall_score", 0))
        sc_dict["overall_grade"] = sc_dict.pop("grade", sc_dict.get("overall_grade", "N/A"))
        sc_dict["trend"] = sc_obj.trend
        sc_dict["trend_delta"] = round(sc_obj.trend_delta, 1)
        result["scorecard"] = sc_dict
        result["finops_grade"] = sc_dict["overall_grade"]
        result["finops_score"] = sc_dict["overall_score"]
        result["finops_trend"] = sc_obj.trend
        result["finops_trend_delta"] = round(sc_obj.trend_delta, 1)
    except asyncio.TimeoutError:
        log.debug("Scorecard timed out — returning without scorecard data")
    except Exception as exc:
        log.debug("Scorecard build failed: %s", exc)

    # ── Savings pipeline ────────────────────────────────────────────────────
    # 1. Run live scanners and persist results to savings_recommendations so
    #    they get stable IDs and can be marked as acted-on from the dashboard.
    aws = configured.get("aws")
    account_id = result.get("account_id", "")

    if aws is not None:
        try:
            # Hard 25-second budget for the scanner suite.
            # First call hits AWS; subsequent calls serve from 12-hour cache instantly.
            live_opps = await asyncio.wait_for(_live_opportunities(aws), timeout=25.0)
            # Persist each into the savings tracker DB (upserts by dedup_key)
            from .recommendations.savings_tracker import record_recommendation
            for opp in live_opps:
                try:
                    record_recommendation(
                        source=opp.get("service", "scanner"),
                        provider="aws",
                        resource_id=opp.get("resource", opp.get("service", "unknown")),
                        resource_type=opp.get("service", ""),
                        resource_name=opp.get("resource", ""),
                        # Store range in current_config so it doesn't affect the dedup key
                        current_config={
                            "monthly_saving_min": opp.get("monthly_saving_min"),
                            "monthly_saving_max": opp.get("monthly_saving_max"),
                        },
                        recommended_config={"action": opp.get("effort", "")},
                        description=opp.get("description", ""),
                        estimated_monthly_savings_usd=opp.get("monthly_saving", 0.0),
                        account_id=account_id,
                    )
                except Exception as exc:
                    log.debug("Failed to persist opportunity: %s", exc)
        except Exception as exc:
            log.debug("Live scanner run failed: %s", exc)

    # 2. Read all open opportunities from DB (now includes scanner results).
    #    This gives us stable IDs for mark-as-done.
    from .recommendations.savings_tracker import list_recommendations, get_summary
    try:
        open_recs = list_recommendations(status="open", limit=20)
        def _build_opp(r: dict) -> dict:
            import json as _json
            saving = round(r["estimated_monthly_savings_usd"], 2)
            opp: dict = {
                "id": r["id"],
                "description": r["description"],
                "monthly_saving": saving,
                "resource": r["resource_name"] or r["resource_id"],
                "service": r["source"],
                "environment_bucket": r.get("environment_bucket"),
                "effort": _effort_from_source(r["source"]),
                "impact": _impact_from_saving(saving),
            }
            # Restore min/max range stored in current_config (doesn't affect dedup key)
            for cfg_field in ("current_config", "recommended_config"):
                try:
                    cfg = _json.loads(r.get(cfg_field, "{}") or "{}")
                    mn = cfg.get("monthly_saving_min")
                    mx = cfg.get("monthly_saving_max")
                    if mn is not None and mx is not None and float(mx) > float(mn):
                        opp["monthly_saving_min"] = round(float(mn), 2)
                        opp["monthly_saving_max"] = round(float(mx), 2)
                        break
                except Exception:
                    pass
            return opp

        # Build and deduplicate by description (handles legacy duplicates in DB)
        seen_desc: set[str] = set()
        opps_deduped = []
        for r in open_recs:
            desc = r.get("description", "")
            if desc in seen_desc:
                continue
            seen_desc.add(desc)
            opps_deduped.append(_build_opp(r))
        # Sort highest saving first, prefer entries with ranges
        opps_deduped.sort(key=lambda o: o.get("monthly_saving", 0), reverse=True)
        # Learning loop: reorder + suppress per what this customer actually acts on.
        # A no-op until the ledger has signal (cold sources keep the savings-desc
        # order, nothing is suppressed), so a fresh install sees no change. Propose-only.
        try:
            from .recommendations.learning import customer_signal, rescore
            sig = customer_signal()
            rs = rescore(opps_deduped, sig, savings_key="monthly_saving", source_key="service")
            shown = rs["ranked"]
            result["recent_opportunities"] = shown
            result["suppressed_opportunities"] = rs["suppressed_for_you"]
            result["opportunities_count"] = len(shown)
            result["opportunities_total_saving"] = round(sum(o.get("monthly_saving", 0) for o in shown), 2)
            result["learning_active"] = any(s.get("coverage") != "COLD" for s in sig.get("by_source", []))
        except Exception as exc:
            log.debug("learning rescore skipped: %s", exc)
            result["recent_opportunities"] = opps_deduped
            result["opportunities_count"] = len(opps_deduped)
            result["opportunities_total_saving"] = round(sum(o["monthly_saving"] for o in opps_deduped), 2)
    except Exception as exc:
        log.debug("Could not read open recs: %s", exc)
        result["opportunities_count"] = 0
        result["opportunities_total_saving"] = 0.0
        result["recent_opportunities"] = []

    # 3. Savings ledger summary (identified → acted on → verified)
    try:
        summary = get_summary()
        result["savings_ledger"] = {
            "identified_monthly": summary.get("potential_monthly_usd", 0.0),
            "acted_on_monthly": summary.get("acted_on_monthly_usd", 0.0),
            "verified_monthly": summary.get("verified_monthly_usd", 0.0),
            "verified_annual": summary.get("verified_annual_usd", 0.0),
            "counts": summary.get("by_status", {}),
        }
    except Exception:
        result["savings_ledger"] = {
            "identified_monthly": result["opportunities_total_saving"],
            "acted_on_monthly": 0.0,
            "verified_monthly": 0.0,
            "verified_annual": 0.0,
            "counts": {},
        }

    # 4. Business context: cost-per-customer + runway (Phase 1 business-context
    # layer). Empty when no metrics are on file so the dashboard shows a prompt.
    try:
        from .connectors.business_metrics import (
            get_latest_metrics, compute_unit_economics, compute_runway,
        )
        latest = get_latest_metrics(n=1)
        if latest:
            m = latest[0]
            monthly_burn = result.get("projected_month_total") or result.get("total_spend_mtd") or 0.0
            econ = compute_unit_economics(monthly_burn, m)
            runway = compute_runway(
                cash_on_hand_usd=m.get("cash_on_hand_usd"),
                infra_monthly_burn_usd=monthly_burn,
                monthly_opex_usd=m.get("monthly_opex_usd"),
                mrr_usd=m.get("mrr_usd") or (m.get("arr_usd", 0) / 12 if m.get("arr_usd") else None),
            )
            result["business_context"] = {
                "has_metrics": True,
                "cost_per_customer_label": econ.get("cost_per_customer_label"),
                "hosting_pct_mrr_label": econ.get("hosting_pct_mrr_label"),
                "hosting_pct_mrr_health": econ.get("hosting_pct_mrr_health"),
                "runway": runway,
                "metrics_as_of": m.get("metric_date"),
            }
        else:
            result["business_context"] = {"has_metrics": False}
    except Exception as exc:
        log.debug("Could not build business context: %s", exc)
        result["business_context"] = {"has_metrics": False}

    return result


def _effort_from_source(source: str) -> str:
    """Map scanner source names to effort levels."""
    zero_effort = {"Commitments", "RDS", "EC2", "commitments", "dbsp"}
    low_effort = {"Textract", "textract", "Lambda", "snapstart"}
    if source in zero_effort:
        return "ZERO"
    if source in low_effort:
        return "LOW"
    return "MEDIUM"


def _impact_from_saving(monthly_usd: float) -> str:
    if monthly_usd >= 500:
        return "high"
    if monthly_usd >= 100:
        return "medium"
    return "low"


# Cache for live scanner results. Keyed by AWS account id, value is
# (fetched_at: datetime, opportunities: list[dict]). Refreshes at most
# every 12 hours so scanner calls don't run on every dashboard load.
_OPP_CACHE: dict[str, tuple[datetime, list[dict]]] = {}
_OPP_CACHE_TTL = timedelta(hours=12)


def clear_opportunity_cache() -> None:
    """Force the next dashboard load to re-run all live scanners."""
    _OPP_CACHE.clear()


# ── Read-only share tokens ────────────────────────────────────────────────────
# Mapping of token → expiry datetime. Generated at startup and on demand.
# Tokens grant view-only access via /view?token=X → cookie → /view.
# The token never appears in URLs after the first exchange.

_RO_TOKEN_TTL = timedelta(hours=24)
_RO_TOKENS: dict[str, datetime] = {}


def _generate_ro_token() -> str:
    """Create a new read-only share token valid for 24 hours."""
    token = secrets.token_urlsafe(32)
    _RO_TOKENS[token] = datetime.now(tz=timezone.utc) + _RO_TOKEN_TTL
    # Clean up expired tokens
    now = datetime.now(tz=timezone.utc)
    expired = [t for t, exp in _RO_TOKENS.items() if now > exp]
    for t in expired:
        _RO_TOKENS.pop(t, None)
    return token


def _ro_token_valid(token: str) -> bool:
    expiry = _RO_TOKENS.get(token)
    return expiry is not None and datetime.now(tz=timezone.utc) < expiry


# Generate a share token at module load so it's ready on first startup.
_SHARE_TOKEN: str = _generate_ro_token()


# Process-level commitment data cache (1-hour TTL).
# Shared so build_scorecard never calls analyze_commitments on every request.
_COMMIT_CACHE: dict[str, Any] | None = None
_COMMIT_CACHE_UNTIL: datetime = datetime.now(tz=timezone.utc)


async def _get_commitment_data() -> dict[str, Any]:
    """Return commitment coverage dict, fetching once per hour with a 5s timeout.

    Always returns a non-None dict so build_scorecard never triggers its own
    internal analyze_commitments call (which would add 5-10 s per request).
    """
    global _COMMIT_CACHE, _COMMIT_CACHE_UNTIL

    now = datetime.now(tz=timezone.utc)
    if _COMMIT_CACHE is not None and now < _COMMIT_CACHE_UNTIL:
        return _COMMIT_CACHE

    # Default: neutral 50% coverage so the scorecard stays honest about uncertainty
    default: dict[str, Any] = {
        "savings_plan_coverage_pct": 0.0,
        "savings_plan_utilization_pct": 100.0,
        "ri_coverage_pct": 0.0,
        "ri_utilization_pct": 100.0,
        "uncovered_on_demand_usd": 0.0,
        "_source": "default",
    }

    try:
        from .recommendations.commitments import analyze_commitments as _ac
        ca = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _ac),
            timeout=5.0,
        )
        if ca is not None:
            default = {
                "savings_plan_coverage_pct": ca.savings_plan_coverage_pct,
                "savings_plan_utilization_pct": ca.savings_plan_utilization_pct,
                "ri_coverage_pct": ca.ri_coverage_pct,
                "ri_utilization_pct": ca.ri_utilization_pct,
                "uncovered_on_demand_usd": ca.uncovered_on_demand_usd,
                "_source": "live",
            }
    except (asyncio.TimeoutError, Exception) as exc:
        log.debug("Commitment data fetch skipped: %s", exc)

    _COMMIT_CACHE = default
    _COMMIT_CACHE_UNTIL = now + timedelta(hours=1)
    return default


async def _live_opportunities(aws: Any) -> list[dict]:
    """Run the high-value scanners live and normalize into dashboard opportunities.

    Results are cached per AWS account for 12 hours.
    """
    import asyncio

    # Try to get a stable cache key (account id or a fixed sentinel).
    try:
        cache_key = str(getattr(aws, "account_id", None) or "default")
    except Exception:
        cache_key = "default"

    now = datetime.now(tz=timezone.utc)
    cached = _OPP_CACHE.get(cache_key)
    if cached is not None:
        fetched_at, opps = cached
        if now - fetched_at < _OPP_CACHE_TTL:
            log.debug("Returning cached opportunities for %s (age %s)", cache_key, now - fetched_at)
            return opps

    out: list[dict] = []

    async def _safe(name: str, coro):
        try:
            return name, await coro
        except Exception as exc:
            log.debug("scanner %s failed: %s", name, exc)
            return name, None

    from .recommendations.textract_env import scan_textract_environment_waste as _tex
    from .recommendations.bedrock_routing import recommend_bedrock_model_routing as _bed
    from .recommendations.commitments import analyze_commitments as _commit
    from .recommendations.public_ipv4 import audit_public_ipv4 as _ipv4
    from .recommendations.graviton import scan_graviton_opportunities as _grav
    from .recommendations.database_savings_plans import recommend_database_savings_plans as _dbsp
    from .recommendations.lambda_snapstart import recommend_lambda_snapstart as _snapstart

    loop = asyncio.get_event_loop()

    # Sync scanners run in a thread so they don't block the event loop.
    tex_fut    = loop.run_in_executor(None, _tex)
    bed_fut    = loop.run_in_executor(None, _bed)
    commit_fut = loop.run_in_executor(None, _commit)
    dbsp_fut   = loop.run_in_executor(None, _dbsp)

    (tex_raw, bed_raw, commit_raw, dbsp_raw,
     ipv4_raw, grav_raw, snap_raw) = await asyncio.gather(
        tex_fut,
        bed_fut,
        commit_fut,
        dbsp_fut,
        _ipv4(aws_client=aws),
        _grav(aws_client=aws),
        _snapstart(aws_client=aws),
        return_exceptions=True,
    )

    raw_map = {
        "textract":  tex_raw,
        "bedrock":   bed_raw,
        "commit":    commit_raw,
        "dbsp":      dbsp_raw,
        "ipv4":      ipv4_raw,
        "graviton":  grav_raw,
        "snapstart": snap_raw,
    }

    for name, data in raw_map.items():
        if data is None or isinstance(data, BaseException):
            if isinstance(data, BaseException):
                log.debug("scanner %s raised: %s", name, data)
            continue
        try:
            if name == "textract" and isinstance(data, dict):
                w = data.get("estimated_monthly_waste", 0) or 0
                if w > 0:
                    callers = data.get("non_prod_callers", [])
                    # Range: min = ~50% of savings (conservative: only some envs disabled),
                    # max = full waste (all non-prod disabled). Anchored on env signals not raw callers.
                    w_min = round(w * 0.5, 2)
                    w_max = round(w, 2)
                    out.append({
                        "description": f"Disable Textract in non-production environments ({len(callers)} caller(s) detected)",
                        "monthly_saving": w_max,
                        "monthly_saving_min": w_min,
                        "monthly_saving_max": w_max,
                        "resource": "Amazon Textract", "effort": "LOW",
                        "impact": "high", "service": "Textract",
                    })
            elif name == "bedrock" and isinstance(data, dict):
                s = data.get("total_monthly_savings", 0) or 0
                if s > 0:
                    out.append({
                        "description": "Route short-context Bedrock calls from Sonnet to Haiku",
                        "monthly_saving": round(s, 2),
                        "resource": "Amazon Bedrock", "effort": "MEDIUM",
                        "impact": "high", "service": "Bedrock",
                    })
            elif name == "commit":
                # analyze_commitments returns a CommitmentAnalysis dataclass or None
                if data is None:
                    continue
                recs = getattr(data, "recommendations", []) or []
                for rec in recs:
                    s = rec.get("monthly_savings", 0) or 0
                    if s > 0:
                        out.append({
                            "description": rec.get("description", "Buy Reserved Instances / Savings Plans"),
                            "monthly_saving": round(s, 2),
                            "resource": "RDS / DocumentDB / EC2", "effort": "ZERO",
                            "impact": "high", "service": "Commitments",
                        })
                # Also surface a summary if uncovered on-demand is significant and no recs yet
                if not recs:
                    uncovered = getattr(data, "uncovered_on_demand_usd", 0) or 0
                    avg_cov = ((getattr(data, "savings_plan_coverage_pct", 0) or 0)
                               + (getattr(data, "ri_coverage_pct", 0) or 0)) / 2
                    savings_est = round(uncovered * 0.4 * 0.55, 2)  # ~40% of uncovered, ~55% RI discount
                    if savings_est > 50 and avg_cov < 80:
                        out.append({
                            "description": f"Buy Reserved Instances / Savings Plans ({avg_cov:.0f}% commitment coverage today)",
                            "monthly_saving": savings_est,
                            "resource": "RDS / DocumentDB / EC2", "effort": "ZERO",
                            "impact": "high", "service": "Commitments",
                        })
            elif name == "dbsp" and isinstance(data, dict):
                s = data.get("estimated_monthly_savings", 0) or 0
                cov = data.get("current_sp_coverage_pct", 100) or 100
                if s > 0 and cov < 80:
                    out.append({
                        "description": f"Buy Database Savings Plans for RDS/Aurora ({cov:.0f}% covered today)",
                        "monthly_saving": round(s, 2),
                        "resource": "RDS / Aurora", "effort": "ZERO",
                        "impact": "high", "service": "RDS",
                    })
            elif name == "snapstart" and isinstance(data, list) and data:
                total = sum(r.get("monthly_pc_cost", 0) or 0 for r in data)
                if total > 0:
                    out.append({
                        "description": f"Enable Lambda SnapStart on {len(data)} Java function(s) to eliminate provisioned concurrency cost",
                        "monthly_saving": round(total, 2),
                        "resource": "AWS Lambda", "effort": "LOW",
                        "impact": "medium", "service": "Lambda",
                    })
            elif name == "ipv4" and isinstance(data, dict):
                w = data.get("total_monthly_waste", 0) or 0
                unattached = data.get("unattached_eips", []) or []
                stopped = data.get("stopped_instance_eips", []) or []
                n = len(unattached) + len(stopped)
                if w > 0 and n > 0:
                    parts = []
                    if unattached: parts.append(f"{len(unattached)} unattached")
                    if stopped: parts.append(f"{len(stopped)} on stopped instances")
                    out.append({
                        "description": f"Release {n} idle Elastic IP(s) ({', '.join(parts)})",
                        "monthly_saving": round(w, 2),
                        "resource": "Elastic IPs", "effort": "ZERO",
                        "impact": "low", "service": "EC2",
                    })
            elif name == "graviton" and isinstance(data, list):
                total = sum(r.get("savings_estimate", 0) or 0 for r in data)
                if total > 0:
                    out.append({
                        "description": f"Migrate {len(data)} instance(s) to Graviton (arm64)",
                        "monthly_saving": round(total, 2),
                        "resource": "EC2 / RDS", "effort": "MEDIUM",
                        "impact": "high", "service": "Compute",
                    })
        except Exception as exc:
            log.debug("normalize %s failed: %s", name, exc)

    # Store in cache.
    _OPP_CACHE[cache_key] = (now, out)
    log.debug("Cached %d opportunities for %s", len(out), cache_key)
    return out


# ── Tableau data fetchers ────────────────────────────────────────────────────

def _fetch_tableau_costs() -> list[dict]:
    """Return cost_snapshots rows from the last 90 days as a list of dicts."""
    try:
        from .storage.db import cost_snapshots, get_engine
        from sqlalchemy import select

        cutoff = (date.today() - timedelta(days=90)).isoformat()
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    cost_snapshots.c.service,
                    cost_snapshots.c.provider,
                    cost_snapshots.c.account_id,
                    cost_snapshots.c.snapshot_date,
                    cost_snapshots.c.amount_usd,
                    cost_snapshots.c.region,
                ).where(cost_snapshots.c.snapshot_date >= cutoff)
                .order_by(cost_snapshots.c.snapshot_date.desc())
            ).fetchall()
        return [
            {
                "service": r.service,
                "provider": r.provider,
                "account_id": r.account_id,
                "snapshot_date": r.snapshot_date,
                "amount_usd": round(r.amount_usd, 4),
                "region": r.region,
            }
            for r in rows
        ]
    except Exception as exc:
        log.debug("Tableau costs fetch failed: %s", exc)
        return []


def _fetch_tableau_opportunities() -> list[dict]:
    """Return all savings_recommendations rows as a list of dicts."""
    try:
        from .storage.db import savings_recommendations, get_engine
        from sqlalchemy import select

        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    savings_recommendations.c.description,
                    savings_recommendations.c.source,
                    savings_recommendations.c.estimated_monthly_savings_usd,
                    savings_recommendations.c.status,
                    savings_recommendations.c.generated_at,
                ).order_by(savings_recommendations.c.estimated_monthly_savings_usd.desc())
            ).fetchall()
        return [
            {
                "title": r.description or r.source,
                "category": r.source.capitalize() if r.source else "",
                "monthly_savings": round(r.estimated_monthly_savings_usd or 0.0, 2),
                "annual_savings": round((r.estimated_monthly_savings_usd or 0.0) * 12, 2),
                "status": r.status or "open",
                "created_at": (
                    r.generated_at.strftime("%Y-%m-%d")
                    if isinstance(r.generated_at, datetime)
                    else str(r.generated_at)[:10]
                ),
            }
            for r in rows
        ]
    except Exception as exc:
        log.debug("Tableau opportunities fetch failed: %s", exc)
        return []


def _fetch_tableau_anomalies() -> list[dict]:
    """Return anomaly rows from the last 90 days as a list of dicts."""
    try:
        from .storage.db import anomalies, get_engine
        from sqlalchemy import select

        cutoff = (date.today() - timedelta(days=90)).isoformat()
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    anomalies.c.service,
                    anomalies.c.detected_at,
                    anomalies.c.severity,
                    anomalies.c.pct_change,
                    anomalies.c.current_amount,
                    anomalies.c.baseline_mean,
                    anomalies.c.acknowledged,
                ).where(anomalies.c.snapshot_date >= cutoff)
                .order_by(anomalies.c.detected_at.desc())
            ).fetchall()
        return [
            {
                "service": r.service,
                "detected_at": (
                    r.detected_at.strftime("%Y-%m-%dT%H:%M:%S")
                    if isinstance(r.detected_at, datetime)
                    else str(r.detected_at)
                ),
                "severity": r.severity,
                "pct_change": round(r.pct_change, 2),
                "current_amount": round(r.current_amount, 4),
                "baseline_mean": round(r.baseline_mean, 4),
                "acknowledged": bool(r.acknowledged),
            }
            for r in rows
        ]
    except Exception as exc:
        log.debug("Tableau anomalies fetch failed: %s", exc)
        return []


def _csv_safe(v):
    """Neutralize spreadsheet formula injection (CWE-1236): a cell starting with
    '=', '+', '-', '@', tab, or CR is treated as a formula by Excel/Sheets. Prefix
    with an apostrophe to force text. Values come from resource/tag names."""
    if not isinstance(v, str):
        return v
    if v and v[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _to_csv(rows: list[dict]) -> bytes:
    """Serialize a list of dicts to CSV bytes."""
    if not rows:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\n")
    writer.writeheader()
    writer.writerows([{k: _csv_safe(val) for k, val in row.items()} for row in rows])
    return buf.getvalue().encode()


# ── Tableau WDC HTML ─────────────────────────────────────────────────────────

_TABLEAU_WDC_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>nable - Tableau Web Data Connector</title>
<link rel="stylesheet" href="/static/fonts/fonts.css">
<script src="https://connectors.tableau.com/libs/tableauwdc-2.3.latest.js" type="text/javascript"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{
  background:#0d0f10;color:#e8ecef;
  font-family:'Bricolage Grotesque',system-ui,sans-serif;
  font-size:15px;line-height:1.5;
  display:flex;align-items:center;justify-content:center;min-height:100vh;
  padding:24px;
}
.card{
  background:#15191c;border:1px solid #252b30;border-radius:12px;
  padding:40px 36px;max-width:480px;width:100%;
}
.logo{font-size:18px;font-weight:600;margin-bottom:24px;letter-spacing:-.01em}
.logo span{color:#4db8d4}
h1{font-size:20px;font-weight:600;margin-bottom:8px}
p{color:#9ba8b4;font-size:14px;margin-bottom:24px;line-height:1.6}
.tables{display:flex;flex-direction:column;gap:8px;margin-bottom:28px}
.table-item{
  background:#1c2126;border:1px solid #252b30;border-radius:8px;
  padding:12px 16px;display:flex;align-items:center;gap:12px;
}
.table-dot{width:8px;height:8px;border-radius:50%;background:#4db8d4;flex-shrink:0}
.table-name{font-weight:500;font-size:14px}
.table-desc{font-size:12px;color:#5a6472;margin-top:2px}
button{
  width:100%;padding:12px;border-radius:8px;border:none;
  background:#4db8d4;color:#0d0f10;font-family:inherit;
  font-size:15px;font-weight:600;cursor:pointer;transition:opacity .15s;
}
button:hover{opacity:.88}
.note{font-size:12px;color:#5a6472;text-align:center;margin-top:16px}
</style>
</head>
<body>
<div class="card">
  <div class="logo"><span>n</span>able</div>
  <h1>Tableau Web Data Connector</h1>
  <p>Connect Tableau Desktop to live nable cost data. Click "Connect to nable" to load the three available tables.</p>
  <div class="tables">
    <div class="table-item">
      <div class="table-dot"></div>
      <div>
        <div class="table-name">Cloud Costs by Service</div>
        <div class="table-desc">Daily spend per service. Last 90 days.</div>
      </div>
    </div>
    <div class="table-item">
      <div class="table-dot"></div>
      <div>
        <div class="table-name">Savings Opportunities</div>
        <div class="table-desc">All open and acted-on recommendations with estimated savings.</div>
      </div>
    </div>
    <div class="table-item">
      <div class="table-dot"></div>
      <div>
        <div class="table-name">Cost Anomalies</div>
        <div class="table-desc">Detected anomalies with severity and delta. Last 90 days.</div>
      </div>
    </div>
  </div>
  <button id="connectBtn">Connect to nable</button>
  <p class="note">Run <code>finops serve</code> to keep this connector active.</p>
</div>
<script>
(function(){
  var myConnector = tableau.makeConnector();

  myConnector.getSchema = function(schemaCallback){
    var costsSchema = {
      id: "costs",
      alias: "Cloud Costs by Service",
      columns: [
        {id:"service",     alias:"Service",          dataType:tableau.dataTypeEnum.string},
        {id:"provider",    alias:"Provider",          dataType:tableau.dataTypeEnum.string},
        {id:"account_id",  alias:"Account ID",        dataType:tableau.dataTypeEnum.string},
        {id:"snapshot_date", alias:"Date",            dataType:tableau.dataTypeEnum.date},
        {id:"amount_usd",  alias:"Amount (USD)",      dataType:tableau.dataTypeEnum.float},
        {id:"region",      alias:"Region",            dataType:tableau.dataTypeEnum.string},
      ]
    };

    var oppsSchema = {
      id: "opportunities",
      alias: "Savings Opportunities",
      columns: [
        {id:"title",           alias:"Opportunity",           dataType:tableau.dataTypeEnum.string},
        {id:"category",        alias:"Category",              dataType:tableau.dataTypeEnum.string},
        {id:"monthly_savings", alias:"Monthly Saving (USD)",  dataType:tableau.dataTypeEnum.float},
        {id:"annual_savings",  alias:"Annual Saving (USD)",   dataType:tableau.dataTypeEnum.float},
        {id:"status",          alias:"Status",                dataType:tableau.dataTypeEnum.string},
        {id:"created_at",      alias:"Found On",              dataType:tableau.dataTypeEnum.date},
      ]
    };

    var anomSchema = {
      id: "anomalies",
      alias: "Cost Anomalies",
      columns: [
        {id:"service",         alias:"Service",               dataType:tableau.dataTypeEnum.string},
        {id:"detected_at",     alias:"Detected At",           dataType:tableau.dataTypeEnum.datetime},
        {id:"severity",        alias:"Severity",              dataType:tableau.dataTypeEnum.string},
        {id:"pct_change",      alias:"% Change",              dataType:tableau.dataTypeEnum.float},
        {id:"current_amount",  alias:"Current Amount (USD)",  dataType:tableau.dataTypeEnum.float},
        {id:"baseline_mean",   alias:"Baseline Mean (USD)",   dataType:tableau.dataTypeEnum.float},
        {id:"acknowledged",    alias:"Acknowledged",          dataType:tableau.dataTypeEnum.bool},
      ]
    };

    schemaCallback([costsSchema, oppsSchema, anomSchema]);
  };

  myConnector.getData = function(table, doneCallback){
    var urlMap = {
      costs:         "/api/tableau/costs",
      opportunities: "/api/tableau/opportunities",
      anomalies:     "/api/tableau/anomalies",
    };
    var url = urlMap[table.tableInfo.id];
    if(!url){ doneCallback(); return; }

    fetch(url)
      .then(function(r){ return r.json(); })
      .then(function(data){ table.appendRows(data); doneCallback(); })
      .catch(function(err){ console.error("nable WDC error:", err); doneCallback(); });
  };

  tableau.registerConnector(myConnector);

  document.getElementById("connectBtn").addEventListener("click", function(){
    tableau.connectionName = "nable Cost Data";
    tableau.submit();
  });
})();
</script>
</body>
</html>
"""


# ── Power BI / OData ──────────────────────────────────────────────────────────

def _odata_response(entity_set: str, rows: list[dict], host: str, scheme: str = "http") -> bytes:
    """Wrap rows in OData v4 JSON envelope."""
    return json.dumps({
        "@odata.context": f"{scheme}://{host}/odata/$metadata#{entity_set}",
        "value": rows,
    }).encode()


def _odata_row_id(rows: list[dict]) -> list[dict]:
    """Add synthetic integer 'id' field required by OData EntityType key."""
    return [{**r, "id": i + 1} for i, r in enumerate(rows)]


_ODATA_METADATA = """\
<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">
  <edmx:DataServices>
    <Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="nable">
      <EntityType Name="CostRecord">
        <Key><PropertyRef Name="id"/></Key>
        <Property Name="id"            Type="Edm.Int32"   Nullable="false"/>
        <Property Name="service"       Type="Edm.String"/>
        <Property Name="provider"      Type="Edm.String"/>
        <Property Name="account_id"    Type="Edm.String"/>
        <Property Name="snapshot_date" Type="Edm.Date"/>
        <Property Name="amount_usd"    Type="Edm.Decimal" Precision="19" Scale="4"/>
        <Property Name="region"        Type="Edm.String"/>
      </EntityType>
      <EntityType Name="Opportunity">
        <Key><PropertyRef Name="id"/></Key>
        <Property Name="id"             Type="Edm.Int32"   Nullable="false"/>
        <Property Name="title"          Type="Edm.String"/>
        <Property Name="category"       Type="Edm.String"/>
        <Property Name="monthly_savings" Type="Edm.Decimal" Precision="19" Scale="2"/>
        <Property Name="annual_savings"  Type="Edm.Decimal" Precision="19" Scale="2"/>
        <Property Name="status"         Type="Edm.String"/>
        <Property Name="created_at"     Type="Edm.Date"/>
      </EntityType>
      <EntityType Name="Anomaly">
        <Key><PropertyRef Name="id"/></Key>
        <Property Name="id"             Type="Edm.Int32"   Nullable="false"/>
        <Property Name="service"        Type="Edm.String"/>
        <Property Name="detected_at"    Type="Edm.DateTimeOffset"/>
        <Property Name="severity"       Type="Edm.String"/>
        <Property Name="pct_change"     Type="Edm.Decimal" Precision="10" Scale="2"/>
        <Property Name="current_amount" Type="Edm.Decimal" Precision="19" Scale="4"/>
        <Property Name="baseline_mean"  Type="Edm.Decimal" Precision="19" Scale="4"/>
        <Property Name="acknowledged"   Type="Edm.Boolean"/>
      </EntityType>
      <EntityContainer Name="NableContainer">
        <EntitySet Name="Costs"         EntityType="nable.CostRecord"/>
        <EntitySet Name="Opportunities" EntityType="nable.Opportunity"/>
        <EntitySet Name="Anomalies"     EntityType="nable.Anomaly"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""


_POWERBI_GUIDE_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>nable - Power BI Connector</title>
<link rel="stylesheet" href="/static/fonts/fonts.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/geist@1.3.0/dist/fonts/geist-mono/style.css">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0f10;color:#f0f2f3;font-family:'Bricolage Grotesque',system-ui,sans-serif;padding:40px 24px}
.wrap{max-width:720px;margin:0 auto}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:40px}
.logo svg{flex-shrink:0}
.logo span{font-size:18px;font-weight:500;letter-spacing:-.02em}
h1{font-size:22px;font-weight:500;margin-bottom:8px}
.sub{color:#56656d;font-size:14px;margin-bottom:36px}
h2{font-size:13px;font-weight:500;text-transform:uppercase;letter-spacing:.06em;color:#94a3ab;margin-bottom:12px}
.card{background:#111416;border:1px solid #242a2e;border-radius:12px;padding:24px;margin-bottom:24px}
ol{padding-left:18px;color:#c8d4d9;font-size:14px;line-height:1.9}
ol li{margin-bottom:4px}
code{background:#181c1f;border:1px solid #2e3539;border-radius:4px;padding:2px 7px;
  font-family:'Geist Mono','JetBrains Mono',monospace;font-size:12.5px;color:#4db8d4}
.url-box{background:#181c1f;border:1px solid #2e3539;border-radius:8px;padding:14px 16px;
  font-family:'Geist Mono','JetBrains Mono',monospace;font-size:13px;color:#4db8d4;
  word-break:break-all;margin:12px 0}
.tag-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.tag{background:#181c1f;border:1px solid #242a2e;border-radius:2px;padding:3px 10px;
  font-size:12px;color:#94a3ab}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:12px}
th{text-align:left;color:#56656d;font-weight:400;padding:6px 0;border-bottom:1px solid #242a2e}
td{padding:8px 0;border-bottom:1px solid #1a1e22;color:#c8d4d9}
td:first-child{color:#f0f2f3}
</style>
</head>
<body>
<div class="wrap">
  <div class="logo">
    <svg width="26" height="26" viewBox="0 0 32 32">
      <rect width="32" height="32" rx="7" fill="#4db8d4"/>
      <path d="M9.5 23V11.5h2.6v1.5c.7-1.1 1.9-1.7 3.4-1.7 2.6 0 4.2 1.7 4.2 4.5V23h-2.7v-6.6c0-1.7-.9-2.6-2.4-2.6s-2.5 1-2.5 2.7V23H9.5Z" fill="#0d0f10"/>
    </svg>
    <span>nable</span>
  </div>

  <h1>Power BI Connector</h1>
  <p class="sub">Connect Power BI Desktop to live nable cost data via OData v4.</p>

  <div class="card">
    <h2>Connect Power BI Desktop</h2>
    <ol>
      <li>Open Power BI Desktop.</li>
      <li>Click <strong>Get Data</strong> and search for <strong>OData feed</strong>.</li>
      <li>Paste the service URL and click OK:</li>
    </ol>
    <div class="url-box" id="svc-url">http://localhost:8080/odata</div>
    <ol start="4">
      <li>In the Navigator panel, select the tables you want: <code>Costs</code>, <code>Opportunities</code>, or <code>Anomalies</code>.</li>
      <li>Click <strong>Load</strong> (or <strong>Transform Data</strong> to apply filters first).</li>
      <li>Build your report. Refresh at any time to pull the latest data from nable.</li>
    </ol>
  </div>

  <div class="card">
    <h2>Available Tables</h2>
    <table>
      <thead><tr><th>Table</th><th>Description</th><th>Key columns</th></tr></thead>
      <tbody>
        <tr>
          <td>Costs</td>
          <td>Daily cost snapshots across all connected providers</td>
          <td>service, provider, account_id, snapshot_date, amount_usd</td>
        </tr>
        <tr>
          <td>Opportunities</td>
          <td>Savings recommendations ranked by impact</td>
          <td>title, category, monthly_savings, annual_savings, status</td>
        </tr>
        <tr>
          <td>Anomalies</td>
          <td>Detected cost spikes from the last 90 days</td>
          <td>service, detected_at, severity, pct_change, current_amount</td>
        </tr>
      </tbody>
    </table>
    <div class="tag-row">
      <span class="tag">OData v4</span>
      <span class="tag">JSON format</span>
      <span class="tag">Live data</span>
      <span class="tag">90-day history</span>
    </div>
  </div>

  <div class="card">
    <h2>Direct Endpoints</h2>
    <p style="font-size:13px;color:#94a3ab;margin-bottom:12px">Or query individual entity sets:</p>
    <table>
      <thead><tr><th>URL</th><th>Returns</th></tr></thead>
      <tbody>
        <tr><td><code>/odata/$metadata</code></td><td>EDMX schema (XML)</td></tr>
        <tr><td><code>/odata/Costs</code></td><td>Cost records (JSON)</td></tr>
        <tr><td><code>/odata/Opportunities</code></td><td>Savings recommendations (JSON)</td></tr>
        <tr><td><code>/odata/Anomalies</code></td><td>Anomaly events (JSON)</td></tr>
      </tbody>
    </table>
  </div>
</div>
<script>
var h = window.location.host || 'localhost:8080';
document.getElementById('svc-url').textContent = window.location.protocol + '//' + h + '/odata';
</script>
</body>
</html>"""


# ── Dashboard HTML ────────────────────────────────────────────────────────────

_DASHBOARD_HTML_PATH = Path(__file__).parent / "static" / "dashboard.html"


def _load_dashboard_html() -> str:
    """Read the dashboard HTML from disk each time so edits are reflected
    on page reload without restarting the server."""
    try:
        return _DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("Could not read dashboard.html: %s", exc)
        return "<h1>Dashboard template missing. Reinstall nable.</h1>"


_DASHBOARD_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>nable | Cost Dashboard</title>
<link rel="stylesheet" href="/static/fonts/fonts.css">
<script src="/static/chart.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0f10;
  --bg1:#111416;
  --bg2:#181c1f;
  --bg3:#1e2327;
  --line:#242a2e;
  --line2:#2e3539;
  --fg:#f0f2f3;
  --fg2:#94a3ab;
  --fg3:#56656d;
  --fg4:#2d3a40;
  --accent:#4db8d4;
  --accent-dim:#2c7d91;
  --success:#3cba7a;
  --warn:#e6a840;
  --alert:#e05c4b;
  --r-xs:2px;
  --r-sm:4px;
  --r-md:6px;
  --r-lg:8px;
  --r-xl:12px;
  --font:'Bricolage Grotesque',system-ui,sans-serif;
  --mono:'Geist Mono','JetBrains Mono',monospace;
}
html,body{background:var(--bg);color:var(--fg);font-family:var(--font);font-size:15px;line-height:1.5;min-height:100vh}
body{padding:0 0 60px}
.container{max-width:1400px;margin:0 auto;padding:0 32px}

/* Top nav bar */
.topbar{background:var(--bg);border-bottom:1px solid var(--line);padding:14px 32px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;position:sticky;top:0;z-index:20}
.topbar-left{display:flex;align-items:center;gap:20px;flex-wrap:wrap}
.logo{font-size:17px;font-weight:600;color:var(--fg);letter-spacing:-.01em}
.logo .n{color:var(--accent)}
.header-title{font-size:14px;color:var(--fg2);border-left:1px solid var(--line2);padding-left:20px}
.topbar-right{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;padding:4px 10px;border-radius:var(--r-lg)}
.badge-green{background:rgba(60,186,122,.12);color:var(--success);border:1px solid rgba(60,186,122,.3)}
.badge-red{background:rgba(224,92,75,.12);color:var(--alert);border:1px solid rgba(224,92,75,.3)}
.badge-dot{width:5px;height:5px;border-radius:50%;background:currentColor}
.header-date{font-size:13px;color:var(--fg2);font-weight:500;letter-spacing:.01em}

/* Main content area */
.main{padding:28px 0 0}

/* Section label */
.section-label{font-size:11px;font-weight:600;color:var(--accent);text-transform:uppercase;letter-spacing:.1em;margin-bottom:14px}

/* Stat cards */
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}}
@media(max-width:500px){.cards{grid-template-columns:1fr}}
.card{background:var(--bg1);border:1px solid var(--line);border-radius:var(--r-xl);padding:22px 24px}
.card-label{font-size:11px;font-weight:500;color:var(--fg3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px}
.card-value{font-size:30px;font-weight:600;line-height:1;color:var(--fg);font-family:var(--mono)}
.card-sub{font-size:12px;color:var(--fg3);margin-top:8px}
.card-sub.up{color:var(--alert)}
.card-sub.down{color:var(--success)}
.card-sub.neutral{color:var(--fg3)}
/* Grade card */
.grade-row{display:flex;align-items:baseline;gap:10px;line-height:1;margin-bottom:6px}
.grade-letter{font-size:44px;font-weight:600;font-family:var(--mono);line-height:1}
.grade-score{font-size:15px;color:var(--fg3);font-family:var(--mono)}
.grade-a{color:var(--success)}
.grade-b{color:var(--accent)}
.grade-c{color:var(--warn)}
.grade-d,.grade-f{color:var(--alert)}
.grade-delta{font-size:12px;color:var(--accent);margin-top:4px}
.grade-gaps{font-size:11px;color:var(--fg3);margin-top:4px}

/* Panel */
.panel{background:var(--bg1);border:1px solid var(--line);border-radius:var(--r-xl);padding:20px 22px}
.panel-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.panel-title{font-size:11px;font-weight:500;color:var(--fg3);text-transform:uppercase;letter-spacing:.08em}
.panel-badge{display:inline-flex;align-items:center;font-size:9px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;padding:3px 8px;border-radius:var(--r-xs);background:var(--bg3);color:var(--fg3);border:1px solid var(--line2)}

/* Charts row */
.charts-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
@media(max-width:768px){.charts-row{grid-template-columns:1fr}}

/* Bottom row */
.bottom-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:40px}
.bottom-row .panel{min-height:320px}
@media(max-width:768px){.bottom-row{grid-template-columns:1fr}}

/* Chart containers */
.chart-wrap{position:relative;height:260px}

/* Efficiency scorecard */
.scorecard-list{list-style:none;display:flex;flex-direction:column;gap:14px}
.sc-item{display:flex;flex-direction:column;gap:6px}
.sc-header{display:flex;align-items:center;justify-content:space-between}
.sc-name{font-size:13px;color:var(--fg)}
.sc-meta{display:flex;align-items:center;gap:8px}
.sc-score{font-size:12px;font-family:var(--mono);color:var(--fg3)}
.sc-grade{font-size:11px;font-weight:700;width:18px;text-align:center;font-family:var(--mono)}
.grade-pill-a{color:var(--success)}
.grade-pill-b{color:var(--accent)}
.grade-pill-c{color:var(--warn)}
.grade-pill-d,.grade-pill-f{color:var(--alert)}
.sc-bar-bg{height:4px;background:var(--bg3);border-radius:2px;overflow:hidden}
.sc-bar-fill{height:100%;border-radius:2px;transition:width .5s ease}
.sc-bar-a{background:var(--success)}
.sc-bar-b{background:var(--accent)}
.sc-bar-c{background:var(--warn)}
.sc-bar-d,.sc-bar-f{background:rgba(224,92,75,.5)}

/* Savings opportunities */
.opp-list{list-style:none;display:flex;flex-direction:column}
.opp-item{display:flex;align-items:center;gap:12px;padding:11px 0;border-bottom:1px solid var(--line)}
.opp-item:last-child{border-bottom:none}
.opp-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.opp-dot-high{background:var(--alert)}
.opp-dot-medium{background:var(--warn)}
.opp-dot-low{background:var(--success)}
.opp-body{flex:1;min-width:0}
.opp-desc{font-size:13px;color:var(--fg);line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.opp-right{display:flex;align-items:center;gap:8px;flex-shrink:0}
.opp-saving{font-size:13px;font-weight:500;color:var(--success);font-family:var(--mono);white-space:nowrap}
.effort-badge{font-size:9px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;padding:2px 7px;border-radius:var(--r-xs);white-space:nowrap}
.effort-low{background:rgba(60,186,122,.12);color:var(--success);border:1px solid rgba(60,186,122,.2)}
.effort-medium{background:rgba(230,168,64,.12);color:var(--warn);border:1px solid rgba(230,168,64,.2)}
.effort-zero{background:rgba(77,184,212,.12);color:var(--accent);border:1px solid rgba(77,184,212,.2)}
.effort-high{background:rgba(224,92,75,.12);color:var(--alert);border:1px solid rgba(224,92,75,.2)}

/* Error banner */
.error-banner{background:rgba(224,92,75,.08);border:1px solid rgba(224,92,75,.2);border-radius:var(--r-lg);padding:12px 16px;color:var(--alert);font-size:13px;margin-bottom:16px}
.bizctx{border:1px solid var(--border);border-radius:var(--r-lg);padding:16px 18px;margin-bottom:16px;background:var(--surface,rgba(255,255,255,.02))}
.bizctx .bc-row{display:flex;gap:32px;flex-wrap:wrap;align-items:baseline}
.bizctx .bc-item{display:flex;flex-direction:column;gap:3px}
.bizctx .bc-label{font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--fg-3,#8a9199)}
.bizctx .bc-value{font-family:var(--mono,monospace);font-size:20px;font-variant-numeric:tabular-nums;color:var(--fg,#e8eaed)}
.bizctx .bc-note{font-size:12px;color:var(--fg-3,#8a9199);margin-top:8px;line-height:1.5}
.bizctx .bc-prompt{font-size:13px;color:var(--fg-2,#b8bdc4);line-height:1.5}
.bizctx .bc-prompt code{font-family:var(--mono,monospace);font-size:12px;color:var(--accent,#4db8d4)}

/* Footer */
footer{text-align:center;padding:16px;font-size:12px;color:var(--fg3)}
footer a{color:var(--accent);text-decoration:none;font-weight:500}
footer a:hover{filter:brightness(1.15)}

@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>

<nav class="topbar">
  <div class="topbar-left">
    <div class="logo"><span class="n">n</span>able</div>
    <div class="header-title" id="hdr-title">Cost Dashboard</div>
  </div>
  <div class="topbar-right">
    <span class="badge badge-green" id="provider-badge">
      <span class="badge-dot"></span>
      <span id="provider-label">CONNECTING</span>
    </span>
    <div class="header-date" id="hdr-date"></div>
  </div>
</nav>

<div class="container">
<div class="main">

  <div id="error-banner" class="error-banner" style="display:none"></div>

  <div id="bizctx" class="bizctx" style="display:none"></div>

  <div class="section-label">Overview</div>

  <div class="cards">
    <div class="card">
      <div class="card-label">MTD Spend</div>
      <div class="card-value" id="stat-mtd">...</div>
      <div class="card-sub neutral" id="stat-delta">loading...</div>
    </div>
    <div class="card">
      <div class="card-label">Projected <span id="proj-month-label">Month</span> Total</div>
      <div class="card-value" id="stat-projected">...</div>
      <div class="card-sub" id="stat-projected-sub">run-rate estimate</div>
    </div>
    <div class="card">
      <div class="card-label">Identified Savings</div>
      <div class="card-value down" id="stat-savings">...</div>
      <div class="card-sub neutral" id="stat-savings-sub">per month</div>
    </div>
    <div class="card">
      <div class="card-label">FinOps Grade</div>
      <div class="grade-row">
        <span class="grade-letter grade-d" id="grade-letter">D</span>
        <span class="grade-score" id="grade-score">-- / 100</span>
      </div>
      <div class="grade-delta" id="grade-delta"></div>
      <div class="grade-gaps" id="grade-gaps"></div>
    </div>
  </div>

  <div class="charts-row">
    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-title">Spend by Service &mdash; Last <span id="chart-days">30</span> Days</div>
        <span class="panel-badge" id="service-chart-badge">AWS ONLY</span>
      </div>
      <div class="chart-wrap"><canvas id="chart-services"></canvas></div>
    </div>
    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-title">3-Month Cost Trend</div>
        <span class="panel-badge">MONTHLY</span>
      </div>
      <div class="chart-wrap"><canvas id="chart-trend"></canvas></div>
    </div>
  </div>

  <div class="bottom-row">
    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-title">Efficiency Scorecard</div>
        <span class="panel-badge" id="scorecard-badge">OVERALL --</span>
      </div>
      <ul class="scorecard-list" id="scorecard-list">
        <li class="sc-item"><span style="color:var(--fg3);font-size:13px">Loading...</span></li>
      </ul>
    </div>
    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-title">Savings Opportunities</div>
        <span class="panel-badge" id="opp-badge">-- OPEN</span>
      </div>
      <ul class="opp-list" id="opp-list">
        <li class="opp-item"><span style="color:var(--fg3);font-size:13px">Loading...</span></li>
      </ul>
    </div>
  </div>

</div>
</div>

<footer>
  Powered by <a href="https://getnable.com" target="_blank" rel="noopener">nable</a>
</footer>

<script>
// ── Utilities ────────────────────────────────────────────────────────────────
function fmt(n){
  if(n==null||isNaN(n)) return '$0';
  if(n>=1000000) return '$'+(n/1000000).toFixed(1)+'M';
  if(n>=1000) return '$'+(n/1000).toFixed(1)+'k';
  return '$'+Math.round(n).toLocaleString('en-US');
}
function fmtDelta(n){
  if(!n||isNaN(n)) return null;
  const sign=n>0?'+':'';
  return sign+fmt(Math.abs(n)).replace('$','')+' ';
}
function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function gradeColor(g){
  const gl=String(g||'f').toLowerCase();
  if(gl==='a') return 'var(--success)';
  if(gl==='b') return 'var(--accent)';
  if(gl==='c') return 'var(--warn)';
  return 'var(--alert)';
}
function gradeBarClass(g){
  const gl=String(g||'f').toLowerCase();
  if(gl==='a') return 'sc-bar-a';
  if(gl==='b') return 'sc-bar-b';
  if(gl==='c') return 'sc-bar-c';
  return 'sc-bar-d';
}
function gradePillClass(g){
  const gl=String(g||'f').toLowerCase();
  return 'grade-pill-'+gl;
}

// ── State ────────────────────────────────────────────────────────────────────
const selectedDays = 30;
const selectedProvider = 'all';
let serviceChart = null;
let trendChart = null;

// ── Header date ──────────────────────────────────────────────────────────────
function initDate(){
  const now=new Date();
  const monthName=now.toLocaleDateString('en-US',{month:'long'}).toUpperCase();
  const year=now.getFullYear();
  document.getElementById('hdr-date').textContent=monthName+' '+now.getDate()+', '+year;
  document.getElementById('proj-month-label').textContent=now.toLocaleDateString('en-US',{month:'long'});
}

// ── Load data ────────────────────────────────────────────────────────────────
async function loadData(){
  try{
    const r=await fetch('/api/data?days='+selectedDays+'&provider='+selectedProvider);
    if(!r.ok) throw new Error('HTTP '+r.status);
    render(await r.json());
  }catch(err){
    console.error('Dashboard load failed:',err);
  }
}

// ── Render ────────────────────────────────────────────────────────────────────
function render(d){
  // Error banner
  const banner=document.getElementById('error-banner');
  if(d.error){banner.textContent=d.error;banner.style.display='block';}
  else{banner.style.display='none';}

  // Business context headline: cost-per-customer + runway, or a cold-start prompt
  const bc=d.business_context||{};
  const bcEl=document.getElementById('bizctx');
  if(bc.has_metrics){
    const items=[];
    if(bc.cost_per_customer_label){
      items.push('<div class="bc-item"><span class="bc-label">Cost per customer</span><span class="bc-value">'+esc(bc.cost_per_customer_label)+'</span></div>');
    }
    if(bc.hosting_pct_mrr_label){
      items.push('<div class="bc-item"><span class="bc-label">Hosting as % of MRR</span><span class="bc-value">'+esc(bc.hosting_pct_mrr_label)+'</span></div>');
    }
    const rw=bc.runway||{};
    if(rw.available && rw.months!=null){
      const lbl=(rw.mode==='company'?'Company runway':'Infra runway');
      items.push('<div class="bc-item"><span class="bc-label">'+lbl+'</span><span class="bc-value">'+rw.months+' mo</span></div>');
    }
    if(items.length){
      let html='<div class="bc-row">'+items.join('')+'</div>';
      if(rw.note){html+='<div class="bc-note">'+esc(rw.note)+'</div>';}
      else if(rw.reason){html+='<div class="bc-note">'+esc(rw.reason)+'</div>';}
      bcEl.innerHTML=html;
      bcEl.style.display='block';
    }else{bcEl.style.display='none';}
  }else if(bc.has_metrics===false){
    bcEl.innerHTML='<div class="bc-prompt">Connect your business to your spend. Set revenue, customers, and cash with <code>set_business_metrics()</code> in Claude to see cost per customer and runway here.</div>';
    bcEl.style.display='block';
  }else{bcEl.style.display='none';}

  // Top nav: account in title, provider badge
  const acct=d.account_id||'';
  document.getElementById('hdr-title').textContent='Cost Dashboard'+(acct?' — Account '+acct:'');
  const providers=d.connected_providers||[];
  const badge=document.getElementById('provider-badge');
  const provLabel=document.getElementById('provider-label');
  if(providers.length>0){
    badge.className='badge badge-green';
    provLabel.textContent=providers.map(p=>p.toUpperCase()).join('+ ')+' CONNECTED';
  }else{
    badge.className='badge badge-red';
    provLabel.textContent='NO PROVIDER';
  }

  // Card 1: MTD Spend
  const now=new Date();
  const dayOfMonth=now.getDate();
  const daysInMonth=new Date(now.getFullYear(),now.getMonth()+1,0).getDate();
  document.getElementById('stat-mtd').textContent=fmt(d.total_spend_mtd||0);
  const deltaEl=document.getElementById('stat-delta');
  deltaEl.textContent=dayOfMonth+' of '+daysInMonth+' days elapsed';
  deltaEl.className='card-sub neutral';

  // Card 2: Projected month total
  document.getElementById('stat-projected').textContent=fmt(d.projected_month_total||0);
  const projDollar=(d.projected_month_total||0)-(d.total_spend_last_month||0);
  const projSubEl=document.getElementById('stat-projected-sub');
  if(Math.abs(projDollar)>1){
    const sign=projDollar>0?'+':'−';
    projSubEl.textContent=sign+'$'+Math.round(Math.abs(projDollar)).toLocaleString('en-US')+' vs last month run rate';
    projSubEl.className='card-sub '+(projDollar>0?'up':'down');
  }else{
    projSubEl.textContent='on track with last month';
    projSubEl.className='card-sub neutral';
  }

  // Card 3: Identified savings
  document.getElementById('stat-savings').textContent=fmt(d.opportunities_total_saving||0);
  const oppCount=d.opportunities_count||0;
  document.getElementById('stat-savings-sub').textContent='per month — '+oppCount+' opportunit'+(oppCount===1?'y':'ies');

  // Card 4: FinOps grade
  const grade=d.finops_grade||'N/A';
  const score=parseFloat(d.finops_score||0).toFixed(1);
  const gradeLetterEl=document.getElementById('grade-letter');
  gradeLetterEl.textContent=grade;
  gradeLetterEl.className='grade-letter grade-'+grade.toLowerCase();
  document.getElementById('grade-score').textContent=score+' / 100';
  // Surface critical gaps from scorecard dimensions
  const dims=(d.scorecard||{}).dimensions||[];
  const failing=dims.filter(dim=>dim.grade==='F'||dim.grade==='D').map(dim=>dim.name.toLowerCase());
  document.getElementById('grade-gaps').textContent=failing.length?failing.length+' critical gap'+(failing.length>1?'s':'')+': '+failing.join(', '):'';

  // Chart days label + service badge
  document.getElementById('chart-days').textContent=selectedDays;
  const svcBadge=document.getElementById('service-chart-badge');
  if(providers.length===1){svcBadge.textContent=providers[0].toUpperCase()+' ONLY';}
  else if(providers.length>1){svcBadge.textContent='ALL PROVIDERS';}
  else{svcBadge.textContent='';}

  // Charts
  renderServicesChart(d.top_services||[]);
  renderTrendChart(d.trend||[]);

  // Scorecard
  renderScorecard(d.scorecard||{});

  // Opportunities
  renderOpportunities(d.recent_opportunities||[],oppCount);
}

// ── Services bar chart ────────────────────────────────────────────────────────
function renderServicesChart(services){
  const labels=services.map(s=>s.service);
  const amounts=services.map(s=>s.amount);
  // Top service red, second teal, rest muted gray
  const colors=services.map((_,i)=>{
    if(i===0) return '#e05c4b';
    if(i===1) return '#4db8d4';
    return 'rgba(94,107,115,.6)';
  });

  if(serviceChart){serviceChart.destroy();serviceChart=null;}
  const ctx=document.getElementById('chart-services').getContext('2d');
  serviceChart=new Chart(ctx,{
    type:'bar',
    data:{labels,datasets:[{data:amounts,backgroundColor:colors,borderRadius:3,borderSkipped:false}]},
    options:{
      indexAxis:'y',responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>' '+fmt(c.raw)}}},
      scales:{
        x:{ticks:{color:'#56656d',font:{family:"'Geist Mono',monospace",size:11},callback:v=>fmt(v)},grid:{color:'rgba(255,255,255,.04)'},border:{color:'transparent'}},
        y:{ticks:{color:'#94a3ab',font:{family:"'Bricolage Grotesque',system-ui,sans-serif",size:12},padding:4},grid:{display:false},border:{color:'transparent'}}
      }
    }
  });
}

// ── Trend line chart ──────────────────────────────────────────────────────────
function renderTrendChart(trend){
  if(!trend||trend.length===0) return;
  const labels=trend.map(t=>t.month);
  const actual=trend.map(t=>t.actual);
  const projected=trend.map(t=>t.projected);

  if(trendChart){trendChart.destroy();trendChart=null;}
  const ctx=document.getElementById('chart-trend').getContext('2d');
  trendChart=new Chart(ctx,{
    type:'line',
    data:{
      labels,
      datasets:[
        {label:'Actual',data:actual,borderColor:'#4db8d4',backgroundColor:'rgba(77,184,212,.1)',borderWidth:2.5,pointRadius:4,pointBackgroundColor:'#4db8d4',tension:.3,fill:true},
        {label:'Projected',data:projected,borderColor:'#e6a840',backgroundColor:'transparent',borderWidth:2,borderDash:[5,4],pointRadius:4,pointBackgroundColor:'#e6a840',tension:.3,fill:false}
      ]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      plugins:{
        legend:{labels:{color:'#94a3ab',font:{family:"'Bricolage Grotesque',system-ui,sans-serif",size:12},boxWidth:16,usePointStyle:true,pointStyle:'line'}},
        tooltip:{callbacks:{label:c=>' '+c.dataset.label+': '+fmt(c.raw)}}
      },
      scales:{
        x:{ticks:{color:'#94a3ab',font:{family:"'Bricolage Grotesque',system-ui,sans-serif",size:12}},grid:{color:'rgba(255,255,255,.04)'},border:{color:'transparent'}},
        y:{ticks:{color:'#56656d',font:{family:"'Geist Mono',monospace",size:11},callback:v=>fmt(v)},grid:{color:'rgba(255,255,255,.04)'},border:{color:'transparent'}}
      }
    }
  });
}

// ── Efficiency scorecard ──────────────────────────────────────────────────────
function renderScorecard(sc){
  const overall=sc.overall_grade||'--';
  const dims=sc.dimensions||[];
  document.getElementById('scorecard-badge').textContent='OVERALL '+overall;

  const el=document.getElementById('scorecard-list');
  if(!dims.length){
    el.innerHTML='<li class="sc-item"><span style="color:var(--fg3);font-size:13px">Run a cost audit to generate scorecard data.</span></li>';
    return;
  }
  el.innerHTML=dims.map(dim=>{
    const g=dim.grade||'F';
    const s=Math.round(dim.score||0);
    const barClass=gradeBarClass(g);
    const pillClass=gradePillClass(g);
    return `<li class="sc-item">
      <div class="sc-header">
        <span class="sc-name">${esc(dim.name)}</span>
        <div class="sc-meta">
          <span class="sc-score">${s} / 100</span>
          <span class="sc-grade ${pillClass}">${g}</span>
        </div>
      </div>
      <div class="sc-bar-bg">
        <div class="sc-bar-fill ${barClass}" style="width:${s}%"></div>
      </div>
    </li>`;
  }).join('');
}

// ── Savings opportunities ─────────────────────────────────────────────────────
function renderOpportunities(opps, count){
  document.getElementById('opp-badge').textContent=(count||opps.length)+' OPEN';

  const el=document.getElementById('opp-list');
  if(!opps||opps.length===0){
    el.innerHTML='<li class="opp-item"><span style="color:var(--fg3);font-size:13px">No open opportunities found. Run a waste audit to surface savings.</span></li>';
    return;
  }
  el.innerHTML=opps.map(o=>{
    const impact=String(o.impact||'medium').toLowerCase();
    const dotClass=impact==='high'?'opp-dot-high':impact==='low'?'opp-dot-low':'opp-dot-medium';
    const effort=String(o.effort||'MEDIUM').toUpperCase().replace(' EFFORT','');
    const effortLabel=effort+' EFFORT';
    let effortClass='effort-medium';
    if(effort==='LOW') effortClass='effort-low';
    else if(effort==='ZERO') effortClass='effort-zero';
    else if(effort==='HIGH') effortClass='effort-high';
    // Format saving: if there's a range in the description, keep it; otherwise show single value
    const saving=fmt(o.monthly_saving||0)+'/mo';
    return `<li class="opp-item">
      <span class="opp-dot ${dotClass}"></span>
      <div class="opp-body">
        <div class="opp-desc">${esc(o.description||o.resource||'Recommendation')}</div>
      </div>
      <div class="opp-right">
        <span class="opp-saving">${saving}</span>
        <span class="effort-badge ${effortClass}">${effortLabel}</span>
      </div>
    </li>`;
  }).join('');
}

// ── Boot ──────────────────────────────────────────────────────────────────────
initDate();
loadData();
setInterval(loadData, 60000);
</script>
</body>
</html>
"""


# ── HTTP request handler ──────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # type: ignore[override]
        log.debug("web: " + format, *args)

    def _localhost_origin(self) -> str:
        port = self.server.server_address[1]
        return f"http://localhost:{port}"

    def _host_allowed(self) -> bool:
        """Reject DNS-rebound requests: a browser script at evil.com that
        rebinds its hostname to 127.0.0.1 still sends Host: evil.com."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        allowed = {"localhost", "127.0.0.1", "::1", "[::1]"}
        extra = os.getenv("FINOPS_DASHBOARD_ALLOWED_HOSTS", "")
        allowed.update(h.strip().lower() for h in extra.split(",") if h.strip())
        return host in allowed

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        # Defense-in-depth for the network-facing dashboard: block framing
        # (clickjacking), MIME sniffing, and referrer leakage.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Access-Control-Allow-Origin", self._localhost_origin())
        self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _send_csv(self, rows: list[dict], filename: str) -> None:
        body = _to_csv(rows)
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", self._localhost_origin())
        self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _cookie_valid(self) -> bool:
        """Validate the session cookie against the server-side session store."""
        if _AUTH_DISABLED or not _DASHBOARD_PASSWORD:
            return True
        cookie_header = self.headers.get("Cookie", "")
        m = re.search(r"nable_session=([A-Za-z0-9_=-]+)", cookie_header)
        if not m:
            return False
        return _session_valid(m.group(1))

    def _ro_cookie_valid(self) -> bool:
        """Validate a read-only share session cookie against the read-only store.

        Read-only tokens live in their own store, so a nable_view value can never
        satisfy _cookie_valid() (full access) even if copied into nable_session.
        """
        cookie_header = self.headers.get("Cookie", "")
        m = re.search(r"nable_view=([A-Za-z0-9_=-]+)", cookie_header)
        if not m:
            return False
        return _ro_session_valid(m.group(1))

    def _any_auth_valid(self) -> bool:
        """Return True if the request has either full or read-only access."""
        return self._cookie_valid() or self._ro_cookie_valid()

    def _cookie_attrs(self) -> str:
        """Common Set-Cookie attributes. Adds Secure when the request reached us
        over HTTPS (directly or via a TLS-terminating proxy that sets
        X-Forwarded-Proto), so the session token is not sent in cleartext."""
        attrs = "Path=/; HttpOnly; SameSite=Strict"
        xfp = (self.headers.get("X-Forwarded-Proto", "") or "").split(",")[0].strip().lower()
        if xfp == "https":
            attrs += "; Secure"
        return attrs

    def _serve_login_page(self, error: bool = False, sso_error: str = "") -> None:
        error_html = ""
        if error:
            error_html = '<p style="color:#e05c4b;margin:0 0 16px;font-size:13px">Incorrect password. Try again.</p>'
        elif sso_error:
            error_html = f'<p style="color:#e05c4b;margin:0 0 16px;font-size:13px">{_html.escape(sso_error)}</p>'

        sso_block = ""
        if _sso.SSO_ENABLED:
            divider = '<div style="display:flex;align-items:center;gap:10px;margin:20px 0"><hr style="flex:1;border:none;border-top:1px solid #242a2e"/><span style="color:#56656d;font-size:12px">or</span><hr style="flex:1;border:none;border-top:1px solid #242a2e"/></div>' if _DASHBOARD_PASSWORD else ""
            sso_block = f"""{divider}<a href="/sso/login" style="display:block;text-decoration:none;margin-top:{'0' if not _DASHBOARD_PASSWORD else '0'}">
  <button type="button" style="width:100%;background:#181c1f;border:1px solid #2e3539;border-radius:8px;
    color:#f0f2f3;cursor:pointer;font-family:inherit;font-size:14px;font-weight:500;
    padding:11px;transition:border-color .15s;display:flex;align-items:center;justify-content:center;gap:8px"
    onmouseover="this.style.borderColor='#4db8d4'" onmouseout="this.style.borderColor='#2e3539'">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
    </svg>
    Sign in with SSO
  </button>
</a>"""

        pw_block = ""
        if _DASHBOARD_PASSWORD:
            pw_block = f"""{error_html}<form method="POST" action="/login">
    <label>Password</label>
    <input type="password" name="password" placeholder="Enter dashboard password" autofocus autocomplete="current-password"/>
    <button type="submit">Sign in</button>
  </form>
  <p class="hint">Password set via<br><code style="color:#4db8d4">FINOPS_DASHBOARD_PASSWORD</code> environment variable</p>"""
        elif sso_error:
            pw_block = error_html

        subtitle = "Sign in to access the dashboard."
        if _sso.SSO_ENABLED and not _DASHBOARD_PASSWORD:
            subtitle = "Sign in with your organization account."

        body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>nable Dashboard</title>
<link rel="stylesheet" href="/static/fonts/fonts.css"/>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d0f10;color:#f0f2f3;font-family:'Bricolage Grotesque',system-ui,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh}}
.card{{background:#111416;border:1px solid #242a2e;border-radius:12px;padding:40px;width:360px}}
.logo{{display:flex;align-items:center;gap:10px;margin-bottom:32px}}
.logo svg{{flex-shrink:0}}
.logo span{{font-size:18px;font-weight:500;letter-spacing:-.02em}}
h1{{font-size:16px;font-weight:500;margin-bottom:6px}}
p.sub{{color:#56656d;font-size:13px;margin-bottom:24px}}
label{{display:block;font-size:12px;color:#94a3ab;letter-spacing:.05em;text-transform:uppercase;margin-bottom:6px}}
input{{width:100%;background:#181c1f;border:1px solid #2e3539;border-radius:6px;
  color:#f0f2f3;font-family:inherit;font-size:14px;padding:10px 12px;outline:none}}
input:focus{{border-color:#4db8d4}}
button{{margin-top:16px;width:100%;background:#4db8d4;border:none;border-radius:8px;
  color:#0d0f10;cursor:pointer;font-family:inherit;font-size:14px;font-weight:500;
  padding:11px;transition:filter .15s}}
button:hover{{filter:brightness(1.1)}}
.hint{{color:#56656d;font-size:11px;margin-top:16px;text-align:center;line-height:1.5}}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <svg width="26" height="26" viewBox="0 0 32 32">
      <rect width="32" height="32" rx="7" fill="#4db8d4"/>
      <path d="M9.5 23V11.5h2.6v1.5c.7-1.1 1.9-1.7 3.4-1.7 2.6 0 4.2 1.7 4.2 4.5V23h-2.7v-6.6c0-1.7-.9-2.6-2.4-2.6s-2.5 1-2.5 2.7V23H9.5Z" fill="#0d0f10"/>
    </svg>
    <span>nable</span>
  </div>
  <h1>Dashboard access</h1>
  <p class="sub">{subtitle}</p>
  {pw_block}
  {sso_block}
</div>
</body>
</html>""".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self._send(403, "text/plain", b"Forbidden: unrecognized Host header")
            return
        """Handle POST endpoints: /login and /api/mark-done."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/mark-done":
            if not self._cookie_valid():
                self._send(401, "application/json", b'{"error":"Unauthorized"}')
                return
            rec_id_str = qs.get("id", [""])[0]
            try:
                rec_id = int(rec_id_str)
                from .recommendations.savings_tracker import mark_acted_on
                ok = mark_acted_on(rec_id)
                body = json.dumps({"ok": ok, "id": rec_id}).encode()
                self._send(200, "application/json", body)
            except (ValueError, TypeError):
                self._send(400, "application/json", b'{"error":"Invalid id"}')
            except Exception as exc:
                body = json.dumps({"error": str(exc)}).encode()
                self._send(500, "application/json", body)
            return

        if path == "/api/agent":
            # The in-browser cost copilot. Reuses the Slack bot's agent loop so
            # there is one brain. Full-access session only (not read-only viewers,
            # since the agent can draft remediations). Runs on the user's own
            # ANTHROPIC_API_KEY; degrades to a friendly message if the key or the
            # anthropic package is missing. Stateless for v1 (one question per call).
            if not self._cookie_valid():
                self._send(401, "application/json", b'{"error":"Unauthorized"}')
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                question = (json.loads(raw or "{}").get("question") or "").strip()[:1000]
            except (ValueError, TypeError):
                self._send(400, "application/json", b'{"error":"Invalid JSON"}')
                return
            if not question:
                self._send(400, "application/json", b'{"error":"empty question"}')
                return
            try:
                from .slack_bot.llm import ask
                result = ask(question, tier="chat")
                cards = [se["card"] for se in (result.side_effects or [])
                         if isinstance(se, dict) and se.get("type") == "cost_card" and se.get("card")]
                payload = {"answer": result.answer, "cards": cards,
                           "cardData": [se.get("data") for se in (result.side_effects or [])
                                        if isinstance(se, dict) and se.get("type") == "cost_card"]}
                self._send(200, "application/json", json.dumps(payload, default=str).encode())
            except Exception as exc:
                log.error("agent query failed: %s", exc, exc_info=True)
                self._send(200, "application/json", json.dumps({"answer": None, "error": "agent error"}).encode())
            return

        # Pinned views: save / remove / reorder moldable cost cards. Full-access
        # cookie only; these only touch the local dashboard_views table.
        if path in ("/api/views", "/api/views/delete", "/api/views/reorder"):
            if not self._cookie_valid():
                self._send(401, "application/json", b'{"error":"Unauthorized"}')
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}
            except (ValueError, TypeError):
                self._send(400, "application/json", b'{"error":"Invalid JSON"}')
                return
            try:
                from .slice.views import pin_view as _pin, unpin_view as _unpin, reorder_views as _reorder
                if path == "/api/views":
                    card = body.get("card") or {}
                    if not card.get("slice"):
                        self._send(400, "application/json", b'{"error":"missing card with a slice"}')
                        return
                    vid = _pin(card, owner="instance", scope=(body.get("scope") or "instance"))
                    self._send(200, "application/json", json.dumps({"pinned": True, "id": vid}).encode())
                elif path == "/api/views/delete":
                    try:
                        vid = int(body.get("id"))
                    except (TypeError, ValueError):
                        self._send(400, "application/json", b'{"error":"missing id"}')
                        return
                    self._send(200, "application/json", json.dumps({"unpinned": _unpin(vid, owner="instance")}).encode())
                else:  # /api/views/reorder
                    _reorder([int(x) for x in (body.get("order") or [])], owner="instance")
                    self._send(200, "application/json", b'{"reordered":true}')
            except Exception as exc:
                log.error("views write failed: %s", exc, exc_info=True)
                self._send(500, "application/json", b'{"error":"views error"}')
            return

        if path != "/login":
            self._send(404, "text/plain", b"Not found")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        params = parse_qs(body)
        submitted = params.get("password", [""])[0]
        if _DASHBOARD_PASSWORD and secrets.compare_digest(submitted, _DASHBOARD_PASSWORD):
            session_token = _create_session()
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"nable_session={session_token}; {self._cookie_attrs()}",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._serve_login_page(error=True)

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self._send(403, "text/plain", b"Forbidden: unrecognized Host header")
            return
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # Serve bundled static assets (Chart.js, fonts — no CDN, works offline + GovCloud)
        if path == "/static/chart.min.js":
            if _CHARTJS_PATH.exists():
                body = _CHARTJS_PATH.read_bytes()
                self._send(200, "application/javascript", body)
            else:
                self._send(404, "text/plain", b"Chart.js not bundled")
            return

        if path.startswith("/static/fonts/"):
            fname = path.removeprefix("/static/fonts/")
            # Confine to the fonts dir: this route runs BEFORE auth and the server
            # binds 0.0.0.0, so an unresolved '..' would be an unauthenticated file
            # read (e.g. GET /static/fonts/../../etc/foo.css via a raw HTTP client
            # that does not normalize the path). Resolve and verify containment.
            fonts_dir = (_STATIC_DIR / "fonts").resolve()
            fpath = (fonts_dir / fname).resolve()
            if not str(fpath).startswith(str(fonts_dir) + os.sep):
                self._send(404, "text/plain", b"Font not found")
                return
            if fpath.exists() and fpath.suffix in (".woff2", ".woff", ".css"):
                if fpath.suffix == ".css":
                    mime = "text/css"
                elif fpath.suffix == ".woff2":
                    mime = "font/woff2"
                else:
                    mime = "font/woff"
                self._send(200, mime, fpath.read_bytes())
            else:
                self._send(404, "text/plain", b"Font not found")
            return

        # ── Read-only share link (/view?token=X) ─────────────────────────────
        if path == "/view":
            token_param = qs.get("token", [""])[0]
            if token_param:
                # Token exchange: validate token → set read-only cookie → redirect to /view
                if _ro_token_valid(token_param):
                    ro_session = _create_ro_session()  # read-only store, not full access
                    _RO_TOKENS.pop(token_param, None)  # one-time exchange: consumed here
                    # Audit log
                    client_ip = self.client_address[0]
                    log.info("Read-only share link accessed from %s", client_ip)
                    self.send_response(302)
                    self.send_header("Location", "/view")
                    self.send_header(
                        "Set-Cookie",
                        f"nable_view={ro_session}; {self._cookie_attrs()}",
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    body = b"<h2>Link expired or invalid. Ask for a new share link.</h2>"
                    self._send(403, "text/html; charset=utf-8", body)
                return

            # /view without token — check read-only cookie
            if self._ro_cookie_valid() or self._cookie_valid():
                html = _load_dashboard_html().replace(
                    "</body>",
                    "<script>window._READONLY=true;</script></body>",
                )
                self._send(200, "text/html; charset=utf-8", html.encode())
            else:
                body = b"<h2>This link has expired. Ask for a new share link.</h2>"
                self._send(403, "text/html; charset=utf-8", body)
            return

        # ── SSO endpoints (pre-auth, no cookie required) ─────────────────────
        if path == "/sso/login":
            if not _sso.SSO_ENABLED:
                self._send(404, "text/plain", b"SSO not configured")
                return
            try:
                auth_url = _sso.build_auth_url()
                self.send_response(302)
                self.send_header("Location", auth_url)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception as exc:
                log.error("SSO login error: %s", exc)
                self._serve_login_page(sso_error=f"SSO configuration error: {exc}")
            return

        if path == "/sso/callback":
            if not _sso.SSO_ENABLED:
                self._send(404, "text/plain", b"SSO not configured")
                return
            code = qs.get("code", [""])[0]
            state = qs.get("state", [""])[0]
            error_param = qs.get("error_description", qs.get("error", [""]))[0]
            if error_param:
                self._serve_login_page(sso_error=f"IdP returned error: {error_param}")
                return
            if not code or not state:
                self._serve_login_page(sso_error="Missing code or state in SSO callback")
                return
            try:
                identity = _sso.exchange_code(code, state)
                session_token = _create_session()
                log.info("SSO login: %s (%s)", identity["name"], identity["email"])
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header(
                    "Set-Cookie",
                    f"nable_session={session_token}; {self._cookie_attrs()}",
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception as exc:
                log.warning("SSO callback failed: %s", exc)
                self._serve_login_page(sso_error=f"Sign-in failed: {exc}")
            return

        # Auth check — skip for health, login, and SSO paths (handled above).
        # Use _any_auth_valid so read-only share-link sessions can reach /api/data.
        # Machine clients (OData, Tableau, API) get a JSON 401 instead of the HTML login page.
        _MACHINE_PREFIXES = ("/odata", "/api/", "/tableau/")
        if path not in ("/health", "/login", "/view") and not self._any_auth_valid():
            if _DASHBOARD_PASSWORD or _sso.SSO_ENABLED:
                if any(path.startswith(p) for p in _MACHINE_PREFIXES):
                    self._send(401, "application/json",
                               json.dumps({"error": "Authentication required"}).encode())
                else:
                    self._serve_login_page()
                return

        # Dashboard
        if path == "/" or path == "/index.html":
            self._send(200, "text/html; charset=utf-8", _load_dashboard_html().encode())

        # Health
        elif path == "/health":
            body = json.dumps({"status": "ok"}).encode()
            self._send(200, "application/json", body)

        # Dashboard data
        elif path == "/api/data":
            try:
                days_param = int(qs.get("days", ["30"])[0])
            except (ValueError, IndexError):
                days_param = 30
            provider_param = qs.get("provider", ["all"])[0]
            loop = asyncio.new_event_loop()
            try:
                # 30-second hard cap on the whole data fetch. First load may be
                # slower (scanner cold start); subsequent loads hit the 12h cache.
                data = loop.run_until_complete(
                    asyncio.wait_for(
                        _fetch_dashboard_data(days=days_param, provider=provider_param),
                        timeout=30.0,
                    )
                )
            except asyncio.TimeoutError:
                data = {
                    "error": "Data fetch timed out. The AWS API is slow. Try refreshing in a moment.",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as exc:
                data = {"error": str(exc), "generated_at": datetime.now(timezone.utc).isoformat()}
            finally:
                loop.close()
            body = json.dumps(data).encode()
            self._send(200, "application/json", body)

        # Pinned views: re-run each saved card over its rolling window (one FOCUS
        # fetch for all of them) so the dashboard shows fresh numbers.
        elif path == "/api/views":
            from .slice.views import list_pinned_views as _list
            pins = _list(owner="instance")
            cards = []
            if pins:
                loop = asyncio.new_event_loop()
                try:
                    from .server import rerun_pinned_views
                    cards = loop.run_until_complete(
                        asyncio.wait_for(rerun_pinned_views(pins), timeout=30.0)
                    )
                except asyncio.TimeoutError:
                    cards = [{"id": p["id"], "card": p.get("card"), "data": None, "error": "timed out"} for p in pins]
                except Exception as exc:
                    log.error("rerun pinned views failed: %s", exc, exc_info=True)
                    cards = []
                finally:
                    loop.close()
            self._send(200, "application/json", json.dumps({"views": cards}, default=str).encode())

        # Tableau WDC connector page
        elif path == "/tableau":
            self._send(200, "text/html; charset=utf-8", _TABLEAU_WDC_HTML.encode())

        # Tableau JSON API endpoints
        elif path == "/api/tableau/costs":
            body = json.dumps(_fetch_tableau_costs()).encode()
            self._send(200, "application/json", body)

        elif path == "/api/tableau/opportunities":
            body = json.dumps(_fetch_tableau_opportunities()).encode()
            self._send(200, "application/json", body)

        elif path == "/api/tableau/anomalies":
            body = json.dumps(_fetch_tableau_anomalies()).encode()
            self._send(200, "application/json", body)

        # Tableau CSV download endpoints
        elif path == "/tableau/costs.csv":
            self._send_csv(_fetch_tableau_costs(), "costs.csv")

        elif path == "/tableau/opportunities.csv":
            self._send_csv(_fetch_tableau_opportunities(), "opportunities.csv")

        elif path == "/tableau/anomalies.csv":
            self._send_csv(_fetch_tableau_anomalies(), "anomalies.csv")

        # Power BI connector guide
        elif path == "/powerbi":
            self._send(200, "text/html; charset=utf-8", _POWERBI_GUIDE_HTML.encode())

        # OData v4 service document
        elif path == "/odata" or path == "/odata/":
            host = self.headers.get("Host", "localhost:8080")
            scheme = self.headers.get("X-Forwarded-Proto", "http")
            body = json.dumps({
                "@odata.context": f"{scheme}://{host}/odata/$metadata",
                "value": [
                    {"name": "Costs",         "kind": "EntitySet", "url": "Costs"},
                    {"name": "Opportunities", "kind": "EntitySet", "url": "Opportunities"},
                    {"name": "Anomalies",     "kind": "EntitySet", "url": "Anomalies"},
                ],
            }).encode()
            self._send(200, "application/json;odata.metadata=minimal", body)

        # OData v4 EDMX metadata
        elif path == "/odata/$metadata":
            self._send(200, "application/xml; charset=utf-8", _ODATA_METADATA.encode())

        # OData v4 entity sets
        elif path == "/odata/Costs":
            host = self.headers.get("Host", "localhost:8080")
            scheme = self.headers.get("X-Forwarded-Proto", "http")
            rows = _odata_row_id(_fetch_tableau_costs())
            self._send(200, "application/json;odata.metadata=minimal",
                       _odata_response("Costs", rows, host, scheme))

        elif path == "/odata/Opportunities":
            host = self.headers.get("Host", "localhost:8080")
            scheme = self.headers.get("X-Forwarded-Proto", "http")
            rows = _odata_row_id(_fetch_tableau_opportunities())
            self._send(200, "application/json;odata.metadata=minimal",
                       _odata_response("Opportunities", rows, host, scheme))

        elif path == "/odata/Anomalies":
            host = self.headers.get("Host", "localhost:8080")
            scheme = self.headers.get("X-Forwarded-Proto", "http")
            rows = _odata_row_id(_fetch_tableau_anomalies())
            self._send(200, "application/json;odata.metadata=minimal",
                       _odata_response("Anomalies", rows, host, scheme))

        else:
            self._send(404, "text/plain", b"Not found")


# ── Local IP detection ────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Best-effort local network IP. Falls back to 127.0.0.1."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


# ── Server start helpers ──────────────────────────────────────────────────────

def _make_server(host: str, port: int) -> HTTPServer:
    """Create a threaded HTTPServer, incrementing port on conflict.

    ThreadingHTTPServer (not plain HTTPServer) so a slow /api/data cost fetch on
    a cache miss (up to 30s) does not serialize every other request. The finance
    deploy targets several concurrent non-engineer users, so one slow fetch must
    not stall the dashboard HTML, static assets, /health, or other users.
    """
    for attempt in range(10):
        try:
            server = ThreadingHTTPServer((host, port + attempt), _Handler)
            server.daemon_threads = True  # do not block process exit on in-flight requests
            if attempt > 0:
                log.info("Port %d in use, using %d instead.", port, port + attempt)
            return server
        except OSError:
            continue
    raise OSError(f"Could not bind to any port in range {port}-{port + 9}")


def start_server_background(host: str = "0.0.0.0", port: int = 8080) -> tuple[HTTPServer, int]:
    """Start the dashboard server in a daemon background thread."""
    server = _make_server(host, port)
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, actual_port


# Tracks which finance services this process started, so run_server can shut
# them down cleanly on Ctrl+C.
_FINANCE_STATE: dict[str, bool] = {"scheduler_started": False}


def _start_finance_services() -> list[str]:
    """Start the always-on finance interfaces alongside the dashboard.

    This is what turns `finops serve` (and the Docker deploy that runs it) into a
    team finance host: non-engineers never install anything, they consume nable
    through the Slack bot and scheduled digests started here.

    Two services, each gated so a solo laptop dashboard stays quiet:
      - Scheduler (snapshots, anomaly alerts, daily + weekly digests). Opt-in via
        FINOPS_ENABLE_SCHEDULER=1 so it does not double-send against a separately
        running MCP server on the same machine. The Docker deploy sets it.
      - Slack bot (two-way cost Q&A). Auto-starts when SLACK_BOT_TOKEN and
        SLACK_APP_TOKEN are both present, since that is itself an explicit signal.

    Returns human-readable status lines for the startup banner. Never raises: a
    failed service degrades to an OFF line so the dashboard still serves.
    """
    status: list[str] = []

    def _truthy(v: str | None) -> bool:
        return (v or "").strip().lower() in ("1", "true", "yes", "on")

    if _truthy(os.getenv("FINOPS_ENABLE_SCHEDULER")):
        try:
            from .scheduler.jobs import start_scheduler
            start_scheduler()
            _FINANCE_STATE["scheduler_started"] = True
            status.append("Scheduler:  ON  (snapshots, anomaly alerts, daily + weekly digests)")
        except Exception as exc:  # noqa: BLE001 - never block the dashboard
            log.warning("Scheduler did not start: %s", exc)
            status.append(f"Scheduler:  OFF ({exc})")
    else:
        status.append("Scheduler:  off (set FINOPS_ENABLE_SCHEDULER=1 to push digests + alerts)")

    if os.getenv("SLACK_BOT_TOKEN") and os.getenv("SLACK_APP_TOKEN"):
        try:
            # slack_bolt is imported lazily inside slack_bot.app.main(), which runs
            # in the daemon thread below. Without this precheck a missing dependency
            # would only fail in-thread, after the banner already claimed ON. Check
            # up front so the status line tells the truth.
            import importlib.util
            if importlib.util.find_spec("slack_bolt") is None:
                raise RuntimeError('slack_bolt not installed (pip install "finops-mcp[slack]")')
            from .slack_bot.app import main as _slack_main
            threading.Thread(target=_slack_main, name="nable-slack-bot", daemon=True).start()
            status.append("Slack bot:  ON  (finance asks in Slack, no install needed)")
        except Exception as exc:  # noqa: BLE001
            log.warning("Slack bot did not start: %s", exc)
            status.append(f"Slack bot:  OFF ({exc})")
    else:
        status.append("Slack bot:  off (set SLACK_BOT_TOKEN + SLACK_APP_TOKEN to enable)")

    return status


def run_server(host: str = "0.0.0.0", port: int = 8080, open_browser: bool = False) -> None:
    """Run the dashboard server in the foreground (blocking)."""
    server = _make_server(host, port)
    actual_port = server.server_address[1]
    local_ip = _local_ip()

    print(f"\n  nable dashboard running at http://{host}:{actual_port}")
    if host == "0.0.0.0":
        print(f"  Share this URL with your team: http://{local_ip}:{actual_port}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "\n  Note: this is plain HTTP. The password and session cookie travel "
            "in cleartext on the network.\n"
            "  Front the finance host with TLS (a reverse proxy that sets "
            "X-Forwarded-Proto: https) so cookies are marked Secure. See DEPLOY.md."
        )
    if _AUTH_DISABLED:
        print(f"\n  Auth disabled (FINOPS_DASHBOARD_PASSWORD=off).")
        print(f"  Anyone on your network can access this dashboard.")
    elif _sso.SSO_ENABLED:
        print(f"\n  SSO enabled ({_sso.SSO_ISSUER})")
        print(f"  Sign in at: http://{local_ip}:{actual_port}/sso/login")
        if _DASHBOARD_PASSWORD:
            print(f"  Password auth also active.")
    elif _PASSWORD_AUTO_GENERATED:
        print(f"\n  Dashboard secured with an auto-generated password.")
        print(f"    URL:      http://{local_ip}:{actual_port}")
        print(f"    Password: {_DASHBOARD_PASSWORD}")
        print(f"\n  To set your own: FINOPS_DASHBOARD_PASSWORD=yourpassword finops serve")
        print(f"  To disable auth: FINOPS_DASHBOARD_PASSWORD=off finops serve")
    else:
        masked = "*" * len(_DASHBOARD_PASSWORD)
        print(f"\n  Password protected.")
        print(f"    URL:      http://{local_ip}:{actual_port}")
        print(f"    Password: {masked}  (set via FINOPS_DASHBOARD_PASSWORD)")
        print(f"\n  To disable auth: FINOPS_DASHBOARD_PASSWORD=off finops serve")
    print(f"\n  Integrations: /tableau  /powerbi  /odata")

    finance_status = _start_finance_services()
    print("\n  Finance interfaces (non-engineers consume nable here):")
    for line in finance_status:
        print(f"    {line}")

    print("\n  Press Ctrl+C to stop.\n")

    # Drain the startup banner (including the auto-generated password) before we
    # block forever in serve_forever. A non-TTY stdout (pipe, file, process
    # manager) is block-buffered, so without this flush the password never
    # appears and you are locked out of your own dashboard.
    import sys
    sys.stdout.flush()

    if open_browser:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{actual_port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping...")
        if _FINANCE_STATE.get("scheduler_started"):
            try:
                from .scheduler.jobs import stop_scheduler
                stop_scheduler()
            except Exception as exc:  # noqa: BLE001
                log.debug("Scheduler shutdown failed: %s", exc)
        server.shutdown()
        print("  Stopped.")
