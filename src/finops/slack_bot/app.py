"""
nable Slack bot — entry point.

Uses Slack Bolt with Socket Mode so no public HTTP endpoint is needed.
The bot answers @nable mentions and optionally posts a daily digest.

Run:
  finops-slack

Or directly:
  python -m finops.slack_bot.app
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are nable, a cloud cost intelligence assistant embedded in Slack.
You have access to real billing data across AWS, Azure, GCP, and SaaS providers.

Answer questions concisely — this is Slack, not a document. Use bullet points and
short sentences. Format numbers with $ and commas. If costs are high, say so directly.
If you spot something worth investigating, flag it. Don't hedge excessively.

Never make up data — only report what the tools return. If a provider isn't connected,
say so and move on. Keep responses under 400 words unless the user asks for detail."""


def _call_claude(user_message: str) -> str:
    """Make a Claude API call with nable tools and return the assistant's text response."""
    try:
        import anthropic
    except ImportError:
        return "Error: `anthropic` package not installed. Run `pip install anthropic`."

    from .tools import TOOL_SCHEMAS, execute_tool

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return "Error: ANTHROPIC_API_KEY not set."

    client = anthropic.Anthropic(api_key=api_key)
    messages: list[dict] = [{"role": "user", "content": user_message}]

    # Agentic loop — keep going until Claude stops using tools
    for _ in range(8):  # max 8 tool calls per response
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

        # Collect any text from this turn
        text_parts = [b.text for b in response.content if hasattr(b, "text")]

        if response.stop_reason == "end_turn":
            return "\n".join(text_parts).strip()

        if response.stop_reason != "tool_use":
            return "\n".join(text_parts).strip() or "No response."

        # Execute tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result_str = execute_tool(block.name, block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        # Add assistant turn and tool results to message history
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Reached maximum tool call depth. Please try a more specific question."


def _strip_mention(text: str) -> str:
    """Remove the @nable mention from the message text."""
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


def _post_daily_digest(client: Any) -> None:
    """Post anomaly digest to SLACK_DAILY_CHANNEL if configured."""
    channel = os.getenv("SLACK_DAILY_CHANNEL")
    if not channel:
        return
    try:
        answer = _call_claude(
            "Give me a brief daily cost digest: total spend yesterday, "
            "any anomalies or spikes, and the top 3 cost drivers. Be concise."
        )
        client.chat_postMessage(
            channel=channel,
            text=f"*nable daily digest*\n\n{answer}",
            mrkdwn=True,
        )
    except Exception as e:
        log.error("Daily digest failed: %s", e)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        print("Error: slack_bolt not installed. Run: pip install finops-mcp[slack]")
        raise SystemExit(1)

    bot_token = os.getenv("SLACK_BOT_TOKEN")
    app_token = os.getenv("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        print("Error: SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set.")
        raise SystemExit(1)

    app = App(token=bot_token)

    @app.event("app_mention")
    def handle_mention(event: dict, say: Any) -> None:
        user_text = _strip_mention(event.get("text", ""))
        if not user_text:
            say("Hi! Ask me anything about your cloud costs.")
            return

        thread_ts = event.get("thread_ts") or event.get("ts")
        # Post a "thinking" reaction
        try:
            app.client.reactions_add(
                channel=event["channel"],
                timestamp=event["ts"],
                name="hourglass_flowing_sand",
            )
        except Exception:
            pass

        answer = _call_claude(user_text)

        say(text=answer, thread_ts=thread_ts, mrkdwn=True)

        # Remove the thinking reaction
        try:
            app.client.reactions_remove(
                channel=event["channel"],
                timestamp=event["ts"],
                name="hourglass_flowing_sand",
            )
        except Exception:
            pass

    @app.event("message")
    def handle_dm(event: dict, say: Any) -> None:
        """Handle direct messages to the bot (no @mention needed in DMs)."""
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id"):
            return
        user_text = event.get("text", "").strip()
        if not user_text:
            return
        answer = _call_claude(user_text)
        say(text=answer, mrkdwn=True)

    # Optional: schedule daily digest via APScheduler
    daily_channel = os.getenv("SLACK_DAILY_CHANNEL")
    if daily_channel:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            scheduler = BackgroundScheduler()
            scheduler.add_job(
                lambda: _post_daily_digest(app.client),
                "cron",
                hour=9,
                minute=0,
                timezone="UTC",
            )
            scheduler.start()
            log.info("Daily digest scheduled for 09:00 UTC → %s", daily_channel)
        except ImportError:
            log.warning("apscheduler not installed — daily digest disabled")

    print("nable Slack bot starting (Socket Mode)...")
    handler = SocketModeHandler(app, app_token)
    handler.start()


if __name__ == "__main__":
    main()
