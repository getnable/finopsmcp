"""
License validation for nable (finops-mcp).

Tiers
─────
free         Permanent. ~90% of the platform. No key required.
             Cost queries, anomaly detection, rightsizing, PR comments,
             Slack/Teams alerts, all connectors, snapshots, attribution.

trial        7 days of full Pro access from first install. Kicks in
             automatically when no key is set. Uses the same dual-store
             (OS keyring + file) so deleting one source can't reset it.

pro          Full access. Unlocked by a signed license key.
             Adds: ticket auto-creation, scheduled email digests,
             commitment purchase recommendations, multi-team org reports.

Key format:  FINOPS-1-{b64_payload}-{b64_hmac}
  payload:   base64url(json: {"e": email, "d": issued_YYYYMMDD, "p": "pro"})
  hmac:      HMAC-SHA256(_SECRET, "1:" + b64_payload)

Generate a key (run once per customer):
  python -c "from finops.license import generate_key; print(generate_key('user@example.com'))"
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import platform
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("finops.license")

_env_secret = os.environ.get("FINOPS_LICENSE_SECRET", "")
# FINOPS_LICENSE_SECRET must be set in the environment. No default is kept in
# source — a committed default allows anyone with repo access to forge valid keys.
# If the env var is missing, pro key validation is disabled and all keys are
# treated as invalid so users fall through to the free/trial tier safely.
_SECRET = _env_secret.encode() if _env_secret else b""
if not _env_secret:
    log.debug(
        "FINOPS_LICENSE_SECRET is not set. Pro license key validation is disabled. "
        "Set this env var to the secret used when keys were issued."
    )
_KEY_TTL_DAYS   = 366          # pro keys expire 1 year after issue date
_VALID_PLANS    = {"pro", "trial", "enterprise"}
_UPGRADE_URL    = "https://getnable.com/#pricing"
_CHECKOUT_URL   = "https://buy.stripe.com/eVq14mbe9ffE3le3wC2Nq02"   # direct Stripe checkout
_ACTIVATE_CMD   = "finops setup license"             # shown after purchase
_TRIAL_DAYS  = 7
_TRIAL_FILE  = Path.home() / ".finops-mcp" / "trial_start"

# Keyring service/username — intentionally generic to avoid being obvious
_KR_SERVICE  = "system.cache.prefs"
_KR_USERNAME = "user.defaults"

# ── Pro-only features (the ~10%) ──────────────────────────────────────────────
# Everything NOT in this set is available on the free tier.
#
# Free tier includes:
#   All cost queries · Anomaly detection + Slack/Teams alerts · Rightsizing (view)
#   PR cost comments + budget CI gate · All cloud + SaaS connectors
#   Kubernetes cost analysis · Helm release visibility · Efficiency scorecard
#   Budgets (create/track/alert) · Scheduled Slack reports · Postgres shared mode
#   Commitment coverage analysis (view %) · Tag attribution · Idle resource detection
#   Multi-account account listing · Storage info · Check notification config
#
PRO_FEATURES: set[str] = {
    "ticket_creation",           # auto-create Jira / Linear / GitHub Issues from any finding
    "scheduled_email_digests",   # email delivery of scheduled reports (Slack delivery is free)
    "commitment_recommendations", # RI / SP purchase recommendations with $ amounts + ROI
    "org_reports",               # full org-wide cost rollup across all accounts / OUs
    "cur_athena_detail",         # line-item CUR data via Athena (per-resource, RI waste, tag breakdown)
    "azure_detail",              # Azure resource-level cost detail and reservation utilization
    "business_metrics",          # unit economics: hosting % of MRR, cost per customer, "so what?" analysis
    # anomaly_detection and rightsizing are intentionally FREE:
    # users discover value → want Slack alerts + ticket auto-creation → upgrade to Team
}


@dataclass
class LicenseStatus:
    mode: str          # "free" | "trial" | "pro" | "invalid"
    email: str
    issued: str        # YYYY-MM-DD or ""
    message: str
    days_remaining: int = -1   # trial days left (-1 = not applicable)

    @property
    def is_pro(self) -> bool:
        return self.mode in ("pro", "trial")

    @property
    def is_free(self) -> bool:
        return self.mode in ("free", "pro", "trial")


# ── Crypto helpers ────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))


def _sign(version: str, payload_b64: str) -> str:
    msg = f"{version}:{payload_b64}".encode()
    return _b64(hmac.new(_SECRET, msg, hashlib.sha256).digest())


# ── Machine fingerprint ───────────────────────────────────────────────────────

def _machine_id() -> str:
    parts = [platform.node(), platform.machine(), str(Path.home())]
    raw = "|".join(parts).encode()
    return hmac.new(_SECRET, raw, hashlib.sha256).hexdigest()[:24]


def _sign_date(iso_date: str) -> str:
    msg = f"trial:{_machine_id()}:{iso_date}".encode()
    return hmac.new(_SECRET, msg, hashlib.sha256).hexdigest()[:32]


# ── Keyring helpers ───────────────────────────────────────────────────────────

def _kr_get() -> date | None:
    try:
        import keyring  # type: ignore
        val = keyring.get_password(_KR_SERVICE, _KR_USERNAME)
        if val:
            iso, _, sig = val.partition(":")
            if sig and hmac.compare_digest(sig, _sign_date(iso)):
                return date.fromisoformat(iso)
            log.debug("Keyring entry signature mismatch - ignoring")
    except Exception:
        pass
    return None


def _kr_set(d: date) -> None:
    try:
        import keyring  # type: ignore
        iso = d.isoformat()
        keyring.set_password(_KR_SERVICE, _KR_USERNAME, f"{iso}:{_sign_date(iso)}")
    except Exception:
        pass


# ── File helpers ──────────────────────────────────────────────────────────────

def _file_get() -> date | None:
    try:
        if _TRIAL_FILE.exists():
            lines = _TRIAL_FILE.read_text().strip().splitlines()
            if not lines:
                return None
            iso = lines[0].strip()
            sig = lines[1].strip() if len(lines) > 1 else ""
            # Require a valid signature; an empty or missing sig is rejected
            if not sig or not hmac.compare_digest(sig, _sign_date(iso)):
                log.debug("Trial file signature missing or invalid, ignoring")
                return None
            return date.fromisoformat(iso)
    except Exception:
        pass
    return None


def _file_set(d: date) -> None:
    try:
        _TRIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        iso = d.isoformat()
        _TRIAL_FILE.write_text(f"{iso}\n{_sign_date(iso)}\n")
        # Owner-only: the signature shouldn't be world-readable (trial-forgery aid).
        try:
            _TRIAL_FILE.chmod(0o600)
        except OSError:
            pass
    except Exception:
        pass


# ── Trial date logic ──────────────────────────────────────────────────────────

def _get_or_create_trial_start() -> date:
    """
    Return the trial start date, using the EARLIEST date across both stores
    so deleting one store can't push the start date forward.
    """
    kr_date   = _kr_get()
    file_date = _file_get()
    candidates = [d for d in (kr_date, file_date) if d is not None]

    if candidates:
        earliest = min(candidates)
        _kr_set(earliest)
        _file_set(earliest)
        return earliest

    today = date.today()
    _kr_set(today)
    _file_set(today)
    return today


# ── Key generation / validation ───────────────────────────────────────────────

def generate_key(email: str, plan: str = "pro") -> str:
    """Generate a signed license key for a customer. Run server-side."""
    payload = _b64(json.dumps({"e": email, "d": date.today().strftime("%Y%m%d"), "p": plan}).encode())
    return f"FINOPS-1-{payload}-{_sign('1', payload)}"


def validate_key(key: str) -> LicenseStatus:
    """Parse and verify a license key string."""
    if not key:
        # No key — give a 7-day pro trial, then drop to free forever
        trial_start   = _get_or_create_trial_start()
        days_used     = (date.today() - trial_start).days
        days_remaining = max(0, _TRIAL_DAYS - days_used)

        if days_remaining > 0:
            return LicenseStatus(
                mode="trial",
                email="",
                issued=trial_start.isoformat(),
                message=(
                    f"Trial: {days_remaining} day{'s' if days_remaining != 1 else ''} remaining — all features unlocked."
                ),
                days_remaining=days_remaining,
            )
        else:
            # Trial over → free tier (not expired / blocked)
            return LicenseStatus(
                mode="free",
                email="",
                issued=trial_start.isoformat(),
                message="Free tier active.",
                days_remaining=0,
            )

    # If the signing secret is not configured, we cannot verify any key.
    # Treat as free/trial rather than silently accepting forged keys.
    if not _SECRET:
        log.warning("Pro key presented but FINOPS_LICENSE_SECRET is not set; treating as unkeyed.")
        return validate_key("")

    parts = key.strip().split("-", 3)
    if len(parts) != 4 or parts[0] != "FINOPS" or parts[1] != "1":
        return LicenseStatus(
            mode="invalid",
            email="",
            issued="",
            message=f"Invalid license key format. Get a valid key at {_UPGRADE_URL}",
        )

    _, version, payload_b64, provided_sig = parts
    if not hmac.compare_digest(_sign(version, payload_b64), provided_sig):
        return LicenseStatus(
            mode="invalid",
            email="",
            issued="",
            message=f"License key signature invalid. Contact support or renew at {_UPGRADE_URL}",
        )

    try:
        payload = json.loads(_unb64(payload_b64))
    except Exception:
        return LicenseStatus(
            mode="invalid",
            email="",
            issued="",
            message="License key payload corrupt. Contact support.",
        )

    email      = payload.get("e", "")
    issued_raw = payload.get("d", "")
    plan       = payload.get("p", "pro")

    # Validate plan field against allowlist
    if plan not in _VALID_PLANS:
        log.warning("License key contains unrecognised plan %r; treating as invalid.", plan)
        return LicenseStatus(
            mode="invalid",
            email=email,
            issued="",
            message=f"License key contains an unrecognised plan. Contact support.",
        )

    try:
        issued     = date(int(issued_raw[:4]), int(issued_raw[4:6]), int(issued_raw[6:8]))
        issued_str = issued.isoformat()
    except Exception:
        issued_str = issued_raw
        issued     = None

    # Expiry check: keys are valid for _KEY_TTL_DAYS from issue date.
    if issued is not None:
        expiry = issued + timedelta(days=_KEY_TTL_DAYS)
        today  = date.today()
        if today > expiry:
            return LicenseStatus(
                mode="invalid",
                email=email,
                issued=issued_str,
                message=(
                    f"License key expired on {expiry.isoformat()}. "
                    f"Renew your subscription at {_UPGRADE_URL}"
                ),
            )

    return LicenseStatus(
        mode=plan,
        email=email,
        issued=issued_str,
        message=f"Team license active: {email}, issued {issued_str}.",
    )


# ── Runtime helpers ───────────────────────────────────────────────────────────

def check_license() -> LicenseStatus:
    """Read FINOPS_LICENSE_KEY from env and return validated status."""
    key    = os.environ.get("FINOPS_LICENSE_KEY", "").strip()
    status = validate_key(key)

    if status.mode == "trial":
        log.info("License: pro trial — %d days remaining", status.days_remaining)
    elif status.mode == "free":
        log.info("License: free tier")
    elif status.mode == "pro":
        log.info("License: pro — %s", status.email)
    else:
        log.warning("License: invalid key — %s", status.message)

    return status


_status: LicenseStatus | None = None


def get_status() -> LicenseStatus:
    global _status
    if _status is None:
        _status = check_license()
    return _status


def require_pro(feature: str) -> dict | None:
    """
    Gate a Pro-only feature. Returns an error dict if access is denied, None if granted.
    Only the features listed in PRO_FEATURES are gated — everything else is free.

    Usage:
        if err := require_pro("ticket_creation"):
            return err
    """
    if feature not in PRO_FEATURES:
        # Caller mistake — feature isn't in the pro set, allow it
        log.warning("require_pro called for non-pro feature %r — allowing", feature)
        return None

    s = get_status()
    if s.is_pro:
        return None

    # Free tier — explain what they're missing and how to unlock it
    friendly = feature.replace("_", " ")

    # Craft a contextual upgrade message based on whether trial expired recently
    if s.mode == "free" and s.issued:
        try:
            trial_start = date.fromisoformat(s.issued)
            days_since_expiry = (date.today() - trial_start).days - _TRIAL_DAYS
            if 0 < days_since_expiry <= 30:
                urgency = (
                    f"Your {_TRIAL_DAYS}-day trial ended {days_since_expiry} day{'s' if days_since_expiry != 1 else ''} ago. "
                )
            else:
                urgency = ""
        except Exception:
            urgency = ""
    else:
        urgency = ""

    # Build a concise FOMO block showing everything Team unlocks
    _TEAM_FEATURES = [
        ("ticket_creation",            "🎫 Auto-create Jira / Linear / GitHub Issues from anomalies & rightsizing"),
        ("scheduled_email_digests",    "📧 Scheduled email reports — weekly, monthly, or custom cadence"),
        ("commitment_recommendations", "💰 RI / Savings Plan recommendations with exact $ ROI"),
        ("org_reports",                "🏢 Org-wide cost rollup across all accounts & OUs"),
        ("cur_athena_detail",          "🔍 Line-item CUR data — per-resource costs, RI waste, tag breakdown"),
        ("azure_detail",               "☁️  Azure resource-level cost detail & reservation utilization"),
        ("business_metrics",           "📈 Unit economics — cost per customer, hosting % of MRR"),
    ]

    lines = [f"⬡  nable Team — everything in free, plus:\n"]
    for key, desc in _TEAM_FEATURES:
        marker = "▶" if key == feature else " "
        lines.append(f"  {marker} {desc}")
    lines.append(f"\n  You hit this because '{friendly}' requires Team.")
    lines.append(f"\n  → 7-day free trial: {_CHECKOUT_URL}")
    lines.append(f"  → Then activate:    {_ACTIVATE_CMD} <your-key>")

    return {
        "error": "pro_required",
        "feature": feature,
        "message": "\n".join(lines),
        "upgrade_url": _CHECKOUT_URL,
        "activate_command": _ACTIVATE_CMD,
        "free_tier_available": True,
    }


def feature_available(feature: str) -> bool:
    """
    Quick boolean check. Returns True for all free-tier features,
    and for pro features only when the user has a pro/trial license.
    """
    if feature not in PRO_FEATURES:
        return True
    return get_status().is_pro
