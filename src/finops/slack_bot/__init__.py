"""
nable Slack bot.

Standalone service — run with: finops-slack

Listens for @nable mentions in Slack channels and answers cost questions
using the same underlying tool logic as the MCP server, but with its
own Claude API call so it works independently of any AI client session.

Required env vars:
  SLACK_BOT_TOKEN     — xoxb-... token from Slack app
  SLACK_APP_TOKEN     — xapp-... token (Socket Mode)
  ANTHROPIC_API_KEY   — for making Claude API calls

Optional:
  SLACK_DAILY_CHANNEL — channel ID to post daily anomaly digest (e.g. C0123456)
  FINOPS_LICENSE_KEY  — same as MCP server
"""
