# Running nable headless

CI, cron, scripts and agents: nable with nobody at a terminal. This page
says what each command does when stdin is closed, what it prints, and how it
exits. Every behaviour in the tables was run on finops-mcp 0.12.0 (2026-10-17)
the way a pipeline runs it: `timeout 90 nable ... < /dev/null`, a fresh
`HOME`, stdout and stderr captured separately, and no cloud credentials. The
`commitments status` rows, `nable --version` and `nable guard check` were run
again that way on 0.13.0 (2026-10-18).

- [The rules every command keeps](#the-rules-every-command-keeps)
- [Commands for scripts](#commands-for-scripts)
- [Commands that need a person](#commands-that-need-a-person)
- [Exit codes](#exit-codes)
- [Environment variables](#environment-variables)
- [Air-gap mode](#air-gap-mode)
- [Running from cron](#running-from-cron)
- [What is hosted only](#what-is-hosted-only)
- [MCP over stdio for headless agents](#mcp-over-stdio-for-headless-agents)
- [CI recipes](#ci-recipes)

## The rules every command keeps

- **Nothing waits on a closed stdin.** A prompt that reads EOF takes its
  default, or the safe answer where the default would make a decision for
  you (connecting an account, approving a guard ask). Commands that exist to
  ask a person are listed [below](#commands-that-need-a-person), with what
  each does instead.
- **`--json` is one document on stdout.** Progress, banners and warnings go
  to stderr, including the `nable setup: all credentials stay on your
  machine` line most commands print first. A failure in `--json` mode is
  still a document (`scan` prints `{"error": {"class", "exit_code",
  "message"}}`), so a script reading stdout always gets an answer.
- **No escape codes in a pipe.** Colour only when stdout is a terminal, and
  never with `NO_COLOR` set or `TERM=dumb`. All 62 headless runs behind this
  page, and a second set with `TERM=xterm-256color`, wrote no escape code.
- **The exit code says what happened.** A command that could not do its job
  (no credentials, nothing stored, nothing delivered, a feature not in this
  package) exits non-zero; [Exit codes](#exit-codes) lists them.
- **No telemetry from CI.** Telemetry is opt-in (`NABLE_TELEMETRY=1`), never
  sent when `CI` is set, and the one-time consent question is asked only with
  a terminal on both stdin and stdout. The one call nable makes on its own is
  a PyPI version check (`scan`, the MCP server's start), which
  `NABLE_NO_UPDATE_CHECK=1` turns off.

Set these on any runner: `FINOPS_NO_KEYRING=1` (no OS keychain, so no unlock
prompt), `FINOPS_DATA_DIR` (where state goes; see [cron](#running-from-cron)),
and credentials the way the cloud's own CLI reads them (`AWS_PROFILE`, the
`AWS_*` key variables, an instance or OIDC role). `nable doctor --json` shows
which `FINOPS_*`/`NABLE_*` names are in effect, names any it does not read with
the closest one it does, and says when a credential stored with `nable setup`
outranks the same name in the environment.

## Commands for scripts

"No credentials" below means a fresh `HOME`, no `AWS_*` variables and no
instance metadata.

| Command | With stdin closed, no credentials | `--json` | Exits |
|---|---|---|---|
| `nable scan` | "no AWS credentials found", the fix, the `--dry-run` and `--demo` pointers | `{"error": {...}}` | 6 |
| `nable scan --demo` | the StreamCo sample, labelled "(demo data)" | report | 0 |
| `nable scan --dry-run` | every API call and IAM permission a scan makes; reads no credentials | the calls and a least-privilege policy | 0 |
| `nable brief --no-save` | a brief of what could not be read, and the fix on stderr | the brief | 6 |
| `nable brief --deliver` | as above, plus "delivery failed: slack" when the push did not land | the brief, with `delivered` | 1 |
| `nable brief --latest` | the last saved brief | `{"error": "no_brief"}` when there is none | 0 |
| `nable why` | "no AWS credentials found"; the Cost Explorer cost estimate on stderr | `{"error": {...}}` | 1 |
| `nable commitments status` | "no AWS credentials found", the fix, the `--dry-run` pointer | the report, with `{"error": {...}}` | 6 |
| `nable commitments status --fail-within 30` | the same, then "nothing was read, so it cannot check" | the same, with `gate` | 2 |
| `nable commitments status --dry-run` | every call and IAM action it makes, each a Describe or Get; reads no credentials | the calls and actions | 0 |
| `nable ai-budget` | Claude Code and Codex CLI usage on this machine, zeros when none | the budget | 0 |
| `nable ai-costs` | nothing connected; `not_connected` lists the providers | the split | 1 |
| `nable budget` / `budget refresh` | budgets against month-to-date spend; refresh rewrites the guard's figure | `{"ok", "budgets", ...}` | 0 |
| `nable budget ci-gate --fail-on-breach` | see [Exit codes](#exit-codes); no history is "cannot check" | `{"ok", "exit_code", "breached", "cannot_check", ...}` | 0, 1, 2 |
| `nable org status` (`review`, `questions`, `export`) | the org model | yes | 0 |
| `nable learn list`, `nable pack list` | lessons; installed packs | yes | 0 |
| `nable doctor`, `finops-doctor` | every check, with the fix for each failure | a list of checks | 0, 1 |
| `finops-doctor --version` | `finops-doctor (finops-mcp) 0.13.0` | | 0 |
| `nable guard check --command "..."` | the verdict the hook would give | the verdict and the policy in effect | 0, 1, 2 |
| `nable guard status`, `guard doctor`, `guard report` | hooks per agent; coverage; the decision ledger | `doctor`, `report`: yes | 0 |
| `nable guard verify-log` | whether the decision ledger's hash chain is intact | yes | 0, 1 |
| `nable guard export` | the verified ledger as JSON lines or CEF (`--format cef`, `--out`) | JSONL | 0 |
| `nable guard reconcile` | needs AWS credentials (CloudTrail, free) | `{"error": ...}` | 1 |
| `nable guard install --harness codex --global` (any agent; `--all`) | writes the hook; never asks. Check `guard status` after: on a machine without uvx the hook it wrote cannot start, and it says so | | 0 |
| `nable setup aws --check-scope` | "Could not get caller identity", nothing checked, how to get credentials | | 1 |
| `nable iam-template`, `setup aws --iam-template`, `connect --scopes` | the policies and the scopes each provider needs | | 0 |
| `nable license-status` (`whoami`, `plan`) | the plan; no escape codes | | 0 (1 on an invalid key) |
| `nable upgrade [VERSION]` | see [Exit codes](#exit-codes) | | 0, 1 |
| `nable uninstall --yes [--purge] [--dry-run]` | removes nable's editor entries and hooks; `--dry-run` changes nothing | | 0 |
| `nable tools`, `nable agents` | what to ask; the agent team and its setup | | 0 |
| `nable serve`, `nable credits` | "a hosted/enterprise feature" on stderr, nothing on stdout | | 1 |
| `nable` with no arguments | the MCP server on stdio (stdin is not a terminal); exits when stdin closes | | 0 |

The `commitments status` rows are from 0.13.0. With credentials it reads only
free Describe calls (RDS, EC2, ElastiCache, Redshift, OpenSearch and MemoryDB
reservations in each region, and the Savings Plans once); `--spend` adds Cost
Explorer and prints what that can cost on stderr before the first request. A
service botocore lists no endpoint for in a region is not called there: it is
listed under `not_offered` in the report, is not a failed read, and leaves the
exit code as it was.

`nable guard hook` is not for scripts: it is the agents' hook protocol, reads
one payload on stdin, answers on stdout and always exits 0, so a hook that
fails never blocks the agent (an empty stdin is "allowing, the hook input was
unreadable").

## Commands that need a person

These ask questions by design. Do not script them; what they do with stdin
closed:

| Command | With stdin closed |
|---|---|
| `nable setup openai` (and every other provider) | reads no key, stores nothing, exit 1 |
| `nable setup aws` | finds no credentials, connects nothing, exit 1 |
| `nable setup` (the menu) | takes the defaults (persona, then aws), connects nothing, exit 0: not a signal |
| `nable welcome`, bare `nable` at a terminal | prints the onboarding, connects nothing, exit 0 |
| `nable connect` | lists what it found and what to set up; connects nothing without a yes, exit 0 |
| `nable login` | no email to read, exit 1 (`nable license KEY` is the scripted way to activate) |
| `nable license` with no key | exit 1 |
| `nable guard approve ID` | refuses without `--as WHO`, exit 2: an approval names the person |
| `nable aws-cur` | without credentials, exit 1. With them it prints the `aws cloudformation deploy` command and, with no one to say yes, deploys nothing: exit 1 |
| `nable infra azure` (`gcp`, `snowflake`) | prompts take blanks; prints the setup with placeholders, exit 0 |

## Exit codes

Every command: 2 is a usage error (an unknown command or flag; argparse's
code), and 0 means it did its job. The rest, per command:

| Command | Codes |
|---|---|
| `scan` | 1 unexpected error, a bad `--regions` value, or no network; 3 credentials expired; 4 permission denied everywhere; 5 partial with nothing usable; 6 no credentials, or AWS rejected them; 7 the local AWS config does not resolve (profile, region); 130 Ctrl-C. A partial scan that still found something is 0 |
| `brief` | 1 an error, or `--deliver` and a channel did not take it (or none was named); 3 credentials expired; 6 nothing could be read. An empty brief is 0 |
| `why`, `ai-costs` | 1 nothing could be read |
| `commitments status` | 3 credentials expired; 4 every commitment read was denied (permission denied everywhere); 5 some reads failed, each listed as not read and never as zero; 6 no credentials, or AWS rejected them. With `--fail-within DAYS` the code is the gate's: 1 a commitment that was read ends within DAYS (this wins); 2 a commitment read failed, so it cannot check (no credentials included); otherwise 0. A `--regions` value that is not a region code is 2 |
| `ai-budget` | 2 a negative cap |
| `budget`, `budget refresh` | 1 the budgets could not be checked |
| `budget ci-gate` | without `--fail-on-breach`: 0 always (it reports). With it: 1 a budget is breached (spend at or past its critical percentage); 2 cannot check (the budget file is missing or invalid, a budget has no cost data for its period, or one could not be read). A breach wins over cannot-check |
| `org`, `learn`, `pack` | 1 the model or pack refused the change, and why on stderr |
| `doctor`, `finops-doctor` | 1 a check failed (warnings alone are 0) |
| `guard check` | 1 the policy could not be used as written (the verdict still prints, judged with the defaults it names); 2 no `--command`. allow, ask and deny are all 0: read `decision` |
| `guard verify-log` | 1 the ledger is broken or something was removed or rewritten (0 with `--reanchor`) |
| `guard reconcile` | 1 CloudTrail could not be read |
| `guard approve` | 2 no terminal and no `--as` |
| `guard install`, `guard uninstall` | 1 a settings file nable will not rewrite (not valid JSON, or comments it would drop), with the reason; nothing was changed |
| `uninstall` | 1 something could not be removed, and which |
| `setup aws --check-scope` | 1 nothing was checked (no credentials), or the key is missing permissions or has write access |
| `setup <provider>`, `setup aws`, `aws-cur` | 1 nothing was stored, connected or deployed |
| `upgrade` | 1 PyPI did not answer, `FINOPS_AIRGAP` is set and no version was named, the new release could not be cached (nothing was re-pinned), or a pin did not move |
| `license-status` | 1 the key is invalid or expired |
| `license`, `login` | 1 nothing was activated |
| `serve`, `credits` | 1 the feature is not in this package (`EXIT_NOT_IN_THIS_PACKAGE`); the reason on stderr, stdout empty |

`nable upgrade` moves every pin nable wrote: the CLI in this environment, the
`finops-mcp==X` argument in Claude Desktop's, Cursor's and Claude Code's MCP
configs, and each guard hook `nable guard install` pinned with uvx (by running
the new release's own `guard install`). An entry with no pin is left as it is.
`nable upgrade --help` lists them.

## Environment variables

Every `FINOPS_*` and `NABLE_*` name the package reads, and nothing else. The
table is generated from `src/finops/env_vars.py`
(`python scripts/headless_env_table.py --write`), and
tests/test_headless_env.py fails when the table, the list there and the source
disagree. Cloud credentials use each cloud's own names (`AWS_PROFILE`,
`AWS_ACCESS_KEY_ID`, `GOOGLE_APPLICATION_CREDENTIALS`, `AZURE_*`), and
providers theirs (`OPENAI_API_KEY`, `SLACK_WEBHOOK_URL`, `DATABASE_URL`).

Flags read 1 as on. Where a section says "any value", anything but 0, false,
no or off counts. Settings read from the environment win over
`nable.policy.yaml`; a credential stored with `nable setup` wins over the same
name exported (`nable doctor` says when).

<!-- env-table:start. Generated by scripts/headless_env_table.py from src/finops/env_vars.py; edit DOC there, then run it with --write. -->

#### Where nable keeps its state

| Variable | What it does |
|---|---|
| `FINOPS_DATA_DIR` | nable's data directory: the credential vault and its key file, the cost history database, the guard ledger and off switch, nable.policy.yaml and saved briefs (default ~/.finops). The AI budget and the response cache, which default to ~/.nable, move here too. |
| `FINOPS_PROFILE` | A named profile: its own vault, database and ledger under ~/.finops/profiles/NAME (`nable profile create NAME`, then `nable profile use NAME`). |
| `FINOPS_DB_PATH` | The SQLite cost history file (default DATA_DIR/finops.db). DATABASE_URL with a postgresql:// URL takes precedence. |
| `FINOPS_HOME` | Where alert state and ai_contracts.json live (default ~/.finops-mcp). |
| `FINOPS_ACCOUNTS_FILE` | The AWS accounts list `nable setup aws` writes (default ~/.finops-mcp/accounts.yaml). |
| `FINOPS_TAG_RULES` | Tag-to-team rules for cost attribution (default ~/.finops/tag_rules.yaml). |
| `FINOPS_ORG_DIR` | The org model directory (default: the repo's nable.org/ when there is one, else the data directory). |
| `FINOPS_POLICY_FILE` | The guard policy file (default DATA_DIR/nable.policy.yaml). Never read from the working directory. |
| `FINOPS_VAULT_KEY` | The vault's master key itself (a Fernet key, or 32 bytes base64), for a runner with no key file or keychain. |
| `FINOPS_VAULT_KEYCHAIN_ONLY` | 1: keep the vault's master key in the OS keychain only, with no key file beside the vault. |
| `FINOPS_NO_KEYRING` | 1: never touch the OS keychain (no unlock prompts); the vault key stays in a 0600 file. Set it on servers and CI runners. FINOPS_AIRGAP implies it. |
| `FINOPS_NO_AUDIT` | Any value: no local audit log of credential reads. |

#### Network, air-gap and privacy

| Variable | What it does |
|---|---|
| `FINOPS_AIRGAP` | 1 (anything but 0, false, no, off): connect to your cost providers only. Nothing to Slack, Teams or email, and no telemetry, update check, benchmarking, email signup, guard release probe or keychain; `nable upgrade` needs an explicit version. |
| `FINOPS_FIPS` | 1: require FIPS-validated OpenSSL; the MCP server exits when it is not active. |
| `FINOPS_GOVCLOUD` | With FINOPS_AIRGAP: require FIPS as well (GovCloud). |
| `NABLE_TELEMETRY` | 1: opt in to anonymous usage telemetry. Off unless set, and never sent from CI. |
| `NABLE_NO_TELEMETRY` | Any value but 0, false, no: telemetry off whatever else is set; also turns off the update check. |
| `NABLE_POSTHOG_KEY` | The telemetry project key, used only once telemetry is opted in. |
| `NABLE_NO_UPDATE_CHECK` | 1: do not ask PyPI whether a newer release exists. |
| `FINOPS_NO_UPDATE_CHECK` | The same as NABLE_NO_UPDATE_CHECK. |
| `NABLE_BENCHMARKING_ENABLED` | true: share anonymised benchmark figures. Off by default. |
| `NABLE_BENCHMARKING_ENDPOINT` | Where benchmark figures go when sharing is on. |
| `NABLE_BENCHMARKING_SALT` | The salt for anonymised benchmark identifiers. |
| `NABLE_NO_COST_EXPLORER` | 1: never call AWS Cost Explorer, which bills per request; what needs it says so. |
| `NABLE_NO_CLOUDWATCH_BILLING` | 1: do not read CloudWatch billing metrics. |
| `FINOPS_CLOUDWATCH_GETMETRICDATA` | 1: allow CloudWatch GetMetricData batching, which AWS bills; off by default. |

#### The guard and its policy

| Variable | What it does |
|---|---|
| `FINOPS_GUARD` | off: every guard hook lets each call through, unchecked and unrecorded (the same as `nable guard off`). |
| `FINOPS_GUARD_STRICT` | 1: also ask before reversible applies (terraform apply, helm upgrade). |
| `FINOPS_GUARD_TEAM` | The team whose budgets a priced change counts against, set where the agent runs. |
| `FINOPS_GUARD_ACCOUNT` | The 12-digit AWS account whose budgets a priced change counts against. |
| `FINOPS_GUARD_STOP_ON_BUDGET` | 1: a change over a cloud budget is denied; 0: asked. Overrides on_budget_breach for one session or CI run. |
| `FINOPS_GUARD_BUDGET_MAX_AGE_HOURS` | How old the budget spend figure may be before the guard stops using it (default 48). |
| `FINOPS_GUARD_PROD_PATTERNS` | Comma-separated regexes for what looks like production in a command; off turns the check off. |
| `FINOPS_GUARD_RELEASE_CHECK` | 0: `guard status` and `guard doctor` do not ask uvx whether a hook's pinned release can start. |
| `FINOPS_GUARD_BACKGROUND_REFRESH` | 0: the guard starts no background refresh of the files it reads (the Cursor usage cache, the budget figure). |
| `FINOPS_GUARD_AUTO_REFRESH_BUDGET` | 1: the background refresh also recomputes the budget spend figure when it is stale. |
| `FINOPS_POLICY_MAX_AUTO_USD` | A cost increase above this many $/month asks (default 500). Overrides max_auto_monthly_usd in the policy file. |
| `FINOPS_POLICY_ALLOWED_ACTIONS` | Comma-separated action types the policy allows (default: the reversible ones). |
| `FINOPS_POLICY_ON_BUDGET_BREACH` | ask or deny: what a change that would take a cloud budget over its limit gets (default ask). |
| `FINOPS_POLICY_VELOCITY_CAP_USD` | $/month of priced launches allowed per window before the guard asks (default 4 x the threshold; 0 off). |
| `FINOPS_POLICY_VELOCITY_WINDOW_MIN` | The velocity window in minutes (default 60). |
| `FINOPS_POLICY_LOOP_COUNT` | The same creation this many times in the loop window asks (default 3; below 2 off). |
| `FINOPS_POLICY_LOOP_WINDOW_MIN` | The loop window in minutes (default 10). |
| `FINOPS_PROTECTED_TAGS` | Comma-separated key=value tags that mark a resource nable never proposes to clean up (the defaults include env=prod and do-not-delete=true). |
| `FINOPS_REQUIRED_TAGS` | Tags every resource must carry, checked in Terraform state and tag audits. |
| `FINOPS_REMEDIATION_ENABLED` | true: let nable open remediation pull requests (off by default; remediation.open_prs in the policy file also turns it on). |
| `FINOPS_REMEDIATION_MODE` | propose (default) or auto. This build only proposes, whatever it says. |

#### Cost data, caches and AI spend

| Variable | What it does |
|---|---|
| `FINOPS_PROVIDER_TIMEOUT_S` | Seconds one provider fetch may take (default 90). |
| `FINOPS_AUDIT_TIMEOUT` | Seconds the full waste audit may run before it reports what finished (default 90). |
| `FINOPS_MAX_LOOKBACK_DAYS` | The furthest back a cost query may reach (default 365). |
| `FINOPS_ACTIVE_TTL` | Seconds the MCP server remembers which providers are connected (default 120). |
| `FINOPS_CACHE_DISABLED` | 1: no response cache; every query reads fresh data. |
| `FINOPS_CACHE_DISK_DISABLED` | 1: keep the cache in memory only (a read-only filesystem). |
| `FINOPS_CACHE_MAX_ENTRIES` | Entries the cache holds (default 512). |
| `FINOPS_CACHE_PERSIST_MIN_TTL` | Entries with at least this TTL in seconds are written to disk (default 3600). |
| `FINOPS_CACHE_TTL` | Default cache TTL in seconds (default 3600). |
| `FINOPS_COST_CACHE_TTL` | Cache TTL for billing data in seconds (default 43200). |
| `FINOPS_K8S_CACHE_TTL` | Cache TTL for Kubernetes data in seconds (default 300). |
| `FINOPS_AI_BUDGET_WARN_PCT` | The share of the AI budget at which `nable ai-budget` warns (default 0.80). |
| `FINOPS_AI_WINDOW_HOURS` | The rolling window `nable ai-budget` reports against (default 5 hours). |
| `FINOPS_AI_USD_PER_MTOK_IN` | Your input rate per million tokens, for the AI budget's estimate (default: a blend of list prices). |
| `FINOPS_AI_USD_PER_MTOK_OUT` | Your output rate per million tokens. |
| `FINOPS_AI_USD_PER_MTOK_CACHE_WRITE` | Your 5-minute cache-write rate per million tokens (default 1.25 x input). |
| `FINOPS_AI_USD_PER_MTOK_CACHE_WRITE_1H` | Your 1-hour cache-write rate (default 2 x input). |
| `FINOPS_AI_USD_PER_MTOK_CACHE_READ` | Your cache-read rate (default 0.1 x input). |
| `FINOPS_AI_CONTRACTS` | Your AI commitment contracts as a JSON array (else FINOPS_HOME/ai_contracts.json). |
| `FINOPS_CURSOR_API_URL` | The Cursor Admin API base URL the AI budget reads (default Cursor's own). |

#### Briefs, reports and email

| Variable | What it does |
|---|---|
| `NABLE_BRIEF_DELIVER` | Comma-separated channels `nable brief --deliver` pushes to: slack, teams, email. Nothing is pushed without --deliver. |
| `NABLE_BRIEF_EMAIL_TO` | Comma-separated addresses for the email channel. |
| `NABLE_BRIEF_URL` | A link to the full brief, added to what is pushed. |
| `NABLE_BRIEF_KEEP_DAYS` | Days saved briefs are kept (default 90). |
| `FINOPS_SMTP_HOST` | SMTP server for email delivery and digests. |
| `FINOPS_SMTP_PORT` | SMTP port (default 587). |
| `FINOPS_SMTP_USER` | SMTP user. |
| `FINOPS_SMTP_PASSWORD` | SMTP password. |
| `FINOPS_SMTP_FROM` | The From address (default the SMTP user). |
| `FINOPS_DIGEST_TO` | Where the weekly email digest goes. |
| `FINOPS_ALERT_DROPS` | Which cost drops alert: high (default), all or never. Spikes always alert. |
| `FINOPS_TICKET_PROVIDER` | The ticketing system to file into when several are connected (jira, linear, github). |
| `FINOPS_SNAPSHOT_CRON` | Reported by check_notification_config only: the schedule hosted nable runs. Nothing here runs on a timer. |
| `FINOPS_DIGEST_CRON` | As FINOPS_SNAPSHOT_CRON, for the daily digest. |
| `FINOPS_ANOMALY_CRON` | As FINOPS_SNAPSHOT_CRON, for the anomaly check. |
| `FINOPS_INVOICE_IMAP_HOST` | IMAP server to read emailed invoices from. |
| `FINOPS_INVOICE_IMAP_USER` | IMAP user. |
| `FINOPS_INVOICE_IMAP_PASSWORD` | IMAP password. |
| `FINOPS_INVOICE_FOLDER` | IMAP folder to read (default INBOX). |
| `FINOPS_INVOICE_SEARCH` | IMAP search for invoices (default UNSEEN SUBJECT invoice). |

#### The MCP server

| Variable | What it does |
|---|---|
| `FINOPS_ALL_TOOLS` | 1: advertise and register every tool, including the extras and the families no provider here uses. |
| `FINOPS_FLAT_TOOLS` | 1: list every tool this machine can use at once, instead of the tiered front door. |
| `NABLE_INTERNAL_TOOLS` | 1: nable staff tools. Not for customers. |
| `FINOPS_PERSONA` | How answers are pitched: engineer, finops, finance, platform or agent (concise, structured, for automated callers). |
| `FINOPS_MAX_RESPONSE_TOKENS` | Above this many tokens a tool response is summarised (default 6000). |
| `FINOPS_USD_PER_1K_TOKENS` | The rate used to estimate what a tool response costs your model to read (default 0.003). |
| `FINOPS_API_KEY` | The caller's nbl_ key in shared Postgres mode with FINOPS_REQUIRE_AUTH=1. |
| `FINOPS_REQUIRE_AUTH` | 1: in shared Postgres mode, every tool call needs a FINOPS_API_KEY with a role. |
| `FINOPS_DEBUG` | 1: full tracebacks and analyzer logs instead of one-line errors. |

#### Clouds, packs and integrations

| Variable | What it does |
|---|---|
| `FINOPS_ORG_ROLE_NAME` | The role assumed in each AWS Organizations member account (default FinOpsReadOnly). |
| `FINOPS_ORG_MAX_ACCOUNTS` | Most member accounts read in one org sweep (default 200). |
| `FINOPS_MAX_ROLE_ARNS` | Most cross-account role ARNs one AWS connector takes (default 50). |
| `NABLE_K8S_TEAM_LABEL` | The Kubernetes label that names a workload's team (default team). |
| `NABLE_K8S_IDLE_THRESHOLD` | Utilisation below which a Kubernetes node counts as idle (default 0.1). |
| `NABLE_OPENCOST_URL` | Your OpenCost API URL (OPENCOST_URL works too). |
| `NABLE_CFN_TEMPLATE_URL` | The read-only key CloudFormation template the one-click link opens (default nable's published one). |
| `NABLE_PACK_REGISTRY` | The pack registry index URL (packs.registry in the policy file, or --registry, also set it). |
| `NABLE_PACK_KEY_PASSPHRASE` | The passphrase for an encrypted pack signing key, where there is no terminal to ask at. |
| `NABLE_COST_GATE_USD` | The PR-comment webhook flags a change above this many $/month (default 500). |
| `NABLE_COMMENT_TAG` | The marker the PR-comment webhook finds its own comment by (default nable-cost). |
| `NABLE_CRITIC_LLM` | 1: let the recommendation critic call a model (ANTHROPIC_API_KEY); off by default, and never in `nable brief`. |
| `NABLE_CRITIC_MODEL` | The model the critic calls when it is on. |

#### License and account

| Variable | What it does |
|---|---|
| `FINOPS_LICENSE_KEY` | A license key (FINOPS-2-...). Set in the environment, it wins over the one stored with `nable license`. |
| `FINOPS_ACCOUNT_BASE` | The account service `nable login` uses (default https://getnable.com). |
| `FINOPS_LICENSE_PRIVATE_KEY` | Issuer side: signs license keys. Not needed to use nable. |
| `FINOPS_LICENSE_SECRET` | The HMAC secret of the retired FINOPS-1- keys. Not needed for FINOPS-2- keys. |

#### Demo, hosted and test hooks

| Variable | What it does |
|---|---|
| `FINOPS_DEMO` | 1: answer from the StreamCo sample until a real provider is connected. |
| `FINOPS_DEMO_MODE` | The same as FINOPS_DEMO. |
| `FINOPS_DEMO_FORCE` | 1: stay on the sample even with a real provider connected. |
| `FINOPS_DASHBOARD_PASSWORD` | The hosted dashboard's password; `serve --demo` sets it to off. |
| `FINOPS_CONTROL_PLANE_SECRET` | Set on a hosted instance, with FINOPS_INSTANCE_ID, to mark it managed. Not for local use. |
| `FINOPS_INSTANCE_ID` | A hosted instance's id; see FINOPS_CONTROL_PLANE_SECRET. |
| `FINOPS_TEST_ONBOARDING_TOTAL` | A test hook for the interactive smoke tests. Never set it. |

<!-- env-table:end -->

## Air-gap mode

`FINOPS_AIRGAP=1` limits nable's own traffic to the cost providers you
connect: AWS, Azure, GCP and the APIs whose keys you gave it.

It turns off (each one checked headless or by a test):

- telemetry, and the one-time consent question;
- the PyPI update check, and the guard's uvx release probe in `guard status`
  and `guard doctor`;
- the OS keychain (the vault key stays in its 0600 file; `FINOPS_NO_KEYRING`
  is implied);
- benchmark sharing and the email signup at the end of setup;
- everything sent to Slack, Teams or email. The senders themselves refuse, so
  every path is covered: `send_report_now` returns `skipped`,
  `send_digest_now` and the email digest send nothing, and `nable brief
  --deliver` sends nothing and exits 1 with "FINOPS_AIRGAP is set, and air-gap
  allows traffic to your cost providers only";
- `nable upgrade` with no version: it does not ask PyPI and exits 1. `nable
  upgrade 0.12.0` works, through the package index pip and uv are configured
  for (`PIP_INDEX_URL`, `UV_INDEX_URL`);
- with `FINOPS_GOVCLOUD`, it also requires FIPS-validated OpenSSL.

It does not stop the calls you ask for to your providers. AWS Cost Explorer is
an AWS API billed per request; `NABLE_NO_COST_EXPLORER=1` forbids it.
`finops-pr-webhook` exists to post to GitHub and does not read the switch.
`nable doctor --json` reports `"airgap": true` in its Environment check.

## Running from cron

```cron
CRON_TZ=UTC
HOME=/home/finops
FINOPS_DATA_DIR=/var/lib/nable
FINOPS_NO_KEYRING=1
AWS_PROFILE=nable-readonly
# 06:13 daily: refresh the budget figure the guard checks priced changes against
13 6 * * * /home/finops/.local/bin/nable budget refresh --json >> /var/log/nable/budget.log 2>&1
# 06:43 daily: the brief, pushed to the channels in NABLE_BRIEF_DELIVER
43 6 * * * flock -n /var/lib/nable/.brief.lock /home/finops/.local/bin/nable brief --json --deliver >> /var/log/nable/brief.log 2>&1
```

- **Absolute paths.** cron's `PATH` is `/usr/bin:/bin`; give nable's full path
  (`which nable`), and set every variable the job needs in the crontab: cron
  reads no shell profile.
- **One data directory, shared safely.** Jobs that share `FINOPS_DATA_DIR`
  (or a job and an editor's MCP server) can start in the same second on a
  directory nable has never used. Creating the database schema takes an
  exclusive lock on `.finops.db.lock` beside it, so two first runs do not both
  create tables; the database runs in WAL mode with a 15-second busy timeout,
  so writers wait for each other instead of failing; the vault's master key is
  claimed with an atomic link, so concurrent first runs agree on one key and
  every stored credential stays readable; the guard ledger takes its lock for
  at most a moment and counts a verdict it could not record (`guard doctor`
  shows the count) rather than hang. The database file is created 0600 before
  SQLite opens it, so a second run never finds it readable by other users.
  Verified: eight `nable budget refresh --json` started together on one fresh
  directory all exited 0 with one JSON document each, and so did eight `nable
  doctor --json` (four rounds of eight).
- **Overlapping runs of the same job.** nable does not stop a second `brief`
  starting while the first still scans; wrap a long job in `flock -n` as above.
- **Exit codes are the alert.** `brief --deliver` exits 1 when a channel did
  not take the brief; `budget refresh` exits 1 when budgets could not be
  checked. Mail or page on non-zero.
- **Nothing here runs on a timer by itself.** The open package has no
  scheduler: `FINOPS_SNAPSHOT_CRON` and its siblings only describe hosted
  nable's schedule. Cost history comes from what you run.

## What is hosted only

These are part of hosted nable, not this package. Here each one says so and
does nothing:

- the web dashboard: `nable serve` exits 1 (`serve --demo` too), and the
  container image's default command is `serve`;
- managed-AI credits: `nable credits` exits 1;
- the scheduler that takes daily snapshots, runs anomaly checks and sends
  digests on a timer: nothing in this package runs unasked;
- the two-way Slack bot; `nable setup slack` sets up the webhook that reports
  you send yourself (`send_report_now`, `brief --deliver`) use;
- SSO: `nable setup sso` stores an OIDC configuration for hosted nable;
- reading the AWS billing export straight from S3 (the CUR reader that
  replaces Cost Explorer in scheduled jobs).

## MCP over stdio for headless agents

`nable` with no arguments and stdin not a terminal is the MCP server, on
stdio: the agent starts it as a subprocess, speaks JSON-RPC on its stdin and
stdout, and the server exits when stdin closes. Its banner and logs go to
stderr, never into the protocol stream. With the Python MCP SDK:

```python
import asyncio
import os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    server = StdioServerParameters(command="nable", args=[],
                                   env={**os.environ, "FINOPS_NO_KEYRING": "1",
                                        "AWS_PROFILE": "nable-readonly"})
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            verdict = await session.call_tool("check_action_policy",
                                              {"action_type": "terminate_instance"})

asyncio.run(main())
```

Run on a fresh `HOME` with no credentials, that initialized in protocol
2025-11-25, listed 10 tools and got `"gate": "escalate"` back for terminating
an instance, in 2.2 seconds. The tool list is tiered: the front door first,
more once a provider is connected. `FINOPS_FLAT_TOOLS=1` lists every tool this
machine can use at once, `FINOPS_ALL_TOOLS=1` every tool, and
`FINOPS_PERSONA=agent` makes answers concise and structured for an automated
caller.

No HTTP transport ships in this package. The server speaks stdio only, nothing
in it listens on a port, and `nable serve` exits 1. An agent on another
machine reaches nable the way it reaches any stdio server, by starting it
there, for example as `ssh build-host nable` as the server command.

## CI recipes

[examples/github-actions](../examples/github-actions/README.md) has four
workflows that run this CLI: a budget gate (`nable budget ci-gate
--fail-on-breach --json`), a daily `nable scan --json` kept as an artifact, a
guard policy check (`nable guard check --json` over the repo's
`nable.policy.yaml` and the commands it should judge), and a weekly
`nable commitments status --json --fail-within 30` run through `uvx`, with a
read-only role assumed through GitHub OIDC, kept as an artifact (exit 1 when a
commitment ends within 30 days, 2 when one could not be read). Each states its
exit codes and what it needs, and fails with a reason rather than passing on
missing data or credentials.
