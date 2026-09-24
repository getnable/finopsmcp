"""CUR queries answer for the days asked, and a timed-out query stops billing.

_partition_filter only selects year/month partitions, so every CUR query read
whole months: a Sep 10-20 question returned all of September. The slice engine
already added day bounds; the connector's own queries did not.

The Athena poll counted only its sleeps toward the deadline and left the query
running when it gave up, so Athena kept scanning (and billing) after nable had
stopped waiting.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import boto3
import pytest

from finops.connectors import cur

START, END = date(2026, 9, 10), date(2026, 9, 20)


@pytest.fixture(autouse=True)
def _cur_env(monkeypatch):
    for k, v in {"CUR_S3_BUCKET": "cur-bucket", "CUR_ATHENA_DATABASE": "cur_db",
                 "CUR_ATHENA_TABLE": "cur_report",
                 "CUR_ATHENA_RESULTS_BUCKET": "athena-results"}.items():
        monkeypatch.setenv(k, v)


def _capture_sql(monkeypatch) -> list[str]:
    seen: list[str] = []

    def _fake_query(sql, timeout_secs=30):
        seen.append(sql)
        return []

    monkeypatch.setattr(cur, "_athena_query", _fake_query)
    return seen


@pytest.mark.parametrize("call", [
    lambda: cur.get_resource_costs(START, END),
    lambda: cur.get_tag_cost_breakdown("team", START, END),
    lambda: cur.get_untagged_resource_cost(START, END),
    lambda: cur.get_savings_plan_showback(START, END),
], ids=["resource_costs", "tag_breakdown", "untagged", "sp_showback"])
def test_usage_queries_are_bounded_to_the_requested_days(monkeypatch, call):
    seen = _capture_sql(monkeypatch)
    call()
    sql = seen[0]
    assert "(year='2026' AND month='09')" in sql
    assert "line_item_usage_start_date >= DATE '2026-09-10'" in sql
    assert "line_item_usage_start_date < DATE '2026-09-21'" in sql


def test_ri_waste_keeps_monthly_rifee_lines_that_overlap_the_window(monkeypatch):
    seen = _capture_sql(monkeypatch)
    cur.get_ri_waste(START, END)
    sql = seen[0]
    assert "line_item_usage_start_date < DATE '2026-09-21'" in sql
    assert "line_item_usage_end_date > DATE '2026-09-10'" in sql


class _SlowAthena:
    """Athena that never finishes and whose status call takes 5 seconds."""

    def __init__(self, clock):
        self.clock = clock
        self.polls = 0
        self.stopped: list[str] = []

    def start_query_execution(self, **kw):
        return {"QueryExecutionId": "qe-123"}

    def get_query_execution(self, QueryExecutionId):
        self.polls += 1
        self.clock.now += 5.0
        return {"QueryExecution": {"QueryExecutionId": QueryExecutionId,
                                   "Status": {"State": "RUNNING"}}}

    def stop_query_execution(self, QueryExecutionId):
        self.stopped.append(QueryExecutionId)
        return {}


def test_athena_timeout_is_wall_clock_and_stops_the_query(monkeypatch):
    clock = SimpleNamespace(now=1000.0)

    def _sleep(s):
        clock.now += s

    monkeypatch.setattr(cur, "time", SimpleNamespace(
        sleep=_sleep, monotonic=lambda: clock.now, time=lambda: clock.now))
    athena = _SlowAthena(clock)
    monkeypatch.setattr(boto3, "client", lambda service, **kw: athena)

    with pytest.raises(cur.CURQueryError, match="timed out"):
        cur._athena_query("SELECT 1", timeout_secs=30)

    # 30s at ~5s per status call is about six polls, not twenty.
    assert athena.polls <= 7
    assert clock.now - 1000.0 <= 30 + 5 + 1.6
    assert athena.stopped == ["qe-123"]
