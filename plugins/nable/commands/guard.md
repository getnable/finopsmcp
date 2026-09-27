---
description: Show whether the nable guard is checking your agent's commands, and turn it off or on
argument-hint: "[status|off|on]"
---

You are helping the user see and control the nable guard: the hook this plugin adds, which prices what Claude is about to run (terraform destroy, kubectl delete, aws ec2 run-instances, the same changes through a Terraform, AWS or Kubernetes MCP tool) and asks before a costly launch or a destroy. Be concise and concrete. No em dashes.

The request is: $ARGUMENTS (empty means status).

1. Find the CLI. Use `nable` if `command -v nable` finds it. Otherwise use `uvx --python 3.12 --from finops-mcp finops` in its place in every command below.

2. Status (no argument, or "status"): run `nable guard status`. Report in two or three lines:
   - Whether the guard is on. "plugin  on (via the Claude Code plugin)" means this plugin's hook is checking commands. "standing aside wherever the settings hook above judges" means a hook from `nable guard install` checks the calls it sees and the plugin's hook checks the rest, so each command is judged exactly once.
   - Whether it is off ("The guard is off"), and how to turn it back on.
   - Anything flagged in amber, with the fix the output gives.
   Then run `nable guard report --days 7` and give one line: how many commands it asked about or blocked this week, and the dollars in play. If the ledger is empty, say the guard has not had to step in yet.

3. Off ("off"): the user asked for this by running this command. Run `nable guard off`. If the guard asks them to confirm first, that is expected: turning the guard off is a change a human approves. Then tell them:
   - The hook stays installed and lets every command through, unchecked and unrecorded, until `nable guard on`.
   - Claude Code cannot switch off one plugin's hooks, which is why this is a nable setting. To remove the guard completely: `/plugin disable nable@nable` or `/plugin uninstall nable@nable`.

4. On ("on"): run `nable guard on` and confirm the guard checks commands again. If the output says FINOPS_GUARD=off is set, tell them to remove it from the environment Claude Code starts in.

What the guard records: every verdict it makes (the command in a redacted summary, the decision, the priced monthly cost) goes to a local, hash-chained ledger, `~/.finops/guard-ledger.jsonl`. Nothing leaves the machine. `nable guard report` summarises it and `nable guard verify-log` checks nothing was edited.

Never turn the guard off unless the user asked for it in this command.
