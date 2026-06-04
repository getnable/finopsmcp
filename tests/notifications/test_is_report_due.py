"""Guards the regression where the Slack scheduler imported a _is_report_due that
did not exist (ImportError swallowed every 5 minutes, so reports never sent).

The import itself must always succeed. The due-logic is only asserted when croniter
is installed, since the helper safely returns False without it (and CI installs
only [dev], not the [croniter] extra)."""
import importlib.util

from datetime import datetime, timedelta, timezone

from finops.notifications.reports import _is_report_due  # must import (regression)

_HAS_CRONITER = importlib.util.find_spec("croniter") is not None


def test_symbol_is_importable_and_callable():
    # Calling it must never raise, with or without croniter.
    result = _is_report_due("0 9 * * *", None)
    assert isinstance(result, bool)


def test_due_logic_when_croniter_present():
    if not _HAS_CRONITER:
        # Without croniter the helper fails safe (returns False, does not spam).
        assert _is_report_due("0 9 * * *", None) is False
        return
    now = datetime.now(timezone.utc)
    # never sent and no created_at -> NOT due (do not blast on the next tick)
    assert _is_report_due("0 9 * * *", None) is False
    # never sent but created just now -> NOT due yet (wait for next scheduled fire)
    assert _is_report_due("0 9 * * *", None, created_at=now) is False
    # never sent, created 2 days ago -> due (a scheduled time has passed since)
    assert _is_report_due("0 9 * * *", None, created_at=now - timedelta(days=2)) is True
    assert _is_report_due("0 9 * * *", now) is False                      # just sent
    assert _is_report_due("0 9 * * *", now - timedelta(days=2)) is True   # stale
    assert _is_report_due("not a cron", None, created_at=now - timedelta(days=2)) is False  # malformed
