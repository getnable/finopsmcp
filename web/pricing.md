# Pricing, nable

nable is a local-first FinOps tool. It runs on your own machine as an MCP server and answers cloud and AI cost questions inside Claude, Cursor and other MCP editors. All paid plans are flat-rate with unlimited seats, not per-seat and not a percentage of your cloud spend. Last updated: 2026-06-12.

## Free
- Price: $0 per month
- Seats: 1, solo use
- Includes: cost queries across AWS, Azure, GCP and 10+ SaaS and AI providers; anomaly detection; rightsizing recommendations; LLM spend tracking by model; every connector
- Not included: Terraform remediation PRs, ticket creation, Slack and Teams alerts, digests, commitment recommendations
- Sign up: no credit card, no expiry

## Pro
- Price: $100 per month flat, unlimited seats, billed monthly
- Annual price: $1,000 per year, about $83 per month, two months free
- Trial: 7-day free trial
- Includes: everything in Free, plus Terraform remediation (patches the .tf and opens the PR), ticket creation (Jira, Linear, GitHub), Slack and Teams alerts, weekly digests, budgets, commitment and savings-plan recommendations, BI dashboards
- Buy monthly: https://buy.stripe.com/9B600igyt1oO1d69V02Nq06
- Buy yearly: https://buy.stripe.com/bJe5kCbe97Nc0924AG2Nq07

## Team
- Price: $1,000 per month flat, unlimited seats, billed monthly
- Annual price: $10,000 per year, two months free
- Trial: 7-day free trial
- Includes: everything in Pro, plus the conversational Slack bot (ask the bill anything in Slack), root cause analysis on cost spikes, chat remediation (drafts the PR or ticket, a human approves), and managed AI included or bring your own key
- Buy monthly: https://buy.stripe.com/3cI3cucid6J85tm3wC2Nq08
- Buy yearly: https://buy.stripe.com/14A6oG0zvgjI9JCffk2Nq09

## Enterprise
- Price: custom
- Adds: SSO, audit logs, Slack support with an SLA
- Contact: hello@getnable.com or book a demo at https://calendar.app.google/2duYBqjLXaTmX5xC8

## Notes
- Flat fee, not a percentage of spend: nable never profits from your bill growing
- Local-first: credentials stay in your OS keyring, cost data caches in a local SQLite database on your machine, nable has no backend that holds your data
- Install: `uvx nable` (uv fetches a matching Python and runs the setup wizard), or `pip install -U finops-mcp` on Python 3.10 or newer
- Works with: Claude Desktop, Cursor, Windsurf, Zed and any MCP client
- Site: https://getnable.com
