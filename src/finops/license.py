"""
License validation for nable (finops-mcp).

Key format:  FINOPS-1-{b64_payload}-{b64_hmac}
  payload:   base64url(json: {"e": email, "d": issued_YYYYMMDD, "p": "pro"})
  hmac:      HMAC-SHA256(_SECRET, "1:" + b64_payload)

Generate a key (run once per customer):
  python -c "from finops.license import generate_key; print(generate_key('user@example.com'))"

Trial mode:  14 days of full Pro access from first install.
             Start date stored in OS keyring (primary) + ~/.finops-mcp/trial_start (backup).
             The earliest date across both stores is always used — deleting one source does
             not reset the trial; the other store still holds the real start date.
Pro mode:    full access — anomaly alerts, digests, rightsizing, tickets, attribution.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import platform
import socket
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger("finops.license")

_SECRET = b"finops-mcp-license-v1-2026"
_UPGRADE_URL = "https://nable.sh/#pricing"
_TRIAL_DAYS = 14
_TRIAL_FILE = Path.home() / ".finops-mcp" / "trial_start"

# Keyring service/username — intentionally generic to avoid being obvious
_KR_SERVICE  = "system.cache.prefs"
_KR_USERNAME = "user.defaults"


@dataclass
class LicenseStatus:
    mode: str          # "trial" | "trial_expired" | "pro" | "invalid"
    email: str
    issued: str        # YYYY-MM-DD or ""
    message: str
    days_remaining: int = -1   # trial days left; -1 means not applicable


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))


def _sign(version: str, payload_b64: str) -> str:
    msg = f"{version}:{payload_b64}".encode()
    return _b64(hmac.new(_SECRET, msg, hashlib.sha256).digest())


# ── Machine fingerprint ───────────────────────────────────────────────────────
# Used to sign the file so it can't be transplanted from another machine or
# edited manually without detection. Derived from stable, hard-to-spoof values.

def _machine_id() -> str:
    parts = [
        platform.node(),          # hostname
        platform.machine(),       # architecture
        str(Path.home()),         # home directory path
    ]
    raw = "|".join(parts).encode()
    return hmac.new(_SECRET, raw, hashlib.sha256).hexdigest()[:24]


def _sign_date(iso_date: str) -> str:
    """HMAC sign a date string bound to this machine."""
    msg = f"trial:{_machine_id()}:{iso_date}".encode()
    return hmac.new(_SECRET, msg, hashlib.sha256).hexdigest()[:32]


# ── Keyring helpers (optional dep) ───────────────────────────────────────────

def _kr_get() -> date | None:
    try:
        import keyring  # type: ignore
        val = keyring.get_password(_KR_SERVICE, _KR_USERNAME)
        if val:
            # stored as "YYYY-MM-DD:sig"
            iso, _, sig = val.partition(":")
            if sig == _sign_date(iso):
                return date.fromisoformat(iso)
            log.debug("Keyring entry signature mismatch — ignoring")
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
            # Accept if sig matches this machine, or if no sig (legacy install)
            if sig == _sign_date(iso) or not sig:
                return date.fromisoformat(iso)
            log.debug("Trial file signature mismatch — ignoring")
    except Exception:
        pass
    return None


def _file_set(d: date) -> None:
    try:
        _TRIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        iso = d.isoformat()
        _TRIAL_FILE.write_text(f"{iso}\n{_sign_date(iso)}\n")
    except Exception:
        pass


# ── Core trial date logic ─────────────────────────────────────────────────────

def _get_or_create_trial_start() -> date:
    """
    Return the trial start date.

    Reads from both OS keyring and file, uses the EARLIEST date found
    (so deleting one store can't push the start date forward). Writes
    the canonical date back to both stores for redundancy.
    """
    kr_date   = _kr_get()
    file_date = _file_get()

    candidates = [d for d in (kr_date, file_date) if d is not None]

    if candidates:
        earliest = min(candidates)
        # Sync both stores to the real earliest date
        _kr_set(earliest)
        _file_set(earliest)
        return earliest

    # First run — record today in both stores
    today = date.today()
    _kr_set(today)
    _file_set(today)
    return today


def generate_key(email: str, plan: str = "pro") -> str:
    """Generate a license key for a paying customer. Run server-side."""
    payload = _b64(json.dumps({"e": email, "d": date.today().strftime("%Y%m%d"), "p": plan}).encode())
    sig = _sign("1", payload)
    return f"FINOPS-1-{payload}-{sig}"


def validate_key(key: str) -> LicenseStatus:
    """Parse and verify a license key string."""
    if not key:
        # No key — check trial status based on first-use date
        trial_start = _get_or_create_trial_start()
        days_used = (date.today() - trial_start).days
        days_remaining = max(0, _TRIAL_DAYS - days_used)

        if days_remaining > 0:
            return LicenseStatus(
                mode="trial",
                email="",
                issued=trial_start.isoformat(),
                message=(
                    f"Free trial — {days_remaining} day{'s' if days_remaining != 1 else ''} remaining. "
                    f"All Pro features are unlocked. "
                    f"Subscribe at {_UPGRADE_URL} to keep access after your trial."
                ),
                days_remaining=days_remaining,
            )
        else:
            return LicenseStatus(
                mode="trial_expired",
                email="",
                issued=trial_start.isoformat(),
                message=(
                    f"Your 14-day free trial has ended. "
                    f"Subscribe at {_UPGRADE_URL} to restore full access."
                ),
                days_remaining=0,
            )

    parts = key.strip().split("-", 3)
    if len(parts) != 4 or parts[0] != "FINOPS" or parts[1] != "1":
        return LicenseStatus(
            mode="invalid",
            email="",
            issued="",
            message=f"Invalid license key format. Get a valid key at {_UPGRADE_URL}",
        )

    _, version, payload_b64, provided_sig = parts
    expected_sig = _sign(version, payload_b64)

    if not hmac.compare_digest(expected_sig, provided_sig):
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

    email = payload.get("e", "")
    issued_raw = payload.get("d", "")
    plan = payload.get("p", "pro")

    try:
        issued = date(int(issued_raw[:4]), int(issued_raw[4:6]), int(issued_raw[6:8]))
        issued_str = issued.isoformat()
    except Exception:
        issued_str = issued_raw

    return LicenseStatus(
        mode=plan,
        email=email,
        issued=issued_str,
        message=f"Pro license active — {email}, issued {issued_str}.",
    )


def check_license() -> LicenseStatus:
    """Read FINOPS_LICENSE_KEY from env and return validated status."""
    key = os.environ.get("FINOPS_LICENSE_KEY", "").strip()
    status = validate_key(key)
    if status.mode == "trial":
        log.info("License: trial — %d days remaining", status.days_remaining)
    elif status.mode == "trial_expired":
        log.warning("License: trial expired")
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
    Call at the top of Pro-only tools.
    Returns an error dict if access is denied, None if granted.

    Usage:
        if err := require_pro("anomaly alerts"):
            return err
    """
    s = get_status()
    if s.mode in ("pro", "trial"):
        return None
    if s.mode == "trial_expired":
        return {
            "error": "trial_expired",
            "feature": feature,
            "message": (
                f"Your 14-day free trial has ended. "
                f"Subscribe at {_UPGRADE_URL} to restore access to '{feature}' and all Pro features."
            ),
            "upgrade_url": _UPGRADE_URL,
        }
    return {
        "error": "license_invalid",
        "message": s.message,
        "upgrade_url": _UPGRADE_URL,
    }
