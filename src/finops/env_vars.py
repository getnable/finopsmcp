"""Every FINOPS_* and NABLE_* environment variable this package reads, by name.

Headless use is configured by environment alone, and a misspelled variable is
silent: FINOPS_AIRGAPP=1 turns nothing on and says nothing. `nable doctor`
checks the environment against this list and names what it does not read,
with the closest name it does. tests/test_headless_env.py holds the list to
the source: a name read anywhere in src/finops must be here, and a name here
must be read somewhere, so it cannot drift either way. DOC below says what
each one does; scripts/headless_env_table.py writes docs/HEADLESS.md's table
from it, and the same test file holds the doc to KNOWN.

Stdlib only: the doctor imports it on every run.
"""
from __future__ import annotations

import difflib
from collections.abc import Mapping

PREFIXES: tuple[str, ...] = ("FINOPS_", "NABLE_")

KNOWN: frozenset[str] = frozenset({
    'FINOPS_ACCOUNTS_FILE', 'FINOPS_ACCOUNT_BASE', 'FINOPS_ACTIVE_TTL', 'FINOPS_AIRGAP',
    'FINOPS_AI_BUDGET_WARN_PCT', 'FINOPS_AI_CONTRACTS', 'FINOPS_AI_USD_PER_MTOK_CACHE_READ',
    'FINOPS_AI_USD_PER_MTOK_CACHE_WRITE', 'FINOPS_AI_USD_PER_MTOK_CACHE_WRITE_1H',
    'FINOPS_AI_USD_PER_MTOK_IN', 'FINOPS_AI_USD_PER_MTOK_OUT', 'FINOPS_AI_WINDOW_HOURS',
    'FINOPS_ALERT_DROPS', 'FINOPS_ALL_TOOLS', 'FINOPS_ANOMALY_CRON', 'FINOPS_API_KEY',
    'FINOPS_AUDIT_TIMEOUT', 'FINOPS_CACHE_DISABLED', 'FINOPS_CACHE_DISK_DISABLED',
    'FINOPS_CACHE_MAX_ENTRIES', 'FINOPS_CACHE_PERSIST_MIN_TTL', 'FINOPS_CACHE_TTL',
    'FINOPS_CLOUDWATCH_GETMETRICDATA', 'FINOPS_CONTROL_PLANE_SECRET',
    'FINOPS_COST_CACHE_TTL', 'FINOPS_CURSOR_API_URL', 'FINOPS_DASHBOARD_PASSWORD',
    'FINOPS_DATA_DIR', 'FINOPS_DB_PATH', 'FINOPS_DEBUG', 'FINOPS_DEMO', 'FINOPS_DEMO_FORCE',
    'FINOPS_DEMO_MODE', 'FINOPS_DIGEST_CRON', 'FINOPS_DIGEST_TO', 'FINOPS_FIPS',
    'FINOPS_FLAT_TOOLS', 'FINOPS_GOVCLOUD', 'FINOPS_GUARD', 'FINOPS_GUARD_ACCOUNT',
    'FINOPS_GUARD_AUTO_REFRESH_BUDGET', 'FINOPS_GUARD_BACKGROUND_REFRESH',
    'FINOPS_GUARD_BUDGET_MAX_AGE_HOURS', 'FINOPS_GUARD_PERSON', 'FINOPS_GUARD_PROD_PATTERNS',
    'FINOPS_GUARD_RELEASE_CHECK', 'FINOPS_GUARD_STOP_ON_BUDGET', 'FINOPS_GUARD_STRICT', 'FINOPS_GUARD_TEAM',
    'FINOPS_HOME', 'FINOPS_INSTANCE_ID', 'FINOPS_INVOICE_FOLDER',
    'FINOPS_INVOICE_IMAP_HOST', 'FINOPS_INVOICE_IMAP_PASSWORD', 'FINOPS_INVOICE_IMAP_USER',
    'FINOPS_INVOICE_SEARCH', 'FINOPS_K8S_CACHE_TTL', 'FINOPS_LICENSE_KEY',
    'FINOPS_LICENSE_PRIVATE_KEY', 'FINOPS_LICENSE_SECRET', 'FINOPS_MAX_LOOKBACK_DAYS',
    'FINOPS_MAX_RESPONSE_TOKENS', 'FINOPS_MAX_ROLE_ARNS', 'FINOPS_NO_AUDIT',
    'FINOPS_NO_KEYRING', 'FINOPS_NO_UPDATE_CHECK', 'FINOPS_ORG_DIR',
    'FINOPS_ORG_MAX_ACCOUNTS', 'FINOPS_ORG_ROLE_NAME', 'FINOPS_PERSONA',
    'FINOPS_POLICY_ALLOWED_ACTIONS', 'FINOPS_POLICY_FILE', 'FINOPS_POLICY_LOOP_COUNT',
    'FINOPS_POLICY_LOOP_WINDOW_MIN', 'FINOPS_POLICY_MAX_AUTO_USD',
    'FINOPS_POLICY_ON_BUDGET_BREACH', 'FINOPS_POLICY_VELOCITY_CAP_USD',
    'FINOPS_POLICY_VELOCITY_WINDOW_MIN', 'FINOPS_PROFILE', 'FINOPS_PROTECTED_TAGS',
    'FINOPS_PROVIDER_TIMEOUT_S', 'FINOPS_REMEDIATION_ENABLED', 'FINOPS_REMEDIATION_MODE',
    'FINOPS_REQUIRED_TAGS', 'FINOPS_REQUIRE_AUTH', 'FINOPS_SMTP_FROM', 'FINOPS_SMTP_HOST',
    'FINOPS_SMTP_PASSWORD', 'FINOPS_SMTP_PORT', 'FINOPS_SMTP_USER', 'FINOPS_SNAPSHOT_CRON',
    'FINOPS_TAG_RULES', 'FINOPS_TEST_ONBOARDING_TOTAL', 'FINOPS_TICKET_PROVIDER',
    'FINOPS_USD_PER_1K_TOKENS', 'FINOPS_VAULT_KEY', 'FINOPS_VAULT_KEYCHAIN_ONLY',
    'NABLE_BENCHMARKING_ENABLED', 'NABLE_BENCHMARKING_ENDPOINT', 'NABLE_BENCHMARKING_SALT',
    'NABLE_BRIEF_DELIVER', 'NABLE_BRIEF_EMAIL_TO', 'NABLE_BRIEF_KEEP_DAYS',
    'NABLE_BRIEF_URL', 'NABLE_CFN_TEMPLATE_URL', 'NABLE_COMMENT_TAG', 'NABLE_COST_GATE_USD',
    'NABLE_CRITIC_LLM', 'NABLE_CRITIC_MODEL', 'NABLE_INTERNAL_TOOLS',
    'NABLE_K8S_IDLE_THRESHOLD', 'NABLE_K8S_TEAM_LABEL', 'NABLE_NO_CLOUDWATCH_BILLING',
    'NABLE_NO_COST_EXPLORER', 'NABLE_NO_TELEMETRY', 'NABLE_NO_UPDATE_CHECK',
    'NABLE_OPENCOST_URL', 'NABLE_PACK_KEY_PASSPHRASE', 'NABLE_PACK_REGISTRY',
    'NABLE_POSTHOG_KEY', 'NABLE_TELEMETRY', 'NABLE_WORKSPACE_KEY',
})

# Names in this repo's docker-compose files and deploy/Slack docs that this
# package does not read. Docker Compose substitutes the first group into the
# compose file itself; the rest are named for the dashboard, its scheduler,
# SSO and the Slack bot, none of which ship in this package. Set here, they
# change nothing, and the doctor says so rather than guessing a typo.
NOT_READ_HERE: dict[str, str] = {
    **dict.fromkeys(("FINOPS_BIND_HOST", "FINOPS_DATABASE_URL", "NABLE_VERSION"),
                    "substituted by Docker Compose into docker-compose.yml; nable never reads it"),
    **dict.fromkeys((
        "FINOPS_ENABLE_SCHEDULER", "FINOPS_DASHBOARD_ALLOWED_HOSTS",
        "FINOPS_MANAGED_AI_BUDGET_USD", "FINOPS_SSO_ISSUER", "FINOPS_SSO_CLIENT_ID",
        "FINOPS_SSO_CLIENT_SECRET", "FINOPS_SSO_REDIRECT_URI", "FINOPS_SSO_ALLOWED_DOMAINS",
        "FINOPS_ALLOW_SELF_APPROVE", "FINOPS_MAX_TOOL_CALLS", "FINOPS_QUERY_TIMEOUT",
        "FINOPS_RCA_TIMEOUT", "FINOPS_SLACK_ALLOW_REMEDIATION", "FINOPS_SLACK_MODEL",
        "FINOPS_SLACK_MODEL_SIMPLE", "FINOPS_TF_DIR", "FINOPS_CLEANUP_ENABLED"),
        "named in this repo's compose files or deploy/Slack docs; the open-source "
        "package does not read it"),
}


# What each name in KNOWN does, for docs/HEADLESS.md: (section, one line).
# scripts/headless_env_table.py writes the doc's table from this, and
# tests/test_headless_env.py holds it to KNOWN and the doc to it, so a name
# added to KNOWN without a line here fails the suite.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("state", "Where nable keeps its state"),
    ("network", "Network, air-gap and privacy"),
    ("guard", "The guard and its policy"),
    ("cost", "Cost data, caches and AI spend"),
    ("delivery", "Briefs, reports and email"),
    ("mcp", "The MCP server"),
    ("cloud", "Clouds, packs and integrations"),
    ("account", "License and account"),
    ("demo", "Demo, hosted and test hooks"),
)

DOC: dict[str, tuple[str, str]] = {
    # ── state ──
    "FINOPS_DATA_DIR": ("state", ("nable's data directory: the credential vault and its key file, "
                        "the cost history database, the guard ledger and off switch, "
                        "nable.policy.yaml and saved briefs (default ~/.finops). The AI budget "
                        "and the response cache, which default to ~/.nable, move here too. An "
                        "absolute path; a leading ~ expands. A relative path is refused: nable "
                        "uses the default, a command says so once, and `nable guard doctor` "
                        "names both.")),
    "FINOPS_PROFILE": ("state", ("A named profile: its own vault, database and ledger under "
                       "~/.finops/profiles/NAME (`nable profile create NAME`, then "
                       "`nable profile use NAME`).")),
    "FINOPS_DB_PATH": ("state", ("The SQLite cost history file (default DATA_DIR/finops.db). "
                       "DATABASE_URL with a postgresql:// URL takes precedence.")),
    "FINOPS_HOME": ("state", ("Where alert state and ai_contracts.json live (default "
                    "~/.finops-mcp).")),
    "FINOPS_ACCOUNTS_FILE": ("state", ("The AWS accounts list `nable setup aws` writes (default "
                             "~/.finops-mcp/accounts.yaml).")),
    "FINOPS_TAG_RULES": ("state", ("Tag-to-team rules for cost attribution (default "
                         "~/.finops/tag_rules.yaml).")),
    "FINOPS_ORG_DIR": ("state", ("The org model directory (default: the repo's nable.org/ when "
                       "there is one, else the data directory).")),
    "FINOPS_POLICY_FILE": ("state", ("The guard policy file (default DATA_DIR/nable.policy.yaml). "
                           "Never read from the working directory.")),
    "FINOPS_VAULT_KEY": ("state", ("The vault's master key itself (a Fernet key, or 32 bytes "
                         "base64), for a runner with no key file or keychain.")),
    "FINOPS_VAULT_KEYCHAIN_ONLY": ("state", ("1: keep the vault's master key in the OS keychain "
                                   "only, with no key file beside the vault.")),
    "FINOPS_NO_KEYRING": ("state", ("1: never touch the OS keychain (no unlock prompts); the vault "
                          "key stays in a 0600 file. Set it on servers and CI runners. "
                          "FINOPS_AIRGAP implies it.")),
    "FINOPS_NO_AUDIT": ("state", ("Any value: no local audit log of credential reads.")),
    # ── network ──
    "FINOPS_AIRGAP": ("network", ("1 (anything but 0, false, no, off): connect to your cost "
                      "providers only. Nothing to Slack, Teams or email, and no telemetry, "
                      "update check, benchmarking, email signup, guard release probe or "
                      "keychain; `nable upgrade` needs an explicit version.")),
    "FINOPS_FIPS": ("network", ("1: require FIPS-validated OpenSSL; the MCP server exits when it "
                    "is not active.")),
    "FINOPS_GOVCLOUD": ("network", ("With FINOPS_AIRGAP: require FIPS as well (GovCloud).")),
    "NABLE_TELEMETRY": ("network", ("1: opt in to anonymous usage telemetry. Off unless set, and "
                        "never sent from CI.")),
    "NABLE_NO_TELEMETRY": ("network", ("Any value but 0, false, no: telemetry off whatever else "
                           "is set; also turns off the update check.")),
    "NABLE_POSTHOG_KEY": ("network", ("The telemetry project key, used only once telemetry is "
                          "opted in.")),
    "NABLE_NO_UPDATE_CHECK": ("network", ("1: do not ask PyPI whether a newer release exists.")),
    "FINOPS_NO_UPDATE_CHECK": ("network", ("The same as NABLE_NO_UPDATE_CHECK.")),
    "NABLE_BENCHMARKING_ENABLED": ("network", ("true: share anonymised benchmark figures. Off "
                                   "by default.")),
    "NABLE_BENCHMARKING_ENDPOINT": ("network", ("Where benchmark figures go when sharing is on.")),
    "NABLE_BENCHMARKING_SALT": ("network", ("The salt for anonymised benchmark identifiers.")),
    "NABLE_NO_COST_EXPLORER": ("network", ("1: never call AWS Cost Explorer, which bills per "
                               "request; what needs it says so.")),
    "NABLE_NO_CLOUDWATCH_BILLING": ("network", ("1: do not read CloudWatch billing metrics.")),
    "FINOPS_CLOUDWATCH_GETMETRICDATA": ("network", ("1: allow CloudWatch GetMetricData "
                                        "batching, which AWS bills; off by default.")),
    # ── guard ──
    "FINOPS_GUARD": ("guard", ("off: every guard hook lets each call through, unchecked and "
                     "unrecorded (the same as `nable guard off`).")),
    "FINOPS_GUARD_STRICT": ("guard", ("1: also ask before reversible applies (terraform apply, "
                            "helm upgrade).")),
    "FINOPS_GUARD_TEAM": ("guard", ("The team whose budgets a priced change counts against, set "
                          "where the agent runs.")),
    "FINOPS_GUARD_ACCOUNT": ("guard", ("The 12-digit AWS account whose budgets a priced change "
                             "counts against.")),
    "FINOPS_GUARD_PERSON": ("guard", ("Whose org model AI budget this machine's agents are held "
                            "to (an email or github:LOGIN), set where the agent runs; else "
                            "git's user.email.")),
    "FINOPS_GUARD_STOP_ON_BUDGET": ("guard", ("1: a change over a cloud budget is denied; 0: "
                                    "asked. Overrides on_budget_breach for one session or CI "
                                    "run.")),
    "FINOPS_GUARD_BUDGET_MAX_AGE_HOURS": ("guard", ("How old the budget spend figure may be "
                                          "before the guard stops using it (default 48).")),
    "FINOPS_GUARD_PROD_PATTERNS": ("guard", ("Comma-separated regexes for what looks like "
                                   "production in a command; off turns the check off.")),
    "FINOPS_GUARD_RELEASE_CHECK": ("guard", ("0: `guard status` and `guard doctor` do not ask "
                                   "uvx whether a hook's pinned release can start.")),
    "FINOPS_GUARD_BACKGROUND_REFRESH": ("guard", ("0: the guard starts no background refresh "
                                        "of the files it reads (the Cursor usage cache, the "
                                        "budget figure).")),
    "FINOPS_GUARD_AUTO_REFRESH_BUDGET": ("guard", ("1: the background refresh also recomputes "
                                         "the budget spend figure when it is stale.")),
    "FINOPS_POLICY_MAX_AUTO_USD": ("guard", ("A cost increase above this many $/month asks "
                                   "(default 500). Overrides max_auto_monthly_usd in the "
                                   "policy file.")),
    "FINOPS_POLICY_ALLOWED_ACTIONS": ("guard", ("Comma-separated action types the policy "
                                      "allows (default: the reversible ones).")),
    "FINOPS_POLICY_ON_BUDGET_BREACH": ("guard", ("ask or deny: what a change that would take a "
                                       "cloud budget over its limit gets (default ask).")),
    "FINOPS_POLICY_VELOCITY_CAP_USD": ("guard", ("$/month of priced launches allowed per "
                                       "window before the guard asks (default 4 x the "
                                       "threshold; 0 off).")),
    "FINOPS_POLICY_VELOCITY_WINDOW_MIN": ("guard", ("The velocity window in minutes (default "
                                          "60).")),
    "FINOPS_POLICY_LOOP_COUNT": ("guard", ("The same creation this many times in the loop "
                                 "window asks (default 3; below 2 off).")),
    "FINOPS_POLICY_LOOP_WINDOW_MIN": ("guard", ("The loop window in minutes (default 10).")),
    "FINOPS_PROTECTED_TAGS": ("guard", ("Comma-separated key=value tags that mark a resource "
                              "nable never proposes to clean up (the defaults include "
                              "env=prod and do-not-delete=true).")),
    "FINOPS_REQUIRED_TAGS": ("guard", ("Tags every resource must carry, checked in Terraform "
                             "state and tag audits.")),
    "FINOPS_REMEDIATION_ENABLED": ("guard", ("true: let nable open remediation pull requests "
                                   "(off by default; remediation.open_prs in the policy file "
                                   "also turns it on).")),
    "FINOPS_REMEDIATION_MODE": ("guard", ("propose (default) or auto. This build only "
                                "proposes, whatever it says.")),
    # ── cost ──
    "FINOPS_PROVIDER_TIMEOUT_S": ("cost", ("Seconds one provider fetch may take (default 90).")),
    "FINOPS_AUDIT_TIMEOUT": ("cost", ("Seconds the full waste audit may run before it reports "
                             "what finished (default 90).")),
    "FINOPS_MAX_LOOKBACK_DAYS": ("cost", ("The furthest back a cost query may reach (default "
                                 "365).")),
    "FINOPS_ACTIVE_TTL": ("cost", ("Seconds the MCP server remembers which providers are "
                          "connected (default 120).")),
    "FINOPS_CACHE_DISABLED": ("cost", ("1: no response cache; every query reads fresh data.")),
    "FINOPS_CACHE_DISK_DISABLED": ("cost", ("1: keep the cache in memory only (a read-only "
                                   "filesystem).")),
    "FINOPS_CACHE_MAX_ENTRIES": ("cost", ("Entries the cache holds (default 512).")),
    "FINOPS_CACHE_PERSIST_MIN_TTL": ("cost", ("Entries with at least this TTL in seconds are "
                                     "written to disk (default 3600).")),
    "FINOPS_CACHE_TTL": ("cost", ("Default cache TTL in seconds (default 3600).")),
    "FINOPS_COST_CACHE_TTL": ("cost", ("Cache TTL for billing data in seconds (default "
                              "43200).")),
    "FINOPS_K8S_CACHE_TTL": ("cost", ("Cache TTL for Kubernetes data in seconds (default 300).")),
    "FINOPS_AI_BUDGET_WARN_PCT": ("cost", ("The share of the AI budget at which `nable "
                                  "ai-budget` warns (default 0.80).")),
    "FINOPS_AI_WINDOW_HOURS": ("cost", ("The rolling window `nable ai-budget` reports against "
                               "(default 5 hours).")),
    "FINOPS_AI_USD_PER_MTOK_IN": ("cost", ("Your input rate per million tokens, for the AI "
                                  "budget's estimate (default: a blend of list prices).")),
    "FINOPS_AI_USD_PER_MTOK_OUT": ("cost", ("Your output rate per million tokens.")),
    "FINOPS_AI_USD_PER_MTOK_CACHE_WRITE": ("cost", ("Your 5-minute cache-write rate per "
                                           "million tokens (default 1.25 x input).")),
    "FINOPS_AI_USD_PER_MTOK_CACHE_WRITE_1H": ("cost", ("Your 1-hour cache-write rate (default "
                                              "2 x input).")),
    "FINOPS_AI_USD_PER_MTOK_CACHE_READ": ("cost", ("Your cache-read rate (default 0.1 x "
                                          "input).")),
    "FINOPS_AI_CONTRACTS": ("cost", ("Your AI commitment contracts as a JSON array (else "
                            "FINOPS_HOME/ai_contracts.json).")),
    "FINOPS_CURSOR_API_URL": ("cost", ("The Cursor Admin API base URL the AI budget reads "
                              "(default Cursor's own).")),
    # ── delivery ──
    "NABLE_BRIEF_DELIVER": ("delivery", ("Comma-separated channels `nable brief --deliver` "
                            "pushes to: slack, teams, email. Nothing is pushed without "
                            "--deliver.")),
    "NABLE_BRIEF_EMAIL_TO": ("delivery", ("Comma-separated addresses for the email channel.")),
    "NABLE_BRIEF_URL": ("delivery", ("A link to the full brief, added to what is pushed.")),
    "NABLE_BRIEF_KEEP_DAYS": ("delivery", ("Days saved briefs are kept (default 90).")),
    "FINOPS_SMTP_HOST": ("delivery", ("SMTP server for email delivery and digests.")),
    "FINOPS_SMTP_PORT": ("delivery", ("SMTP port (default 587).")),
    "FINOPS_SMTP_USER": ("delivery", ("SMTP user.")),
    "FINOPS_SMTP_PASSWORD": ("delivery", ("SMTP password.")),
    "FINOPS_SMTP_FROM": ("delivery", ("The From address (default the SMTP user).")),
    "FINOPS_DIGEST_TO": ("delivery", ("Where the weekly email digest goes.")),
    "FINOPS_ALERT_DROPS": ("delivery", ("Which cost drops alert: high (default), all or never. "
                           "Spikes always alert.")),
    "FINOPS_TICKET_PROVIDER": ("delivery", ("The ticketing system to file into when several "
                               "are connected (jira, linear, github).")),
    "FINOPS_SNAPSHOT_CRON": ("delivery", ("Reported by check_notification_config only: the "
                             "schedule hosted nable runs. Nothing here runs on a timer.")),
    "FINOPS_DIGEST_CRON": ("delivery", ("As FINOPS_SNAPSHOT_CRON, for the daily digest.")),
    "FINOPS_ANOMALY_CRON": ("delivery", ("As FINOPS_SNAPSHOT_CRON, for the anomaly check.")),
    "FINOPS_INVOICE_IMAP_HOST": ("delivery", ("IMAP server to read emailed invoices from.")),
    "FINOPS_INVOICE_IMAP_USER": ("delivery", ("IMAP user.")),
    "FINOPS_INVOICE_IMAP_PASSWORD": ("delivery", ("IMAP password.")),
    "FINOPS_INVOICE_FOLDER": ("delivery", ("IMAP folder to read (default INBOX).")),
    "FINOPS_INVOICE_SEARCH": ("delivery", ("IMAP search for invoices (default UNSEEN SUBJECT "
                              "invoice).")),
    # ── mcp ──
    "FINOPS_ALL_TOOLS": ("mcp", ("1: advertise and register every tool, including the extras "
                         "and the families no provider here uses.")),
    "FINOPS_FLAT_TOOLS": ("mcp", ("1: list every tool this machine can use at once, instead of "
                          "the tiered front door.")),
    "NABLE_INTERNAL_TOOLS": ("mcp", ("1: nable staff tools. Not for customers.")),
    "FINOPS_PERSONA": ("mcp", ("How answers are pitched: engineer, finops, finance, platform "
                       "or agent (concise, structured, for automated callers).")),
    "FINOPS_MAX_RESPONSE_TOKENS": ("mcp", ("Above this many tokens a tool response is "
                                   "summarised (default 6000).")),
    "FINOPS_USD_PER_1K_TOKENS": ("mcp", ("The rate used to estimate what a tool response costs "
                                 "your model to read (default 0.003).")),
    "FINOPS_API_KEY": ("mcp", ("The caller's nbl_ key in shared Postgres mode with "
                       "FINOPS_REQUIRE_AUTH=1.")),
    "FINOPS_REQUIRE_AUTH": ("mcp", ("1: in shared Postgres mode, every tool call needs a "
                            "FINOPS_API_KEY with a role.")),
    "FINOPS_DEBUG": ("mcp", ("1: full tracebacks and analyzer logs instead of one-line errors.")),
    # ── cloud ──
    "FINOPS_ORG_ROLE_NAME": ("cloud", ("The role assumed in each AWS Organizations member "
                             "account (default FinOpsReadOnly).")),
    "FINOPS_ORG_MAX_ACCOUNTS": ("cloud", ("Most member accounts read in one org sweep "
                                "(default 200).")),
    "FINOPS_MAX_ROLE_ARNS": ("cloud", ("Most cross-account role ARNs one AWS connector takes "
                             "(default 50).")),
    "NABLE_K8S_TEAM_LABEL": ("cloud", ("The Kubernetes label that names a workload's team "
                             "(default team).")),
    "NABLE_K8S_IDLE_THRESHOLD": ("cloud", ("Utilisation below which a Kubernetes node "
                                 "counts as idle (default 0.1).")),
    "NABLE_OPENCOST_URL": ("cloud", ("Your OpenCost API URL (OPENCOST_URL works too).")),
    "NABLE_CFN_TEMPLATE_URL": ("cloud", ("The read-only key CloudFormation template the "
                               "one-click link opens (default nable's published one).")),
    "NABLE_PACK_REGISTRY": ("cloud", ("The pack registry index URL (packs.registry in the "
                            "policy file, or --registry, also set it).")),
    "NABLE_PACK_KEY_PASSPHRASE": ("cloud", ("The passphrase for an encrypted pack signing key, "
                                  "where there is no terminal to ask at.")),
    "NABLE_COST_GATE_USD": ("cloud", ("The PR-comment webhook flags a change above this many "
                            "$/month (default 500).")),
    "NABLE_COMMENT_TAG": ("cloud", ("The marker the PR-comment webhook finds its own comment by "
                          "(default nable-cost).")),
    "NABLE_CRITIC_LLM": ("cloud", ("1: let the recommendation critic call a model "
                         "(ANTHROPIC_API_KEY); off by default, and never in `nable brief`.")),
    "NABLE_CRITIC_MODEL": ("cloud", ("The model the critic calls when it is on.")),
    # ── account ──
    "FINOPS_LICENSE_KEY": ("account", ("A license key (FINOPS-2-...). Set in the environment, "
                           "it wins over the one stored with `nable license`.")),
    "FINOPS_ACCOUNT_BASE": ("account", ("The account service `nable login` uses (default "
                            "https://getnable.com).")),
    "FINOPS_LICENSE_PRIVATE_KEY": ("account", ("Issuer side: signs license keys. Not needed to "
                                   "use nable.")),
    "FINOPS_LICENSE_SECRET": ("account", ("The HMAC secret of the retired FINOPS-1- keys. "
                              "Not needed for FINOPS-2- keys.")),
    "NABLE_WORKSPACE_KEY": ("account", ("The paid workspace's analyst key `nable cloud "
                            "connect --send` and `nable cloud status` use (made on its "
                            "/workspace page). Read only by those two, sent only in the "
                            "Authorization header over HTTPS, and never stored.")),
    # ── demo ──
    "FINOPS_DEMO": ("demo", ("1: answer from the StreamCo sample until a real provider is "
                    "connected.")),
    "FINOPS_DEMO_MODE": ("demo", ("The same as FINOPS_DEMO.")),
    "FINOPS_DEMO_FORCE": ("demo", ("1: stay on the sample even with a real provider connected.")),
    "FINOPS_DASHBOARD_PASSWORD": ("demo", ("The hosted dashboard's password; `serve --demo` "
                                  "sets it to off.")),
    "FINOPS_CONTROL_PLANE_SECRET": ("demo", ("Set on a hosted instance, with FINOPS_INSTANCE_ID, "
                                    "to mark it managed. Not for local use.")),
    "FINOPS_INSTANCE_ID": ("demo", ("A hosted instance's id; see FINOPS_CONTROL_PLANE_SECRET.")),
    "FINOPS_TEST_ONBOARDING_TOTAL": ("demo", ("A test hook for the interactive smoke tests. "
                                     "Never set it.")),
}


def ours(name: str) -> bool:
    return name.startswith(PREFIXES)


def unknown(environ: Mapping[str, str]) -> list[dict[str, str | None]]:
    """FINOPS_* / NABLE_* names set in `environ` that this package never
    reads, each with why (when known) or the closest name it does read."""
    out: list[dict[str, str | None]] = []
    for name in sorted(n for n in environ if ours(n) and n not in KNOWN):
        close = difflib.get_close_matches(name, KNOWN, n=1, cutoff=0.8)
        out.append({"name": name, "did_you_mean": close[0] if close else None,
                    "note": NOT_READ_HERE.get(name)})
    return out
