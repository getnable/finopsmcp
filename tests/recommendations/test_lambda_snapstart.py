"""Tests for finops.recommendations.lambda_snapstart."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.lambda_snapstart import (
    JAVA_RUNTIMES,
    PC_COST_PER_GB_SECOND,
    SECONDS_PER_MONTH,
    _snapstart_enabled,
    recommend_lambda_snapstart,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def _make_aws_client():
    client = MagicMock()
    client._session = None
    return client


def _make_fn(name: str, runtime: str, snapstart_apply_on: str | None = None, memory: int = 512) -> dict:
    fn = {
        "FunctionName": name,
        "Runtime": runtime,
        "MemorySize": memory,
    }
    if snapstart_apply_on is not None:
        fn["SnapStart"] = {"ApplyOn": snapstart_apply_on}
    return fn


@contextmanager
def _mock_session(functions: list[dict], pc_configs: dict | None = None):
    """
    Build a mocked boto3 session.

    pc_configs: dict mapping function_name -> list of PC config dicts.
                If None, all functions return empty PC configs.
    """
    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session

        lambda_client = MagicMock()
        session.client.return_value = lambda_client

        paginator = MagicMock()
        paginator.paginate.return_value = [{"Functions": functions}]
        lambda_client.get_paginator.return_value = paginator

        def list_pc(FunctionName, **kw):
            configs = (pc_configs or {}).get(FunctionName, [])
            return {"ProvisionedConcurrencyConfigs": configs}

        lambda_client.list_provisioned_concurrency_configs.side_effect = list_pc

        yield lambda_client


# ── unit: _snapstart_enabled ──────────────────────────────────────────────────

def test_snapstart_enabled_returns_true_when_published_versions():
    fn = _make_fn("fn", "java17", snapstart_apply_on="PublishedVersions")
    assert _snapstart_enabled(fn) is True


def test_snapstart_enabled_returns_false_when_none():
    fn = _make_fn("fn", "java17", snapstart_apply_on="None")
    assert _snapstart_enabled(fn) is False


def test_snapstart_enabled_returns_false_when_missing():
    fn = _make_fn("fn", "java17")  # no SnapStart key
    assert _snapstart_enabled(fn) is False


# ── unit: Java runtime filtering ─────────────────────────────────────────────

def test_java_runtimes_set_contents():
    assert "java17" in JAVA_RUNTIMES
    assert "java21" in JAVA_RUNTIMES
    assert "java11" in JAVA_RUNTIMES
    assert "python3.12" not in JAVA_RUNTIMES
    assert "nodejs20.x" not in JAVA_RUNTIMES


# ── unit: PC cost math ────────────────────────────────────────────────────────

def test_pc_monthly_cost_formula():
    # 10 concurrency, 1 GB memory
    provisioned = 10
    memory_gb = 1.0
    expected = provisioned * memory_gb * SECONDS_PER_MONTH * PC_COST_PER_GB_SECOND
    assert abs(expected - 108.0) < 1.0


# ── integration: non-Java runtimes skipped ────────────────────────────────────

def test_skips_non_java_runtimes():
    aws_client = _make_aws_client()
    functions = [
        _make_fn("py-fn", "python3.12"),
        _make_fn("node-fn", "nodejs20.x"),
    ]
    with _mock_session(functions):
        result = _run(recommend_lambda_snapstart(aws_client=aws_client, regions=["us-east-1"]))
    assert result == []


# ── integration: Java with no SnapStart, no PC ────────────────────────────────

def test_java_without_snapstart_and_no_pc():
    aws_client = _make_aws_client()
    functions = [_make_fn("java-fn", "java17")]

    with _mock_session(functions, pc_configs={}):
        result = _run(recommend_lambda_snapstart(aws_client=aws_client, regions=["us-east-1"]))

    assert len(result) == 1
    finding = result[0]
    assert finding["function_name"] == "java-fn"
    assert finding["runtime"] == "java17"
    assert finding["snapstart_enabled"] is False
    assert finding["has_provisioned_concurrency"] is False
    assert finding["monthly_pc_cost"] == 0.0
    assert finding["recommendation"] == "enable_snapstart_eliminate_cold_starts_free"


# ── integration: Java with PC but no SnapStart ────────────────────────────────

def test_java_with_pc_no_snapstart_flagged_as_replace():
    aws_client = _make_aws_client()
    functions = [_make_fn("java-pc-fn", "java11", memory=1024)]
    pc_data = {
        "java-pc-fn": [
            {
                "FunctionArn": "arn:aws:lambda:us-east-1:123:function:java-pc-fn:prod",
                "AllocatedProvisionedConcurrentExecutions": 10,
            }
        ]
    }

    with _mock_session(functions, pc_configs=pc_data):
        result = _run(recommend_lambda_snapstart(aws_client=aws_client, regions=["us-east-1"]))

    assert len(result) == 1
    finding = result[0]
    assert finding["has_provisioned_concurrency"] is True
    assert finding["monthly_pc_cost"] > 0
    assert finding["recommendation"] == "enable_snapstart_replace_provisioned_concurrency"


# ── integration: Java with SnapStart already enabled, no PC ──────────────────

def test_java_with_snapstart_no_pc_no_action():
    aws_client = _make_aws_client()
    functions = [_make_fn("java-snap-fn", "java21", snapstart_apply_on="PublishedVersions")]

    with _mock_session(functions, pc_configs={}):
        result = _run(recommend_lambda_snapstart(aws_client=aws_client, regions=["us-east-1"]))

    assert len(result) == 1
    assert result[0]["recommendation"] == "no_action_snapstart_enabled"
    assert result[0]["snapstart_enabled"] is True
    assert result[0]["has_provisioned_concurrency"] is False


# ── integration: SnapStart on but PC still configured ────────────────────────

def test_java_with_snapstart_and_pc_recommends_removing_pc():
    aws_client = _make_aws_client()
    functions = [_make_fn("java-both-fn", "java17", snapstart_apply_on="PublishedVersions", memory=512)]
    pc_data = {
        "java-both-fn": [
            {
                "FunctionArn": "arn:aws:lambda:us-east-1:123:function:java-both-fn:1",
                "AllocatedProvisionedConcurrentExecutions": 5,
            }
        ]
    }

    with _mock_session(functions, pc_configs=pc_data):
        result = _run(recommend_lambda_snapstart(aws_client=aws_client, regions=["us-east-1"]))

    assert len(result) == 1
    assert result[0]["recommendation"] == "remove_provisioned_concurrency_snapstart_already_enabled"
    assert result[0]["has_provisioned_concurrency"] is True


# ── integration: sorted by monthly_pc_cost descending ────────────────────────

def test_sorted_by_pc_cost_descending():
    aws_client = _make_aws_client()
    functions = [
        _make_fn("small-fn", "java11", memory=512),
        _make_fn("big-fn", "java11", memory=2048),
    ]
    pc_data = {
        "small-fn": [{"FunctionArn": "arn:...:small-fn:1", "AllocatedProvisionedConcurrentExecutions": 2}],
        "big-fn":   [{"FunctionArn": "arn:...:big-fn:1",   "AllocatedProvisionedConcurrentExecutions": 20}],
    }

    with _mock_session(functions, pc_configs=pc_data):
        result = _run(recommend_lambda_snapstart(aws_client=aws_client, regions=["us-east-1"]))

    assert len(result) == 2
    assert result[0]["monthly_pc_cost"] >= result[1]["monthly_pc_cost"]
    assert result[0]["function_name"] == "big-fn"


# ── integration: empty region returns no findings ────────────────────────────

def test_returns_empty_when_no_functions():
    aws_client = _make_aws_client()

    with _mock_session([], pc_configs={}):
        result = _run(recommend_lambda_snapstart(aws_client=aws_client, regions=["us-east-1"]))

    assert result == []
