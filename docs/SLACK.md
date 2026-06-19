# nable in Slack

A conversational FinOps teammate in your workspace. Once an engineer sets it up,
anyone on the team can ask cost questions, run root cause analysis, and kick off
fixes without leaving Slack.

> **Plan:** the conversational `@nable` bot is part of nable Team ($1,000/mo
> flat, unlimited seats), with a 7-day free trial. The free tier already covers
> cost queries, anomaly detection, rightsizing, and one-way Slack alerts; the
> two-way bot needs Team or an active trial. Start one with `finops setup
> license`. Plans: [getnable.com/#pricing](https://getnable.com/#pricing).

## What it does

**Ask anything.** Mention `@nable` in a channel or DM it. The bot answers with
real data from your connected providers through 57+ tools: cloud spend, AI/LLM
costs, Kubernetes, SaaS, anomalies, forecasts, commitments, waste audits, team
attribution.

```
@nable what did we spend last week?
@nable which team owns the RDS cost increase?
@nable how much are we wasting on idle resources?
@nable what's our Bedrock bill and is caching on?
```

**Follow-ups work.** Threads have memory. Ask "what's driving that?" after an
answer and the bot knows what "that" is. Context lasts 48 hours per thread.

**Root cause analysis.** Anomaly alerts come with an Investigate button. Free
text works too: questions like "why did our AWS bill spike?" route to a deeper
investigation pass that compares periods, finds the top drivers, and reports
the dollar impact, the likely cause with evidence, and the next step.

**Remediation, gated.** The bot can draft a ticket (Jira, Linear, or GitHub
Issues) or a Terraform rightsizing PR straight from a conversation. Drafts are
previews: a card shows what would change (files, estimated savings) and a human
with the analyst role or above must click Approve before anything is filed or
opened. Approval cards expire after 24 hours.

Two safety defaults to know:

- Drafting is off until you enable `FINOPS_REQUIRE_AUTH=1`. Without real
  authentication every Slack user is an admin, which would let anyone draft
  and approve their own action. Solo operators can opt in instead with
  `FINOPS_SLACK_ALLOW_REMEDIATION=1`.
- The person who requested an action cannot approve it. A teammate has to
  click Approve. Solo operators can allow self-approval with
  `FINOPS_ALLOW_SELF_APPROVE=1`.

```
@nable draft a ticket for the top rightsizing recommendation
@nable open a PR to right-size the staging RDS instances
```

**Alerts and digests.** Anomaly cards with Acknowledge / Create Ticket /
Investigate buttons, hourly budget checks, scheduled reports. These ride on the
same bot process.

## Add nable to your Slack (about 2 minutes)

**1. Create the app from a manifest.** Open https://api.slack.com/apps → **Create
New App** → **From a manifest** → pick your workspace, paste the manifest below
(also at [`docs/slack-app-manifest.yaml`](slack-app-manifest.yaml)), Create.

```yaml
display_information:
  name: nable
  description: Cloud cost intelligence. Ask your bill anything.
  background_color: "#0d0f10"
features:
  bot_user:
    display_name: nable
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - im:history
      - im:read
      - im:write
      - reactions:read
      - reactions:write
      - users:read
      - users:read.email
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.im
  interactivity:
    is_enabled: true
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

**2. Get the two tokens.** Basic Information → App-Level Tokens → Generate one
with scope `connections:write` (that is your `xapp-...` App Token). Then Install
to Workspace; OAuth & Permissions shows your `xoxb-...` Bot Token.

**3. Run it locally** (nothing is hosted; the bot runs on your machine, your
tokens stay in your OS keyring). Requires Python 3.10 or newer, check with
`python3 --version`. On older Python, pip reports `No matching distribution
found for finops-mcp`:

```
pip install "finops-mcp[slack]"
finops setup slack          # paste the two tokens + your Anthropic key when prompted
finops-slack                # start the bot
```

`finops setup slack` also prints this exact manifest, so you can run it first and
copy from the terminal instead. Connect a cloud account first if you have not:
`finops setup aws` (or azure/gcp).

> Why this and not the Slack Marketplace: nable's bot runs in Socket Mode on your
> own machine, so your tokens and bill never leave it. Marketplace apps must be
> publicly hosted OAuth apps that hold every workspace's tokens, which is the
> opposite of local-first. The manifest install is the same two minutes without
> handing your workspace to a third party.

## Access control

By default every Slack user gets full access. For teams, set
`FINOPS_REQUIRE_AUTH=1` and create API keys with emails matching your Slack
users. Roles:

| Role    | In Slack                                                        |
|---------|-----------------------------------------------------------------|
| viewer  | All read-only questions: costs, anomalies, audits, forecasts    |
| analyst | Plus: acknowledge anomalies, set budgets, draft and approve tickets and PRs |
| admin   | Plus: trigger digests                                           |

Role checks run twice: tools a user cannot call are never shown to the model,
and every call is re-checked at execution time. Destructive tools (resource
cleanup) and credential management are never reachable from Slack.

## Model usage and cost

The bot runs on your own Anthropic API key with tiered routing so casual
questions stay cheap:

| Tier   | Used for                  | Default model   |
|--------|---------------------------|-----------------|
| simple | Button follow-ups         | claude-haiku-4-5  |
| chat   | Free-text questions       | claude-sonnet-4-5 |
| rca    | Root cause investigations | claude-opus-4-5   |

Override with `FINOPS_SLACK_MODEL` (all tiers) or
`FINOPS_SLACK_MODEL_SIMPLE` / `_CHAT` / `_RCA`. Tool schemas and the system
prompt are prompt-cached, so repeat questions cost a fraction of the first.

## Configuration reference

| Variable | Purpose |
|----------|---------|
| `SLACK_BOT_TOKEN` | Bot OAuth token (xoxb-...), stored by the wizard |
| `SLACK_APP_TOKEN` | Socket Mode token (xapp-...), stored by the wizard |
| `ANTHROPIC_API_KEY` | Powers the answers |
| `SLACK_ALERT_CHANNEL` | Where anomaly and budget alerts post |
| `FINOPS_TF_DIR` | Terraform directory for rightsizing PRs |
| `GITHUB_FINOPS_TF_REPO` | org/repo the rightsizing PR targets |
| `FINOPS_REQUIRE_AUTH` | Set to 1 to enforce roles by Slack email. Also unlocks remediation drafting |
| `FINOPS_SLACK_ALLOW_REMEDIATION` | Set to 1 to allow drafting without auth (solo use) |
| `FINOPS_ALLOW_SELF_APPROVE` | Set to 1 to let the requester approve their own action (solo use) |
| `FINOPS_QUERY_TIMEOUT` | Seconds before a question is cut off (default 60) |
| `FINOPS_RCA_TIMEOUT` | Seconds for investigations (default 150) |
| `FINOPS_MAX_TOOL_CALLS` | Tool call budget per question (default 12) |

## Microsoft Teams

Teams gets one-way Adaptive Card alerts and digests today (`finops setup teams`).
Two-way conversational Teams is planned; it needs an Azure Bot registration and
a public endpoint, a different transport from Slack Socket Mode.
