# Pricing, nable

nable is a local-first FinOps tool. It runs on your own machine as an MCP server and answers cloud and AI cost questions inside Claude, Cursor and other MCP editors. Paid plans are flat-rate with unlimited seats, never per-seat and never a percentage of your cloud spend. Managed single-tenant hosting is available as an add-on; contact us for a demo. Last updated: 2026-07-04.

## Dev
- Price: $0 per month
- Seats: 1, solo use
- Runs: local, on your own machine
- AI: bring your own LLM key
- Includes: cost queries across AWS, Azure, GCP and 15 more SaaS and AI providers; anomaly detection; rightsizing recommendations; LLM spend tracking by model; every connector
- Not included: Terraform remediation PRs, ticket creation, Slack and Teams alerts, digests, commitment recommendations
- Sign up: no credit card, no expiry

## Pro
- Price: $25 per month flat, unlimited seats, billed monthly
- Annual price: $250 per year, about $21 per month, two months free
- Trial: 7-day free trial
- Runs: local, on your own machine
- AI: bring your own LLM key
- Includes: everything in Dev, plus Terraform remediation (patches the .tf and opens the PR), ticket creation (Jira, Linear, GitHub), Slack and Teams alerts, weekly digests, budgets, commitment and savings-plan recommendations, the conversational Slack bot, root cause analysis with chat remediation, BI dashboards
- Buy monthly: https://buy.stripe.com/5kQeVc4PL9Vk4piaZ42Nq0a
- Buy yearly: https://buy.stripe.com/eVqaEW961aZocVO8QW2Nq0b

## Startups
- Price: $1,000 per month, unlimited seats, billed monthly
- Annual price: $10,000 per year, two months free
- Trial: 7-day free trial
- Runs: local, on your own machine. Managed single-tenant hosting is available (see Hosting below).
- AI: bring your own LLM key. A managed AI agent comes with hosting.
- Includes: everything in Pro, plus org scale (your whole org, more accounts and connectors) and priority support
- Buy monthly: https://buy.stripe.com/3cI3cucid6J85tm3wC2Nq08
- Buy yearly: https://buy.stripe.com/14A6oG0zvgjI9JCffk2Nq09

## Hosting (optional add-on)
- What: we run nable single-tenant for you, plus a managed AI agent and dashboards anyone on your team can use without a terminal. Your bill and credentials are never pooled with another customer's.
- Pricing: contact us for a demo, hello@getnable.com or https://calendar.app.google/2duYBqjLXaTmX5xC8
- Note: hosting is billed on top of your flat plan, and nable never charges a percentage of your cloud spend

## Enterprise
- Price: custom
- Adds: SSO (Okta, Entra, Google), audit logs, Slack support with an SLA
- Contact: hello@getnable.com or book a demo at https://calendar.app.google/2duYBqjLXaTmX5xC8

## Notes
- Flat fee, not a percentage of spend: nable never profits from your bill growing
- Local-first (Dev, Pro, Startups): credentials stay in your OS keyring, cost data caches in a local SQLite database on your machine, nable has no backend that holds your data
- Hosting (optional add-on on any paid plan): managed single-tenant, your data is never pooled with another customer's. Contact us for a demo.
- Install: `uvx nable` (uv fetches a matching Python and runs the setup wizard), or `pip install -U finops-mcp` on Python 3.11 or newer
- Works with: Claude Desktop, Cursor, Windsurf, Zed and any MCP client
- Site: https://getnable.com
