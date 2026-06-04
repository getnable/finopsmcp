# Changelog

All notable changes to finops-mcp (nable).

## 0.8.44

Fix the usage signal. The telemetry was wired but three bugs were corrupting or
silently dropping it, so active-install counts were unreliable.

### Fixed
- The first-run `install_completed` event was sent with a constant
  `distinct_id="install"`, so every install collapsed into one analytics person
  and installs could not be counted. It now uses the per-install anonymous ID.
- Telemetry was delivered over `urllib`, which fails certificate verification on
  python.org macOS builds (empty trust store until the user runs Install
  Certificates), silently dropping every event for that segment. It now sends via
  httpx (bundled CA store), with a urllib fallback.
- `finops doctor` reported "No usage telemetry active" when telemetry is on by
  default. It now reports the real posture and how to opt out.

### Changed
- First-run output now discloses the anonymous usage ping next to the
  credentials-stay-local line, with the `NABLE_NO_TELEMETRY=1` opt-out. Telemetry
  still never includes cost data, account IDs, or credentials.

## 0.8.43

A multi-agent code review and debugging pass (10 reviewers, every finding
adversarially verified) plus a token-cost reduction for users. 18 confirmed
findings fixed, including three cost-correctness regressions from the 0.8.40
pass and two pre-auth holes in the team-host deploy.

### Fixed
- **S3 Intelligent-Tiering savings were silently dropped** from the three
  consolidated reports (full audit, CSV export, Notion publish). They filtered on
  a recommendation string the engine stopped emitting in 0.8.40, so S3 IT waste
  never reached the consolidated totals. Now matched correctly, with a contract
  test pinning the string.
- **Idle-EC2 network guard read ~12x too low.** It averaged the NetworkOut metric
  instead of summing it, so a busy instance still looked idle. Uses Sum now.
- **Idle-EC2 metal sizing** assumed 96 vCPU for every `*.metal`, over-estimating
  savings on smaller metal types. Falls back to a conservative default.
- **Textract non-prod detection** missed hyphenated `non-prod` names. Restored
  without re-introducing substring false positives.
- **Read-only dashboard share links could escalate to full access.** Read-only and
  full sessions shared one token store, so a share token replayed as a login
  cookie passed the full-access check. They now use separate stores.
- **Pre-auth path traversal** in the dashboard font route let an unauthenticated
  client on the network read arbitrary `.css`/`.woff` files. Paths are now
  confined to the fonts directory.
- **Slack scheduled reports never sent**: the job imported a function that did not
  exist, failing silently every five minutes. Implemented it (`_is_report_due`).
- **Double digests/alerts**: the MCP server and `finops serve` could both run the
  scheduler against one database. Added a single-owner lock (Postgres advisory
  lock, file lock locally).
- Database Savings Plan recommendations now report `data_incomplete` on a Cost
  Explorer failure instead of a confident $0, and size the commitment at the
  discounted rate rather than on-demand.
- Smaller hardening: `Secure` cookies behind TLS, a plaintext-bind warning,
  clean scheduler shutdown on Ctrl+C, HTML-escaped report titles, a guarded
  Slack request verifier, and a date-parse guard on `export_cost_report`.

### Changed
- **Dashboard serves concurrently.** Switched to a threaded HTTP server so one
  slow cost fetch no longer stalls every other finance user.
- **Lower token cost on large results.** `list_idle_resources` and
  `get_rightsizing_recommendations` capped their detail lists to a token budget
  (costliest first), with the omitted count surfaced. On an 800-resource account
  the idle response drops from ~44k to ~6k tokens, and the totals stay exact.

## 0.8.42

DX polish on the team-host deploy, found in review.

### Changed
- `finops serve --help` now discloses that the command also hosts the scheduler
  and the Slack bot, when each turns on, and how dashboard auth works. The help
  text used to describe only the dashboard, so an operator did not know `serve`
  was the finance host.

### Fixed
- The startup banner no longer reports `Slack bot: ON` when `slack_bolt` is not
  installed. `slack_bolt` imports lazily inside the bot thread, so a missing
  dependency failed in-thread after the banner already claimed ON. The banner
  now prechecks the dependency and reports `OFF` with the reason.

## 0.8.41

Give non-engineers access without an install. The engineer sets nable up; finance
consumes it through Slack, email, and the dashboard, and credentials stay inside
your own infrastructure.

### Added
- `finops serve` is now an always-on team host. It starts the scheduler (pushed
  snapshots, anomaly alerts, daily and weekly digests) when
  `FINOPS_ENABLE_SCHEDULER=1`, and the Slack bot (two-way cost Q&A) when
  `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are set. A startup banner shows which
  finance interfaces are live. A failed service degrades to OFF; the dashboard
  still serves.
- `DEPLOY.md`: a one-command team deploy guide (`docker compose up -d`).

### Changed
- `docker-compose.yml` and `.env.example` now pass the Slack, scheduler, and SMTP
  settings, so the existing compose file is the finance deploy.

## 0.8.40

False-positive correctness pass: nine optimization engines were flagging healthy
resources as waste. Each fix shipped with a regression test, and the full set was
adversarially re-verified (FP removed, no new bug, no over-suppression of real waste).

### Fixed
- **S3 Intelligent-Tiering**: read bucket size across the IntelligentTiering* storage
  classes, not StandardStorage alone. On a real IT bucket the bytes live under
  `IntelligentTieringFAStorage`, so the old query read ~0 and flagged every IT bucket
  as tiny-object waste.
- **S3 bucket keys**: stop fabricating a 100,000 KMS-calls fallback. When bucket
  metrics are absent, the bucket is still surfaced but savings are reported as
  unknown (0.0) instead of an invented number.
- **Textract environment**: match non-prod name signals (`qa`, `dev`, `test`, `uat`,
  `staging`) on token boundaries, not raw substrings, so `latest-invoice-handler` and
  `developer-portal` are no longer flagged. Acronym runs (`QA-doc-processor`,
  `UATPipeline`) are preserved so real non-prod callers still match.
- **Database Savings Plans**: size commitments off instance-hour usage only, excluding
  storage, IOPS, and backup line items that a Database SP does not discount.
- **NLB cross-zone**: single-AZ load balancers have no cross-AZ charge to remove;
  multi-AZ recommendations now carry an availability caveat instead of a blind
  "disable" action.
- **Spot diversification**: attribute-based instance selection and the
  `price-capacity-optimized` allocation strategy are no longer flagged as
  under-diversified.
- **Idle EC2**: skip instances with sustained network egress, and size vCPU from a
  real instance-type map instead of a broken string parse.
- **Rightsizing dedup**: collapse a resource flagged by both the heuristic and Compute
  Optimizer into one recommendation so savings are not double-counted.
- **EBS snapshot replication**: mark cross-region snapshot costs as an upper bound
  (snapshots are incremental, so real storage is a fraction of the provisioned size).

### Changed
- **S3 Intelligent-Tiering** is now framed as an ROI decision: the recommendation
  reports the monitoring fee as a percentage of the storage savings it unlocks. Under
  8% it is worth keeping, 8-100% is marginal, at or above 100% it is waste, with the
  math surfaced in `roi_summary`.

## 0.8.38

Faster, clearer terminal experience and proactive trust checks.

### Added
- `finops doctor` now verifies the extended Cost Explorer permissions nable needs
  (`ce:GetSavingsPlansCoverage`, `ce:GetReservationUtilization`), not just
  `ce:GetCostAndUsage`. A credential that passes the core check but misses these
  used to fail only mid-query ("no identity-based policy allows the
  ce:GetSavingsPlansCoverage action"). Doctor now catches it up front and prints
  the exact fix.
- `finops doctor` ends a clean run with a "what to ask next" nudge.
- `finops tools`: a grouped cheat-sheet of example questions to ask nable in
  Claude (costs, waste/savings, network/traffic, Kubernetes, AI/LLM, anomalies).

### Changed
- `finops --help` leads with a quick-start (`finops welcome` / `setup` / `doctor`
  / `serve` / `tools`) instead of dumping every subcommand.

## 0.8.37

Trust and correctness pass, plus a cross-cloud traffic tool.

### Fixed
- Corrected wrong pricing constants used in savings estimates (all independently
  re-verified against AWS pricing):
  - CloudWatch Logs Infrequent Access ingestion ($0.50/$0.25 per GB, was 6.6x low)
  - Lambda provisioned-concurrency keep-warm rate ($0.0000041667/GB-s, was 2.3x high)
  - CloudWatch composite alarms ($0.50/alarm/mo)
  - Public IPv4, NAT Gateway, and EFS cross-AZ rates
- Two wiring bugs that could abort the full cost audit, export, and scorecard
  tools (commitment and database-savings-plans analysis were called incorrectly).
- `create_rightsizing_tickets` no longer errors on a missing import.

### Added
- `get_traffic_cost_breakdown`: cross-cloud network spend, split internal
  (cross-AZ, cross-region, NAT) vs external (internet egress, CDN), with a
  per-scope solve playbook (VPC endpoints, topology-aware routing, CDN).
- Business-context layer (Pro): cost-per-customer, runway (infra and company),
  and a board-ready markdown cost summary. New `set_business_metrics` inputs for
  cash, last raise, and monthly opex.
- Local dashboard headline showing cost-per-customer and runway.
