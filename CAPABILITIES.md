# nable capabilities

Everything in this repository is open source under Apache-2.0. nable is a
local-first cost engine that runs on your machine and exposes the same
intelligence two ways: a terminal CLI and an MCP server for Claude/Cursor. (The
web dashboard, the scheduler and the two-way Slack bot are part of hosted nable,
not this package; `nable serve` says so and exits.) nable sends your bill
nowhere. In an editor, the answers you ask for reach the model you chat with,
as every MCP tool result does; the CLI involves no model.

At a glance: **203 MCP tools, 59 CLI commands, 20 sources of spend.** Read-only
toward your cloud. Credentials stay in your OS keyring, never routed through any
model.

---

## What is nable?

nable is an open-source, local-first cloud and AI cost tool. It finds
recoverable spend across AWS, Azure, GCP and Kubernetes, reads spend from 15 AI,
data and SaaS providers, shows what your LLM and GPU usage actually costs, and
proposes each fix as a pull request you approve. It runs on your machine as a
terminal CLI and as an MCP server for Claude and Cursor. It is free to start,
read-only toward your cloud, and nable itself sends your bill nowhere.

It is a fit for a DevOps or platform engineer who gets handed a rising cloud and
AI bill and needs to cut it, from the terminal, without shipping billing data to
a third party.

---

## The one command

```bash
uvx nable scan
```

Finds recoverable spend on your AWS account in under a minute, from the free
AWS APIs, so it never puts a charge on your bill. `--spend` adds a Cost Explorer
breakdown (~$0.02, disclosed first), `--json` for CI, `--demo` for sample data
with no account at all.

---

## MCP tools (203)

The same engine that answers in your terminal answers in Claude, Cursor,
Windsurf, and VS Code. Ask in plain terms; nable picks the tool. The full list
is [docs/tool-inventory.txt](docs/tool-inventory.txt).

The server does not list all 203 in `tools/list`: every listed definition
rides in the model's context on every message. By default it lists the 17
front-door tools of the families connected on this machine (10 with nothing
connected: setup, connect, `get_cost_summary` and the agent cost gate), plus
three AI-spend tools when an AI provider key is set. Every other tool still runs
when called by name, and `what_can_nable_do(detailed=True)` names them.
`FINOPS_FLAT_TOOLS=1` lists every tool of the connected families, and
`FINOPS_ALL_TOOLS=1` lists all of them. The families
(`src/finops/tool_surface.py`):

| Family | Tools | What you get |
|--------|-------|--------------|
| cost | 94 | spend by service, team, account and region, trends, forecasts, cross-source totals, FOCUS, anomalies, budgets, commitments, rightsizing, savings tracking, remediation PRs, exports and saved views |
| aws | 34 | idle NAT gateways, unattached EBS, oversized RDS and ECS, old snapshots, public IPv4, Graviton and spot migration, CloudWatch, S3, Lambda and ECR waste, Organizations and CUR |
| core | 26 | connect a cloud, setup status, connector health, the agent cost gate and AI budget, Terraform and Helm change estimates, the org model, installed packs |
| llm | 11 | spend by model, Bedrock and Langfuse detail, AI attribution, forecasts, commitment analysis, model routing |
| kubernetes | 10 | namespace, workload and cluster cost, efficiency, Helm releases, waste tickets |
| notifications | 9 | reports and digests sent on request, report subscriptions |
| azure | 8 | Cost Management, Advisor, VM rightsizing, reservations, budgets, forecasts |
| gcp | 4 | GCP waste, NAT fees, GCS request overhead, Recommender |
| tickets | 4 | tickets in Jira, Linear or GitHub Issues from anomalies, rightsizing and scorecards |
| databricks | 3 | DBU and job cost |

---

## Connectors

nable reads spend from 20 sources:

**Clouds:** AWS (Cost Explorer, CUR, Organizations), Azure (Cost Management),
GCP (Cloud Billing and the BigQuery billing export).
**Kubernetes:** a list-price estimate from your kubeconfig, or OpenCost's
Allocation API when you run OpenCost.
**AI / LLM:** OpenAI, Anthropic, Amazon Bedrock, Vertex AI, OpenRouter, LiteLLM,
Langfuse.
**Data platforms:** Databricks, Snowflake, MongoDB Atlas.
**SaaS:** Datadog, New Relic, Cloudflare, Twilio, Vercel.

Also connected, without a spend figure: **Modal, Together and Replicate** (the
key is checked, but their public APIs return no per-period cost, so nable
reports the account as reachable and points you at invoice import),
**Cohere and Mistral** (`nable cohere` and `nable mistral` store the key;
nothing reads their usage yet), **Stripe** (MRR and paying customers for unit
economics, not a cost source) and **Helm** (release visibility).
**Infrastructure as code:** the cost of a Terraform plan or a Helm diff before
it is applied.

Connect AWS or GCP by SSO login or a CLI profile and nable stores no secret at
all, it just references your existing login. Only keys you paste directly are
encrypted in your OS keyring.

---

## CLI (59 commands)

As `nable --help` groups them:

```
get answers   scan  brief  why  ai-budget  ai-costs  budget
start here    welcome  connect  org  learn  setup  doctor  tools  serve
              upgrade
clouds        aws  aws-cur  azure  gcp
ai providers  openai  anthropic  openrouter  litellm  modal  together
              replicate  cohere  mistral
saas          datadog  newrelic  databricks  snowflake  mongodb  twilio
              cloudflare  vercel  langfuse
alerts        slack  teams  notion  n8n
editor+agents claude  guard  agents
account       login  logout  license  license-status  whoami  plan  credits
              uninstall
advanced      config  vault  profile  sso  iam-template  infra  pack
```

`nable connect` scans the machine for provider credentials and connects
everything in one keystroke. `nable serve` says the web dashboard is part of
hosted nable and exits.

---

## The agent team

Propose-only, never auto-execute. The code is open, and all three are free
today: the Budget Guard always, the other two while their pricing is decided
(`_UNGATED_AI_FEATURES` in `src/finops/license.py`).

- **Budget Guard** checks infra actions against your cost policy before they run.
- **Savings Analyst** proposes each fix as a pull request you approve.
- **The Ledger** tracks realized savings: measured from your bill when CUR line
  items cover the change and at least a week has settled, otherwise priced at
  your effective or list rate and labeled as an estimate.

Plus `finops guard` (a cost guardrail your AI agents check before spending) and a
`estimate_change_cost` preflight on Terraform and Helm diffs.

---

## Free vs Pro

Breadth is free on purpose: one tool across every cloud, SaaS, and AI provider.

**Free forever:** every cost query, anomaly detection, all waste and rightsizing
findings, `nable scan`, every connector, the agent guard, Slack and Teams alerts,
PR comments.

**Free today, while their pricing is decided:** forecasts, alert policies,
commitment recommendations, AI unit economics, remediation pull requests and
the Ledger.

**Pro ($25/mo):** ticket creation, email reports and digests sent on request,
the org-wide rollup. **Team ($1,000/mo flat):** the conversational Slack bot and
chat remediation, which run in hosted nable.

Running as an MCP server inside Claude or Cursor uses your existing membership as
the model. No Anthropic API key, no per-token cost. `nable scan` is deterministic
and uses no model at all.

---

## How nable compares

| | nable | AWS Cost Explorer / Cost Optimization Hub | Infracost | Vantage / CloudHealth |
|---|---|---|---|---|
| Open source | Yes (Apache-2.0) | No (AWS's Billing MCP server is) | The CLI (Apache-2.0); Infracost Cloud is SaaS | No |
| Where it runs | Your machine; nable sends your cost data nowhere | In AWS | CLI on your machine or in CI, pricing through Infracost's hosted pricing API | SaaS |
| Cross-cloud (AWS + Azure + GCP) | Yes | AWS only | IaC for any of them, before deploy | Yes |
| AI / LLM / GPU cost | Yes | AWS services only (Bedrock, SageMaker, GPU instances) | No | Vantage: yes; CloudHealth: an AI and token dashboard |
| Terminal + CI | Yes | Console, API and AWS CLI (API billed per request) | CLI and CI (PR comments) | Dashboard and API |
| Works inside Claude / Cursor | Yes, a local MCP server | AWS's Billing and Cost Management MCP server | Agent plugins for Claude Code, Cursor, Codex, Copilot and Gemini CLI | Vantage: hosted MCP server; CloudHealth: none found |
| Checks an agent's command before it runs | Yes, a hook (cost + policy) | No | Not as a hook on commands; its agent skills price IaC as it is written | No |
| Proposes fixes as PRs | Yes, on running waste | No | AutoFix PRs for policy issues in IaC code | Vantage: a FinOps agent that remediates with approval; no IaC PR found |
| Price | Free to start | Console free; API $0.01 per request | CLI free; Infracost Cloud paid | Vantage: free up to $2,500/mo of tracked spend, then paid; CloudHealth: paid |

What others offer was checked on 2026-10-13 against: AWS's [Billing and Cost
Management MCP server announcement](https://aws.amazon.com/about-aws/whats-new/2025/08/aws-billing-cost-management-mcp-server)
and [Cost Explorer pricing](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/);
Infracost's [docs](https://www.infracost.io/docs/) (agent plugins, IDE
extensions) and [AutoFix](https://www.infracost.io/docs/infracost_cloud/autofix/);
Vantage's [remote MCP server](https://vantage.sh/blog/remote-mcp),
[FinOps Agent](https://docs.vantage.sh/vantage_finops_agent) and
[pricing](https://vantage.sh/pricing); CloudHealth's
[June 2026 release roundup](https://community.broadcom.com/blogs/tj-march/2026/07/02/fresh-from-the-labs-june-2026)
(AI and Tokenomics dashboard). Products change; tell us when a row goes stale.

nable is the open-source, cross-cloud option that also covers AI provider spend
and runs on your own machine. Single-cloud waste (idle EBS, oversized RDS) is
available free from the cloud's own tools; nable's difference is unifying every
cloud, SaaS, and AI provider in one place, in the terminal, with fixes you approve.

## FAQ

**Is nable free?**
Yes. The terminal scan, every cost query, anomaly detection, all waste and
rightsizing findings, and every connector are free forever. Every install starts
with a 7-day trial of Pro features. Pro ($25/mo) adds ticket creation, email
reports and digests sent on request, and the org-wide rollup. Team ($1,000/mo
flat, unlimited seats) adds the conversational Slack bot and chat remediation.
Forecasts, commitment recommendations, remediation PRs, and the Ledger are free
today while their pricing is decided. Reports and alerts on a schedule are nable
Cloud; this install sends them when you ask.

**Does my billing data leave my machine?**
nable never uploads it. It reads your cost and usage data on your machine and
sends it to no nable service. Telemetry is off unless you set
`NABLE_TELEMETRY=1` or say yes when asked once at a terminal, and then carries
tool names and counts, never cost figures (`src/finops/telemetry.py`). In an editor, the
answers you ask for reach the model you chat with (Claude, Cursor's model), as
every MCP tool result does. `nable scan` and the rest of the CLI involve no
model. This is something you can read in the source, not take on faith.

**What clouds and providers does nable support?**
AWS, Azure and GCP; Kubernetes (a kubeconfig estimate, or OpenCost); AI and LLM
providers (OpenAI, Anthropic, Amazon Bedrock, Vertex AI, OpenRouter, LiteLLM,
Langfuse); data platforms (Databricks, Snowflake, MongoDB Atlas); and SaaS
(Datadog, New Relic, Cloudflare, Twilio, Vercel). Modal, Together, Replicate,
Cohere and Mistral connect but return no spend figure yet (see Connectors).

**How is nable different from AWS Cost Explorer?**
Cost Explorer is AWS-only, and its API bills per request. nable is cross-cloud,
runs in your terminal and in Claude/Cursor, covers AI provider spend (OpenAI,
Anthropic and others) that no cloud console shows, and proposes fixes as pull
requests. The `nable scan` default also makes
zero paid API calls, so it never charges your AWS bill to show you your own costs.

**Is there an open-source alternative to Vantage or CloudHealth?**
nable is an open-source (Apache-2.0), local-first alternative for cost queries,
waste detection, rightsizing, and AI/GPU cost. It runs on your machine instead of
a hosted SaaS, so your billing data stays local.

**How do I track LLM and GPU cost?**
Connect your AI providers and ask nable for spend by model, unit economics, token
forecasting, and AI billing blind spots, across OpenAI, Anthropic, Bedrock,
Vertex AI, OpenRouter, LiteLLM and Langfuse, in one view.

**Do I need an API key or a credit card?**
No. `uvx nable scan` runs with the AWS credentials already on your machine. As an
MCP server, it uses your existing Claude or Cursor membership as the model, with
no Anthropic API key and no per-token cost.

**What is nable's MCP server?**
An implementation of the Model Context Protocol with 203 cost tools for Claude,
Cursor, Windsurf, and VS Code (it lists the front-door ones and runs the rest
by name), so you can ask about your cloud and AI spend in chat and get answers
priced on your real rates.

---

## Trust

- **Local-first.** Everything runs on your machine, and nable sends your bill
  to no service of its own. In an editor, the answers you ask for reach the
  model you chat with; the CLI involves no model.
- **No-egress.** A claim you can read in the source, not take on faith.
- **Read-only toward your cloud.** nable never changes your cloud: cleanup and
  rightsizing come back as commands and pull requests for a person to run.
- **No credentials through the model.** Secrets stay in the OS keyring or a
  0600 file; no MCP tool takes a secret as an argument.
- **Apache-2.0.** Open and auditable. A hosted platform is available for teams.

See [getnable.com](https://getnable.com) for docs and the hosted platform.
