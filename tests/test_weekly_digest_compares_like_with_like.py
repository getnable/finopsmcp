"""The weekly email digest compares a week with a week of the same coverage.

The daily digest already refuses to compare two days that cover different
providers; the weekly one did not. A provider whose snapshots stopped (a failed
fetch writes no rows, it does not write zeros) simply vanished from this week's
sum, and the email reported the hole as a drop in spend.

Worked example: last week AWS $1,000/day and Datadog $100/day, 7 days each,
$7,700. This week AWS has no snapshots and Datadog is flat at $100/day: $700.
The digest said "-90.9% vs prior week". Nothing dropped; the one provider both
weeks cover is flat, so the like-for-like change is 0.0%, and the email has to
say that AWS is missing.

The period label was also a day off: the sum covers the seven days ending
yesterday, the label named the seven days ending today.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from finops.notifications import email_digest as ed
from finops.scheduler import jobs


@pytest.fixture
def digest_env(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "t.db"))
    for v, val in (("FINOPS_SMTP_HOST", "127.0.0.1"), ("FINOPS_SMTP_USER", "u"),
                   ("FINOPS_SMTP_PASSWORD", "p"), ("FINOPS_DIGEST_TO", "a@example.com")):
        monkeypatch.setenv(v, val)
    import finops.storage.db as db_mod
    monkeypatch.setattr(db_mod, "_ENGINE", None)
    import finops.recommendations.rightsizing as rs
    monkeypatch.setattr(rs, "analyze_rightsizing", lambda *a, **k: [])
    sent: dict = {}

    def _capture(**kw):
        sent.update(kw)
        return {"sent": True, "recipient": "a@example.com", "missing": [], "error": ""}

    monkeypatch.setattr(ed, "send_weekly_digest_result", _capture)
    yield sent
    db_mod._ENGINE = None


def _seed(provider: str, per_day: float, days: list[date]) -> None:
    from finops.storage.snapshots import store_snapshots
    store_snapshots([{"provider": provider, "service": "svc", "account_id": "",
                      "region": "", "snapshot_date": d, "amount_usd": per_day}
                     for d in days])


def _week(offset_days: int) -> list[date]:
    today = date.today()  # noqa: DTZ011 - the job under test reads the local date
    return [today - timedelta(days=offset_days + i) for i in range(1, 8)]


def _render(sent: dict) -> str:
    kw = {k: v for k, v in sent.items()
          if k in ("total_spend", "prev_total", "top_providers", "anomalies",
                   "recommendations", "has_data", "delta_current", "coverage_note",
                   "recommendations_note")}
    return ed._build_html(sent.get("period_label") or "", **kw)


def test_a_provider_missing_this_week_is_not_reported_as_a_spend_drop(digest_env):
    _seed("aws", 1000.0, _week(7))          # last week only
    _seed("datadog", 100.0, _week(7))
    _seed("datadog", 100.0, _week(0))       # this week, flat
    jobs.job_weekly_email_digest()
    html = _render(digest_env)
    assert "-90.9%" not in html, html
    assert "+0.0% vs prior week" in html, html
    assert "AWS" in digest_env.get("coverage_note", ""), digest_env


def test_a_provider_with_missing_days_is_left_out_of_the_comparison(digest_env):
    # AWS is in both weeks but only 3 of this week's 7 days were snapshotted.
    _seed("aws", 1000.0, _week(7))
    _seed("aws", 1000.0, _week(0)[:3])
    _seed("datadog", 100.0, _week(7))
    _seed("datadog", 110.0, _week(0))
    jobs.job_weekly_email_digest()
    html = _render(digest_env)
    assert "+10.0% vs prior week" in html, html    # Datadog 700 -> 770
    assert "AWS" in digest_env.get("coverage_note", ""), digest_env


def test_full_coverage_compares_the_whole_bill(digest_env):
    _seed("aws", 1000.0, _week(7))
    _seed("aws", 1100.0, _week(0))
    jobs.job_weekly_email_digest()
    html = _render(digest_env)
    assert "+10.0% vs prior week" in html, html
    assert not digest_env.get("coverage_note"), digest_env


def test_the_period_label_names_the_days_that_were_summed():
    today = date.today()  # noqa: DTZ011 - the label is built from the local date
    first, last = today - timedelta(days=7), today - timedelta(days=1)
    label = ed.default_period_label(today)
    assert label == f"{first.strftime('%b %d')} – {last.strftime('%b %d, %Y')}"


# ── the digest's savings section ─────────────────────────────────────────────

_CO_REC = {"instance_id": "i-0big01", "name": "analytics", "region": "us-east-1",
           "resource_type": "ec2", "source": "compute_optimizer",
           "current_type": "r5.2xlarge", "recommended_type": "r5.xlarge",
           "monthly_savings": 183.96, "verdict": "review", "why": "over-provisioned",
           "action": "Resize needs a stop/start (brief downtime); fully reversible."}


def test_the_digest_carries_the_rightsizing_recommendations(digest_env, monkeypatch):
    """analyze_rightsizing's rows (rightsizing_summary) carry no "title" or
    "description" key, and the digest read r["title"] inside a try whose
    except set the list to []. On a fixture account where
    get_rightsizing_recommendations returns i-0big01, r5.2xlarge to r5.xlarge,
    $183.96/mo (Compute Optimizer; (0.504 - 0.252) x 730 at the AmazonEC2
    us-east-1 list), the weekly email said "No recommendations at this
    time." """
    import finops.recommendations.rightsizing as rs

    monkeypatch.setattr(rs, "analyze_rightsizing", lambda *a, **k: ["one"])
    monkeypatch.setattr(rs, "rightsizing_summary",
                        lambda recs, **k: {"recommendations": [dict(_CO_REC)]})
    _seed("aws", 100.0, _week(7))
    _seed("aws", 100.0, _week(0))
    jobs.job_weekly_email_digest()
    html = _render(digest_env)
    assert "No recommendations at this time" not in html, html
    assert "i-0big01" in html or "analytics" in html, html
    assert "r5.xlarge" in html and "Save $184/mo" in html, html


def test_a_rightsizing_read_that_failed_is_not_no_recommendations(digest_env, monkeypatch):
    import finops.recommendations.rightsizing as rs

    def _denied(*a, **k):
        raise RuntimeError("AccessDeniedException")

    monkeypatch.setattr(rs, "analyze_rightsizing", _denied)
    _seed("aws", 100.0, _week(7))
    _seed("aws", 100.0, _week(0))
    jobs.job_weekly_email_digest()
    html = _render(digest_env)
    assert "No recommendations at this time" not in html, html
    assert "could not be read" in html, html
