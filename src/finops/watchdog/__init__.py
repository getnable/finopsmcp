"""
Watchdog: an always-on agent that watches spend versus utilization and prepares
one-click fixes. It never executes them. Cloud access is read-only.

See internal/watchdog-design.md for the full flow:
detect -> prepare -> push one-click -> human approves -> verify.

PROPOSE-ONLY, ALWAYS. Nothing in this package mutates cloud state.
"""
from __future__ import annotations

from .correlator import (
    CorrelatedFinding,
    PreparedRemediation,
    correlate_spend_and_utilization,
    correlation_summary,
)

__all__ = [
    "CorrelatedFinding",
    "PreparedRemediation",
    "correlate_spend_and_utilization",
    "correlation_summary",
]
