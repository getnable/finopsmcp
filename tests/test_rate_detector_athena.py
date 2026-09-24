"""The rate detector's Athena query waits by the clock, stops when it gives up,
and reads every page of results.

Its poll loop counted only its own sleeps, so a status call that took five
seconds stretched a "60 second" wait to over three minutes. On timeout it just
returned, leaving the query scanning and billing per byte. And it read the
first results page only. The CUR connector fixed all three; this is the same
fix, through the same helper.
"""
from __future__ import annotations

import time

import boto3
import pytest

from finops.recommendations import rate_detector


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def sleep(self, s):
        self.now += s


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(time, "sleep", c.sleep)
    monkeypatch.setattr(time, "monotonic", lambda: c.now)
    monkeypatch.setenv("CUR_ATHENA_S3_OUTPUT", "s3://results/finops-rates/")
    return c


def _cell_rows(rows: list[tuple[str, str, str, str]]) -> list[dict]:
    return [{"Data": [{"VarCharValue": v} for v in r]} for r in rows]


class _Athena:
    def __init__(self, clock, state="RUNNING", pages=None):
        self.clock = clock
        self.state = state
        self.pages = pages or []
        self.polls = 0
        self.stopped: list[str] = []
        self.sts_calls = 0

    def start_query_execution(self, **kw):
        return {"QueryExecutionId": "qe-rates"}

    def get_query_execution(self, QueryExecutionId):
        self.polls += 1
        self.clock.now += 5.0      # a slow status call
        return {"QueryExecution": {"QueryExecutionId": QueryExecutionId,
                                   "Status": {"State": self.state}}}

    def get_caller_identity(self):
        self.sts_calls += 1
        return {"Account": "111"}

    def stop_query_execution(self, QueryExecutionId):
        self.stopped.append(QueryExecutionId)
        return {}

    def get_query_results(self, QueryExecutionId, **kw):
        return self.pages[0]

    def get_paginator(self, name):
        assert name == "get_query_results"
        pages = self.pages

        class _P:
            def paginate(self, **kw):
                return iter(pages)
        return _P()


def test_a_query_that_never_finishes_is_stopped_on_a_wall_clock_deadline(clock, monkeypatch):
    athena = _Athena(clock)
    monkeypatch.setattr(boto3, "client", lambda service, **kw: athena)

    assert rate_detector._detect_from_cur_athena("cur_db", "cur") is None

    assert athena.stopped == ["qe-rates"], "the timed-out query was left running"
    waited = clock.now - 1000.0
    assert waited <= rate_detector._ATHENA_TIMEOUT_SECS + 5 + 1.6, f"waited {waited:.0f}s"


def test_every_results_page_is_read(clock, monkeypatch):
    header = ("service", "avg_public_rate", "avg_actual_rate", "line_count")
    pages = [
        {"ResultSet": {"Rows": _cell_rows([header, ("AmazonEC2", "1.0", "0.8", "500")])},
         "NextToken": "p2"},
        {"ResultSet": {"Rows": _cell_rows([("AmazonRDS", "2.0", "1.0", "300")])}},
    ]
    athena = _Athena(clock, state="SUCCEEDED", pages=pages)
    monkeypatch.setattr(boto3, "client", lambda service, **kw: athena)

    profile = rate_detector._detect_from_cur_athena("cur_db", "cur")

    assert profile is not None
    assert profile.per_service_discount == {"AmazonEC2": 0.2, "AmazonRDS": 0.5}
    assert profile.overall_discount_pct == pytest.approx(1 - 1.8 / 3.0, abs=1e-4)
    assert athena.stopped == []
    # CUR_ATHENA_S3_OUTPUT is set, so there is no account id to look up.
    assert athena.sts_calls == 0
