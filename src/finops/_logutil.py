"""Small logging helpers shared across the package."""
from __future__ import annotations

import logging

# Keys already logged this process. Used to log a recurring, non-transient
# condition (missing IAM permission, no data) exactly once instead of on every
# dashboard refresh / scheduler tick.
_seen: set[str] = set()


def log_once(logger: logging.Logger, level: int, key: str, msg: str, *args) -> None:
    """Log ``msg`` once per process for a given ``key``, then suppress repeats."""
    if key in _seen:
        return
    _seen.add(key)
    logger.log(level, msg, *args)


def note_sp_error(logger: logging.Logger, what: str, exc: Exception) -> None:
    """Classify a Savings Plans / Cost Explorer fetch error and log it once.

    AccessDenied is a config problem (missing IAM permission), so it is shown
    once at WARNING with the fix. DataUnavailable is benign (no active plans),
    shown once at DEBUG. Anything else is shown once at DEBUG. Either way it
    never spams the console on repeated fetches.
    """
    code = ""
    if hasattr(exc, "response"):
        code = exc.response.get("Error", {}).get("Code", "")  # type: ignore[attr-defined]
    text = f"{code} {exc}"

    if "AccessDenied" in text:
        log_once(
            logger, logging.WARNING, f"sp-accessdenied-{what}",
            "%s unavailable: the IAM user is missing a Cost Explorer Savings Plans "
            "permission (ce:GetSavingsPlansCoverage / ce:GetSavingsPlansUtilization). "
            "Add the Savings Plans read actions to the policy, or run: "
            "finops setup aws --iam-template. Skipping Savings Plans data. "
            "(shown once)", what,
        )
    elif "DataUnavailable" in text:
        log_once(
            logger, logging.DEBUG, f"sp-dataunavailable-{what}",
            "%s: no Savings Plans data for this period (no active plans). "
            "Skipping. (shown once)", what,
        )
    else:
        log_once(logger, logging.DEBUG, f"sp-error-{what}",
                 "%s fetch failed: %s (shown once)", what, exc)
