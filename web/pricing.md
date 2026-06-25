# Pricing, nable

nable is a local-first FinOps tool. It runs on your own machine as an MCP server and answers cloud and AI cost questions inside Claude, Cursor and other MCP editors. Paid plans are flat-rate with unlimited seats, never per-seat and never a percentage of your cloud spend. Last updated: 2026-06-24.

## Dev
- Price: $0 per month
- Seats: 1, solo use
- Runs: local, on your own machine
- AI: bring your own LLM key
- Includes: cost queries across AWS, Azure, GCP and 10+ SaaS and AI providers; anomaly detection; rightsizing recommendations; LLM spend tracking by model; every connector
- Not included: Terraform remediation PRs, ticket creation, Slack and Teams alerts, digests, commitment recommendations
- Sign up: no credit card, no expiry

## Pro
- Price: $100 per month flat, unlimited seats, billed monthly
- Annual price: $1,000 per year, about $83 per month, two months free
- Trial: 7-day free trial
- Runs: local, on your own machine
- AI: bring your own LLM key
- Includes: everything in Dev, plus Terraform remediation (patches the .tf and opens the PR), ticket creation (Jira, Linear, GitHub), Slack and Teams alerts, weekly digests, budgets, commitment and savings-plan recommendations, the conversational Slack bot, root cause analysis with chat remediation, BI dashboards
- Buy monthly: https://buy.stripe.com/9B600igyt1oO1d69V02Nq06
- Buy yearly: https://buy.stripe.com/bJe5kCbe97Nc0924AG2Nq07

## Startups
- Price: $1,000 per month, unlimited seats, billed monthly
- Annual price: $10,000 per year, two months free
- Trial: 7-day free trial
- Runs: we host it for you, single-tenant. Dashboards anyone can use without a terminal. Your bill is never pooled with another customer's.
- AI: bring your own LLM key, or use the managed AI agent we run for you, with usage metered above an included monthly allowance.
- Includes: everything in Pro, plus single-tenant hosting and the managed AI agent
- Buy monthly: https://buy.stripe.com/3cI3cucid6J85tm3wC2Nq08
- Buy yearly: https://buy.stripe.com/14A6oG0zvgjI9JCffk2Nq09

## Enterprise
- Price: custom
- Adds: SSO (Okta, Entra, Google), audit logs, Slack support with an SLA
- Contact: hello@getnable.com or book a demo at https://calendar.app.google/2duYBqjLXaTmX5xC8

## Notes
- Flat fee, not a percentage of spend: nable never profits from your bill growing
- Local-first (Dev and Pro): credentials stay in your OS keyring, cost data caches in a local SQLite database on your machine, nable has no backend that holds your data
- Hosted (Startups and Enterprise): single-tenant, your data is never pooled with another customer's
- Install: `uvx nable` (uv fetches a matching Python and runs the setup wizard), or `pip install -U finops-mcp` on Python 3.10 or newer
- Works with: Claude Desktop, Cursor, Windsurf, Zed and any MCP client
- Site: https://getnable.com
