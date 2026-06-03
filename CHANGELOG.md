# Changelog

All notable changes to finops-mcp (nable).

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
