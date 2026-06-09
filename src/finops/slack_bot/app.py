"""
nable Slack bot — interactive, two-way cost intelligence.

Features:
  • @nable <question>  → Claude answers with real cost data (any channel)
  • DM the bot        → same, no @mention needed
  • Anomaly alerts    → Block Kit cards with Acknowledge / Create Ticket / Investigate buttons
  • Interactive buttons → acknowledgment updates DB, ticket fires ticketing integration
  • Scheduled reports → sent on configurable cron (see reports.py)

Setup (Socket Mode — no public HTTP endpoint required):
  SLACK_BOT_TOKEN   xoxb-...   (Slack App → OAuth & Permissions)
  SLACK_APP_TOKEN   xapp-...   (Slack App → Basic Information → App-Level Tokens)
  ANTHROPIC_API_KEY sk-ant-...

Optional:
  SLACK_ALERT_CHANNEL   #finops-alerts   (anomaly + budget alerts)
  SLACK_REPORT_CHANNEL  #finops-reports  (scheduled reports default channel)
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import time
from typing import Any

log = logging.getLogger(__name__)

# ── Per-user rate limiter ────────────────────────────────────────────────────

_RATE_LIMIT_MAX = 10        # max requests per window
_RATE_LIMIT_WINDOW = 60     # window in seconds
_user_request_times: dict[str, collections.deque] = {}


def _is_rate_limited(user_id: str) -> bool:
    """Return True if user_id has exceeded the per-user rate limit."""
    now = time.monotonic()
    if user_id not in _user_request_times:
        _user_request_times[user_id] = collections.deque()
    dq = _user_request_times[user_id]
    # Evict timestamps outside the window
    while dq and dq[0] < now - _RATE_LIMIT_WINDOW:
        dq.popleft()
    if len(dq) >= _RATE_LIMIT_MAX:
        return True
    dq.append(now)
    return False

def _resolve_finops_role(slack_user_id: str, client: Any = None) -> str:
    """
    Map a Slack user to a finops RBAC role.

    When FINOPS_REQUIRE_AUTH != "1", returns "admin" (permissive mode).
    Otherwise looks up the Slack user's email via the Slack API, then
    queries the api_keys table for a matching active key and returns
    its role.  Falls back to "viewer" if no match is found.
    """
    if os.environ.get("FINOPS_REQUIRE_AUTH") != "1":
        return "admin"

    if client is None:
        return "viewer"

    try:
        resp = client.users_info(user=slack_user_id)
        email = resp["user"]["profile"].get("email", "")
    except Exception:
        log.warning("Failed to resolve Slack user %s to email", slack_user_id)
        return "viewer"

    if not email:
        return "viewer"

    try:
        from ..storage.db import api_keys, get_engine
        from sqlalchemy import select

        with get_engine().connect() as conn:
            row = conn.execute(
                select(api_keys.c.role).where(
                    api_keys.c.email == email,
                    api_keys.c.is_active == True,
                )
            ).fetchone()
        if row:
            return row.role
    except Exception:
        log.warning("Failed to query api_keys for email %s", email)

    return "viewer"


def _identity_for_slack(slack_user_id: str, client: Any = None) -> Any:
    """Resolve a Slack user to an Identity and set it for the current thread.

    Returns the Identity so it can be passed into the agentic loop, which runs
    in a separate worker thread where ContextVars do not propagate.
    """
    from ..auth.rbac import Identity, set_current_identity

    role = _resolve_finops_role(slack_user_id, client)

    email = ""
    try:
        if client:
            resp = client.users_info(user=slack_user_id)
            email = resp["user"]["profile"].get("email", "")
    except Exception:
        pass

    ident = Identity(
        key_id=0,
        name=f"slack:{slack_user_id}",
        email=email,
        role=role,
        scope_team=None,
        scope_provider=None,
    )
    set_current_identity(ident)
    return ident


def _call_claude(user_message: str, tier: str = "chat", identity: Any = None) -> str:
    """Thin compatibility wrapper around the tiered loop (no memory, no side effects)."""
    from .llm import ask

    return ask(user_message, tier=tier, identity=identity).answer


def _post_side_effects(client: Any, channel: str, thread_ts: str | None, side_effects: list[dict]) -> None:
    """Post approval cards (and any future side effects) produced during a loop."""
    from .remediation import approval_blocks

    for effect in side_effects or []:
        if effect.get("type") != "approval_card":
            continue
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Approval needed",
                blocks=approval_blocks(effect["action_id"]),
            )
        except Exception as e:
            log.error("Failed to post approval card: %s", e)


# ── Block Kit builders ────────────────────────────────────────────────────────

def _anomaly_alert_blocks(anomaly: dict[str, Any]) -> list[dict]:
    severity  = anomaly.get("severity", "medium")
    direction = anomaly.get("direction", "spike")
    sev_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(severity, "⚠️")
    dir_emoji = "📈" if direction == "spike" else "📉"
    pct       = abs(anomaly.get("pct_change", 0))
    sign      = "+" if direction == "spike" else "-"
    anomaly_id = str(anomaly.get("id", ""))

    return [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"{sev_emoji} Cost Anomaly — {severity.upper()} severity"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Provider*\n{anomaly.get('provider', '').upper()}"},
            {"type": "mrkdwn", "text": f"*Service*\n{anomaly.get('service', '')}"},
            {"type": "mrkdwn", "text": f"*Change*\n{dir_emoji} {sign}{pct:.0f}% vs 28-day avg"},
            {"type": "mrkdwn", "text": f"*Spend*\n${anomaly.get('current_amount', 0):,.2f}"},
            {"type": "mrkdwn", "text": f"*Baseline avg*\n${anomaly.get('baseline_mean', 0):,.2f}"},
            {"type": "mrkdwn", "text": f"*Z-score*\n{anomaly.get('z_score', 0):.2f}"},
        ]},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Acknowledge"},
             "style": "primary", "action_id": "ack_anomaly", "value": anomaly_id},
            {"type": "button", "text": {"type": "plain_text", "text": "🎫 Create Ticket"},
             "action_id": "create_ticket", "value": json.dumps(anomaly)},
            {"type": "button", "text": {"type": "plain_text", "text": "🔍 Investigate"},
             "action_id": "investigate_anomaly",
             "value": json.dumps({"provider": anomaly.get("provider"), "service": anomaly.get("service")})},
        ]},
        {"type": "divider"},
    ]


def _budget_alert_blocks(budget_status: dict[str, Any]) -> list[dict]:
    pct  = budget_status.get("pct_used", 0)
    icon = "🔴" if budget_status.get("status") == "exceeded" else "🟡"
    return [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"{icon} Budget Alert — {budget_status.get('name', '')}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Spent*\n${budget_status.get('spent', 0):,.0f}"},
            {"type": "mrkdwn", "text": f"*Limit*\n${budget_status.get('limit', 0):,.0f}"},
            {"type": "mrkdwn", "text": f"*Used*\n{pct:.0f}%"},
            {"type": "mrkdwn", "text": f"*Remaining*\n${budget_status.get('remaining', 0):,.0f}"},
        ]},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "📊 View Details"},
             "action_id": "view_budget_details", "value": str(budget_status.get("id", ""))},
        ]},
        {"type": "divider"},
    ]


# ── Interaction handlers ──────────────────────────────────────────────────────

def _handle_ack_anomaly(body: dict, client: Any, respond: Any) -> None:
    action = body["actions"][0]
    anomaly_id = int(action.get("value", 0))
    user = body.get("user", {}).get("name", "someone")
    try:
        from ..anomaly.detector import acknowledge_anomaly
        acknowledge_anomaly(anomaly_id)
        respond(replace_original=False, text=f"✅ Anomaly #{anomaly_id} acknowledged by @{user}")
        try:
            client.reactions_add(channel=body["channel"]["id"],
                                 timestamp=body["message"]["ts"], name="white_check_mark")
        except Exception:
            pass
    except Exception as e:
        respond(text=f"❌ Failed to acknowledge: {e}", replace_original=False)


def _handle_create_ticket(body: dict, respond: Any) -> None:
    action = body["actions"][0]
    try:
        anomaly = json.loads(action.get("value", "{}"))
        from ..integrations.ticketing import create_ticket
        url = create_ticket(anomaly)
        if url:
            respond(text=f"🎫 Ticket created: {url}", replace_original=False)
        else:
            respond(text="❌ No ticketing provider configured. Set JIRA_BASE_URL, JIRA_API_TOKEN, JIRA_USER_EMAIL, JIRA_PROJECT_KEY",
                    replace_original=False)
    except Exception as e:
        respond(text=f"❌ Ticket creation failed: {e}", replace_original=False)


def _handle_investigate(body: dict, client: Any, respond: Any, identity: Any = None) -> None:
    action = body["actions"][0]
    try:
        info = json.loads(action.get("value", "{}"))
        respond(text="🔍 Investigating...", replace_original=False)
        answer = _call_claude(
            f"Run a root cause analysis for the cost anomaly on {info.get('provider','')} / "
            f"{info.get('service','')}. Start with explain_recent_cost_drivers, then drill into "
            f"the affected service with get_costs_by_service and check get_anomalies for context. "
            "Report: the dollar impact, the most likely cause with evidence, two alternative "
            "explanations, and the single next step. If a fix is actionable, offer to draft a ticket.",
            tier="rca",
            identity=identity,
        )
        channel   = body.get("channel", {}).get("id", "")
        thread_ts = body.get("message", {}).get("ts", "")
        if channel:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=answer, mrkdwn=True)
    except Exception as e:
        respond(text=f"❌ Investigation failed: {e}", replace_original=False)


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _start_slack_scheduler(app: Any) -> None:
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        import asyncio

        scheduler = BackgroundScheduler(timezone="UTC")

        def _run_reports() -> None:
            try:
                from ..notifications.reports import list_subscriptions, run_subscription, _is_report_due
                for sub in list_subscriptions():
                    if _is_report_due(sub["cron"], sub.get("last_sent_at"), sub.get("created_at")):
                        try:
                            asyncio.run(run_subscription(sub["id"]))
                        except Exception as e:
                            log.error("Report '%s' failed: %s", sub["name"], e)
            except Exception as e:
                log.error("Report scheduler failed: %s", e)

        def _budget_alerts() -> None:
            try:
                from ..budget.enforcer import check_all_budgets
                channel = os.getenv("SLACK_ALERT_CHANNEL", "")
                if not channel:
                    return
                for b in check_all_budgets():
                    if b["status"] in ("exceeded", "warning"):
                        app.client.chat_postMessage(
                            channel=channel,
                            text=f"Budget alert: {b['name']} at {b['pct_used']:.0f}%",
                            blocks=_budget_alert_blocks(b),
                        )
            except Exception as e:
                log.error("Budget alert job failed: %s", e)

        scheduler.add_job(_run_reports, "interval", minutes=5, id="slack_reports")
        scheduler.add_job(_budget_alerts, "interval", hours=1, id="budget_alerts")
        scheduler.start()
        log.info("Slack scheduler started")
    except ImportError:
        log.warning("apscheduler not installed — scheduled reports/alerts disabled")


# ── Bot entry point ───────────────────────────────────────────────────────────

def _strip_mention(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        print("Error: slack_bolt not installed. Run: pip install finops-mcp[slack]")
        raise SystemExit(1)

    # Fall back to the credential vault for anything not in the environment,
    # so `finops setup slack` is enough and no .env file is required.
    try:
        from ..security.vault import Vault

        loaded = Vault.default().load_to_env()
        if loaded:
            log.info("Loaded %d credentials from vault", loaded)
    except Exception as e:
        log.debug("Vault unavailable, using environment only: %s", e)

    bot_token = os.getenv("SLACK_BOT_TOKEN")
    app_token = os.getenv("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        print("Error: SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set.")
        print("Run: finops setup slack  (choose the conversational bot option)")
        raise SystemExit(1)
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Warning: ANTHROPIC_API_KEY not set. The bot will connect but can't answer questions.")

    app = App(token=bot_token)

    # Load the MCP tool registry once at startup so the first question is fast.
    try:
        from .bridge import warm

        print(f"Bridged {warm()} MCP tools into the Slack loop.")
    except Exception as e:
        log.error("Tool bridge failed to load, falling back to nothing: %s", e)

    def _converse(event: dict, say: Any, user_text: str, channel: str, thread_ts: str | None) -> None:
        """Shared mention/DM path: identity, memory, tiering, side effects."""
        from .llm import ask, pick_tier
        from . import memory

        user_id = event.get("user", "")
        identity = _identity_for_slack(user_id, app.client)
        history = memory.load_history(channel, thread_ts)
        result = ask(
            user_text,
            tier=pick_tier(user_text),
            identity=identity,
            history=history,
            requested_by=user_id,
        )
        say(text=result.answer, thread_ts=thread_ts, mrkdwn=True)
        memory.save_turn(channel, thread_ts, user_text, result.answer)
        _post_side_effects(app.client, channel, thread_ts, result.side_effects)

    @app.event("app_mention")
    def handle_mention(event: dict, say: Any) -> None:
        user_id = event.get("user", "")
        if _is_rate_limited(user_id):
            say("Rate limit exceeded, please wait a moment.")
            return
        user_text = _strip_mention(event.get("text", ""))
        if not user_text:
            say("Hi! Ask me anything about your cloud costs. Try: _\"show me last month's spend\"_")
            return
        thread_ts = event.get("thread_ts") or event.get("ts")
        try:
            app.client.reactions_add(channel=event["channel"], timestamp=event["ts"], name="hourglass_flowing_sand")
        except Exception:
            pass
        _converse(event, say, user_text, event["channel"], thread_ts)
        try:
            app.client.reactions_remove(channel=event["channel"], timestamp=event["ts"], name="hourglass_flowing_sand")
        except Exception:
            pass

    @app.event("message")
    def handle_dm(event: dict, say: Any) -> None:
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id"):
            return
        user_id = event.get("user", "")
        if _is_rate_limited(user_id):
            say("Rate limit exceeded, please wait a moment.")
            return
        user_text = event.get("text", "").strip()
        if not user_text:
            return
        # DMs are flat, so the channel itself is the conversation key.
        _converse(event, say, user_text, event.get("channel", ""), None)

    @app.action("ack_anomaly")
    def on_ack(body: dict, client: Any, respond: Any) -> None:
        _identity_for_slack(body.get("user", {}).get("id", ""), client)
        _handle_ack_anomaly(body, client, respond)

    @app.action("create_ticket")
    def on_ticket(body: dict, client: Any, respond: Any) -> None:
        _identity_for_slack(body.get("user", {}).get("id", ""), client)
        _handle_create_ticket(body, respond)

    @app.action("investigate_anomaly")
    def on_investigate(body: dict, client: Any, respond: Any) -> None:
        identity = _identity_for_slack(body.get("user", {}).get("id", ""), client)
        _handle_investigate(body, client, respond, identity=identity)

    @app.action("view_budget_details")
    def on_budget(body: dict, client: Any, respond: Any) -> None:
        identity = _identity_for_slack(body.get("user", {}).get("id", ""), client)
        bid = body["actions"][0].get("value", "")
        respond(text=_call_claude(
            f"Show budget details for budget ID {bid}: spend, burn rate, what's driving it.",
            tier="simple",
            identity=identity,
        ), replace_original=False)

    @app.action("approve_action")
    def on_approve(body: dict, client: Any, respond: Any) -> None:
        from .remediation import approve_action

        user = body.get("user", {})
        identity = _identity_for_slack(user.get("id", ""), client)
        action_id = int(body["actions"][0].get("value", 0))
        outcome = approve_action(action_id, resolved_by=user.get("id", ""), role=identity.role)
        if outcome.get("error"):
            respond(text=f"❌ {outcome['error']}", replace_original=False)
            return
        url = outcome.get("pr_url") or outcome.get("ticket_url") or ""
        label = "PR opened" if outcome.get("pr_url") else "Ticket created"
        suffix = f": {url}" if url else "."
        respond(
            text=f"✅ Action #{action_id} approved by <@{user.get('id','')}>. {label}{suffix}",
            replace_original=False,
        )

    @app.action("cancel_action")
    def on_cancel(body: dict, client: Any, respond: Any) -> None:
        from .remediation import cancel_action

        user = body.get("user", {})
        _identity_for_slack(user.get("id", ""), client)
        action_id = int(body["actions"][0].get("value", 0))
        outcome = cancel_action(action_id, resolved_by=user.get("id", ""))
        if outcome.get("error"):
            respond(text=f"❌ {outcome['error']}", replace_original=False)
        else:
            respond(text=f"🚫 Action #{action_id} cancelled by <@{user.get('id','')}>.", replace_original=False)

    _start_slack_scheduler(app)

    # Warn if running in Postgres mode without auth enforcement
    if os.getenv("DATABASE_URL") and os.getenv("FINOPS_REQUIRE_AUTH") != "1":
        log.warning(
            "WARNING: Running in shared/Postgres mode without FINOPS_REQUIRE_AUTH=1. "
            "All users have full access. Set FINOPS_REQUIRE_AUTH=1 to enforce RBAC."
        )

    print("nable Slack bot starting (Socket Mode)...")
    SocketModeHandler(app, app_token).start()


# ── Public helper for anomaly scheduler ──────────────────────────────────────

async def send_interactive_anomaly_alert(anomaly: dict[str, Any]) -> bool:
    import httpx
    token   = os.getenv("SLACK_BOT_TOKEN", "")
    webhook = os.getenv("SLACK_WEBHOOK_URL", "")
    channel = os.getenv("SLACK_ALERT_CHANNEL", os.getenv("SLACK_CHANNEL", "#finops-alerts"))
    blocks  = _anomaly_alert_blocks(anomaly)
    pct     = abs(anomaly.get("pct_change", 0))
    text    = (f"Cost anomaly: {anomaly.get('provider','').upper()}/{anomaly.get('service','')} "
               f"{'+' if anomaly.get('direction') == 'spike' else '-'}{pct:.0f}% ({anomaly.get('severity','')})")
    if token:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post("https://slack.com/api/chat.postMessage",
                             headers={"Authorization": f"Bearer {token}"},
                             json={"channel": channel, "text": text, "blocks": blocks})
            return r.json().get("ok", False)
    elif webhook:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(webhook, json={"text": text, "blocks": blocks})
            return r.status_code == 200
    return False


if __name__ == "__main__":
    main()
