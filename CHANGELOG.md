# Changelog

All notable changes to finops-mcp (nable).

## Unreleased

### Changed
- **`finops welcome` never dead-ends, and shows value before asking for keys.**
  Onboarding analytics showed most installers skipped the credential step and
  saw nothing, then left. The first run now (1) detects an existing AWS
  credential chain and offers a one-keystroke read-only scan of your real bill,
  no setup menu; (2) if you skip or decline, still shows nable on a sample bill
  so the value lands before you leave the terminal; (3) labels demo output as
  sample data instead of claiming it scanned your account.

## 0.8.56

### Added
- **The Slack bot now sees the whole platform.** A new tool bridge exposes the
  MCP server's registry to the conversational loop: 57+ tools instead of 6.
  Ask about AI spend, Kubernetes waste, commitments, team attribution, audits,
  forecasts, anything the server can answer. One source of truth, no schema
  drift, role-gated (viewer/analyst/admin) both before the model sees a tool
  and again at call time. Destructive tools and credential management are
  never bridged.
- **Real conversations.** Threads now have memory: follow-ups like "what's
  driving that?" work. Rolling window per thread, 48h TTL, DMs keep a running
  context per channel.
- **Root cause analysis on tap.** The Investigate button and "why did costs
  spike" questions route to a deeper investigation pass built on
  explain_recent_cost_drivers, reporting dollar impact, likely cause with
  evidence, alternatives, and the next step.
- **Remediation with an approval gate.** From a Slack conversation, nable can
  draft a ticket (Jira/Linear/GitHub) or a Terraform rightsizing PR. Drafts
  post a preview card with dry-run details (files, estimated $/mo). Nothing is
  filed or opened until a human with the analyst role or above clicks Approve.
  Approvals expire after 24h.
- **Tiered model routing.** Quick lookups run on Haiku, free-text questions on
  Sonnet, RCA investigations on Opus. Override with FINOPS_SLACK_MODEL or
  per-tier FINOPS_SLACK_MODEL_{SIMPLE,CHAT,RCA}. A cost tool should not burn
  Opus tokens asking what yesterday cost.
- **finops setup slack option 3: conversational bot.** Prints a paste-ready
  Slack app manifest, validates the token against auth.test, stores
  credentials in the OS-keyring vault, and the bot loads them from the vault
  at startup. No .env file required.

### Security
- **Remediation drafting requires real authentication.** With
  FINOPS_REQUIRE_AUTH off, every Slack user resolves to admin, which would
  have let anyone draft and approve their own PR or ticket. Drafting is now
  disabled unless auth is on, or the operator opts in with
  FINOPS_SLACK_ALLOW_REMEDIATION=1. Enforced both when tool schemas are
  exposed to the model and again at execution time.
- **Self-approval blocked.** The requester of a pending action cannot click
  Approve on it. A different person must review. Solo operators can opt out
  with FINOPS_ALLOW_SELF_APPROVE=1.

### Fixed
- **Slack RBAC identity now reaches the tools.** Identity was stored in a
  ContextVar set in the handler thread, but tools ran in a worker thread where
  ContextVars do not propagate, so role enforcement silently saw no identity
  when FINOPS_REQUIRE_AUTH=1. Identity is now passed into the loop and set in
  the worker thread.

## 0.8.55

### Changed
- **`optimize_ai_spend` separates realizable savings from routing ceilings.** The
  headline `addressable_savings` counts only levers we can stand behind (prompt
  caching, measured error spend). Model-routing downgrades (Sonnet to Haiku and
  the like) are reported as `potential_upside`, clearly labeled as a ceiling that
  assumes eligible calls move, with a pointer to the per-function analyzer. No
  more inflated headline number.

### Added
- **Bedrock input/output/cache cost split from Cost Explorer.** `optimize_ai_spend`
  now spots the most common AI waste with no CloudWatch needed: an input-heavy
  Bedrock bill running with no prompt caching. It quantifies the caching
  opportunity and flips the spend-shape driver to caching when input dominates.
- **Bedrock SKU display names fire model-switch recommendations.** Cost Explorer
  reports Bedrock spend as names like "Claude Sonnet 4.5"; these now normalize to
  canonical model ids so Sonnet to Haiku recs work for Bedrock-only users.

## 0.8.54

### Added
- **`optimize_ai_spend`: a ranked, dollar-quantified plan to cut your AI bill.**
  Fuses cross-provider LLM costs (OpenAI, Anthropic, Bedrock, Azure OpenAI,
  Vertex) with the AI KPI report into one ranked set of levers: model routing,
  prompt caching, output reduction, error reduction, and model consolidation.
  Only levers with a grounded basis carry a dollar figure, governance levers are
  listed without inflating the headline, and output-trim savings are skipped for
  any model that already has a routing rec so nothing double-counts. It also
  decomposes spend into its real driver: model choice, token size, or request
  volume. Read-only intelligence, never a runtime proxy.
- **nable is now a Claude Code plugin.** Run `/plugin marketplace add chaandannn/finopsmcp`
  then `/plugin install nable@nable` to register the MCP server with no
  hand-edited config. Ships a `/nable:connect` command that connects a cloud
  account and shows your first cost number inside the editor.

## 0.8.53

### Changed
- **One-command onboarding: `uvx --from finops-mcp finops welcome`.** This fetches
  a matching Python, installs nable, and runs the setup wizard in a single step
  with no PATH setup. Replaces `uv tool install finops-mcp && finops welcome`,
  which silently failed on any machine where `~/.local/bin` was not on PATH (the
  `finops` command was "not found" right after a successful install). Site, docs,
  and install widgets updated.

### Added
- **The welcome wizard now pays off in the terminal.** After connecting a cloud
  account it scans and prints a real number right there: total spend, the top
  driver, your AI/ML share of the bill, and any idle waste, before you ever open
  your editor. Onboarding used to hand you a config wizard and send you off with
  nothing to show; now it finds money first. Fully guarded, so a slow or failing
  scan never blocks setup.
- **`finops welcome --demo`** runs the entire flow on realistic sample data with
  no account or credentials, so anyone evaluating nable sees the value moment first.
- The wizard now auto-configures Claude/Cursor (prefers `uvx finops-mcp`, no PATH
  dependency) instead of asking you to run a second command and press Enter.

## 0.8.52

### Fixed
- **Sync tools no longer error under MCP dispatch.** `whoami`, `create_api_key`,
  `list_api_keys`, and `revoke_api_key` are synchronous, but the instrumentation
  wrapper awaited every tool result unconditionally, so they failed with "object
  dict can't be used in 'await' expression" when called through the server. The
  wrapper now only awaits coroutines.
- **`explain_recent_cost_drivers` works again.** It unpacked the float grand
  total from `_gather_costs` and called `.keys()` on it ("'float' object has no
  attribute 'keys'"). It now diffs the per-service breakdown as intended.
- **`get_llm_costs` now sees Bedrock spend.** Cost Explorer labels model spend
  under SKU service names like "Claude Sonnet 4.5 (Amazon Bedrock Edition)", which
  the hardcoded `SERVICE == "Amazon Bedrock"` filter missed, reporting $0. nable
  now discovers the actual Bedrock service names via `GetCostAndUsage` (the same
  permission cost queries already use, no extra IAM grant) and attributes spend
  by model.
- Added dispatch-level regression tests so sync-tool breakage and these data
  bugs cannot ship silently again.

## 0.8.51

### Changed
- **Team pricing is now $100/seat/mo ($1,000/seat/yr).** The in-tool upgrade
  nudges, feature-gate messages, onboarding email, and checkout link all
  reflect the new price, so the product matches the site.

### Added
- **Contextual Team upsells.** Free users now get a one-time, topic-specific
  nudge keyed to what they just asked: anomalies surface auto-ticketing and
  Slack/Teams alerts, rightsizing surfaces the auto-PR, attribution surfaces
  scheduled digests, commitments surface coverage-gap modeling, and so on.
  Shown at most once per topic per session, helpful, not spammy, and never to
  paying users.
- **Board-ready export upgrades.** `export_board_summary` now includes AI spend
  as a share of MRR and AI cost per customer, pulling MRR and paying customers
  from Stripe when connected (the cost-per-customer bridge), with a precise
  "built on your machine, no nable backend holds it" note.

## 0.8.50

### Added
- **Unit economics populate themselves from Stripe.** Cost per customer and
  AI-as-percent-of-MRR used to require manually entering MRR and paying customers
  with `set_business_metrics`. If Stripe is connected (`STRIPE_SECRET_KEY`), nable
  now pulls MRR and active paying-customer count from your live subscriptions and
  fills those in automatically, so `get_llm_unit_economics_full`,
  `get_unit_economics`, and `get_business_metrics` answer on the first question
  with no data entry. Manual entry always wins; the Stripe pull only fills gaps,
  and the snapshot is persisted once a day so it trends over time. MRR normalizes
  every billing interval (month, year, week, day, and `interval_count`) to a
  monthly figure and skips metered/usage-based items, so it is a conservative
  floor, never an overstatement. Mixed-currency and truncated-line-item cases are
  surfaced as caveats rather than hidden.

## 0.8.49

Bug-fix pass from a multi-agent debug + security audit (adversarially verified).

### Fixed
- **Cost queries and waste scans no longer freeze the server.** The AWS cost
  connector and the rightsizing / deep-audit / idle scanners ran blocking SDK
  calls directly on the MCP event loop, so every query stalled the editor for
  seconds to minutes. They now run off the loop via `asyncio.to_thread`.
- **The Team tier works on Postgres.** Three queries used SQLite-only
  `date('now', ...)` / `datetime('now')` syntax that raises on Postgres, the
  shared-team mode the Team tier sells (waste-pattern scan, the Slack budget
  command, forecast-model persistence). Dates are now computed in Python and bound.
- **No more duplicate anomaly alerts or tickets.** Anomaly detection now dedups on
  (provider, service, account, date, direction), so a cron retry or the
  `run_anomaly_check_now` tool cannot re-alert or re-create a ticket for the same
  spend event. Also fixed the anomaly id returned wrong on Postgres.

### Security
- **CSV formula injection neutralized** (CWE-1236). Exported reports prefix a
  leading `=`, `+`, `-`, `@`, tab, or CR with an apostrophe, so a resource named
  `=HYPERLINK(...)` can't run as a formula when finance opens the file in Excel.
- **`start_dashboard_server` defaults to localhost** instead of all interfaces, and
  now surfaces the auto-generated password in the result so you can actually log in.
  Pass `expose=true` to bind the network, with a cleartext-HTTP warning.
- **Air-gap mode honors its promise.** Benchmarking egress (to bench.nable.dev) and
  external report delivery (Slack/Teams/email) are now suppressed when
  `FINOPS_AIRGAP` is set, instead of egressing anyway.
- The README data-handling claim is now precise (matches the rest of the docs).

## 0.8.48

Dogfooding fixes: precise errors, cleaner prompts, honest security wording.

### Fixed
- **Rightsizing now names the exact missing permission.** When the IAM identity
  lacks an action like `rds:DescribeDBInstances`, the tool used to return an empty
  result and the model would guess ("maybe CloudWatch, maybe a region"). It now
  reports the precise missing IAM action and how to add it.
- **The role prompt accepts the role name.** Typing `finops` at the persona prompt
  used to silently fall back to the default (Engineer). It now matches the role by
  number, name, or keyword, and warns on an unrecognized answer instead of guessing.
- **No more doubled yes/no markers.** Prompts read `Write config? [Y/n]:` instead
  of `Write config? [Y/n] [y]:`.

### Changed
- **Security wording is precise.** The old "your data never leaves your machine"
  overstated it. Credentials never leave your machine and nable has no backend that
  holds your data, but the figures you ask about go to your editor's own AI to
  answer the question, the same as any prompt. The docs, site, and emails now say
  this plainly, and note the local dashboard / CLI path for zero AI exposure.

## 0.8.47

Onboarding and activation pass. The goal: get a new user from install to a real,
dollar-quantified insight with the fewest possible steps.

### Changed
- **`finops setup aws` is now detect-then-confirm.** It probes the machine for
  credentials you already have (SSO login, AWS CLI profile, or default chain),
  shows the account it found, and connects it on one keystroke. If you use `aws`
  already, you type nothing. The old flow asked for an account name, a region,
  and an auth method up front. Manual entry is now the fallback, and it lists
  your actual profiles instead of asking you to type one blind.
- **No more region prompt.** Cost Explorer is global and the scanners already
  auto-discover regions, so the prompt was asking for something that changed
  nothing. Defaults silently to us-east-1.
- **The editor is wired in the same flow.** Connecting an account now registers
  the MCP server too, so there is no separate `finops setup claude` step.
- **The MCP server standardizes on the name `nable`.** A legacy `finops`
  registration is migrated in place so you are never double-registered.
- **Team price is $40/mo everywhere.** The product had been quoting $19.99 in
  upgrade nudges and trial emails while the site said $40.

### Added
- **First cost answer surfaces real waste.** Your first query now proactively
  reports the idle resources nable found, in plain dollars, instead of only
  answering the literal question.
- **Upgrade prompts cite your actual ROI.** When nable has already found savings
  that dwarf the $40 plan, the nudge says so ("found $X/mo, Nx the Team plan").

### Fixed
- Setup never persists a broken account: credentials are verified before saving.
- Credential detection cannot hang (bounded connect/read timeouts).
- Removed dead `finops-infra` and `finops-license` console aliases.

## 0.8.46

Bug-fix pass from a multi-agent troubleshooting run (13 confirmed defects,
adversarially verified). Several were in the 0.8.45 Azure code.

### Fixed
- **VM rightsizing was broken on every real Azure subscription.** The CPU metrics
  query used `isoformat()` ("+00:00"), and Azure Resource Manager decodes the `+`
  to a space, returning HTTP 400. So every VM read as "no CPU data", rightsizing
  found nothing, and it falsely blamed a missing Monitoring Reader role. Now uses
  a `Z` suffix. The same fix was applied to the doctor probe.
- **Azure forecast** no longer crashes on a short/empty response row, no longer
  sums the wrong column when a `Cost` column is absent (it skips the subscription
  instead of reporting nonsense dollars), and splits actual vs forecast by date
  when the `CostStatus` column is missing instead of labeling everything forecast.
- **The 5 Azure tools** now run their blocking REST calls off the event loop
  (`asyncio.to_thread`), so a large VM scan no longer freezes other tool calls,
  the Slack bot, and the scheduler.
- **Dashboard session store** is now lock-guarded. Under the threaded server, two
  concurrent logins could race the prune and 500 with "dictionary changed size
  during iteration".
- **Scheduled reports no longer blast on subscribe.** A new subscription was
  treated as "never sent" and sent a full report within ~5 minutes. It now waits
  for the first scheduled time after creation.
- **Scheduler shutdown** releases its cross-process single-owner lock, so another
  host can take over (Postgres mode) and the connection/file handle is not leaked.
- A malformed `FINOPS_SNAPSHOT_CRON` no longer aborts MCP server startup (removed
  a dead parse line).

## 0.8.45

Azure gets the same depth nable already had for AWS. Built clean-room from the
Azure REST APIs (no heavy SDKs), so teams on Azure can ask the same questions AWS
teams ask and get real answers.

### Added
- **`get_azure_vm_rightsizing`**: finds idle and oversized VMs from Azure Monitor
  CPU, with real per-VM cost joined from Cost Management. Idle VMs are
  deallocate/delete candidates, underutilized VMs are downsize candidates, and
  bursty VMs are left alone, the Azure parallel of nable's idle-EC2 and
  rightsizing engines. Just ask "vm rightsizing".
- **`get_azure_advisor_recommendations`**: Azure Advisor cost recommendations with
  Microsoft-computed annual savings (the Azure parallel of Compute Optimizer).
- **`get_azure_budgets`**: reads the budgets you already set in the Azure Portal
  (Consumption Budgets API) and reports consumption and warning/exceeded status.
- **`forecast_azure_costs`**: uses Azure Cost Management's own forecast model,
  blending actual billed days with Microsoft's forecast for the rest of the month.
- **`get_azure_cost_by_dimension`**: break spend down by service, resource group,
  location, or meter.

All five are free (matching the AWS optimization tools) and degrade to a clean
error when Azure is not configured.

### Ready-to-use hardening (from a pre-publish security + DX review)
- **RBAC is now surfaced, not silent.** The Azure tools span three roles per
  subscription (Cost Management Reader, Reader, Monitoring Reader). `finops doctor`
  now probes them and names any missing role with the exact `az role assignment`
  fix, `finops setup azure` prints the roles, and the README documents them. A
  missing role no longer shows up as a confusing empty result.
- **VM rightsizing scans the costliest VMs first and caps the scan**
  (`max_vms_scanned`, default 200), so a large estate cannot hang on hundreds of
  serial Azure Monitor calls.
- VM rightsizing and Advisor now return an actionable `permission_hint` when they
  list resources but get no data (the usual sign of a missing role).

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
