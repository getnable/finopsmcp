"""
Runtime configuration helpers for nable.

Environment variables:
  FINOPS_AIRGAP=1         Disable all non-provider outbound (telemetry, version checks).
  FINOPS_NO_AUDIT=1       Disable the local immutable audit log.
  FINOPS_PROFILE=<name>   Use a named profile directory under ~/.finops/profiles/.
  NABLE_NO_TELEMETRY=1    Disable anonymous usage telemetry (PostHog).
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# Evaluated once at import time so every caller gets a consistent value
# for the lifetime of the process.
_AIRGAP_RAW = os.environ.get("FINOPS_AIRGAP", "").strip()
AIRGAP: bool = _AIRGAP_RAW not in ("", "0", "false", "no")


def is_airgap() -> bool:
    """Return True when FINOPS_AIRGAP=1 is set."""
    return AIRGAP


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
