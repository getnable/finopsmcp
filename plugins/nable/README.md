# nable for Claude Code

Local-first FinOps, installed as a Claude Code plugin. Ask your AWS, Azure, GCP, Kubernetes, and SaaS bill in plain English, right where you already work.

## Install

```
/plugin marketplace add chaandannn/finopsmcp
/plugin install nable@nable
```

That registers the `nable` MCP server (`uvx finops-mcp`). Restart Claude Code if prompted, then ask a cost question.

## Connect your cloud

The plugin wires up the runtime. To link your accounts and see your first cost number, run the setup wizard once:

```
uvx --from finops-mcp finops welcome
```

It writes your config, stores credentials in your OS keychain, and runs a read-only scan. Want to see it on sample data first:

```
uvx --from finops-mcp finops welcome --demo
```

## What you get

- 160+ tools for cost queries, anomaly detection, rightsizing, and PRs
- AI spend tracked by model, alongside cloud, Kubernetes, and SaaS
- Read-only by default. It runs on your machine. No vendor holds your data.

Docs: https://getnable.com
