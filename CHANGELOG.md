# Changelog

All notable changes to finops-mcp (nable).

## 0.8.120

Faster to value, lighter in your context window. Anomaly detection now works on
day one: nable seeds its baselines from your Cost Explorer history instead of
waiting a week for snapshots. After your first bill renders, nable suggests
three questions tailored to your account's actual shape. A new
nable_setup_status tool lets your AI agent finish your setup for you, without a
secret ever passing through chat. And 27 rarely-used tools no longer load by
default, cutting roughly 25k tokens of tool definitions from every session
(their capabilities remain in the full audit; FINOPS_ALL_TOOLS=1 restores
them).

## 0.8.119

Hosting credits are wired end to end on the software side. A Stripe credit
purchase now records a grant on your customer record, and the new `finops
credits` command applies it to the instance: grants stack within the month,
expire at the period roll, and arm the managed-AI meter on their own. The
dashboard warns in the chat itself once 80% of the month's allowance is used,
so the stop at zero is never a surprise. Upgrade links from in-product nudges
now carry attribution tags so we can see which moments actually convert.

## 0.8.118

Numbers you can defend. The Textract non-prod finding now headlines the
conservative floor of its savings range instead of the best case, with the full
range stated alongside, and that same floor flows through the dashboard, the
savings ledger, and upgrade math. Slack/Teams alerts now page on cost spikes;
routine cost drops are recorded and queryable but no longer page (a
high-severity drop still does, since something may have stopped running;
FINOPS_ALERT_DROPS tunes this). Anomaly summaries separate spikes from drops.
And scan_waste_patterns, benchmark_costs, and forecast_costs no longer require
an account id; nable resolves the connected account automatically.

## 0.8.117

Every tool is now fully documented. All 183 MCP tools describe their parameters
and include example queries, which helps your AI client pick the right tool and
fill its arguments correctly, and completes the description-quality pass started
in 0.8.116. A CI check now blocks any new tool from shipping with a bare
description. The web tool reference is regenerated from the improved docs.

## 0.8.116

Audits got twice as cheap to read. Waste-audit findings carried repeated
boilerplate (the same why/remediation on every resource, empty fields, a
pricing note per finding); the guidance now lives once per category in a
playbooks map and empty fields are dropped, halving tokens per finding, so the
same response budget returns roughly twice the findings. Tool descriptions for
several housekeeping tools were rewritten, with a test that stops new tools
shipping bare ones. And upgrade nudges now cite the deduplicated savings
ledger, so the ROI number in the pitch matches the one in the report.

## 0.8.115

Multi-provider cost answers stay lean as you scale. Cross-provider queries used to
inline every service of every provider, so at ~18 connected providers a single
answer ballooned toward 5k tokens. Now each provider shows its top services and
rolls the rest into a total, cutting the payload ~40% at 18 providers with totals
unchanged. Latency was already flat (the provider fan-out runs concurrently);
this fixes the token cost. Single-provider queries keep full detail.

## 0.8.114

Removed GitHub and PagerDuty as cost connectors. They report usage, not dollars,
so they always showed \$0 and added a line that looked broken rather than any real
cost signal. GitHub ticketing and PR comments are unaffected (separate feature),
and you can still capture GitHub/PagerDuty spend from their invoice emails via
fetch_invoice_emails.

## 0.8.113

Cleaner cost categories and clearer connect-time expectations. Observability
tools (Datadog, New Relic, PagerDuty) and developer tools (GitHub) now get their
own FOCUS service categories instead of landing in a catch-all "Other". And the
providers that report usage rather than dollars now say so when you connect them:
PagerDuty and GitHub explain where the dollar figure comes from, and New Relic
prompts for your contract rates so it can show real dollars instead of $0.

## 0.8.112

Cross-cloud commitment reporting now catches GCP. The FOCUS normalizer detected
AWS Savings Plans and Azure reservations but missed GCP committed-use discounts
whenever the billing export omitted the coded credit type. It now reads the
credit name too, so a GCP CUD is labeled as reliably as the other two and shows
up when you group spend by commitment. The saving already reached effective cost;
this fixes the label.

## 0.8.111

Faster on big orgs. Tag attribution compiles its rules once instead of re-sorting
them for every cost line, and the AWS Organizations reporting path (org rollup,
top-spending accounts, account anomalies, weekly digest) is now read-through
cached like the rest of the cost data. Previously those reporting calls re-hit
Cost Explorer every time, so one session could pay for the same org query several
times over. No behavior change, just fewer API calls and less repeated work.

## 0.8.110

GCP goes deeper. New `get_gcp_recommendations` pulls Google's native Recommender
API: machine-type rightsizing, committed-use-discount buys, Cloud SQL
idle/overprovisioned, Cloud Run cost tuning, and idle VMs/disks/IPs/images,
priced against your real SKU rates instead of list-price estimates. It
complements the resource scanner (`audit_gcp_waste`): the scanner covers you on
day one with read scope; the Recommender API adds depth once Google has ~8 days
of usage. Findings sort by monthly savings and follow the same propose-only
trust model. Needs the Recommender API enabled and `roles/recommender.viewer`.

## 0.8.109

Connect everything in minutes. New `finops connect` scans this machine for
provider credentials (environment, gcloud, gh, ~/.modal.toml,
~/.databrickscfg) and connects them all in one keystroke. GCP setup is now
detect-and-confirm like AWS: it finds gcloud/ADC credentials, discovers your
billing accounts and BigQuery export automatically, no service-account JSON
scavenger hunt. Every paste-a-key wizard now links the exact page where the
key lives, and the welcome flow offers whatever else is on your machine
right after your first bill renders.

## 0.8.108

Accuracy fixes from a platform audit: the savings ledger no longer
double-counts recommendations (the "potential savings" total was inflated
by legacy duplicate rows), cost summaries no longer mislabel an active
account as zero-spend, forecast_costs works without an explicit account id,
and upgrade copy says "$25/mo flat" not "per seat".

## 0.8.107

Pro is now $25/mo ($250/yr, two months free), down from $100. New checkout
links and every price surface updated; Startups is unchanged.

## 0.8.106

The guardrail installs itself, and the docs finally show all 182 tools.

- `finops guard install`: a Claude Code hook that auto-checks every
  infra-mutating command (terraform destroy, kubectl delete,
  terminate-instances, commitment purchases) against your policy before the
  agent runs it. Ask on one-way doors, deny out-of-allowlist actions, silent
  on everything else. Advisory only; nable never executes.
- Tool reference: getnable.com/tools.html lists all 182 MCP tools, searchable,
  generated from source on every release so it cannot drift.
- Terminal URLs (one-click AWS key, docs) are now real clickable hyperlinks
  in iTerm2, VS Code, kitty, and WezTerm.

## 0.8.105

Real data or nothing: the first run never invents numbers.

### Changed
- **The guided onboarding no longer falls back to a sample bill.** Before, a user
  who skipped the credential step, or whose scan came up empty, was shown nable on
  made-up example numbers. Now that path shows an honest empty state ("No numbers
  yet, on purpose") and the fastest way to a real one: the one-click read-only AWS
  key when it is published, plus `finops setup aws` / `finops setup openai`. No toy
  figures ever land in front of someone evaluating the product. `finops welcome
  --demo` still exists as an explicit, clearly-labeled sample walkthrough for
  anyone who wants it.

## 0.8.104

Smarter first run: setup now ends by offering a budget your agents respect.

### Added
- **Post-scan budget step in onboarding.** After the first-run value moment shows
  a real spend number, nable offers to set a monthly budget seeded from that
  number (about 15% headroom, rounded to a clean figure). It heads off the
  find-out-the-hard-way bill and, more to the point, sets the number every agent
  checks against before it acts. The closing line points at `check_action_policy`
  and `check_budget_status`, so an agent builder sees the spend-control story on
  day one instead of in a docs page. Interactive only, skippable with `n`, and it
  never prompts a returning user who already has a total budget.

## 0.8.103

First-run diagnostic fixes: the value moment no longer hangs, and the package metadata is honest again.

### Fixed
- **The first-run value moment no longer hangs.** The headline spend number was
  computed in under a second but printed only after three optional scans (idle
  resources, AI-spend plan, LLM bill) that ran serially with wall-clock caps
  summing to ~45s. A real new user could stare at "Scanning..." for up to ~40s.
  The headline now prints immediately, and the optional scans run concurrently
  (added wait is the slowest single scan, ~10s, not their sum). Demo mode hid
  this, which is why it kept slipping past.
- **`finops doctor` no longer reads the OS keychain on every run.** It now
  resolves the vault master key in the vault's own order (env, then the 0600
  `vault.key` file, then the keyring), so on a file-first install it reports the
  key correctly without touching (or, on macOS, prompting for) the keychain.
- **Friendlier Snowflake missing-dependency error.** A slim install now gets
  "Run: pip install 'finops-mcp[snowflake]'" instead of a raw ModuleNotFoundError,
  and the setup wizard folds the `snowflake` extra into the pinned launch command
  when Snowflake is connected.
- **`__version__` was stale (0.8.101) while the package was 0.8.102.** Now in sync.
- Retired "plain English" from the first-run wizard screen, the finance persona,
  and the dashboard sandbox (the earlier scrub was web-only).
- Corrected tool count (165/160+ to 180+) and plugin version (0.8.77) in the
  Claude plugin and marketplace manifests, fixed a description typo, and replaced
  the inaccurate "credentials stay in your OS keychain" with "on your machine."

## 0.8.102

The keychain-prompt fix and the FOCUS long tail: one normalized cost dataset across clouds, SaaS and AI.

### Fixed
- **No more recurring macOS credential prompts.** Every license check used to
  read the trial date from the OS keychain and rewrite it, which recreated the
  item, reset its ACL, and made macOS re-prompt on every session ("python wants
  to access system.cache.prefs"). The signed trial file is now the primary
  store and the keychain is written exactly once, at creation. The vault master
  key is likewise resolved from a 0600 key file first, with the keyring as the
  durable recovery copy, so version upgrades no longer prompt either. Set
  `FINOPS_VAULT_KEYCHAIN_ONLY=1` to keep the key exclusively in the keychain.
  The trial keychain entry is renamed to an honest `nable-trial` and migrates
  automatically.

### Added
- **FOCUS 2.0 across the usage-based long tail.** Snowflake, Datadog, MongoDB
  Atlas, New Relic, Databricks, Vercel, Langfuse, Cloudflare, PagerDuty, GitHub
  and Twilio now normalize into the same FOCUS 2.0 records as AWS, Azure and
  GCP, via one generic translator. `get_focus_costs` and `slice_costs` query
  all of them in a single shape.
- **LLM/AI spend in the unified dataset.** OpenAI, Anthropic, OpenRouter and
  LiteLLM spend joins the FOCUS dataset, one record per model with token counts
  and request volume preserved as tags. Bedrock and Vertex are excluded from
  the merge since they already arrive through the AWS and GCP exports, so
  nothing is double-counted. Filter with `provider="openai"` or `provider="ai"`.
- **Agent cost-control gate.** Agents can call `check_action_policy` and
  `estimate_change_cost` before applying a change, getting an allow, block or
  escalate answer against your budgets and policies plus a cheaper path when
  one exists.

### Hardened
- FOCUS translators degrade safely on hostile provider data: non-finite costs
  are clamped, malformed gateway responses drop fields instead of records, and
  internal per-provider payloads no longer leak into `get_llm_costs` responses.

## 0.8.101

Hardening pass: credentials at rest, role enforcement on hosted boxes, and a safer fleet and build pipeline.

### Security
- **The vault master key can live off the data volume.** Hosted deployments can
  now supply `FINOPS_VAULT_KEY` from the host environment, so the Fernet key no
  longer has to sit in the same `/data` volume as the encrypted `vault.db`. A
  leaked or snapshotted data volume is then useless on its own.
  `deploy/provision-tenant.sh` generates one per tenant. Existing boxes are
  unaffected: a blank key keeps the previous self-managed key-file behavior.
- **Role enforcement now works on a single-tenant box.** `require_role` honors an
  explicitly attached identity even in permissive SQLite mode, and defaults to
  admin when none is attached, so the box owner can never be locked out. The
  dashboard agent attaches the session's role, and a control-plane analyst login
  gets a real analyst ceiling instead of silent admin.
- **Public image hardening.** `.dockerignore` now excludes `*.db`, `*.key`,
  `*.pem`, `.finops`, and `tenants/`, so no local vault or tenant secret can be
  baked into the published image.

### Fixed
- **Fleet updates fail loudly on a misconfigured fleet.** `deploy/fleet-update.sh`
  preflights that at least one tagged box is reachable via SSM and errors instead
  of dispatching a silent no-op when the per-box role or tag is missing.

### Added
- **CI guard against stale web bundles.** A new workflow rebuilds `web/app.js` and
  `web/tweaks-panel.js` from their JSX with a pinned esbuild and fails if the
  committed bundle is out of date. The pre-commit hook is pinned to the same
  version so the two never disagree.

## 0.8.100

The OS keychain is read once per process, not every few minutes.

### Fixed
- **No more repeated macOS Keychain prompts.** `Vault.default()` and the license
  trial check read the OS keychain on every call, so a long-running server
  re-read it constantly and macOS re-prompted ("python wants to use your
  confidential information") every few minutes. Both now cache the master key and
  the trial date in-process and read the keychain at most once per session. Click
  "Always Allow" on the first prompt and it stays quiet.

## 0.8.99

Audit fixes: tool loading, share links, doctor, and the serve dashboard.

### Fixed
- **MCP tool loading.** The startup banner printed to stdout on the server path,
  the same channel as the JSON-RPC handshake, so a strict MCP client could load
  zero nable tools. The banner now goes to stderr and stdout stays clean.
- **`finops serve` showed $0 for Azure, GCP, and SaaS.** The serve process never
  hydrated the vault into the environment, so providers that read credentials
  from env looked disconnected. It now hydrates and uses the full connector set.
- **Read-only share links are read-only again.** The `/view` flag was injected
  after the page scripts ran, so the "View only" badge never showed and the Ask
  composer stayed live. It is injected in the head now, before the scripts run.
- **`finops doctor` no longer reports "Cost Explorer: ✓" on a real error.** A
  throttle, an expired token, or a network failure was treated as healthy. It
  now reports "could not verify" and never green on an unconfirmed error.
- **The Bedrock cost probe can't hang.** The STS auth check had no timeout; it
  now bounds connect and read and skips Bedrock cleanly on failure.

### Changed
- **A legible "no AI key" message.** The dashboard Ask and the Slack bot now say
  to set `ANTHROPIC_API_KEY` and where to get one, instead of a vague guess.
- **Airgap covers the AI assistant.** `FINOPS_AIRGAP=1` now disables the agent
  (which would otherwise send query results to Anthropic), and SECURITY.md names
  the two egress paths (the assistant and anonymous telemetry) directly.
- **Dashboard polish.** A fresh account reads "not scored" instead of a false
  "healthy", "New view" pre-fills an editable prompt instead of auto-sending,
  and a long answer shows a "still working" note instead of static dots.
- Server error responses (SSO, `/api/data`, mark-done) are generic to the client
  and logged in full server-side, no raw exceptions or account identifiers.
- Docs now state Python 3.11+ (the package requires 3.11), not 3.10.

## 0.8.98

The clean dashboard redesign goes live.

### Dashboard
- **The redesign is now the dashboard, not a mockup.** A left sidebar carries the
  workspace: Dashboards (Cost overview, New view), an Agents group (cost investigation,
  rightsizing, idle cleanup, anomalies, commitments), chat History, and a single-tenant
  security card. The main view opens on a greeting that leads with what nable found
  ("nable found $X/mo to save"), icon-tile metric cards with trend pills, a spend-trend
  chart beside an Ask panel, and a savings-opportunities table.
- **A favicon.** The browser tab showed a generic globe; it now carries the nable mark.
- The agents, the Ask-panel suggestion chips, and New view each open a focused Ask
  session. Review on an opportunity opens it in the agent to draft the change.
- All of it rides the real /api/data wiring: charts, scorecard, opportunities, pinned
  views, and in-session history are unchanged underneath the new design.

## 0.8.97

The dashboard, rebuilt around the agent.

### Dashboard
- **Opportunities-first opening.** Leads with what nable found ("Savings nable found
  $X/mo") and an honest realized counter, instead of a raw cost table. Realized is $0
  on day one, so the opportunity is the value shown first.
- **The sidebar is the AI spine.** A Workspace group, an Agents group (cost
  investigation, rightsizing, idle cleanup, anomalies, commitments) that each launch a
  focused Ask session, and a chat History group.
- **Onboarding checklist** that auto-checks what's already set up (provider connected,
  business context) and hands the rest to the agent, collapsed to a one-line nudge.
- **Cleaner craft.** Surfaced metric cards with icon tiles, one tight metric row
  instead of a sprawling grid, the left rail dropped, tighter panel spacing.
- Fixed a history-click bug that fired a brand-new chat instead of loading the question.
- The 0.8.96 logo edit reached only the fallback copy of the dashboard; it now lands in
  the served file (static/dashboard.html).

## 0.8.96

Faster answers, sharper cost accuracy, a security fix, and managed single-tenant hosting.

### Performance
- **The agent runs its tool calls in parallel.** A question that needs several cost
  lookups used to run them one after another, each a slow Cost Explorer round-trip.
  They now run concurrently, so multi-tool answers come back faster.
- **Cost data is cached to disk, not just memory.** A restart, a redeploy, or the
  next day's first query serves the prior fetch instead of re-hitting Cost Explorer.
  Best-effort: any disk problem falls back to a normal fetch, never to stale numbers.
- Concurrent fetch in the cost-per-commit path and parallel Cost Explorer calls in
  tag-coverage analysis.

### Cost accuracy
- **Bedrock no longer reports $0.** An exact-match SERVICE filter missed Bedrock's
  per-model line items; spend is attributed by service name now.
- **Textract waste no longer inflates the "unknown" bucket N times.** The per-tag
  breakdown resets its buckets correctly.
- Fixed a 30-day-sum bug in cost totals and a GitHub timestamp key mismatch.

### Agent
- **Spend questions get the number, not a waste audit.** "What did we spend this
  month?" now leads with the total and top services instead of pivoting to a savings
  pitch.

### Security
- **Blocked git-ref argument injection (RCE) in the PR tools** and hardened the GCP
  attribution SQL against unsanitized label keys.

### Hosting (managed single-tenant)
- **Closed the deploy gaps found in the first live single-tenant deploy** and pass
  the Anthropic key plus managed-AI budget through to the instance, so the hosted Ask
  tab works.
- Graceful shutdown on SIGTERM so a hosted box survives `docker stop` and restarts.
- Real icon and wordmark logo in the hosted dashboard topbar.

### Site
- Marketing-only front page with the demo front and center; Pricing and FAQ moved to
  /pricing. Centered, consistent layout, Architecture and Security collapsed into
  tabs, and a plain-language overview atop the docs.

### Fixes
- Side-effect-free `--help`, better `finops login` error handling, and genericized
  customer names in tool docstring examples.

## 0.8.95

Login-first activation everywhere.

### Activation
- **Pro-gated tools now point to `finops login`, not a license key.** When a free
  account hits a Pro feature in Claude or Cursor, the upgrade prompt reads "already
  subscribed? sign in: finops login" instead of asking for a key to paste.
- The post-purchase email leads with `finops login` (enter your email, paste the
  6-digit code), keeping manual key activation as a fallback.

## 0.8.94

One-step Pro activation, and managed-AI credits that reset monthly.

### Activation
- **New `finops login` (and `finops logout`): sign in by email, no license key to
  copy.** Enter the email you bought Pro with, paste the 6-digit code we send, and
  nable stores the license locally so the server picks it up automatically. The key
  never has to be copied, pasted, or remembered. `finops logout` removes it.
- `check_license` now reads the license stored by `finops login` from the local
  vault when `FINOPS_LICENSE_KEY` is unset; an explicit env var still wins (CI,
  power users). Validation stays fully offline against the bundled public key.

### Billing
- **Managed-AI credits are use-it-or-lose-it.** The monthly allowance no longer
  carries forward; the ledger resets each period. This backs the credit-based
  hosting add-on (Pro 500 credits, Startups 10,000), billed on top of the flat plan.

## 0.8.93

The policy-bounded guardrail (advisory): an agent can ask "should I apply this?"

### Agent-native
- **New `check_action_policy` tool: an advisory allow / block / escalate gate.**
  Describe a remediation action (rightsizing, idle_cleanup, purchase_commitment, and
  the like) plus the cost change, and nable checks it against a human-authored policy.
  Reversible, allowlisted, in-budget actions return `allow`; one-way doors (delete,
  terminate, buy a commitment) and over-budget changes return `escalate`; disallowed
  action types return `block`. It composes the cost preflight with the policy gate.
- **Advisory only, propose-only intact.** nable returns advice; a human approves and
  applies. It never auto-executes. The auto-execute relaxation (B2) is a separate,
  explicit decision and is deliberately not built.
- New `policy.py` (pure, tested): door classification (two-way vs one-way),
  `evaluate_action_gate`, and `load_policy` with env overrides
  (`FINOPS_POLICY_MAX_AUTO_USD`, `FINOPS_POLICY_ALLOWED_ACTIONS`). 16 new tests; full
  suite 1091 passed.

## 0.8.92

The agent-native on-ramp: a cost preflight agents call before they act.

### Agent-native
- **New `estimate_change_cost` tool: cost preflight with a machine verdict.** Call it
  before applying an infra change (a Terraform plan, a `helm diff`, or a known monthly
  delta) to get `ok` / `warn` / `over_budget` / `no_budget` plus the monthly and annual
  cost delta and the budget headroom. Read-only: it estimates and checks against your
  budget, it never applies anything. Wraps the existing Terraform/Helm estimators and
  the budget enforcer, and is the seed of the policy-bounded guardrail.
- **New `agent` persona for concise, structured output.** Automated callers get terse,
  machine-readable responses instead of human prose. Activate per-process with
  `FINOPS_PERSONA=agent` in the server env (env wins over the config file) or
  `finops config --persona agent`.

## 0.8.91

AI engineering report now works for teams that commit straight to main.

### AI unit economics
- **`get_ai_engineering_report` attributes commits, not just merged PRs.** It only
  looked at merged pull requests, so a repo that pushes directly to `main` (no PRs)
  showed zero AI output even with hundreds of model-authored commits. The model
  trailer ("Co-Authored-By: Claude Opus 4.8 ...") lives in the commit message, so
  commits attribute exactly like PRs do, by model, sized high/medium/low, joined to
  spend for a **cost per commit**.
- **New `unit` argument**: `"pr"`, `"commit"`, or `"auto"` (default). Auto uses PRs
  when the repo has any in the window, else commits, so PR-shops and commit-to-main
  shops both work. The unit actually used comes back in the report's `unit` field.
- Commit sizing uses the GitHub GraphQL history (message + additions/deletions +
  author in one call per 100 commits), so every commit is counted and cost-per-commit
  divides spend by the true count, not a truncated sample.

## 0.8.90

Onboarding fixes from launch-day dogfooding.

### Install
- **`uvx nable` no longer dies on a missing cryptography wheel.** Python 3.10 is the
  one supported version without a prebuilt `cryptography` wheel, so on a 3.10-default
  machine (an Anaconda base, an old system Python) uv fell back to compiling it from
  source and crashed on any arch or toolchain mismatch. Bump `requires-python` to
  `>=3.11` so uv resolves to a 3.11+ interpreter that has wheels. If Python downloads
  are disabled in your uv config, `uvx --python 3.12 nable` forces a good interpreter.

### Setup wizard
- **The closing message now matches the provider you just connected.** It was
  hardcoded to "ask what are my AWS costs" after every connector; connect GitHub and
  it now suggests "what has my AI coding shipped, by model?", connect Slack and it
  says alerts will post to Slack, and so on.
- **Next-step command hints adapt to how you launched nable.** `serve` and `setup`
  hints print as `uvx nable …` for a uvx run (no `finops` on PATH) and `finops …`
  for a pip install.

## 0.8.89

Onboarding seamlessness. The hero command finally launches the good flow, and the
first run can no longer crash or dead-end.

### Onboarding
- **`uvx nable` now runs the guided welcome flow**, not a persona quiz plus a
  26-provider menu. Bare `finops` routes to `finops welcome` (ambient-credential
  scan, value moment, never dead-ends); the full provider menu stays behind the
  explicit `finops setup`. The best onboarding was already built, new users just
  weren't being pointed at it.
- **The value moment can no longer crash the flow.** `_show_value_moment` now
  swallows a failed import (for example a broken or arch-mismatched native
  dependency) or any scan error and degrades to the demo number plus the setup
  close, instead of a raw traceback at the moment the user expects their first
  figure.
- **Next-step hints match how you launched nable.** A `uvx nable` run is
  ephemeral, so `finops doctor` would be "command not found". The welcome flow now
  prints `uvx nable doctor` for uvx users and `finops doctor` for pip installs.
- **No log noise on the first line.** The license status check dropped from INFO
  to debug, so onboarding no longer opens with `INFO License: ... license.py:436`
  before the banner.

### Release hygiene
- Sync `server.json` and the Claude Code plugin pin to the package version (they
  drifted at 0.8.88), so registry and plugin installs never point at a stale
  release. The suite fails if they drift again.

## 0.8.88

Tighten the free tier. Free was handing over the continuous, acting, cross-cloud,
forecasting, and AI-unit-economics surface: the whole product minus a few buttons.
This draws the pull/push line. Free answers when you ask. Pro runs for you.

### Licensing
- **Five new Pro gates** behind `require_pro`: `alerts` (proactive alert policies
  and scheduled push), `forecasting` (cost, Azure, and LLM projections),
  `ai_unit_economics` (cost per PR by model, AI KPIs, the GitHub
  engineering-attribution report), `remediation` (drafting rightsizing and
  terraform-tag PRs), and `cross_cloud` (the unified compare-providers and
  total-spend-all-sources view). 19 tools gate on these.
- **The first value moment stays free.** Cost queries, anomaly detection,
  rightsizing findings, single-provider views, and AI spend totals are never
  gated. Activation is the bottleneck, not generosity: free is connect, ask, see
  a real dollar figure; Pro is it keeps watching, acts, and unifies across clouds.
- **Demo mode unlocks everything.** `require_pro` short-circuits when `is_demo()`
  is true, so the product still demos in full to anyone evaluating it. Gating only
  bites real free-tier accounts.

## 0.8.87

Hardening from a full adversarial audit of the 0.8.86 work.

### Connectors
- **Anthropic Cost API never reports a truncated total as authoritative.** If
  pagination hits the page cap it falls back to the estimate (and logs it) rather
  than returning a partial sum as billed dollars. Undated cost buckets are skipped
  so the daily breakdown and the total stay consistent.

### AI KPIs
- **No more fabricated "F, no cache hits".** A dollars-only Cost API result (no
  token counts) now falls through to the honest "no token-level data" note instead
  of being graded as zero cache usage.

### Hosting and deploy (single-tenant)
- **docker-compose:** persist data into the mounted volume (`FINOPS_DATA_DIR=/data`,
  it was writing to the container home and losing everything on rebuild); bind the
  raw app port to loopback by default so only Caddy's 443 is public; map the
  operator-facing `FINOPS_DATABASE_URL` to the `DATABASE_URL` the app reads so
  team/RBAC mode actually engages.
- **server:** `FINOPS_REQUIRE_AUTH=1` now hard-forbids an unauthenticated dashboard
  even when the password is set to "off".
- **provisioning:** validate the slug/domain, force `chmod 600` on the tenant env
  on every run, pin the EC2 deploy to a release tag (not main HEAD), and document
  revoking the customer's cloud key on offboarding.

### Wizard
- Fix the AWS `provider_connected` telemetry that mislabeled the auth method after
  the one-click menu was renumbered.

## 0.8.86

Anthropic costs now come from the actual billed Cost API, not estimates.

### Connectors
- **Anthropic Cost API.** Anthropic released the organization Cost API
  (`/v1/organizations/cost_report`). nable now pulls actual billed dollars from it
  instead of estimating from token counts × list prices. It activates
  automatically when an Anthropic Admin key is configured (the setup wizard
  collects it) and falls back to the usage-based estimate otherwise. Token-based
  KPIs (cache hit rate, context-window use) keep working, enriched from the
  Usage API.

### Deploy
- **Single-tenant hosted provisioning (AWS EC2).** Hand-cranked runbook plus a
  helper to stand up a managed single-tenant instance per customer, with
  control-plane env wiring (`FINOPS_INSTANCE_ID`, `FINOPS_CONTROL_PLANE_SECRET`)
  for one-click login from the getnable.com account. See docs/PROVISIONING.md.

## 0.8.85

Easier AWS connect: one-click CloudFormation in the setup wizard.

### CLI
- **One-click read-only AWS key.** The wizard's AWS step now offers "One-click
  CloudFormation" alongside paste-a-key and SSO. It opens the AWS console with a
  read-only stack pre-loaded; you click Create, copy the access key from the
  stack Outputs, and paste it back. Collapses the credential step (the spot most
  people abandoned) from a dozen console clicks to two copy-pastes, and it works
  even with no AWS credentials configured locally. The wizard also records which
  connect method you pick (`aws_connect_method`) so we can see which one converts.

## 0.8.84

Fix: no more telemetry log noise in the setup wizard.

### CLI
- **Quieter `uvx nable` setup.** The interactive wizard silenced the AWS SDK and
  the scheduler but not the HTTP client, so `httpx` logged every anonymous
  telemetry POST ("HTTP Request: POST .../capture/") straight into the prompts,
  including on top of "Write config?". The wizard now lowers `httpx`/`httpcore`/
  `urllib3`/`posthog` to WARNING the way `finops welcome` already did. Telemetry
  is unchanged; only the stray log line is gone.

## 0.8.83

The dashboard, reborn: full-screen, true black, and it builds views for you.

### Dashboard
- **Full-screen, advanced-AI redesign.** The Ask tab now fills the screen like a
  real AI console (composer pinned at the bottom, a calm thinking state, crisp
  message rows). The palette is true black, dark-only, and the rigid card grid
  gives way to a flowing layout where panels float on the black with varied
  rhythm.
- **Ask to build a view.** Ask the cost console for a slice ("spend by team this
  quarter") and it pins a live card to your dashboard with no reload, plus a
  managed-AI usage meter per turn for credit billing.
- **Sleek copilot voice.** The chat (and the Slack bot) drop the emoji bullets,
  "TL;DR" headers, and em dashes for a sharp senior-analyst voice: severity in
  plain words, resource ids in backticks.

## 0.8.82

Fix: the dashboard Ask tab works on a default install.

### Dashboard
- **The in-browser cost copilot no longer needs an extra.** The Ask tab calls the
  Anthropic SDK directly, but `anthropic` was only in the `slack` and
  `pr-comments` extras, so a plain `uvx nable serve` returned "nable isn't fully
  set up yet" even with `ANTHROPIC_API_KEY` set. The SDK is now a core dependency
  (it is light: pure Python on httpx and pydantic, both already core), so the chat
  works out of the box once you pass your key.

## 0.8.81

The dashboard, rebuilt.

### Dashboard
- **Full visual redesign of `finops serve` / `nable serve`.** Rebuilt on the Cold
  Graphite design system: dark locked by default, Geist Mono tabular numbers as
  the hero of every metric, ice-blue accents, hairline borders, the proper radius
  scale. KPI cards, charts, the efficiency scorecard, and savings opportunities
  are now instrument-grade, and the Ask tab is a terminal-style cost console with
  a proposed-action card (Approve / Dismiss) instead of generic chat bubbles. All
  data binding is unchanged: this is a restyle, not a behavior change.

## 0.8.80

Fix: the visual dashboard was missing from pip and uvx installs.

### Dashboard
- **`finops serve` / `nable serve` now actually renders.** The bundled dashboard
  template (`static/dashboard.html`) was matched by an over-broad `.gitignore`
  rule, so hatchling's VCS file selection dropped it from the wheel. Every pip or
  uvx install got the fallback "Dashboard template missing" page instead of the
  dashboard. The template is now force-included in the wheel, with a build check
  so it cannot silently drop again.

## 0.8.79

Faster cold start.

### Install and onboarding
- **Slimmer default install.** The Azure and Google Cloud SDKs (and Google's
  grpcio/protobuf chain) are no longer core dependencies. They moved to
  `finops-mcp[azure]` and `finops-mcp[gcp]` extras, so an AWS-only install (the
  common path) downloads far fewer packages and the first cold `uvx` launch is
  much faster. The SDKs are imported lazily and provider detection is env-var
  only, so the AWS path never touches them. The setup wizard folds the right
  extra into the launch command automatically when you connect Azure or GCP, and
  `finops-mcp[all]` still pulls everything. Also synced the Claude Code plugin
  pin (it had lagged at 0.8.77).

## 0.8.78

One command to install.

### Install and onboarding
- **`uvx finops-mcp` is the whole command now.** Run it in a terminal and it
  launches the onboarding wizard; an MCP client (Claude Desktop, Cursor) running
  it over stdio still gets the server. Any subcommand routes to the CLI too, so
  `uvx finops-mcp setup`, `uvx finops-mcp doctor`, and `uvx finops-mcp welcome
  --demo` all work. No more `uvx --python 3.12 --from finops-mcp finops welcome`.
  The site, docs, and README now lead with the short command; the MCP config the
  wizard writes keeps its pinned managed Python for reproducibility.

## 0.8.77

Deep GCP audits, plus a round of security hardening.

### GCP
- **`audit_gcp_waste`.** First resource-level GCP audit: unattached persistent
  disks, reserved static IPs that are not in use, snapshots past an age
  threshold, and idle VMs (CPU joined from Cloud Monitoring). Findings come back
  sorted by estimated monthly savings with by-category, by-severity and
  by-project rollups. Set `GCP_PROJECT_IDS` (the setup wizard now prompts for it)
  to enable it; needs `roles/compute.viewer` and `roles/monitoring.viewer`.

### Security
- **SSO callback hardening.** Removed the unsigned-state escape hatch (state is
  always signature-verified now) and re-validate the post-login redirect to
  same-site paths, closing an account and license-key takeover via open redirect.
- **demo-ask fails closed** when Vercel KV is unset, so the live model never runs
  without a durable cross-instance cost cap.
- **Export path confinement.** `export_cost_report_csv` confines writes to your
  home or temp dir and refuses dotfile targets.
- **Supply chain.** Every GitHub Action is pinned to a commit SHA, `mcp-publisher`
  is pinned and SHA256-verified before it runs, and the trial clock signs with a
  dedicated key.

### Install and onboarding
- **Python 3.10+ is explicit now.** The README, SLACK.md, DEPLOY.md and the plugin
  and editor READMEs state the requirement and decode the cryptic
  `No matching distribution found` pip error (it means your Python is older than
  3.10). A stdlib preflight guard fails every console entry point with a clear
  message on an older interpreter, and `finops doctor` reports the running Python.
- **`finops serve` is secure by default.** It binds `127.0.0.1` instead of
  `0.0.0.0`, so a laptop run never opens a LAN port unprompted; add `--host 0.0.0.0`
  (still password-protected) to share it with your team. It flushes the
  auto-generated password before blocking, so it is visible under a pipe or process
  manager, and skips the LAN-IP probe on a local run.
- **The dashboard tells the truth.** A connected provider whose fetch fails (expired
  token, AccessDenied) now surfaces the error instead of showing a green badge over
  $0.
- **Slack bot tier surfaces before setup.** `finops setup slack` shows your
  Team/trial status up front (the conversational bot is Team-only, with a trial),
  checks the `slack_bolt` dependency instead of letting `finops-slack` crash, and
  every printed `finops-mcp[slack]` install command is quoted so it works in zsh.
- **Brand and font.** The dashboard and login page now use Bricolage Grotesque,
  self-hosted with no external font request, and the MCP server self-reports as
  `nable`.

## 0.8.76

Onboarding instrumentation and a faster ambient connect, aimed at the activation
wall (105 wizard starts, 5 provider connects). Now we can see exactly where people
drop, and the credential-detection step never hangs.

### Activation
- **Step-level funnel events.** The AWS connect flow now emits a `setup_step` event
  at each stage: connect opened, ambient probe done (with candidate count), ambient
  confirmed/declined, no-ambient-creds, manual opened, method selected, one-click
  offered, connect attempted, verify failed. The flow previously fired only start and
  success, so the ~95% who abandoned mid-wizard were invisible.
- **Failed manual connects now report.** A credential that fails verification emits
  `provider_connect_failed` instead of silently returning, so broken-key drop-offs are
  finally measurable.
- **Ambient detection is parallel and time-capped.** `_detect_aws_candidates` probed
  each AWS profile sequentially with multi-second timeouts and no global cap, so a
  machine with several profiles (or an expiring SSO token, or firewalled IMDS) stalled
  onboarding for 30s or more. Probes now run concurrently under a hard 4-second
  deadline, so detect-then-connect stays fast and never freezes. Named profiles are
  still preferred; the default chain is deduped against them.

Full suite: 829 passed.

## 0.8.75

The AI cost moat: optimize, forecast, and monitor token spend, including committed
contracts. Visibility is table stakes; this release moves nable to the layer where
the defensibility is. Plus a stack-tailored "what can you do?" so a connected user
actually discovers the 160+ tools.

### AI commitments & contracts
- **Reserved-Instance analysis for tokens.** A new commitment engine optimizes spend
  against committed AI contracts: prepaid credits, Azure OpenAI PTUs, AWS Bedrock
  Provisioned Throughput, and enterprise rate cards. For each it reports coverage,
  utilization, your effective $/Mtok versus on-demand, break-even, a right-size
  recommendation, and runway, priced against your actual negotiated terms, not list.
  With no contract configured it tells you whether your spend is high and stable
  enough to justify buying one. Tool: `get_llm_commitment_analysis`. Configure
  contracts via `FINOPS_AI_CONTRACTS` or `~/.finops-mcp/ai_contracts.json`, which
  stay on your machine.

### AI forecast & monitor
- **Token-spend forecasting with an exhaustion date.** Projects the AI bill,
  month-over-month growth, and the day your credits or commitment run out, using the
  per-account Holt-Winters forecaster. Tool: `forecast_llm_costs`.
- **Daily token-spend monitor.** Watches for spend spikes and commitment contracts
  needing attention (under-utilized capacity, enterprise minimum shortfall, expiring
  commitment), with a Slack alert and self-healing dedup. Tool: `get_ai_spend_monitor`,
  runs daily via the scheduler. Credits-to-cash stays with the existing credit job to
  avoid double-alerting.

### Discovery
- **"What can you do?" is now tailored to your stack.** It detects everything connected
  (cloud, SaaS, LLM providers, Kubernetes) and renders a capability map grouped by
  outcome, with the asks you'd actually type, the dollar impact, honest per-group tool
  counts, and the highest-value thing left to connect. Replaces a hardcoded AWS-centric
  list that missed most of the product.

### Copy
- The share card and the plugin marketplace listing no longer say "plain English"; the
  positioning is now "FinOps that lives in your AI editor."

Full suite: 822 passed.

## 0.8.74

A security pass (/cso) and a correctness pass (/debug) over the recent work, plus
onboarding polish so a connect-to-value run holds up for a real first user.

### Security
- **Removed `sts:AssumeRole` from the read-only connect key.** It's a privilege-
  escalation primitive, not a read: in an account with a role that trusts the
  account root (common), a holder of the "read-only" key could assume it and gain
  its permissions. The single-account connect never assumes a role, so it's gone,
  and `sts:Assume` is now in the over-privilege guard. The key is now strictly read.
- Corrected the CloudFormation Outputs wording: the secret access key is not "shown
  once," it persists in the stack Outputs; the description now says so and points to
  deleting the stack to revoke.

### Correctness (verified review findings)
- **Credit alarm no longer cries wolf at month start.** AWS posts promotional
  credits on a lag, so a healthy, fully-covered account looked uncovered early in
  the month and could fire a false "credits flipped to cash" alert. It now assesses
  the latest settled month, not the in-progress one.
- **OpenRouter window is end-inclusive.** It was dropping today's spend, and a
  single-day query returned empty. Also hardened the credits fallback against a
  malformed/null API response.
- **Context-window KPI no longer reports nonsense for Anthropic.** With no per-
  request data it was dividing whole-period tokens by 1 and labeling a thousands-
  of-percent figure "healthy"; it now says the data is unavailable, and Anthropic
  populates request counts when the API provides them.
- Fixed `model_sprawl` double-counting `o3-mini`/`o1-mini` as both cheap and
  expensive (substring match), a single-month credit query mislabeled as "no
  credits", a mislabeled `monthly_usd` on the blind-spot tool (now `window_usd`,
  it's a window sum), a dead success-log in the credit-check job, and a corrupt
  alert-dedup file that would re-send daily (now self-heals). Clamped a positive
  credit-clawback row so it can't produce negative coverage.

### Onboarding
- **The value moment shows the token bill, not just the cloud bill.** A user with
  both AWS and a model provider now sees their AI/LLM spend alongside cloud spend,
  the bigger number for an AI-native team and the thing no cloud dashboard shows.
- AI-native fast path: ambient `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` offers an
  instant token-bill scan, plus an explicit OpenAI/Anthropic connect menu option.
- A connected model key that returns no billing data now explains it needs an admin
  key (with the exact URL), instead of dead-ending on an empty bill.
- `finops doctor` reports the license tier, so activating a key is verifiable.
- API-key entry strips wrapping quotes/whitespace and warns on a wrong-provider
  paste, instead of silently storing a broken key.

## 0.8.73

### The one-click connect key is now strictly read-only
The published CloudFormation template advertised "no create, modify, or delete
permissions of any kind" but its action list still carried `logs:PutRetentionPolicy`,
a write, left over from a never-built auto-remediation path. nable never calls it
(the log-retention feature only reads and then hands the user a CLI command to run
themselves), so it was dead permission that contradicted the read-only claim on the
exact artifact a security-minded user reads at connect time.

- Removed `logs:PutRetentionPolicy` from the connect credential. The one-click key
  and the role/Terraform templates are now 100% read (Get/Describe/List + STS auth),
  so "read-only, auditable" is defensible verbatim.
- Added `logs:Put/Create/Delete` to the over-privilege guard so a write can't creep
  back into the policy unnoticed, plus a test asserting every action in the connect
  key is a read verb.

## 0.8.72

### Onboarding: kill the no-creds connect wall
The activation funnel showed the problem plainly: ~99 machines start setup in 30
days, ~5 connect a real provider. The entire drop is at the connect step, and the
worst case is the user with no local AWS credentials, who was forced to hand-mint
an IAM key in the console.

- **One-click AWS connect is now live.** The read-only CloudFormation template is
  published, so `quick_create_available()` is true by default and `finops setup aws`
  surfaces a launch-stack link: the user clicks, creates a read-only stack, and
  pastes two outputs. No pre-existing credentials needed. This stays local-first:
  the template is a static public artifact, the keys are created in the user's own
  account, and nothing routes through nable's servers.
- **The welcome flow leads with it.** When the one-click link is available, the
  connect menu and the skip-path hint both surface "Fastest, one-click read-only
  AWS key" so a no-creds user sees the fast path immediately instead of a key
  prompt they can't satisfy. Gated on publish, so it never shows a dead link.
- **AWS CloudShell fast-path.** For a no-local-key user, the connect offer now
  points at CloudShell (already authenticated): `pip install finops-mcp && finops
  welcome` shows their real bill in seconds via ambient-credential detection, with
  nothing to mint.

## 0.8.71

### AI-native cost coverage
- **New gateway connectors: OpenRouter and LiteLLM.** OpenRouter is where a large
  share of early AI startups route token traffic (one key, 300+ models); the
  connector reads per-model cost, tokens, and requests from the activity endpoint,
  and falls back to a credits-only summary on a standard key. LiteLLM reads a
  self-hosted proxy's `/spend/logs` and aggregates spend by model and day, all on
  the user's own network. Both flow into `get_llm_costs`.
- **GPU/inference-infra connectors: Modal, Together, Replicate** (`get_gpu_infra_costs`).
  These hold the largest variable cost for model-builders. Each confirms the
  credential and reports the gate honestly (billing APIs are Team/Enterprise-only
  or omit per-range cost) rather than fabricate spend. Track these via invoice
  import until a usable usage endpoint exists.
- **`list_connected_providers` now has an `llm` category** so OpenAI, Anthropic,
  Vertex, OpenRouter, LiteLLM, Modal, Together, and Replicate are visible. They
  were previously absent from the provider list entirely.

### AI KPI engine correctness
- **OpenAI now feeds the AI KPIs, not just Anthropic.** `full_kpi_report` built its
  combined token map from `[anthropic_data]` only, and the OpenAI connector
  returned cost without tokens, so an OpenAI-heavy account (the common case) got
  empty cache, context-window, and prompt-efficiency analysis with a misleading
  "connect OpenAI with an admin key" note OpenAI could not satisfy. The OpenAI
  connector now emits `by_model_tokens` (fresh input, cache reads, request counts)
  matching the Anthropic shape, the aggregator surfaces merged per-model tokens
  across all providers, and the KPI engine consumes them. Cache analysis now works
  for OpenAI too. Guarded against double-counting Anthropic when both inputs are
  present.

### Credit cliff + AI-billing blind spots
- **AWS credit-to-cash flip detection** (`get_credit_status`). Reads Cost Explorer's
  RECORD_TYPE (Charge type) to separate gross usage, credits applied, and net cash
  per month, then detects the moment promotional credits stop covering the bill —
  the cliff where an early startup first feels cost pain, which AWS sends no native
  alert for. No CUR/Athena pipeline: works on a read-only key. Honest that AWS
  exposes no API for the remaining Activate balance, so the trend is inferred from
  observed consumption.
- **Cash-flip alarm**: a daily `credit_check` scheduler job fires a Slack alert once
  when the flip trips, deduped by month and status.
- **AI-billing blind spots** (`get_ai_billing_blind_spots`). Flags Bedrock,
  Marketplace, and SageMaker spend that bypasses AWS Cost Anomaly Detection, so a
  spike does not go unnoticed until the invoice lands.

### Setup
- Added setup-wizard entries and CLI subcommands for openrouter, litellm, modal,
  together, and replicate.

## 0.8.70

### Remediation
- **The rightsizing find -> fix -> prove loop now actually closes.** The PR pipeline
  already resolved a cloud resource ID to its Terraform address from state and
  opened a real PR with the fix, but the verification it promised in the PR body
  ("nable will auto-verify the change and record realized savings within 24h") was
  never scheduled, so it only ran if a human remembered to call `verify_savings`.
  Added a daily `auto_verify` scheduler job that re-reads the live resource and
  records the realized saving once a merged change is applied. The promise is now
  kept automatically.
- **Clearer skips.** When a recommendation can't be located in Terraform, the skip
  message now says whether no state was found (run from your IaC dir, or pass
  `resource_overrides`) or the resource isn't managed in this state, instead of a
  generic "cannot locate" line.
- Tests: locked in the auto-resolution-from-real-`tfstate` path (the defensible
  code), plus the auto-verify job wiring.

### Onboarding
- **The first-run "sample bill" no longer renders empty.** On the skip-for-now
  path, the value-moment ran `list_idle_resources` and `optimize_ai_spend`
  alongside the cost summary, but `list_idle_resources` had no demo guard, so in
  demo mode it reached for real AWS and blocked the whole scan, leaving "Here's
  nable on a sample bill" followed by nothing. Now the headline number is fetched
  first on its own wall-clock cap and always renders; the optional idle/AI scans
  run only for real accounts (no demo dataset exists for them) and each on its own
  cap, so a slow or blocking scan can never blank the number the user came for.

## 0.8.68

### Telemetry
- **`provider_connected` now fires on the ambient-credential path too.** When a
  user connects via an existing AWS profile, SSO, or the default credential chain
  (no manual key entry), the welcome flow confirms a real read and emits
  `provider_connected` with `auth_method="ambient"`. Previously only the manual
  access-key/role/profile path emitted it, so the activation metric was blind to
  everyone who connected the easy way and undercounted real connections.

## 0.8.67

### Onboarding
- **The first-run "show your bill" scan can no longer hang setup.** It was
  wrapped in a 35s asyncio timeout, but that timeout cannot fire when a blocking
  call (an SSO token refresh or a slow Cost Explorer request) pins the event
  loop, so for some credential setups the welcome flow hung forever at "Show your
  real AWS bill now?". The scan now runs in a daemon thread with a real
  wall-clock cap that returns on time regardless of blocking I/O, plus a "this
  can take up to ~30s" progress line so it never looks dead. On timeout it falls
  back gracefully; your editor config is already written by this point.

## 0.8.66

### Install reliability
- **Install and launch now pin a clean managed Python (`--python 3.12`).** On an
  Apple Silicon Mac with an x86_64 Anaconda base, uvx would source-build
  cryptography for the wrong architecture and fail ("incompatible architecture"),
  which also meant the MCP server could silently fail to start in Claude or Cursor
  for that cohort. Forcing a managed interpreter makes the build arch-native and
  isolated from any conda/system Python. Applied consistently across the install
  command, the configs the wizard writes (Claude Desktop, Cursor, Claude Code),
  the Cursor one-click deeplink, the Claude Code plugin, the README, and the docs,
  so the interpreter is cached at install time and the launch is a cache hit.

## 0.8.65

### Onboarding
- **The setup wizard now wires Cursor and Claude Code, not just Claude Desktop.**
  `finops welcome` writes `~/.cursor/mcp.json` and prints the exact
  `claude mcp add` command for Claude Code, and it reports which editors were
  actually configured instead of telling a Cursor user "you're set up" with
  nothing written. The shared config builder keeps all three clients in sync and
  never clobbers an unparseable config.
- **Restart guidance on the success screen.** MCP clients only load servers at
  startup, so the finish step now says to fully quit and reopen your editor, with
  a "you should see nable in your tool list" checkpoint.
- **Credential entry is no longer a dead end.** The auth-method menu validates
  your pick (a typo re-prompts instead of silently forcing manual key entry),
  temporary `ASIA` keys are accepted, and a no-key path exits cleanly with the
  steps to get one rather than blocking.
- **The one-click read-only key link is only shown once its template is
  published**, so the wizard never points you at a dead URL; until then it prints
  console steps. Adds the read-only-key CloudFormation template and a publish
  script.
- **Entry friction.** README lists `uv` as a prerequisite and sets the first-run
  download expectation. The `setup_wizard_started` ping is now off the critical
  path so a slow network can't stall the terminal before output.

### Telemetry
- `first_cost_query_success` no longer fires in demo mode, so the activation
  metric counts only people who saw their own real cost number.

## 0.8.64

### Security
- **The published least-privilege IAM policy is now complete.** Three
  free-tier tools (idle load balancer detection, ECR cleanup, ECS
  rightsizing) call AWS describe APIs whose actions were missing from the
  generated policy, so a credential scoped to it hit AccessDenied on those
  tools. Added the read-only actions (ELB, ECR, ECS, S3 multipart, two org
  actions) to `finops setup aws --iam-template`/`--iam-terraform` and
  `--check-scope`. Still write-free except the one documented
  `logs:PutRetentionPolicy`. Full policy published at getnable.com/iam.
- **Air-gap now covers the setup-wizard email capture.** It POSTed to
  getnable.com without checking `FINOPS_AIRGAP`; it is now gated, so air-gap
  mode truly sends nothing to any non-provider endpoint.

## 0.8.63

### Fixed
- **Demo mode now answers the headline questions.** A cold install
  (`finops welcome --demo`, no credentials) is the first thing a cautious
  trialist runs. `optimize_ai_spend` returned "No AI spend detected" there,
  hiding the AI-cost differentiator; it now runs the real optimizer over
  sample data and headlines the signature finding (input-heavy uncached
  Bedrock spend) with honestly confidence-labelled levers.
  `explain_recent_cost_drivers` returned "No providers connected"; it now
  answers with a coherent cost-change story.
- `finops --version` now works (it errored with "unrecognized arguments").
- CLI help no longer says "plain English."

## 0.8.62

### Performance
- **Cost queries drop from ~20s to the slowest single provider, repeats are
  free.** Azure and GCP connectors ran their sync SDK calls on the event
  loop, blocking every other connector; the AI-spend path fetched four
  providers serially with blocking HTTP at 10 call sites; six server loops
  queried providers one at a time; and only AWS had a cache. All connectors
  now run concurrently off the event loop with a shared 12h read-through
  cache. Measured 4x3s providers: 3.0s cold, 0ms warm.
- Hard per-provider timeout (`FINOPS_PROVIDER_TIMEOUT_S`, default 90s) so one
  hung provider API can no longer freeze a query. The Azure SDK ships with no
  timeout at all.
- AWS Cost Explorer / STS clients now set botocore connect/read timeouts;
  the defaults leaked worker threads for minutes under throttling.

### Fixed
- **Teams report delivery was silently broken.** reports.py called
  `teams.send_to_webhook`, which never existed, so every Teams report
  subscription threw and was swallowed. Implemented it (refuses non-Office
  webhook hosts) with regression tests.
- Setup wizard told buyers their license key starts with `FINOPS-1-`; real
  keys are `FINOPS-2-`. The in-product upsell branded the seven Pro features
  as "Team" and linked the $1,000 Team checkout for a $100 Pro gate; both now
  correct.
- Dashboard fetches run in parallel under the same provider timeout and reject
  requests with an unrecognized Host header (DNS-rebinding hardening,
  `FINOPS_DASHBOARD_ALLOWED_HOSTS` extends the allowlist).
- SQLite WAL/SHM sidecar files clamped to 0600 like the main db.
- Slack approval cards strip `<!channel>`/`<@user>` tokens from user-supplied
  ticket previews.

## 0.8.61

### Security
- **v1 license keys are retired.** The legacy HMAC signing secret was exposed
  in public git history, making every v1 key forgeable. v1 keys are no longer
  generated or accepted; v2 (Ed25519) keys are unaffected.

### Fixed
- **All telemetry now honors opt-out, air-gap and CI suppression.** The setup
  wizard and server sent events through a path that skipped the guards, so
  CI runs, air-gapped machines and opted-out users still pinged PostHog, and
  offline machines stalled up to 9 seconds per CLI command. The check now
  lives in the sender itself.
- `NABLE_NO_TELEMETRY=0` no longer opts out; falsy values ("0", "false",
  "no") are treated as off, matching `FINOPS_AIRGAP` parsing.
- A fast-exiting first run could mark the welcome sentinel and exit before
  the `install_completed` event was delivered, permanently uncounting that
  install. The sender now gets a short window to land.

## 0.8.60

### Fixed
- **`install_completed` telemetry now counts real installs, not automation.** It
  fired on any first CLI run, so cache-warm subprocesses, piped invocations, CI
  runners, and fresh uvx environments each logged a phantom install with a
  throwaway id, badly inflating install counts. It now only fires on an
  interactive, non-CI first run (stdin/stdout are a TTY). CI and build runners
  send no telemetry at all. A non-interactive first run is a no-op and leaves
  the first-run sentinel unset, so the first genuine human run still counts once.

## 0.8.59

### Security
- **Rotated the license signing key.** The previous Ed25519 public key shipped
  in the package paired with a private seed that had been committed to the
  public repo (in a test file), so anyone could mint valid license keys. The
  key is rotated; the leaked seed no longer validates against the bundled key,
  and a regression test fails the build if it ever does again. The test suite
  now uses its own throwaway keypair, genuinely separate from production.

## 0.8.58

### Added
- **Team plan ($1,000/mo flat, unlimited seats).** The conversational layer is
  now a paid tier: the Slack bot (questions, thread memory, RCA), chat
  remediation behind the human approval gate, and managed AI. Hard gate: the
  bot checks the license at startup and on every question; free and solo Pro
  keys get a clear upgrade message, trial keys get the full product. New
  "team" license plan with require_team gating; enterprise keys now correctly
  pass pro gates too (they previously failed the is_pro check). Pricing page
  updated: $1,000/mo or $10,000/yr, unlimited seats.

## 0.8.57

### Changed
- **Releases can no longer slow or break existing installs.** Configs written
  by the wizard now pin the exact version (`uvx finops-mcp==X`) instead of
  resolving "latest" at client startup, which could exceed Claude Desktop's
  startup timeout on the first launch after a release ("Server disconnected").
  Upgrades are now explicit: `finops upgrade` resolves the latest version,
  downloads it into the uvx cache up front, then moves the config pin, so the
  next Claude Desktop restart is instant. The Claude Code plugin pins the
  server version the same way, and the test suite fails a release that bumps
  the package without the plugin pin.
- **`finops welcome` never dead-ends, and shows value before asking for keys.**
  Onboarding analytics showed most installers skipped the credential step and
  saw nothing, then left. The first run now (1) detects an existing AWS
  credential chain and offers a one-keystroke read-only scan of your real bill,
  no setup menu; (2) if you skip or decline, still shows nable on a sample bill
  so the value lands before you leave the terminal; (3) labels demo output as
  sample data instead of claiming it scanned your account.

### Fixed
- **Onboarding can no longer hang on the ambient AWS check.** The first-run
  credential probe is capped at 3 seconds, so a firewalled IMDS endpoint or a
  stale SSO profile times out cleanly instead of freezing setup.
- **Slack thread memory no longer loses turns under concurrency.** Two quick
  @nable mentions in the same thread used to race and drop one exchange; the
  read-modify-write is now serialized per thread.
- **`finops-slack --help` works** and prints real usage instead of failing with
  a token error. The bot also prints a clear ready line on start, and warns at
  startup when no cloud provider is connected (cost questions would otherwise
  come back empty with no explanation).
- **Friendlier in-Slack errors.** A misconfigured bot tells end users "nable
  isn't fully set up yet, ask whoever installed me" instead of leaking env-var
  names, with the technical detail logged server-side.
- **Demo mode no longer leaks.** `FINOPS_DEMO` is set only for the duration of a
  sample-data scan and restored after, so it can't switch a later real scan to
  demo data in the same process.

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
