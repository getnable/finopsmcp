"""Every FINOPS_* and NABLE_* environment variable this package reads, by name.

Headless use is configured by environment alone, and a misspelled variable is
silent: FINOPS_AIRGAPP=1 turns nothing on and says nothing. `nable doctor`
checks the environment against this list and names what it does not read,
with the closest name it does. tests/test_headless_env.py holds the list to
the source: a name read anywhere in src/finops must be here, and a name here
must be read somewhere, so it cannot drift either way. docs/HEADLESS.md
documents each one.

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
    'FINOPS_GUARD_BUDGET_MAX_AGE_HOURS', 'FINOPS_GUARD_PROD_PATTERNS',
    'FINOPS_GUARD_STOP_ON_BUDGET', 'FINOPS_GUARD_STRICT', 'FINOPS_GUARD_TEAM',
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
    'NABLE_POSTHOG_KEY', 'NABLE_TELEMETRY',
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
