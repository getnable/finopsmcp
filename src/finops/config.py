"""
Runtime configuration helpers for nable.

Environment variables:
  FINOPS_AIRGAP=1         Disable all non-provider outbound (telemetry, version checks).
  FINOPS_FIPS=1           Require FIPS-validated OpenSSL. Server exits if not available.
                          Automatically implied when FINOPS_AIRGAP=1 on GovCloud.
  FINOPS_NO_AUDIT=1       Disable the local immutable audit log.
  FINOPS_PROFILE=<name>   Use a named profile directory under ~/.finops/profiles/.
  NABLE_NO_TELEMETRY=1    Disable anonymous usage telemetry (PostHog).
"""
from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

# Evaluated once at import time so every caller gets a consistent value
# for the lifetime of the process.
_AIRGAP_RAW = os.environ.get("FINOPS_AIRGAP", "").strip()
AIRGAP: bool = _AIRGAP_RAW not in ("", "0", "false", "no")

_FIPS_RAW = os.environ.get("FINOPS_FIPS", "").strip()
FIPS_REQUIRED: bool = _FIPS_RAW not in ("", "0", "false", "no")


def is_airgap() -> bool:
    """Return True when FINOPS_AIRGAP=1 is set."""
    return AIRGAP


def is_fips_mode() -> bool:
    """
    Return True if the underlying OpenSSL is running in FIPS mode.
    Requires Python's ssl module and OpenSSL 3.x with FIPS provider loaded.
    """
    try:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # OpenSSL 3.x exposes FIPS mode via the context's options
        # Check for FIPS via hashlib — SHA-1 is disabled in FIPS mode
        import hashlib
        try:
            hashlib.md5(b"test")
            return False  # MD5 available = not FIPS
        except ValueError:
            return True   # MD5 blocked = FIPS mode active
    except Exception:
        return False


def check_fips_compliance() -> None:
    """
    If FINOPS_FIPS=1 (or implied by FINOPS_AIRGAP on GovCloud), verify
    that FIPS-validated crypto is active. Exit with a clear error if not.

    Call once at server startup for FedRAMP/GovCloud deployments.
    """
    required = FIPS_REQUIRED or (AIRGAP and os.environ.get("FINOPS_GOVCLOUD", "").strip() not in ("", "0"))
    if not required:
        return

    if is_fips_mode():
        log.info("FIPS mode active — cryptographic operations use FIPS-validated OpenSSL.")
    else:
        log.error(
            "FIPS mode required (FINOPS_FIPS=1) but OpenSSL is NOT in FIPS mode. "
            "On RHEL/Amazon Linux: sudo fips-mode-setup --enable && reboot. "
            "On Ubuntu: enable the ubuntu-advantage fips-updates channel. "
            "Exiting to prevent non-FIPS crypto in a FedRAMP environment."
        )
        sys.exit(1)


def check_airgap_and_warn() -> None:
    """
    Log a single info-level notice when air-gap mode is active.
    Call once at server startup.
    """
    if AIRGAP:
        log.info(
            "Air-gap mode active. Non-provider outbound disabled "
            "(telemetry, version checks). Set FINOPS_AIRGAP=0 to re-enable."
        )
    check_fips_compliance()
