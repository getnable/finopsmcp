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
pip install finops-mcp
finops setup        # connects your providers + auto-configures Claude Desktop
```

That's it. `finops setup` detects Claude Desktop, resolves the correct binary path,
and writes `claude_desktop_config.json` automatically. Restart Claude Desktop and ask:
*"What are my AWS costs this month?"*

**14-day free trial, all features unlocked. No credit card required.**

---

### Manual Claude Desktop config (if needed)

If `finops setup` doesn't auto-configure, run:

```bash
finops setup claude
```

Or add manually — use the **absolute path** from `which finops-mcp`:

**macOS / Linux:**
```json
{
  "mcpServers": {
    "finops": { "command": "/usr/local/bin/finops-mcp" }
  }
}
```

Config file locations:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

> **Why absolute path?** Claude Desktop is a GUI app — it doesn't inherit your
> shell's `$PATH`. A bare `finops-mcp` command will fail unless it's in `/usr/bin`.
> Always use the full path from `which finops-mcp`.

---

### Troubleshooting

```bash
finops-doctor          # checks credentials, DB, network, audit log
finops setup claude    # re-run Claude Desktop configuration only
```

**Common issues:**

| Symptom | Fix |
|---|---|
| Tools don't appear in Claude | Use absolute path in config (`which finops-mcp`) |
| `command not found: finops-mcp` | Re-install: `pip install finops-mcp` |
| Python 3.8/3.9 errors | nable requires Python ≥ 3.10: `python3.10 -m pip install finops-mcp` |
| Corporate SSL errors | `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org finops-mcp` |
| Permission denied | Install to user: `pip install --user finops-mcp` |

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
