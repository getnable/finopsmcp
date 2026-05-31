"""
Tests for the immutable audit logger (GovCloud compliance).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from finops.audit.logger import AuditLogger, _hash_key, _audit_log_path


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_logger(tmp_path: Path, **env_overrides) -> tuple[AuditLogger, Path]:
    """Return an AuditLogger that writes to a temp directory."""
    log_path = tmp_path / "audit.log"

    # Patch the path function to use tmp_path
    with patch("finops.audit.logger._audit_log_path", return_value=log_path):
        logger = AuditLogger()
    logger._path = log_path
    return logger, log_path


# ── unit tests ────────────────────────────────────────────────────────────────

def test_log_success(tmp_path):
    logger, log_path = _make_logger(tmp_path)
    logger.log_tool_call(tool="get_cost_summary", duration_ms=123, outcome="success")

    assert log_path.exists()
    record = json.loads(log_path.read_text().strip())
    assert record["tool"] == "get_cost_summary"
    assert record["duration_ms"] == 123
    assert record["outcome"] == "success"
    assert "error" not in record
    assert record["user_identity"] == "local"
    # ts should be ISO 8601
    assert "T" in record["ts"] and record["ts"].endswith("Z")


def test_log_error_includes_message(tmp_path):
    logger, log_path = _make_logger(tmp_path)
    logger.log_tool_call(
        tool="get_cost_summary",
        duration_ms=5,
        outcome="error",
        error="boto3 credentials not found",
    )
    record = json.loads(log_path.read_text().strip())
    assert record["outcome"] == "error"
    assert record["error"] == "boto3 credentials not found"


def test_log_denied(tmp_path):
    logger, log_path = _make_logger(tmp_path)
    logger.log_tool_call(tool="create_rightsizing_tickets", duration_ms=1, outcome="denied")
    record = json.loads(log_path.read_text().strip())
    assert record["outcome"] == "denied"


def test_log_account_field(tmp_path):
    logger, log_path = _make_logger(tmp_path)
    logger.log_tool_call(
        tool="get_cost_summary",
        duration_ms=200,
        outcome="success",
        account="123456789012",
    )
    record = json.loads(log_path.read_text().strip())
    assert record["account"] == "123456789012"


def test_log_null_account_when_not_set(tmp_path):
    logger, log_path = _make_logger(tmp_path)
    logger.log_tool_call(tool="whoami", duration_ms=2, outcome="success")
    record = json.loads(log_path.read_text().strip())
    assert record["account"] is None


def test_multiple_calls_append(tmp_path):
    logger, log_path = _make_logger(tmp_path)
    logger.log_tool_call(tool="tool_a", duration_ms=10, outcome="success")
    logger.log_tool_call(tool="tool_b", duration_ms=20, outcome="success")

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["tool"] == "tool_a"
    assert json.loads(lines[1])["tool"] == "tool_b"


def test_disabled_by_env(tmp_path):
    with patch.dict(os.environ, {"FINOPS_NO_AUDIT": "1"}):
        logger = AuditLogger()
    logger._path = tmp_path / "audit.log"
    logger.log_tool_call(tool="get_cost_summary", duration_ms=1, outcome="success")
    assert not logger._path.exists()


def test_write_failure_does_not_raise(tmp_path):
    logger, log_path = _make_logger(tmp_path)
    # Point at an unwritable location
    logger._path = Path("/proc/unwritable_dir/audit.log")
    # Should not raise
    logger.log_tool_call(tool="get_cost_summary", duration_ms=1, outcome="success")


def test_api_key_hashed_not_logged(tmp_path):
    logger, log_path = _make_logger(tmp_path)
    fake_key = "nbl_deadbeef1234567890abcdef1234567890"
    with patch.dict(os.environ, {"FINOPS_API_KEY": fake_key}):
        logger.log_tool_call(tool="get_cost_summary", duration_ms=1, outcome="success")
    record = json.loads(log_path.read_text().strip())
    # Raw key must NOT appear
    assert fake_key not in json.dumps(record)
    # Identity should be a short hash
    assert record["user_identity"] != "local"
    assert len(record["user_identity"]) == 16


def test_hash_key_is_deterministic():
    h1 = _hash_key("nbl_abc123")
    h2 = _hash_key("nbl_abc123")
    assert h1 == h2
    assert len(h1) == 16


def test_hash_key_different_for_different_keys():
    h1 = _hash_key("nbl_key_one")
    h2 = _hash_key("nbl_key_two")
    assert h1 != h2


def test_profile_path_uses_env(monkeypatch):
    monkeypatch.setenv("FINOPS_PROFILE", "govcloud")
    path = _audit_log_path()
    assert "profiles" in str(path)
    assert "govcloud" in str(path)
    assert str(path).endswith("audit.log")


def test_no_profile_path_is_default(monkeypatch):
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    path = _audit_log_path()
    assert "profiles" not in str(path)
    assert str(path).endswith(".finops/audit.log")
