# nable — Cloud Cost Intelligence for Claude

**Ask Claude about your cloud costs in plain English.**

nable is an MCP server that connects Claude Desktop, Cursor, Windsurf, and any MCP-compatible AI client to your real billing data across AWS, Azure, GCP, and 10 SaaS tools.

```
"What drove our AWS costs up 40% this month?"
"Which team is spending the most on Datadog?"
"Show me rightsizing opportunities for EC2."
"Create a Jira ticket for any anomalies over $500."
```

No dashboards. No SQL. Just ask.

---

## Quick start

```bash
pip install finops-mcp[pdf,snowflake,keyring]
finops setup   # interactive wizard — connects your providers
```

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "finops": { "command": "finops-mcp" }
  }
}
```

Restart Claude Desktop and ask: *"What are my AWS costs this month?"*

**14-day free trial, all features unlocked. No credit card required.**

---

## Connectors

| Provider | Coverage |
|---|---|
| AWS | Cost Explorer — real spend, savings plans, reservations |
| Azure | Cost Management API |
| GCP | Cloud Billing API + BigQuery export |
| Datadog | Usage Metering API v2 — real dollar amounts |
| Snowflake | ACCOUNT_USAGE.METERING_HISTORY |
| Stripe | Balance Transactions API |
| MongoDB Atlas | Invoice API |
| Twilio | Usage Records API |
| Cloudflare | Billing API |
| GitHub | Actions minutes + Copilot seats |
| Vercel | Invoice API (Enterprise) |
| PagerDuty | Seat count (no billing API) |
| New Relic | Data ingest + user counts |

---

## Pro features

- **Anomaly detection** — flags spend spikes automatically, alerts via Slack or Teams
- **Cost attribution** — break down spend by team, service, or tag
- **Rightsizing recommendations** — underutilized EC2 instances
- **Auto-ticketing** — creates Jira, Linear, or GitHub issues for anomalies
- **Weekly digest** — email summary every Monday, no AI session needed
- **Invoice email parsing** — connects to your billing inbox via IMAP for vendors without APIs

Subscribe at [nable.sh](https://nable.sh) after your trial.

---

## Security

All credentials are encrypted with Fernet and stored in your OS keyring (macOS Keychain, Windows Credential Manager, or libsecret on Linux). They never leave your machine.

---

## Docs

Full setup guide: [nable.sh/docs](https://nable.sh/docs)
