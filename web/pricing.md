# Pricing, nable

nable is a local-first FinOps tool. It runs on your own machine as an MCP server and answers cloud and AI cost questions in plain English inside Claude, Cursor and other MCP editors. Last updated: 2026-06-06.

## Free
- Price: $0 per month
- Seats: 1, solo use
- Includes: plain-English cost queries across AWS, Azure, GCP and 10+ SaaS and AI providers; anomaly detection; rightsizing recommendations; LLM spend tracking by model; PR comments; Slack and Teams alerts; every connector
- Not included: automated ticket creation, scheduled email digests, commitment recommendations
- Sign up: no credit card, no expiry

## Team
- Price: $100 per seat per month, billed monthly
- Annual price: $1,000 per seat per year, billed annually, about $83 per seat per month, two months free
- Trial: 7-day free trial on the monthly plan
- Includes: everything in Free, plus automated ticket creation (Jira, Linear, GitHub), scheduled email digests, commitment and savings-plan recommendations, and org reports
- Buy monthly: https://buy.stripe.com/9B600igyt1oO1d69V02Nq06
- Buy yearly: https://buy.stripe.com/bJe5kCbe97Nc0924AG2Nq07

## Notes
- Local-first: credentials stay in your OS keyring, cost data caches in a local SQLite database on your machine, nable has no backend that holds your data
- Install: `uv tool install finops-mcp` (uv fetches a matching Python), or `pip install -U finops-mcp` on Python 3.10 or newer
- Works with: Claude Desktop, Cursor, Windsurf, Zed and any MCP client
- Site: https://getnable.com
