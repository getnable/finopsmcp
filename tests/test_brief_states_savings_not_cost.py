"""The brief says what a fix saves, and promises only checks nable makes.

Each item's sentence read "It costs $X a month" with X the finding's saving.
That is true for a delete (the saving is the whole cost) and false for every
change that keeps the resource: a 500 GB gp2 volume costs $50.00/mo and moving
it to gp3 saves $10.00/mo, so the brief told the reader a $50 volume cost $10.

Each item also ended "nable re-checks this against the bill and records the
measured result, not the estimate". Brief items come from the resource audit
and are never written to the savings ledger, so nothing re-checks them; only a
recommendation marked acted on is verified (verify_savings).
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from finops.briefing import build_brief

TODAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)


def _gp2():
    return {"resource_id": "vol-0gp2", "resource_type": "EBS Volume", "region": "us-east-1",
            "waste_type": "gp2_should_migrate_to_gp3", "estimated_monthly_savings": 10.0,
            "detail": "500 GB gp2 volume. Migrating to gp3 saves $10.00/mo",
            "size_gb": 500, "volume_type": "gp2"}


def test_a_change_that_keeps_the_resource_is_not_described_as_its_cost():
    item = build_brief([_gp2()], today=TODAY, use_llm=False, now=NOW).items[0]
    words = item.in_words()
    assert "It costs $10 a month" not in words, words
    assert "saves about $10 a month" in words, words


def test_the_verification_step_does_not_promise_a_recheck_nable_never_runs():
    item = build_brief([_gp2()], today=TODAY, use_llm=False, now=NOW).items[0]
    assert "nable re-checks this" not in item.verification, item.verification
    assert "about $10" in item.verification, item.verification
