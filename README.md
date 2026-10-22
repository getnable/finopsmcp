# nable

**nable prices what your coding agents are about to do, before they do it. Free, local, one command.**

[![PyPI](https://img.shields.io/pypi/v/finops-mcp?label=pypi&color=4db8d4)](https://pypi.org/project/finops-mcp/)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/finops-mcp?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/finops-mcp)
[![Tests](https://github.com/getnable/finopsmcp/actions/workflows/test.yml/badge.svg)](https://github.com/getnable/finopsmcp/actions/workflows/test.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-4db8d4)](LICENSE)
[![MCP Toplist](https://mcptoplist.com/badge/io.github.getnable%2Ffinops-mcp.svg)](https://mcptoplist.com/server/io.github.getnable%2Ffinops-mcp)

nable puts a check in front of the commands a coding agent is about to run, in Claude Code, Cursor, Codex CLI, GitHub Copilot, Gemini CLI and Cline (and in front of MCP tool calls in the first three). A launch is priced at list price before it runs, a one-way door (destroy, terminate, a commitment purchase) stops for a human, and every verdict goes to a hash-chained ledger on your machine. The guard needs no cloud account, no API key and no license.

nable is also an open-source (Apache-2.0), local-first FinOps tool for cloud and AI cost: a read-only scan that finds waste in your AWS account using only free APIs, AI coding-agent spend priced per model with per-session caps, and AI cost split by project, team and user. It runs in your terminal or as an MCP server inside Claude, Cursor and VS Code. nable sends your billing data to no service of its own; in an editor, the answers you ask for reach the model you chat with, as every MCP tool result does.

## Try it

```bash
uvx nable guard install      # Claude Code, this project. --all: every agent found. --global: every project.
uvx nable guard try          # see it first: four sample commands through the gate, nothing executed
```

What the agent gets back when it tries a costly launch or a destroy (real output, no cloud account needed):

```text
$ uvx nable guard check --command "aws ec2 run-instances --instance-type p4d.24xlarge --count 8"
  ask  nable guard: 8x p4d.24xlarge at $21.9576/hr (on-demand us-east-1 list price) is ~$128,233/mo. The +$128,233/mo impact is over your $500 auto threshold; a human should review it. Budget not checked: there is no spend figure on this machine yet; `nable budget refresh` computes one.

$ uvx nable guard check --command "terraform destroy -auto-approve"
  ask  nable guard: This would destroy infrastructure (`terraform destroy -auto-approve`). It cannot be undone; confirm to proceed.
```

Using the Claude Code plugin? Installing it turns the guard on, no second step:

```text
/plugin marketplace add getnable/finopsmcp
/plugin install nable@nable
```

`nable guard off` (or `FINOPS_GUARD=off`) pauses every nable hook and `nable guard on` resumes it; an agent that tries `nable guard off` itself is asked about first.

In Claude Code, Cursor and Copilot CLI, `ask` stops the agent until you confirm; Codex CLI, Gemini CLI, Cline and Copilot's cloud agent refuse the command and show the reason (per-agent table under "Guard hook" below). A launch under the threshold stays silent: a `t3.micro` is ~$8/mo, under the default $500/mo (`FINOPS_POLICY_MAX_AUTO_USD`). Restart the agent after installing so it picks up the hook.

What needs a cloud account: pricing and asking do not. The budget stop, `nable guard reconcile` (CloudTrail) and the scan below read your own account, read-only.

## Scan what is already running

```bash
uvx nable scan
```

```text
nable scan · profile prod
account 3521… · this account only
scanning 17 regions …
  us-east-1 ......... 17 findings
  eu-west-1 ......... 1 finding
────────────────────────────────────────────
$659/mo recoverable
       $350/mo  1 idle RDS instance (db.r5.xlarge MySQL, no connections in 14 days), eu-west-1
       $210/mo  14 unattached EBS volumes (2.1 TB, gp2), us-east-1
     $98.55/mo  3 idle NAT gateways, us-east-1
run `nable scan --spend` for the spend breakdown (uses Cost Explorer, ~$0.02)
```

Uses the AWS credentials already on your machine and reads only free cloud APIs, so scanning never adds to your bill. `uvx nable scan --demo` runs on sample data with no account at all. Add `--json` for CI, or `--spend` for a deeper breakdown.

`uvx nable commitments status` lists the RDS, EC2, ElastiCache, Redshift, OpenSearch and MemoryDB reservations and the Savings Plans the account holds and when each ends, from free reads; `--fail-within 30` makes it a CI check (exit 1 when one ends within 30 days, 2 when one could not be read). [examples/github-actions/commitments-status.yml](examples/github-actions/commitments-status.yml) runs it every week through a read-only role assumed with GitHub OIDC and keeps the JSON.

From there nable:

- **Shows what you spend** across AWS, Azure, GCP, Kubernetes, and 15 AI, data and SaaS providers, in one place.
- **Finds what you are wasting** (idle servers, oversized databases, forgotten storage) and puts a dollar figure on each one.
- **Fixes it, with your approval,** by opening a pull request, then confirms the change happened and measures the saving: from your bill when CUR line items cover it, otherwise at your effective or list rate, labeled as an estimate.
- **Budgets your AI agents:** `nable ai-budget` prices Claude Code and Codex CLI usage per model from local logs, with a per-session cap; `nable ai-costs --by project|team|user` splits OpenAI, Anthropic, LiteLLM and Langfuse spend.

![nable demo: a sample bill in seconds](https://raw.githubusercontent.com/getnable/finopsmcp/main/docs/demo.gif)

## Use it in your editor

`uvx nable` runs as a local MCP server inside Claude, Cursor, and VS Code, on your existing Claude or Cursor membership, no API key and no per-token cost. Then ask:

- "Why did our AWS bill jump last month?"
- "How much are we spending on OpenAI and Anthropic?"
- "Which instances should we downsize?"
- "Open a Jira ticket for any waste over $200/mo"

## How nable compares

|  | nable | AWS Cost Explorer | Vantage / CloudHealth | Infracost |
|---|---|---|---|---|
| Open source | Yes (Apache-2.0) | No (AWS's Billing MCP server is) | No | The CLI (Apache-2.0); Infracost Cloud is SaaS |
| Where your data lives | Your machine | AWS | Vendor SaaS | Your machine or CI; resources are priced through Infracost's hosted pricing API |
| Clouds covered | AWS, Azure, GCP, Kubernetes | AWS only | Multi-cloud | IaC, any cloud |
| AI and GPU spend | Yes (OpenAI, Anthropic, Bedrock, Vertex AI, GPUs) | AWS services only (Bedrock, SageMaker, GPU instances) | Vantage: yes; CloudHealth: an AI and token dashboard | No |
| Runs in Claude / Cursor / VS Code | Yes (local MCP) | AWS's Billing and Cost Management MCP server | Vantage: hosted MCP server; CloudHealth: none found | Agent plugins for Claude Code, Cursor and others; IDE extensions |
| Prices an agent's command before it runs | Yes: a hook in Claude Code, Cursor, Codex CLI, Copilot, Gemini CLI and Cline prices launches, stops one-way doors, and logs every verdict | No | No | Not as a hook on commands; its agent skills price IaC as it is written, and CI prices the pull request |
| Fixes waste | Opens a pull request, you approve | No | Vantage: a FinOps agent that remediates with approval; no IaC pull request found | AutoFix PRs for policy issues in IaC code, not from what running resources use |
| Answers | What an agent is about to spend; what you spend and waste now | AWS spend | Multi-cloud spend | Cost of an IaC change before deploy |
| Price | Free (local); paid plans are flat, never a percentage of spend | Console free; API $0.01 per request | Vantage: free up to $2,500/mo of tracked spend, then paid; CloudHealth: paid | CLI free; Infracost Cloud paid |

Infracost prices an infrastructure change as it is written and in the pull request; nable's guard prices the command an agent is about to run, wherever it came from, and the scan finds waste in what is already running. Other products' rows were checked on 2026-10-13; the sources are listed in [CAPABILITIES.md](CAPABILITIES.md#how-nable-compares). Fuller breakdowns: [nable vs Vantage](https://getnable.com/nable-vs-vantage), [vs CloudHealth](https://getnable.com/nable-vs-cloudhealth), [vs Kubecost](https://getnable.com/nable-vs-kubecost).

## Agent guard

`nable guard install` adds a hook to Claude Code (and `--all` to every supported agent it finds: Cursor, Codex CLI, GitHub Copilot, Gemini CLI, Cline) that checks each infrastructure command, or MCP call in Claude Code, Cursor and Codex, before it runs: a one-way door (destroy, terminate, a commitment purchase) asks you first, and a launch is priced at list price so a ~$128k/mo `run-instances` (8x p4d.24xlarge) asks instead of passing. An EC2 Capacity Reservation is priced the same way, since it bills from creation whether or not anything runs in it; with a commitment duration it is a commitment purchase, and asks whatever the policy says. The purchases it covers, and the ones it does not, are listed in [CHANGELOG.md](CHANGELOG.md#0130-2026-10-18). It also watches the pattern across calls: a velocity cap on the monthly run-rate let through per hour (`FINOPS_POLICY_VELOCITY_CAP_USD`, default four times the $500/mo per-action threshold) and loop detection for the same creation repeated (three identical `create-stack` in ten minutes asks). Every verdict goes to a local hash-chained ledger.

**AI budgets that stay current.** When a Claude Code, Codex or Cursor spend cap is set, the guard weighs each call against it. Cursor usage comes from its Admin API (`CURSOR_ADMIN_API_KEY`): the hook never waits on the network, and when its copy is over an hour old it refreshes it in the background and says so (`FINOPS_GUARD_BACKGROUND_REFRESH=0` turns that off). `FINOPS_GUARD_AUTO_REFRESH_BUDGET=1` does the same for the cloud-budget figure, recomputing it from local cost history with no billed calls.

**Team and per-developer AI budgets, as plain files.** An `ai_budget` fact in your org model's `budgets.yaml` sets a monthly budget for a team (`monthly_usd`, and `per_person_usd` for each of its people), for one person (an email or `github:login`) or for the org, in list-price-equivalent US dollars: what the agents' tokens cost at the provider's published rate (`llm_prices`), usage and not an invoice. At the agent's next shell or MCP call, the guard compares this machine's month-to-date coding-agent spend with the person's budget, else their team's `per_person_usd` (a proposed personal budget only adds an ask, it never takes a confirmed per-person budget away), and with the team's and the org's `monthly_usd` (this machine alone, so a team's figure can only be under-counted, and the ask says so). Over one, it asks, naming the file, the fact key, the figure and its price source; `FINOPS_GUARD_STOP_ON_BUDGET=1` or `on_budget_breach: deny` makes it a deny. A proposed budget, or one from a repo you have not trusted, only asks. The stricter of this and `nable ai-budget`'s own caps answers. Whose budget it is: `FINOPS_GUARD_PERSON`, else git's `user.email` (read once and kept). The guard asks before an agent changes git's `user.email`, whether with `git config` (an `include.path` or `includeIf` that can set it included) or by writing a git config file directly: the repo's `.git/config`, `~/.gitconfig` and the XDG one are among the guard's own files. Should the email change some other way, the next call asks once, naming both, and the earlier one's budgets still apply for the rest of the month. Tokens of a model `llm_prices` has no cited rate for are counted and said, never priced at a guess. The month is priced once and kept (0600, beside `ai-budget.json`), so a call over unchanged transcripts prices nothing and a growing one prices only its new lines.

**A team's whole figure, from a paid workspace.** On a paid workspace, `nable cloud status --box https://HOST` also reads the box's month to date for you, your teams and the org, and keeps it in `cloud-standing.json` beside the ledger. The guard then compares, for each budget, the larger of this machine's figure and the box's, never their sum, and says both, the box's as-of time and its age. It reads the file only, never the network, and only for this UTC month, this person and the box this machine sends to. Your local `budgets.yaml` alone sets the thresholds. `nable guard doctor` says when the box's figure is not used, and why.

```bash
nable org set ai_budget --subject team:payments --monthly-usd 5000 --per-person-usd 400
nable org set ai_budget --subject person:alice@acme.com --monthly-usd 300
```

**Stops tied to the budget.** A priced change is also checked against the cloud budgets you set (the `set_budget` tool or a `budget.yml`): when month-to-date spend plus the change's cost for the rest of the month would take a budget over its limit, the guard asks, naming the budget, the spend so far, the change's monthly figure and the projected overage. `on_budget_breach: deny` in `nable.policy.yaml` (in nable's data directory, `~/.finops` by default, or at `FINOPS_POLICY_FILE`) makes that a hard stop; `FINOPS_GUARD_STOP_ON_BUDGET=1` or `=0` overrides it for one session or CI run. Total, provider and service budgets apply from the command itself; team and account budgets apply when `FINOPS_GUARD_TEAM` or `FINOPS_GUARD_ACCOUNT` names them where the agent runs. The hook reads a small spend summary rather than the database: every budget check writes it, `nable budget refresh` is the one to schedule, and a figure older than 48 hours (`FINOPS_GUARD_BUDGET_MAX_AGE_HOURS`) or from last month is not used, which the verdict on a priced change says. `nable guard doctor` lists the budgets the guard enforces, the ones it cannot place a change in, and the age of its figure.

```bash
nable guard report --session <id>     # what it asked, blocked and let through, in dollars
nable guard reconcile --hours 24      # CloudTrail's creates, destroys and commitments against the ledger
nable guard export --format cef       # the verified ledger, for a SIEM
nable budget refresh                  # recompute budgets and the guard's spend figure
nable budget ci-gate --fail-on-breach --json   # a pipeline step that fails on a breached budget
```

`ci-gate` reports and exits 0 unless `--fail-on-breach` is passed; with it, a breached budget (spend at or past its critical percentage) exits 1 and a check that cannot run exits 2. `--budget-file budget.yml` syncs the file first.

`reconcile` needs `cloudtrail:LookupEvents` (free, read-only) and matches by kind and time, since the ledger holds no resource ids. The guard is a seatbelt, not a security boundary: `nable guard doctor` lists what it does not see.

**It guards its own settings.** An agent that writes, moves or deletes the files that decide what the guard does (the org model, `nable.policy.yaml`, installed packs, the ledger, the off switch, the hook entries in each agent's settings) is asked about first, from the shell in every agent and through Claude Code's Write and Edit tools too. So are `nable guard off`, `nable org confirm` and `nable pack install` run by an agent, from any entry point (`nable`, `finops`, `finops-mcp`, `uvx`, `python -m`).

## Molded to your org

`nable org init` reads what your org already says about itself (CODEOWNERS, Terraform, AWS Organizations, tags, account and namespace names) and proposes who owns what, which environments are which, and which tag keys mean team or cost center. It then asks at most ten questions, highest spend first, most of them in bulk ("payments owns these 8 things, $18,500/mo: yes, no or edit"). Answers are stored as plain YAML in a `nable.org/` folder you own: in your repo if you want it reviewed by pull request (`nable org init --here`), otherwise in nable's data directory.

```bash
nable org init            # propose, then ask the week-one questions
nable org status          # what is confirmed, proposed, stale or in conflict, and spend coverage
nable org questions       # the open questions, with the command that answers each
nable org export --format json
```

What a person confirms changes what nable does: guard asks name the owner ("Owned by payments (#payments-oncall)"), the guard takes the team for team budgets and per-team thresholds from the repo you work in, tickets carry the owner's team and channel, findings carry an owner, and attribution and prod or non-prod detection use your confirmed facts before any guess. Agents, adapters and the MCP tools can only propose; confirming takes a person at the CLI or a merged pull request. A guess may make nable more careful, never less. A `nable.org/` that arrives inside a cloned repo is read on top of your own model, and until you `nable org trust` it, its owners are shown as likely and its thresholds may only lower yours.

## It stops asking the same thing twice

The guard records how each ask was answered (Claude Code, Cursor and Copilot report when an asked command ran). When people keep approving the same kind of change in the same scope, at least 5 times over a week with nothing declined or reverted, nable proposes a threshold for that scope and shows the evidence: "approved 6 times since Sep 19, max $1,121/mo, none declined or reverted". A person confirms it with `nable org confirm`, and from then on that change goes through without a prompt. Repeated declines propose asking sooner. One-way doors always ask.

```bash
nable learn infer --dry-run    # what nable would propose, and why
nable guard report             # asks approved, declined and unknown, per action and scope
nable org set freeze --scope environment:prod --start 2026-11-26T00:00-05:00 --end 2026-12-01T00:00-05:00 --reason "Black Friday"
nable guard approve <id>       # let one blocked command through, once, from your own terminal
```

Change freezes and approval chains are org facts too: during a freeze every priced change and one-way door in its scope asks (or is denied, if you confirmed the freeze in deny mode), and remediation pull requests and tickets go to the approvers you named. Agents that cannot pause to ask (Codex CLI, Gemini CLI, Cline, the Copilot cloud agent) get a deny with an approval id; a person runs `nable guard approve <id>` and the identical command runs once within 15 minutes.

## Packs

Packs extend nable with an org's own rules: policies, guard rules that can only tighten, remediation playbooks, price books, report templates, coding-agent skills, and (sandboxed) connectors, org adapters and ticket or Slack sinks. Each declares what it may read, which secrets it gets and which hosts it may reach in `nable-pack.toml`; you see that at install, and an update that asks for more waits for your approval again.

```bash
nable pack install ./my-pack          # a folder, a .tar.gz, git+https://...@<commit>, or ns/name from a registry
nable pack audit                      # what is installed, what each may do, and whether any file changed
nable pack validate ./my-pack         # for authors
```

An `ns/name` install reads a registry index named by `packs.registry` in your org policy, `--registry` or `NABLE_PACK_REGISTRY`; the default index (`getnable/registry` on GitHub) is not published yet and answers 404, so name one of your own. Your org policy file can limit where packs come from, cap what they may ask for, and require signatures (`packs:` in `nable.policy.yaml`). Details, the capability list and the honest limits of sandboxing on a laptop are in [docs/PACKS.md](docs/PACKS.md).

## Bring it into a paid workspace

Everything above runs on your machine and stays there. When you move to a paid workspace, `nable cloud connect` carries three things into it: your org model (the files in your org dir, `budgets.yaml` among them), the guard's decision ledger, each line exactly as stored, and this machine's coding-agent usage as one `agent_usage` record per UTC day, developer, agent, model and project for the closed days of the last 28, fewer when Claude Code keeps its transcripts for less (tokens by direction, and list-price-equivalent USD only for tokens priced at a cited `llm_prices` row; the project is the repo only when your org model names it, else `unmapped`; no prompt, file content, path or host name). The developer is whoever sends it: `--as WHO`, else at your terminal git's `user.email`, then `$USER`; the dry run shows it, and the workspace rolls the records up by developer, team and project. The ledger's records are your agents' commands and the paths the guard judged, with credentials redacted and nothing else changed, so they can name paths, hosts and user names; the dry run counts the records that do. It is a dry run first, and a dry run sends nothing: it prints where the bytes would go, which files and how many records, the size, the sha256 and a confirm code, and saves those exact bytes for you to read.

```bash
nable cloud connect --box https://your-workspace.example.com                        # the dry run
nable cloud connect --box https://your-workspace.example.com --send --confirm <code>
nable cloud status  --box https://your-workspace.example.com                        # what the workspace holds
nable cloud connect --box https://your-workspace.example.com --delete-org-file teams.yaml   # remove one file there
```

The workspace keeps each org model file you send until another upload carries that file: an upload with only `owners.yaml` leaves its `teams.yaml` as it was. A file equal to one it held and later replaced is not applied, and the send says so; `--delete-org-file NAME` removes a file there, and a copy of it sent after the delete applies, which is how a team goes back on purpose. `nable cloud status` also reads your month to date and your teams' from the box, for the guard (above), when the box offers it.

`--send` sends exactly the bytes the confirm code names, to the box it names (the code is the sha256 of the box, a newline and the bytes), and refuses if anything changed since you looked. Run it at your own terminal, or pass `--as WHO`; the guard asks before an agent runs it. HTTPS only, and never in air-gap mode. The workspace key is an analyst key made on the workspace's /workspace page, read from `NABLE_WORKSPACE_KEY`, a `--key-file` with mode 0600, or a hidden prompt; it is never taken from the command line and never stored. A ledger whose hash chain breaks, and a repo's `nable.org/` you have not trusted, are shown and not sent.

## Setup

Requires Python 3.11+. Need `uv`? `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`).

```bash
uvx nable
```

The setup wizard finds AWS or GCP credentials already on your machine (an SSO login, a CLI profile, or default credentials), connects the one you pick, and configures your editor. Usually you never type a key.

In CI, cron, scripts or an agent with no terminal: [docs/HEADLESS.md](docs/HEADLESS.md) has each command's behaviour with stdin closed, its `--json` output and exit codes, every environment variable, air-gap mode, and MCP over stdio.

**Cursor one-click:** [`Add nable to Cursor`](cursor://anysphere.cursor-deeplink/mcp/install?name=nable&config=eyJjb21tYW5kIjogInV2eCIsICJhcmdzIjogWyItLXB5dGhvbiIsICIzLjEyIiwgImZpbm9wcy1tY3AiXX0=)

Free forever for the local tool. A hosted version for teams (dashboards without a terminal, SSO, scheduled reports, always-on agents) is at [getnable.com/pricing](https://getnable.com/pricing).

<details>
<summary><b>Manual editor config</b> — only needed if setup didn't auto-configure</summary>

If `finops setup` doesn't auto-configure, run:

```bash
finops setup claude
```

Or add manually to `claude_desktop_config.json`:

**With uvx (recommended):**
```json
{
  "mcpServers": {
    "nable": { "command": "uvx", "args": ["--python", "3.12", "finops-mcp"] }
  }
}
```

**With absolute path:**
```json
{
  "mcpServers": {
    "nable": { "command": "/usr/local/bin/finops-mcp" }
  }
}
```
Use the path from `which finops-mcp`.

Config file locations:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

> **Why uvx?** Claude Desktop is a GUI app and doesn't inherit your shell's PATH. uvx runs finops-mcp in its own isolated environment. It's the most reliable option on corporate machines with managed Python installs.

</details>

<details>
<summary><b>Give your agent cost controls</b> — a pre-action budget gate for coding agents</summary>

nable is not just tools your agent reads from. It is a pre-action gate your agent
calls **before** it makes a cost-affecting change: it prices the change, checks it
against your budget, and offers a cheaper path. It never applies anything itself.
Propose-only, your agent proposes and a human approves.

Add one line to your agent's system prompt (Claude Code, Cursor, or any MCP client):

> Before you apply any infrastructure change (a terraform apply, a helm upgrade,
> creating or resizing a resource) or start an expensive job, first call
> `check_action_policy` with the action and the change (a terraform plan, a helm
> diff, or a `monthly_delta_usd`). Relay the verdict, the dollar impact, and the
> cheaper path when one is offered. Never apply a `block` or an `escalate` action;
> surface it to the human. nable is advisory and propose-only.

The gate returns `allow` / `warn` / `block` / `escalate` against your policy, the
monthly and annual dollar impact, and a spot alternative when the change is compute.
One-way doors (delete, terminate, buy a commitment) and over-budget changes always
escalate to a human, and over-budget changes are blocked outright when the policy
sets `on_budget_breach: deny`.

**And a budget for the agent itself.** Run `nable ai-budget` once, it asks whether
you are on a flat plan or a metered API and what you pay, then remembers. On a flat
plan it tracks how much subsidized compute you pull for your fixed fee and warns
before you run low; on metered it gates on a dollar spend cap. `check_ai_budget` does
the same for the agent mid-task. It reads your Claude Code usage locally, nothing
uploaded. Every response is priced at its own model's list rate, cache writes and
reads included, and the report splits cost by model and by session. A per-task cap
(`nable ai-budget --session-cap 40`, or "this task may spend at most $40" to the
agent) is measured against one Claude Code session, subagents included, and
`check_ai_budget` returns the headroom left under it. Add to your system prompt:

> Before starting a large task, call `check_ai_budget`. If it returns `warn` or
> `over`, tell me where I stand before continuing.

It reports your real usage and burn rate against your budget, not a fabricated
percentage of a plan's hidden rate limit.

</details>

<details>
<summary><b>Guard hook</b> for Claude Code, Cursor, Codex, Copilot, Gemini CLI, and Cline, and managed deployment</summary>

`nable guard install` puts the same check in front of the agent's shell commands
as a hook, so it runs whether or not the agent remembers to call the gate:

```bash
nable guard install --all --global      # every supported agent found on this machine
nable guard install --harness gemini    # one agent, this project only
nable guard doctor                      # what is covered here, and what is not
```

| Agent | Where the hook goes | What it sees | When the policy wants a human |
|---|---|---|---|
| Claude Code | `.claude/settings.json` | shell commands, MCP tools | asks |
| Cursor | `.cursor/hooks.json` | shell commands, MCP tools | asks |
| Codex CLI | `.codex/hooks.json` | shell commands, MCP tools | denies, with the reason |
| GitHub Copilot | `.github/hooks/nable-guard.json` | shell commands | asks in Copilot CLI; denies, with the reason, in the cloud agent |
| Gemini CLI | `.gemini/settings.json` | shell commands | denies, with the reason |
| Cline (macOS, Linux) | `.clinerules/hooks/PreToolUse` | shell commands | stops the task, with the reason |

`--global` writes the user-level file instead (`~/.claude/settings.json`,
`~/.cursor/hooks.json`, `$CODEX_HOME/hooks.json`, `~/.copilot/hooks/`,
`~/.gemini/settings.json`, `~/Documents/Cline/Hooks/`). A hook the user
installed is one the user can remove.

**Managed deployment.** To make the hook a policy a user cannot remove, deploy
it through the agent's admin settings. Replace `<version>` with the release you
tested (`nable --version`), and make sure `uvx` is on the users' PATH, or use
the absolute path of an installed `finops` binary instead of the `uvx` form.

Claude Code reads `managed-settings.json` from
`/Library/Application Support/ClaudeCode/` (macOS), `/etc/claude-code/`
(Linux and WSL) or `C:\Program Files\ClaudeCode\` (Windows), or a drop-in file
in `managed-settings.d/` next to it. User, project and local settings add their
hooks beside a managed one but cannot remove it, and a user's `disableAllHooks`
cannot turn it off:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "^(Bash|mcp__.*)$",
        "hooks": [
          {"type": "command", "command": "uvx --from finops-mcp==<version> finops guard hook", "timeout": 30}
        ]
      }
    ]
  }
}
```

Codex reads `/etc/codex/requirements.toml` (`%ProgramData%\OpenAI\Codex\requirements.toml`
on Windows). A hook there is managed: always on, trusted without the
"Hooks need review" step, and not something a user can disable:

```toml
[[hooks.PreToolUse]]
matcher = "^(Bash|mcp__.*)$"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "uvx --from finops-mcp==<version> finops guard hook"
timeout = 30
```

Both also have a switch that runs only the managed hooks and ignores every
user, project and plugin hook: `"allowManagedHooksOnly": true` in Claude Code's
managed settings, `allow_managed_hooks_only = true` at the top level of Codex's
`requirements.toml`. Set it only if no one's own hooks should run. Copilot CLI
has the same idea in policy hook files, `/etc/github-copilot/policy.d/*.json`
(root-owned, not group or world writable), in the format of
`.github/hooks/nable-guard.json`.

Sources: [Claude Code managed settings](https://code.claude.com/docs/en/managed-settings),
[`allowManagedHooksOnly`](https://code.claude.com/docs/en/settings-reference#allowmanagedhooksonly),
Codex `codex-rs/config/src/config_requirements.rs` and `codex-rs/hooks/src/engine/discovery.rs`
in [openai/codex](https://github.com/openai/codex),
[Copilot hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference#policy-hooks).

The guard is a seatbelt, not a security boundary: give agents read-only cloud
credentials and keep write access behind a human.

</details>

<details>
<summary><b>Connectors (20 sources of spend)</b>: every provider and what it pulls, plus Azure roles</summary>

| Provider | What it pulls |
|---|---|
| AWS | Cost Explorer (the API bills $0.01 per request; `nable scan` uses only free APIs unless you pass `--spend`) · CUR line items through Athena (billed per byte scanned) · Organizations |
| Azure | Cost Management API · Advisor cost recs · VM rightsizing (Azure Monitor) · native budgets · forecast |
| GCP | Cloud Billing API + BigQuery billing export · Recommender |
| Kubernetes | a list-price estimate from your kubeconfig |
| OpenCost | its Allocation API, when you run OpenCost in the cluster |
| OpenAI | API usage and token spend by model |
| Anthropic | Claude API usage and token spend |
| Amazon Bedrock | spend by model, through Cost Explorer |
| Vertex AI | spend from the BigQuery billing export |
| OpenRouter | gateway usage |
| LiteLLM | spend logged by a self-hosted proxy |
| Langfuse | Daily metrics API: model cost, token usage, trace volume |
| Datadog | Usage Metering API v2: real dollar amounts |
| Snowflake | ACCOUNT_USAGE.METERING_HISTORY |
| Databricks | DBU usage and SQL warehouse spend |
| MongoDB Atlas | Invoice API |
| Twilio | Usage Records API |
| Cloudflare | Billing API |
| Vercel | Invoice API (Enterprise) |
| New Relic | Data ingest + user counts |

Also connected, without a spend figure: Modal, Together and Replicate (nable checks the key, but their public APIs return no per-period cost, so it points you at invoice import), and Cohere and Mistral (`nable cohere` and `nable mistral` store the key; nothing reads their usage yet). Stripe, when `STRIPE_SECRET_KEY` is set, feeds MRR and paying customers into unit economics; it is not a cost source.

**Azure roles.** The Azure tools span three RBAC roles, granted to the service principal on each subscription (run `finops doctor` to check):

```bash
# repeat per subscription
az role assignment create --assignee <client-id> --role 'Cost Management Reader' --scope /subscriptions/<sub-id>
az role assignment create --assignee <client-id> --role Reader --scope /subscriptions/<sub-id>
az role assignment create --assignee <client-id> --role 'Monitoring Reader' --scope /subscriptions/<sub-id>
```

</details>

<details>
<summary><b>FAQ</b> — free vs paid, providers, how it compares to Cost Explorer / Vantage</summary>

**What is a FinOps MCP server?** An MCP (Model Context Protocol) server that answers cloud-cost questions from inside an AI editor. nable runs locally as one, so you can ask Claude, Cursor, or VS Code about your AWS, Azure, GCP, and AI spend and it reads your real cost data on your machine to answer.

**Is nable free?** Yes. The terminal scan, every cost query, anomaly detection, all waste and rightsizing findings, and every connector are free forever. Every install starts with a 7-day trial of Pro features. Pro ($25/mo) adds ticket creation (Jira, Linear, GitHub Issues), email reports and digests sent on request, and the org-wide rollup across accounts. Team ($1,000/mo flat, unlimited seats) adds the conversational Slack bot and chat remediation. Forecasts, commitment recommendations, remediation PRs, and the Ledger are free today while their pricing is decided. Reports and alerts on a schedule, sent without anyone asking, are nable Cloud; this install sends them when you ask.

**Does my billing data leave my machine?** nable never uploads it: it reads your cost data on your machine and sends it to no nable service, and telemetry is off unless you turn it on (and never carries cost figures). The one thing that sends your own files anywhere is `nable cloud connect --send`, which you run to carry your org model and guard ledger into a paid workspace, and only with the confirm code its dry run showed you for those bytes and that box. In an editor, the answers you ask for reach the model you chat with, as every MCP tool result does; `nable scan` and the rest of the CLI involve no model. You can confirm the no-egress behavior in the source.

**What clouds and providers does it support?** AWS, Azure and GCP; Kubernetes (a kubeconfig estimate, or OpenCost); AI and LLM providers (OpenAI, Anthropic, Bedrock, Vertex AI, OpenRouter, LiteLLM, Langfuse); data platforms (Databricks, Snowflake, MongoDB Atlas); and SaaS (Datadog, New Relic, Cloudflare, Twilio, Vercel). Modal, Together, Replicate, Cohere and Mistral connect but return no spend figure yet (see Connectors).

**How is it different from AWS Cost Explorer?** Cost Explorer is AWS-only, and its API bills per request. nable is cross-cloud, runs in your terminal and in Claude/Cursor, covers AI provider spend (OpenAI, Anthropic and others) no cloud console shows, and proposes fixes as pull requests. `nable scan` also makes zero paid API calls by default.

**Is there an open-source alternative to Vantage or CloudHealth?** nable is an open-source (Apache-2.0), local-first alternative for cost queries, waste detection, rightsizing, and AI/GPU cost, running on your machine instead of a hosted SaaS.

</details>

<details>
<summary><b>Troubleshooting</b> — install and setup fixes</summary>

```bash
finops-doctor          # checks credentials, DB, network, audit log
finops setup claude    # re-run editor configuration only
```

| Symptom | Fix |
|---|---|
| Tools don't appear in Claude | Switch to uvx config or use absolute path |
| `command not found: finops-mcp` | Re-install with `pip install finops-mcp` or use `uvx` |
| AWS returns no data | Run `finops setup aws` |
| `No matching distribution found for finops-mcp` | Your Python is older than 3.11. Install on 3.11+ (`uvx --python 3.12 nable`, or `python3.11 -m pip install finops-mcp`). |
| `cryptography` build error / `maturin failed` | uv tried to compile on Python 3.10. Use 3.11+: `uvx --python 3.12 nable`. |
| Corporate SSL errors | `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org finops-mcp` |
| Works at home, not at work | Use `uvx` (corporate IT often strips custom PATH entries) |

</details>

## License

Apache-2.0 in full. The hosted enterprise layer (web dashboard, scheduler, two-way Slack bot, SSO, control plane) lives in a separate private repo. What is in this one: [CAPABILITIES.md](CAPABILITIES.md); every MCP tool: [docs/tool-inventory.txt](docs/tool-inventory.txt).

[getnable.com](https://getnable.com) · [Docs](https://getnable.com/docs) · [Privacy](https://getnable.com/privacy) · [Security](https://scorecard.dev/viewer/?uri=github.com/getnable/finopsmcp)

<sub>mcp-name: io.github.getnable/finops-mcp</sub>
