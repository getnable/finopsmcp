# nable: The FinOps copilot that runs on your machine

**Connect Claude to your real AWS, Azure, GCP, and SaaS billing data. Ask questions in plain English. Get answers in seconds.**

![nable in Claude Desktop](docs/claude-cost-answer.png)

```
User: Just downloaded nable: What's our cloud spend this month?

Claude: Here's your spend breakdown:

Total: $8,421.63 / month

Provider    | Spend
------------|----------
AWS         | $7,284.17
Datadog     | $847.20
Snowflake   | $290.26

Top AWS services:
- EC2: $3,184.52
- EKS: $1,821.34
- RDS: $1,243.80
- ElastiCache: $612.40
- S3: $184.22

Most spend is concentrated in us-east-1 ($5,841 of the $7,284 AWS total).
```


---

## Quick start

**Step 1: Install**
```bash
pip install finops-mcp
```

**On Anaconda?** Use uvx instead (it's isolated and won't touch your Anaconda environment):
```bash
brew install uv        # or: curl -LsSf https://astral.sh/uv/install.sh | sh
uvx finops-mcp setup
```

**Step 2: Connect your providers**
```bash
finops setup
```

`finops setup` walks you through connecting AWS, Azure, GCP, Datadog, Snowflake, and more. It auto-configures Claude Desktop at the end.

**Step 3: Ask Claude**

Restart Claude Desktop, then ask: *"What are my AWS costs this month?"*

**1-month free trial, all features unlocked. No credit card required.**

---

## What you can ask

- "What drove our AWS bill up 40% last month?"
- "Which Kubernetes namespace is over-provisioned?"
- "Are there any unusual cost spikes this week?"
- "Which EC2 instances should we downsize?"
- "Compare our cloud spend vs SaaS spend"
- "Create a Jira ticket for any EC2 waste over $200/mo"
- "Which team is spending the most on Datadog?"
- "What will our AWS bill look like next month?"
- "Show me RDS instances with low CPU that we could right-size"
- "What's our effective discount rate from Savings Plans?"

---

## How it works

nable runs entirely on your machine. Your credentials are encrypted with Fernet and stored in your OS keyring (macOS Keychain, Windows Credential Manager, or libsecret on Linux). Nothing leaves your machine except the API calls to your cloud providers.

nable is read-only by default. It never writes to your AWS account unless you explicitly enable cleanup mode. Run `finops setup aws --iam-template` to generate a least-privilege IAM policy with exactly the permissions nable needs.

---

## Manual Claude Desktop config

If `finops setup` doesn't auto-configure, run:

```bash
finops setup claude
```

Or add manually to `claude_desktop_config.json`:

**With uvx (recommended):**
```json
{
  "mcpServers": {
    "finops": { "command": "uvx", "args": ["finops-mcp"] }
  }
}
```

**With absolute path:**
```json
{
  "mcpServers": {
    "finops": { "command": "/usr/local/bin/finops-mcp" }
  }
}
```
Use the path from `which finops-mcp`.

Config file locations:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

> **Why uvx?** Claude Desktop is a GUI app and doesn't inherit your shell's PATH. uvx sidesteps this by running finops-mcp in its own isolated environment. It's the most reliable option on corporate machines with managed Python installs.

---

## Connectors (17)

| Provider | What it pulls |
|---|---|
| AWS | Cost Explorer (free tier) · CUR via S3 (Team: line-item granularity, savings plans, reservations) |
| Azure | Cost Management API |
| GCP | Cloud Billing API + BigQuery export |
| Datadog | Usage Metering API v2: real dollar amounts |
| Snowflake | ACCOUNT_USAGE.METERING_HISTORY |
| Langfuse | Daily metrics API: model cost, token usage, trace volume |
| MongoDB Atlas | Invoice API |
| Twilio | Usage Records API |
| Cloudflare | Billing API |
| GitHub | Actions minutes + Copilot seats |
| Vercel | Invoice API (Enterprise) |
| PagerDuty | Seat count |
| New Relic | Data ingest + user counts |

---

## What nable actually does

nable is not just a connector that pipes billing data into Claude. It runs active analysis on your infrastructure and surfaces findings as tools Claude can reason about and act on.

**AWS deep audit** goes well beyond Cost Explorer. It pulls CloudWatch metrics for every running resource and flags waste that never shows up on your bill until it's too late: gp2 volumes that should be gp3 (20% cheaper, same performance), unattached EBS volumes, idle NAT Gateways costing $32/mo in base charges, RDS backup retention set way too high, CloudWatch Log Groups with no retention policy growing forever, and Lambda functions allocated 2x the memory they actually use. Think of it as Compute Optimizer plus the layer underneath it.

**Anomaly detection** uses z-score, CUSUM drift, and day-of-week seasonal normalization. When something spikes, it drills into Cost Explorer by tag and tells you which team, environment, or service drove it.

**Rightsizing** combines AWS Compute Optimizer with nable's own CloudWatch analysis. It gives you specific recommended instance types with estimated savings, not just a list of underutilized resources.

**Commitment analysis** (Team plan) models Savings Plans and Reserved Instance coverage against your actual usage. It shows your current effective discount rate, coverage gaps, and what you would save by purchasing additional commitments.

---

## Team plan

- Anomaly alerts via Slack or Teams
- Cost attribution by team, service, or tag
- Auto-ticketing: creates Jira, Linear, or GitHub issues for anomalies and waste findings
- Scheduled email reports
- Commitment purchase recommendations with ROI projections
- Org-wide multi-account cost rollup
- Invoice email parsing via IMAP for vendors without APIs

**$39.99/mo. First month free, no credit card required.** Subscribe at [getnable.com](https://getnable.com).

---

## Troubleshooting

```bash
finops-doctor          # checks credentials, DB, network, audit log
finops setup claude    # re-run Claude Desktop configuration only
```

| Symptom | Fix |
|---|---|
| Tools don't appear in Claude | Switch to uvx config or use absolute path |
| `command not found: finops-mcp` | Re-install with `pip install finops-mcp` or use `uvx` |
| Python 3.8/3.9 errors | nable requires Python 3.10+: `python3.10 -m pip install finops-mcp` |
| Corporate SSL errors | `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org finops-mcp` |
| Permission denied | Install to user: `pip install --user finops-mcp` or use `uvx` |
| Works at home, not at work | Use `uvx` (corporate IT often strips custom PATH entries) |

---

## Docs

Full setup guide: [getnable.com/docs](https://getnable.com/docs)
