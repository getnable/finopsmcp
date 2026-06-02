# Changelog

All notable changes to finops-mcp (nable).

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
