"""send_weekly_digest_now reports what happened, not what was hoped.

With no SMTP configured it answered {"sent": true, "recipient": "configured
address"}: send_weekly_digest returned False, job_weekly_email_digest dropped
the return value, and the tool assumed success. A user told "sent" waited for
an email that could never arrive.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

import finops.server  # noqa: F401  (tools register against the server)
from finops.notifications import email_digest as ed
from finops.tools import notifications as nt

_SMTP = ("FINOPS_SMTP_HOST", "FINOPS_SMTP_PORT", "FINOPS_SMTP_USER",
         "FINOPS_SMTP_PASSWORD", "FINOPS_SMTP_FROM", "FINOPS_DIGEST_TO")


@pytest.fixture
def no_smtp(monkeypatch, tmp_path):
    for v in _SMTP:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "t.db"))
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    monkeypatch.setattr(nt._srv, "require_pro", lambda *a, **k: None)
    # The digest's rightsizing section reads live AWS; never from a unit test.
    import finops.recommendations.rightsizing as rs
    monkeypatch.setattr(rs, "analyze_rightsizing", lambda *a, **k: [])
    yield
    db_mod._ENGINE = None


def _call(fn, **kw):
    out = fn(**kw)
    return asyncio.run(out) if inspect.isawaitable(out) else out


def test_no_smtp_is_not_sent_and_names_what_is_missing(no_smtp):
    out = _call(nt.send_weekly_digest_now)
    assert out["sent"] is False
    for v in ("FINOPS_SMTP_HOST", "FINOPS_SMTP_USER", "FINOPS_SMTP_PASSWORD",
              "FINOPS_DIGEST_TO"):
        assert v in out["missing"], v


def test_a_partial_smtp_config_names_only_what_is_missing(no_smtp, monkeypatch):
    monkeypatch.setenv("FINOPS_SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("FINOPS_SMTP_USER", "u")
    out = _call(nt.send_weekly_digest_now)
    assert out["sent"] is False
    assert out["missing"] == ["FINOPS_SMTP_PASSWORD", "FINOPS_DIGEST_TO"]


def test_an_smtp_failure_is_reported_with_its_reason(no_smtp, monkeypatch):
    for v, val in (("FINOPS_SMTP_HOST", "127.0.0.1"), ("FINOPS_SMTP_USER", "u"),
                   ("FINOPS_SMTP_PASSWORD", "p"), ("FINOPS_DIGEST_TO", "a@example.com"),
                   ("FINOPS_SMTP_PORT", "1")):
        monkeypatch.setenv(v, val)
    out = _call(nt.send_weekly_digest_now)
    assert out["sent"] is False
    assert out.get("error")


def test_a_real_send_says_sent_and_to_whom(no_smtp, monkeypatch):
    for v, val in (("FINOPS_SMTP_HOST", "smtp.example.com"), ("FINOPS_SMTP_USER", "u"),
                   ("FINOPS_SMTP_PASSWORD", "p"), ("FINOPS_DIGEST_TO", "a@example.com")):
        monkeypatch.setenv(v, val)
    sent: list = []

    class _SMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            pass

        def sendmail(self, frm, to, msg):
            sent.append(to)

    monkeypatch.setattr(ed.smtplib, "SMTP", _SMTP)
    out = _call(nt.send_weekly_digest_now)
    assert out["sent"] is True
    assert out["recipient"] == "a@example.com"
    assert sent == ["a@example.com"]


def test_the_job_returns_the_result(no_smtp):
    from finops.scheduler.jobs import job_weekly_email_digest
    out = job_weekly_email_digest()
    assert isinstance(out, dict) and out["sent"] is False and out["missing"]
