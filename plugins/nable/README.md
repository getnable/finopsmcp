# nable for Claude Code

Prices what your coding agent is about to do before it runs. Installing this plugin turns on the nable guard: before Claude runs a costly launch, a destroy or a commitment purchase, you see the price and decide. The same plugin answers what is driving your AWS, Azure, GCP, Kubernetes and AI bill, right where you already work. Local and read-only.

## Install

These are Claude Code slash commands, so run them at the **terminal Claude Code CLI** prompt (`claude`), not a plain shell. Some managed or GUI Claude surfaces don't expose `/plugin`.

Run them **one at a time**, waiting for the first to finish before the second. Pasting both at once makes the first command swallow the second and fail.

1. Add the marketplace:

```
/plugin marketplace add getnable/finopsmcp
```

2. Once it says the marketplace was added, install the plugin:

```
/plugin install nable@nable
```

That registers the `nable` MCP server and the guard hook, both run through `uvx`, so [uv](https://docs.astral.sh/uv/) must be installed. Restart Claude Code if prompted. There is no second step: the guard is on.

## The guard

Every Bash command and MCP tool call Claude is about to make goes through the guard first. Ordinary commands pass silently. It steps in on:

- one-way doors: `terraform destroy`, `kubectl delete`, `aws ec2 terminate-instances`, a commitment purchase. Claude Code asks you to confirm, with the reason.
- priced launches: `aws ec2 run-instances --instance-type p4d.24xlarge --count 8` shows the monthly cost at list price, and asks when it is over your threshold or your budget.
- the same changes made through a Terraform, AWS or Kubernetes MCP server.

It never runs anything itself, and if it fails for any reason (no network for uvx, a bug) the command goes through as if the guard were not there.

Check on it from Claude Code:

```
/nable:guard
```

That shows whether the guard is on and what it did this week. In a terminal, `nable guard status` and `nable guard doctor` say the same in more detail, and `nable guard try` runs four sample commands through it (`uvx --from finops-mcp finops guard status` and so on if you do not have the `nable` command).

### Turning it off

Claude Code has no switch for one plugin's hooks, so nable has its own:

```
nable guard off     # the hook stays installed and lets everything through
nable guard on      # back on
```

`/nable:guard off` does the same from inside Claude Code. Setting `FINOPS_GUARD=off` in the environment Claude Code starts in also turns it off. To remove the guard completely, disable or uninstall the plugin: `/plugin disable nable@nable` or `/plugin uninstall nable@nable`.

If you already installed the guard with `nable guard install`, you keep one guard, not two: the plugin's hook stands aside while that settings hook is there, so each command is judged and recorded once. `nable guard install` itself skips writing a settings hook when the plugin is enabled.

### What it records, and where

Each verdict (the command as a redacted summary, the decision, the priced monthly cost, the agent session) is appended to a hash-chained ledger on your machine, `~/.finops/guard-ledger.jsonl` (or `$FINOPS_DATA_DIR`). Nothing is sent anywhere. Commands the guard has no opinion on are not recorded. `nable guard report` summarises what it asked, blocked and let through, and `nable guard verify-log` checks nothing was edited or removed. While the guard is off, nothing is recorded.

The guard is a seatbelt, not a security boundary: an agent can route around a hook. Give agents read-only cloud credentials and keep write access behind a human.

## Guided connect

After installing, run the bundled command to connect a cloud account and see your first cost number without leaving the editor:

```
/nable:connect
```

It checks what nable can already see (AWS often works with zero setup, it reuses your existing credential chain), shows your spend if you are connected, and gives you the exact terminal command if you are not.

## Connect your cloud

The plugin wires up the runtime. To link your accounts and see your first cost number, run the setup wizard once. Requires Python 3.10 or newer, check with `python3 --version`:

```
uvx --python 3.12 --from finops-mcp finops welcome
```

It writes your config, stores credentials in your OS keychain, and runs a read-only scan. Want to see it on sample data first:

```
uvx --from finops-mcp finops welcome --demo
```

## What you get

- The guard: the price of a change before your agent makes it, on by default
- 180+ tools for cost queries, anomaly detection, rightsizing, and PRs
- AI spend tracked by model, alongside cloud, Kubernetes, and SaaS
- A `cloud-costs` skill that tells Claude when to reach for these tools and how to answer the guard
- Read-only by default. It runs on your machine. No vendor holds your data.

Docs: https://getnable.com
