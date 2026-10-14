# Deploy nable for your team

What this package deploys is the CLI and the MCP server, on each engineer's
machine, optionally sharing one Postgres database. The always-on pieces that
let people without a terminal use nable (the web dashboard, digests and
anomaly alerts on a schedule, the two-way Slack bot, SSO) are hosted nable, not
this package:

- the web dashboard moved to the hosted product in 0.8.173, and `finops serve`
  in this package prints that and exits;
- the scheduler and the Slack bot moved in 0.8.211 (CHANGELOG.md), and nothing
  in this package runs on a timer.

So the `Dockerfile`, `docker-compose.yml` and `docker-compose.selfhost.yml` in
this repository, whose default command is `finops serve`, do not give you a
dashboard from this repository alone, and `docker-compose.yml`'s `tls` profile
mounts a `deploy/Caddyfile` that is not in it. For a hosted or self-hosted
dashboard, see [getnable.com](https://getnable.com).

## Per-engineer install (5 minutes each, or one MDM policy)

1. Pin the version. Install with `uvx --python 3.12 --from finops-mcp==X.Y.Z finops welcome`
   or ship it via your existing tooling (Homebrew/pip mirror, MDM script).
   finops-mcp needs Python 3.11 or newer: the `--python 3.12` flag fetches a
   matching interpreter, and a pip-mirror or MDM path on a system Python below
   3.11 fails with `No matching distribution found`. The setup wizard writes
   editor config automatically; `finops upgrade` moves a machine forward
   deliberately, never silently.
2. Issue scoped credentials, not personal keys. Your platform team creates
   one read-only role from the generated template:
   `finops setup aws --iam-template` (CloudFormation) or `--iam-terraform`.
   Verify any credential with `finops setup aws --check-scope`.
3. Credentials land in nable's encrypted local vault on each machine (its key
   is a 0600 file with a copy in the OS keyring). Nothing to distribute in
   plaintext, nothing to rotate centrally when an engineer leaves beyond the
   IAM role itself.

## Shared team mode: one Postgres

Set `DATABASE_URL` to the same `postgresql://` connection URL on every machine
(install `finops-mcp[postgres]` for the driver) and they share cost snapshots,
budgets and history instead of each keeping a local SQLite file. Add
`FINOPS_REQUIRE_AUTH=1` to enforce role-based access with API keys (viewer,
analyst, admin, optionally scoped to a team or provider): an admin creates
keys with the `create_api_key` tool, which writes each key to a 0600 file
rather than returning it through the conversation, and each machine sets its
key as `FINOPS_API_KEY`.

## Controls your security team will ask about

- Read-only by architecture. The IAM template grants no create, modify or
  delete permission (`finops setup aws --iam-template`), and nable never
  mutates your cloud: idle-resource cleanup returns the command for a person
  to run (`src/finops/cleanup/actions.py`), and rightsizing comes back as a
  pull request.
- Audit log of every tool call (duration, outcome, actor in team mode).
- `FINOPS_AIRGAP=1` forbids all non-provider traffic; `NABLE_NO_TELEMETRY=1`
  disables telemetry alone (it is off unless someone opts in);
  `NABLE_NO_UPDATE_CHECK=1` (or `FINOPS_NO_UPDATE_CHECK=1`) stops the check
  against PyPI for a newer version.
- Cost figures are composed by the AI editor each engineer already uses; for
  zero model exposure use the CLI. nable adds no new
  model endpoint.
- Full architecture writeup: https://getnable.com/security
