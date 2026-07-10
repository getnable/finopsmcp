# Pricing, nable

nable is a local-first FinOps tool. It runs on your own machine as an MCP server and answers cloud and AI cost questions inside Claude, Cursor and other MCP editors. The full local product is free. Enterprise is a managed, always-on deployment with custom pricing. nable never charges a percentage of your cloud spend. Last updated: 2026-07-10.

## Community
- Price: $0, no credit card, no expiry
- Runs: local, on your own machine, on your existing Claude or Cursor membership (no separate LLM key required)
- Includes: cost queries across AWS, Azure, GCP and 15 more SaaS and AI providers; anomaly detection; rightsizing recommendations; the agent team (Budget Guard, remediation PRs you approve, verified savings); AI/LLM spend tracking by model; forecasts and commitment recommendations; every connector; a self-hostable dashboard (Docker)
- Propose-only: nable never changes your cloud on its own
- Install: `uvx nable`

## Enterprise
- Price: custom, tailored to your team
- Everything in Community, plus:
- Managed single-tenant hosting: always-on monitoring and push alerts (Slack, email), running 24/7 instead of only when your laptop is open
- Dashboards and Slack for the whole team, no terminals
- SSO (Okta, Entra, Google), RBAC and audit logs
- Your bill and credentials are never pooled with another customer's
- Priority support and a custom SLA
- Contact: hello@getnable.com or book a demo at https://calendar.app.google/2duYBqjLXaTmX5xC8

## Notes
- Team pricing is being finalized; early users get the best terms we will ever offer
- Never a percentage of your cloud spend: nable does not profit from your bill growing
- Local-first: credentials stay on your machine (SSO and CLI profiles are referenced, never stored; pasted keys are encrypted in your OS keyring), cost data caches in a local SQLite database, nable has no backend that holds your data
- Install: `uvx nable` (uv fetches a matching Python and runs the setup wizard), or `pip install -U finops-mcp` on Python 3.11 or newer
- Works with: Claude Desktop, Cursor, Windsurf, Zed and any MCP client
- Site: https://getnable.com
