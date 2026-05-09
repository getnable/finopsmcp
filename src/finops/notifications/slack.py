from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

_SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
_DIRECTION_EMOJI = {"spike": "📈", "drop": "📉"}


def _webhook_url() -> str:
    return os.environ.get("SLACK_WEBHOOK_URL", "")


def _bot_token() -> str:
    return os.environ.get("SLACK_BOT_TOKEN", "")


def _channel() -> str:
    return os.environ.get("SLACK_CHANNEL", "#finops-alerts")


async def send_webhook(blocks: list[dict], text: str = "") -> bool:
    url = _webhook_url()
    if not url:
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json={"text": text, "blocks": blocks})
        return r.status_code == 200


async def send_bot(blocks: list[dict], text: str = "") -> bool:
    token = _bot_token()
    if not token:
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": _channel(), "text": text, "blocks": blocks},
        )
        return r.json().get("ok", False)


async def send(blocks: list[dict], text: str = "") -> bool:
    if _webhook_url():
        return await send_webhook(blocks, text)
    if _bot_token():
        return await send_bot(blocks, text)
    return False


def is_configured() -> bool:
    return bool(_webhook_url() or _bot_token())


# ── Block Kit builders ────────────────────────────────────────────────────────

def anomaly_blocks(anomaly: dict[str, Any]) -> list[dict]:
    emoji = _SEVERITY_EMOJI.get(anomaly["severity"], "⚠️")
    d_emoji = _DIRECTION_EMOJI.get(anomaly["direction"], "↕️")
    pct = abs(anomaly["pct_change"])
    sign = "+" if anomaly["direction"] == "spike" else "-"

    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} Cost Anomaly — {anomaly['severity'].upper()} severity"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Provider*\n{anomaly['provider'].upper()}"},
            {"type": "mrkdwn", "text": f"*Service*\n{anomaly['service']}"},
            {"type": "mrkdwn", "text": f"*Change*\n{d_emoji} {sign}{pct:.0f}% vs 28-day avg"},
            {"type": "mrkdwn", "text": f"*Today*\n${anomaly['current_amount']:,.2f}"},
            {"type": "mrkdwn", "text": f"*Baseline avg*\n${anomaly['baseline_mean']:,.2f}"},
            {"type": "mrkdwn", "text": f"*Z-score*\n{anomaly['z_score']:.2f}"},
        ]},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"Detected {anomaly.get('detected_at', '')} · Account: {anomaly.get('account_id', '')}"}
        ]},
        {"type": "divider"},
    ]


def daily_digest_blocks(
    report_date: date,
    grand_total: float,
    prev_total: float,
    by_provider: dict[str, float],
    top_services: list[dict],
    active_anomaly_count: int,
) -> list[dict]:
    delta = grand_total - prev_total
    delta_pct = (delta / prev_total * 100) if prev_total else 0
    trend_emoji = "📈" if delta > 0 else "📉" if delta < 0 else "➡️"
    sign = "+" if delta >= 0 else ""

    provider_text = "\n".join(
        f"• *{p.upper()}*: ${v:,.2f}" for p, v in sorted(by_provider.items(), key=lambda x: -x[1])
    )
    service_text = "\n".join(
        f"{i+1}. {s['service']}: *${s['amount_usd']:,.2f}* ({s.get('pct', 0):.1f}%)"
        for i, s in enumerate(top_services[:5])
    )

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 FinOps Daily — {report_date.strftime('%B %d, %Y')}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Total spend*\n${grand_total:,.2f}"},
            {"type": "mrkdwn", "text": f"*vs yesterday*\n{trend_emoji} {sign}{delta_pct:.1f}% ({sign}${abs(delta):,.2f})"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*By provider*\n{provider_text}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Top services*\n{service_text}"}},
    ]

    if active_anomaly_count > 0:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"⚠️ *{active_anomaly_count} active anomaly{'s' if active_anomaly_count > 1 else ''}* detected — review with Claude: _\"show me the cost anomalies\"_"},
        })

    blocks.append({"type": "divider"})
    return blocks


async def send_anomaly_alert(anomaly: dict[str, Any]) -> bool:
    blocks = anomaly_blocks(anomaly)
    pct = abs(anomaly["pct_change"])
    sign = "+" if anomaly["direction"] == "spike" else "-"
    text = f"Cost anomaly: {anomaly['provider']} / {anomaly['service']} {sign}{pct:.0f}% ({anomaly['severity']})"
    return await send(blocks, text)


async def send_daily_digest(
    report_date: date,
    grand_total: float,
    prev_total: float,
    by_provider: dict[str, float],
    top_services: list[dict],
    active_anomaly_count: int,
) -> bool:
    blocks = daily_digest_blocks(report_date, grand_total, prev_total, by_provider, top_services, active_anomaly_count)
    return await send(blocks, f"FinOps daily digest — ${grand_total:,.2f} total spend")
