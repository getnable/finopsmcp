# nable

*FinOps MCP server for Claude and Cursor, across AWS, Azure, GCP, Kubernetes, and 15+ SaaS and AI providers.*

**The cost brain your AI agents check before they spend.** Ask your whole cloud and AI bill anything inside Claude or Cursor, get genuine savings priced on your real rates, and the fix as a pull request you approve. One tool across AWS, Azure, GCP, Kubernetes, and 15+ SaaS and AI providers.

[![PyPI](https://img.shields.io/pypi/v/finops-mcp?label=pypi&color=4db8d4)](https://pypi.org/project/finops-mcp/)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/finops-mcp?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/finops-mcp)
[![Tests](https://github.com/chaandannn/finopsmcp/actions/workflows/test.yml/badge.svg)](https://github.com/chaandannn/finopsmcp/actions/workflows/test.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-4db8d4)](LICENSE)

nable is FinOps that lives inside your AI. Ask Claude or Cursor about your cloud, SaaS, and AI spend and it answers with real numbers, priced on your actual rates, not list price. It finds the savings genuinely worth taking, proposes each fix as a pull request you approve, and checks the next bill to prove it worked. As your agents start spending real money, nable is the cost brain they check before they act.

Everything runs on **your machine** and your bill never leaves it, so the no-egress claim is something you can read in the source, not take on faith. Connect AWS or GCP by SSO login or a CLI profile and nable stores no secret at all, it just references your existing login; only keys you paste directly are encrypted in your OS keyring. It is read-only by default and never changes your cloud on its own. The local agent is open and auditable; a hosted platform is available for teams.

**[getnable.com](https://getnable.com)** · docs, quickstart, and the hosted platform

![nable demo: uvx nable welcome --demo shows a sample bill in seconds](https://raw.githubusercontent.com/chaandannn/finopsmcp/main/docs/demo.gif)

### Free to start, runs on your Claude membership

`uvx nable` and ask away. nable runs as a local MCP server inside Claude Desktop, Claude Code, or Cursor, so **your existing Claude Pro/Max or Cursor membership is the model. There is no Anthropic API key and no per-token cost.** Tool calls count against your normal chat usage, the same as any long conversation, never a separate bill.

Cost queries, anomaly detection, and rightsizing findings are **free forever**. The agent team (the budget guard your agents check before they act, remediation PRs, and the learning loop) is Pro.

---

## Quick start

Requires Python 3.11 or newer. The `uvx` command below fetches a matching Python for you. If you take the `pip` path instead, check yours first with `python --version` (or `python3 --version`). On older Python, pip reports `No matching distribution found for finops-mcp`.

**Step 1: Install and run the setup wizard**

Need `uv`? It is not preinstalled on macOS or most Linux:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
# or: brew install uv
```
Then:
```bash
uvx nable
```

No `uv` and don't want it? On Python 3.11+, `pip install -U finops-mcp && finops welcome` works too.

First run downloads dependencies, so give it a moment before the welcome screen appears.

The wizard walks through connecting your providers and auto-configures Claude Desktop at the end. No config file editing, no manual env vars.

**Using Cursor?** One-click install (opens Cursor and adds nable):

[`cursor://anysphere.cursor-deeplink/mcp/install?name=nable&config=eyJjb21tYW5kIjogInV2eCIsICJhcmdzIjogWyItLXB5dGhvbiIsICIzLjEyIiwgImZpbm9wcy1tY3AiXX0=`](cursor://anysphere.cursor-deeplink/mcp/install?name=nable&config=eyJjb21tYW5kIjogInV2eCIsICJhcmdzIjogWyItLXB5dGhvbiIsICIzLjEyIiwgImZpbm9wcy1tY3AiXX0=)

Then run `finops setup` once to connect a cloud account.

**On Anaconda?** Use uvx (isolated, won't touch your Anaconda environment):
```bash
brew install uv && uvx nable setup
```

**Step 2: Connect AWS (usually one keystroke)**

```bash
finops setup aws
```

The wizard checks for AWS credentials you already have (an SSO login, an AWS CLI profile, or default credentials), shows you the account it found, and connects it when you confirm. If you use `aws` on this machine already, you will not type a single key.

```
Checking for AWS credentials on this machine...
✓ Found working credentials: profile 'default' -> account 1234
  Connect this account? [Y/n]
```

Only if no working credentials are found does it walk you through creating a read-only access key. Want the IAM policy to hand your platform team first? Run `finops setup aws --iam-template`.

**Step 3: Restart Claude Desktop and ask**

```
What are my AWS costs this month?
```

Once you see a real cost breakdown, you're live. Also works with Cursor, Windsurf, and VS Code.

**Free. The full local product, including the agent team, costs nothing right now. No credit card, no trial clock.**

---

To add more providers later:
```bash
finops setup aws      # add another AWS account
finops setup azure    # add Azure
finops setup slack    # configure alerts
finops setup license  # activate a license key (Enterprise)
```

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

## Local-first and auditable

Your credentials are encrypted with Fernet and stored in your OS keyring (macOS Keychain, Windows Credential Manager, or libsecret on Linux). They never leave your machine. Cost data is cached in a local SQLite database, and nable has no backend, so we never see your cost data or credentials. One honest caveat: when you ask a question in your AI editor, the figures nable returns go to your editor's own AI to answer it, the same as any prompt, never to a nable server. If you need zero AI exposure, use the CLI (`finops` commands), which never touches a model. Teams share findings via Slack alerts, Notion publishing, and CSV exports. No shared database required.

nable is read-only by default. It never writes to your AWS account unless you explicitly enable cleanup mode. Run `finops setup aws --iam-template` to generate a least-privilege IAM policy with exactly the permissions nable needs.

None of this is take-our-word-for-it. Read the source, check the [OpenSSF Scorecard](https://scorecard.dev/viewer/?uri=github.com/chaandannn/finopsmcp), run `finops-doctor` to see exactly what nable touches, and set `NABLE_NO_TELEMETRY=1` (or `FINOPS_AIRGAP=1` to forbid every non-provider request) if you want it locked down.

---

## Privacy Policy

Full policy: **https://getnable.com/privacy**

nable runs locally and is private by design:

- **Data collection.** In local mode, nable collects no personal data on a server. Your cloud, SaaS, and AI billing data is read from your own provider APIs and cached only on your machine.
- **Usage and storage.** Credentials are encrypted (Fernet) and stored in your OS keyring; cost data is cached in a local SQLite database on your machine. There is no nable backend in the local path.
- **Third-party sharing.** nable does not sell, rent, or share your data. Your billing data and credentials are never sent to a nable server. When you ask a question in your AI editor, the figures nable returns are answered by your editor's own AI provider (the same as any prompt), never routed through nable. Optional integrations you configure (Slack, Notion, Jira, GitHub, email) send only the specific findings you direct to them.
- **Telemetry.** Anonymous, opt-outable product telemetry (no credentials, no cost data). Disable with `NABLE_NO_TELEMETRY=1`, or block all non-provider network calls with `FINOPS_AIRGAP=1`.
- **Data retention.** Local data lives on your machine until you delete it. Uninstall or clear the local store to remove it.
- **Contact.** chaaandannn@gmail.com.

The optional hosted/managed tier runs single-tenant with its own privacy terms at the link above; your data is never pooled with another customer's.

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
    "nable": { "command": "uvx", "args": ["--python", "3.12", "finops-mcp"] }
  }
}
```

**With absolute path:**
```json
{
  "mcpServers": {
    "nable": { "command": "/usr/local/bin/finops-mcp" }
  }
}
```
Use the path from `which finops-mcp`.

Config file locations:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

> **Why uvx?** Claude Desktop is a GUI app and doesn't inherit your shell's PATH. uvx sidesteps this by running finops-mcp in its own isolated environment. It's the most reliable option on corporate machines with managed Python installs.

## Give your agent cost controls

nable is not just tools your agent reads from. It is a pre-action gate your agent
calls **before** it makes a cost-affecting change: it prices the change, checks it
against your budget, and offers a cheaper path. It never applies anything itself.
Propose-only, your agent proposes and a human approves.

Add one line to your agent's system prompt (Claude Code, Cursor, or any MCP client):

> Before you apply any infrastructure change (a terraform apply, a helm upgrade,
> creating or resizing a resource) or start an expensive job, first call
> `check_action_policy` with the action and the change (a terraform plan, a helm
> diff, or a `monthly_delta_usd`). Relay the verdict, the dollar impact, and the
> cheaper path when one is offered. Never apply a `block` or an `escalate` action;
> surface it to the human. nable is advisory and propose-only.

That turns any agent into a cost-aware one. The gate returns `allow` / `warn` /
`block` / `escalate` against your policy, the monthly and annual dollar impact, a
budget verdict labeled with its data age, and a spot alternative when the change is
compute. One-way doors (delete, terminate, buy a commitment) and over-budget changes
always escalate to a human.

---

## Connectors (17)

| Provider | What it pulls |
|---|---|
| AWS | Cost Explorer (free tier) · CUR via S3 (Pro: line-item granularity, savings plans, reservations) |
| Azure | Cost Management API · Advisor cost recs · VM rightsizing (Azure Monitor) · native budgets · forecast |
| GCP | Cloud Billing API + BigQuery export |
| Datadog | Usage Metering API v2: real dollar amounts |
| Snowflake | ACCOUNT_USAGE.METERING_HISTORY |
| Langfuse | Daily metrics API: model cost, token usage, trace volume |
| MongoDB Atlas | Invoice API |
| Twilio | Usage Records API |
| Cloudflare | Billing API |
| Vercel | Invoice API (Enterprise) |
| New Relic | Data ingest + user counts |
| Stripe | Fees and billing activity |
| Databricks | DBU usage and SQL warehouse spend |
| OpenAI | API usage and token spend by model |
| Anthropic | Claude API usage and token spend |

**Azure roles.** The Azure tools span three RBAC roles, granted to the service
principal on each subscription. Without them, the affected tools return empty
results (run `finops doctor` to check):

| Role | Unlocks |
|---|---|
| Cost Management Reader | cost queries, budgets, forecast, cost-by-dimension |
| Reader | Azure Advisor recommendations + VM list (rightsizing) |
| Monitoring Reader | VM CPU metrics (rightsizing) |

```bash
# repeat per subscription
az role assignment create --assignee <client-id> --role 'Cost Management Reader' --scope /subscriptions/<sub-id>
az role assignment create --assignee <client-id> --role Reader --scope /subscriptions/<sub-id>
az role assignment create --assignee <client-id> --role 'Monitoring Reader' --scope /subscriptions/<sub-id>
```

---

## What nable actually does

nable is not just a connector that pipes billing data into Claude. It runs active analysis on your infrastructure and surfaces findings as tools Claude can reason about and act on.

Every finding is classified by how sure we are. A **recommendation** is something nable measured: a precise dollar figure, a safe fix, and a check that the savings actually landed on your next bill. An **investigation** is a signal worth confirming: an honest order-of-magnitude, never a fake-precise number, with the steps to confirm it. nable proposes, you approve, and it verifies. It never changes your infrastructure on its own.

**AWS deep audit** goes well beyond Cost Explorer. It pulls CloudWatch metrics for every running resource and flags waste that never shows up on your bill: gp2 volumes that should be gp3 (20% cheaper, same performance), unattached EBS volumes, idle NAT Gateways costing $32/mo in base charges, RDS backup retention set way too high, CloudWatch Log Groups with no retention policy growing forever, and Lambda functions allocated 2x the memory they actually use. Think of it as Compute Optimizer plus the layer underneath it.

**Anomaly detection** uses z-score, CUSUM drift, and day-of-week seasonal normalization. When something spikes, it drills into Cost Explorer by tag and tells you which team, environment, or service drove it. Anomaly findings and Slack/Teams alerts are free; auto-ticketing is a paid feature.

**Rightsizing** combines AWS Compute Optimizer with nable's own CloudWatch analysis. It gives you specific recommended instance types with estimated savings, not just a list of underutilized resources. Recommendations are free; ticket auto-creation is a paid feature.

**Commitment analysis** (a paid feature) models Savings Plans and Reserved Instance coverage against your actual usage. It shows your current effective discount rate, coverage gaps, and what you would save by purchasing additional commitments.

---

## Open-core

The **local agent** is open-source and free: the MCP server, every connector, cost queries, anomaly detection, rightsizing, AI and LLM spend tracking, and remediation drafts (the PRs and tickets you approve). Run it on your machine, audit it, fork the connectors.

A **hosted platform** is available for teams who would rather have it run for them: a managed, single-tenant workspace with dashboards anyone can use without a terminal, SSO and roles, scheduled reports, and a managed AI agent. Single-tenant by design, your bill is never pooled with anyone else's.

See [getnable.com/pricing](https://getnable.com/pricing) for current plans: Community is free, Enterprise is custom.

### License

This repository is **Apache-2.0** in full: the MCP server and all tools, every connector, FOCUS normalization, anomaly detection, rightsizing, the cost-to-code blame engine, the propose-only PR loop, and the learning loop. Fork it, build on it.

The hosted enterprise layer (the web dashboard, SSO, the control plane, and managed-AI billing) is not part of this repo. It lives in a separate private repository and is offered as the hosted platform above.

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
| AWS returns no data | Run `finops setup aws`. The wizard writes credentials to your editor config automatically. |
| `No matching distribution found for finops-mcp` | Your Python is older than 3.11. Check with `python --version`, then install on 3.11+ (`uvx nable`, or `python3.11 -m pip install finops-mcp`). |
| `cryptography` build error / `maturin failed` | uv tried to compile cryptography from source on Python 3.10, which has no prebuilt wheel. Use 3.11+: `uvx nable`, or force it with `uvx --python 3.12 nable`. |
| Python 3.8 / 3.9 / 3.10 errors | nable requires Python 3.11+: `python3.11 -m pip install finops-mcp` |
| Corporate SSL errors | `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org finops-mcp` |
| Permission denied | Install to user: `pip install --user finops-mcp` or use `uvx` |
| Works at home, not at work | Use `uvx` (corporate IT often strips custom PATH entries) |

---

## Docs

Full setup guide: [getnable.com/docs](https://getnable.com/docs)

---
<sub>mcp-name: io.github.chaandannn/finops-mcp</sub>
