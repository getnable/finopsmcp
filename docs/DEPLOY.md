# Deploy nable for your whole team

nable runs locally in Claude Desktop or Cursor for the engineer who sets it up.
But finance, your manager, and anyone else who wants the numbers should not have
to install a terminal tool or hold cloud credentials. This guide stands up one
always-on instance that everyone consumes, while the credentials stay inside your
own infrastructure. They never go to a nable-hosted service.

One engineer runs this once. After that, non-engineers use nable through three
interfaces, none of which require an install:

- **Slack** — ask "what did we spend on Snowflake last month" and get an answer.
- **Email** — daily and weekly cost digests land in their inbox.
- **A web dashboard** — a browser view at a shared URL.

## What you need

- Docker and Docker Compose.
- Cloud read credentials (AWS Cost Explorer at minimum). The same ones you would
  give the local install.
- Optional but recommended for finance: a Slack app and an SMTP account.

## 1. Configure

```bash
cp .env.example .env
```

Fill in `.env`:

- **Cloud credentials**: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `AWS_DEFAULT_REGION` (and Azure/GCP if you use them).
- **Dashboard**: set `FINOPS_DASHBOARD_PASSWORD` to a password. On a trusted
  internal network you can set it to `off`.
- **Scheduler**: `FINOPS_ENABLE_SCHEDULER=1` is already the default in the
  compose file. This is what pushes the digests and anomaly alerts.
- **Slack** (the finance Q&A interface): create a Slack app with Socket Mode
  enabled, then set `SLACK_BOT_TOKEN` (`xoxb-...`) and `SLACK_APP_TOKEN`
  (`xapp-...`). Optionally set `SLACK_ALERT_CHANNEL` and `SLACK_REPORT_CHANNEL`.
- **Email** (the digest interface): set the `FINOPS_SMTP_*` values for your mail
  provider.

The Slack bot starts only when both Slack tokens are present. The scheduler runs
only when `FINOPS_ENABLE_SCHEDULER=1`. Anything you leave blank is simply skipped.

## 2. Run

```bash
docker compose up -d
```

That single command starts the dashboard, the scheduler, and the Slack bot in one
container. The compose file also includes an optional Postgres for shared team
mode; remove that service if you prefer SQLite.

Check the logs to confirm the finance interfaces came up:

```bash
docker compose logs -f nable
```

You should see a startup banner like:

```
Finance interfaces (non-engineers consume nable here):
  Scheduler:  ON  (snapshots, anomaly alerts, daily + weekly digests)
  Slack bot:  ON  (finance asks in Slack, no install needed)
```

## 3. Onboard the non-engineers

- **Slack**: invite the nable bot to a channel, or have people DM it. They ask.

- **Email digests**: subscribe recipients from the dashboard, or with the
  `subscribe_to_report` tool in Claude.
- **Dashboard**: share the URL and the password.

## Why this keeps nable local-first

The credentials live in your `.env` on your own host. nable does not phone home,
and there is no nable-operated server holding your cloud keys. "Local-first" here
means the trust boundary is your infrastructure, not ours. A finance person asking
a question in Slack is talking to your instance, not to a multi-tenant SaaS.

## Keeping it running

`restart: unless-stopped` is already set, so the container comes back after a
reboot. To update:

```bash
git pull
docker compose build
docker compose up -d
```

## Enterprise rollout: nable for a whole engineering org

The model: every engineer runs nable locally against a scoped read-only
credential; the Slack bot and Postgres run as one small shared service. No
nable servers are involved at any point.

### Per-engineer install (5 minutes each, or one MDM policy)

1. Pin the version. Install with `uvx --from finops-mcp==X.Y.Z finops welcome`
   or ship it via your existing tooling (Homebrew/pip mirror, MDM script).
   The setup wizard writes editor config automatically; `finops upgrade`
   moves a machine forward deliberately, never silently.
2. Issue scoped credentials, not personal keys. Your platform team creates
   one read-only role from the generated template:
   `finops setup aws --iam-template` (CloudFormation) or `--iam-terraform`.
   Verify any credential with `finops setup aws --check-scope`.
3. Credentials land in the OS keyring per machine. Nothing to distribute in
   plaintext, nothing to rotate centrally when an engineer leaves beyond the
   IAM role itself.

### The shared piece: team mode

Run Postgres (your infra) and the Slack bot as a service (this document,
above). That adds: shared cost snapshots, RBAC (viewer/analyst/admin with
team scoping), the conversational Slack bot, and approval-gated remediation.
SSO via OIDC: set the issuer/client env vars and roles map from your IdP.

### Controls your security team will ask about

- Read-only by architecture; one optional write permission
  (`logs:PutRetentionPolicy`), and destructive cleanup is off unless
  `FINOPS_CLEANUP_ENABLED=true`.
- Audit log of every tool call (duration, outcome, actor in team mode).
- `FINOPS_AIRGAP=1` forbids all non-provider traffic; `NABLE_NO_TELEMETRY=1`
  disables telemetry alone.
- Cost figures are composed by the AI editor each engineer already uses; for
  zero model exposure use the local dashboard or CLI. nable adds no new
  model endpoint.
- Full architecture writeup: https://getnable.com/security
