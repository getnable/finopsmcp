# nable in Slack

There are two Slack surfaces, and only one of them is in this package.

| | What it does | Where it lives |
|---|---|---|
| Alerts and reports | Posts cost digests, the weekly insight and anomaly alerts to a channel, when you ask | This package (free) |
| The `@nable` bot | Two-way: answers cost questions in channels and DMs, runs root cause investigations, drafts tickets and pull requests behind an approval gate | Hosted nable, on the Team plan ($1,000/mo flat, unlimited seats); not in this repository |

The bot, its scheduler and its own console command moved out of the
open-source package in 0.8.211 (see CHANGELOG.md): its 5-minute report
scheduler could reach a billed Cost Explorer call nobody asked for, and
nothing in this package runs on a timer. This package has no `[slack]`
extra and installs no command that starts the bot. Plans:
[getnable.com/#pricing](https://getnable.com/#pricing).

## Alerts and reports (this package)

```
nable slack          # or: finops setup slack
```

Pick one:

1. **Incoming webhook.** Posts to the one channel the webhook was created for.
2. **Bot token** (`xoxb-...`) and a channel. Posts Block Kit messages.

Setup offers one test message to the destination you entered, and stores the
webhook or token in nable's encrypted local vault (its key is a 0600 file in
nable's data directory, with a copy in your OS keyring). Then ask in your
editor; these are MCP tools:

- `send_digest_now`: a cost digest to Slack (and Teams, if configured), now
- `push_weekly_insight`: week-over-week change, top movers, open savings,
  active anomalies and budget alerts
- `send_report_now`: run one of your report subscriptions immediately

This install sends when you ask. Sending on a schedule, without anyone asking,
is hosted nable.

## The `@nable` bot (hosted nable)

`finops setup slack` offers a third choice, the conversational bot. It stores
the bot's tokens and prints the Slack app manifest below, for the hosted
package that runs the bot (nable-enterprise). It does not install or start a
bot: nothing in this repository answers an `@nable` mention.

The manifest (also at [`docs/slack-app-manifest.yaml`](slack-app-manifest.yaml),
and printed by setup):

```yaml
display_information:
  name: nable
  description: Cloud cost intelligence. Ask nable about your cloud and AI spend.
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

Create the app at https://api.slack.com/apps (**Create New App**, **From a
manifest**), then: Basic Information, App-Level Tokens, generate one with scope
`connections:write` (the `xapp-...` App Token); Install to Workspace; OAuth &
Permissions shows the `xoxb-...` Bot Token. The bot's own behavior, roles and
settings are documented with the hosted product.

## Microsoft Teams

Teams gets one-way Adaptive Card alerts and digests, sent when you ask
(`nable teams`, or `finops setup teams`). There is no two-way Teams bot.
