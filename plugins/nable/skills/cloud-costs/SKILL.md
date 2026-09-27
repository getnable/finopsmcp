---
name: cloud-costs
description: Use nable's cost tools when the user asks what something costs, why a cloud or AI bill went up, what their AI coding spend is, or before launching, resizing or destroying infrastructure. Also explains how to respond when the nable guard asks for confirmation before a command runs.
---

# Cloud and AI costs with nable

nable is this plugin's local MCP server. It reads cost data with the user's own credentials and changes nothing. Answer with the numbers its tools return; never estimate a figure a tool could give you, and never invent one.

## Which tool

- **What are we spending, and on what:** `get_cost_summary` (total, top services, trend), `get_top_cost_drivers`.
- **Why the bill went up:** `explain_recent_cost_drivers` (what moved in the last N days; `root_cause: true` for the deeper read), `get_anomalies` for spikes.
- **AI and LLM spend:** `get_llm_costs`, `get_llm_cost_by_model`, `get_ai_cost_attribution` (by team, project or model).
- **Your own coding-agent budget:** `get_ai_budget_status` (where this agent stands now), `check_ai_budget` (before a large task).
- **Before a change:** `estimate_terraform_cost` or `estimate_change_cost` for a Terraform plan or helm diff, and `check_action_policy` to see whether the user's policy lets it proceed, needs review, or blocks it.
- **Nothing connected:** `list_connected_providers` and `check_connector_health`, then point the user to `/nable:connect`.

Before you run anything that launches, resizes or deletes cloud resources, price it first and tell the user the monthly figure.

## When the guard asks

This plugin also installs the nable guard, a hook that checks your Bash and MCP tool calls before they run. It stays silent on ordinary commands. On a costly launch, a destroy, a commitment purchase, or a change over budget, it pauses the call and shows the user a reason with the price, for example "~$X/mo at list" or "It cannot be undone".

When that happens:

1. Tell the user plainly what the command does and what it costs, using the guard's reason. Do not argue with it or play it down.
2. Let the user decide. If they decline, propose a cheaper or reversible alternative (a smaller instance, a plan before an apply, a stop instead of a terminate).
3. If the guard denies a command, it says why. Report that, and do not retry the same command.

Never try to get around the guard: do not rephrase, split, encode or wrap the command in a script to avoid the check, do not run it through another tool, and do not turn the guard off, edit its settings, or uninstall it unless the user asks you to in so many words. Only the user turns it off, with `nable guard off` or `/nable:guard off`.
