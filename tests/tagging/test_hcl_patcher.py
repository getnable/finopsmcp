"""Tests for finops.tagging.hcl_patcher."""
from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from finops.tagging.hcl_patcher import (
    _build_patched_content,
    generate_rightsizing_diff,
    patch_file,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _violation(rtype: str, name: str, missing: list[str]) -> dict:
    return {"type": rtype, "name": name, "missing_tags": missing}


# ── Test 1: inject tags into a resource with no tags block ────────────────────

def test_inject_tags_no_existing_block(tmp_path: Path) -> None:
    original = (
        'resource "aws_instance" "web" {\n'
        '  instance_type = "t3.micro"\n'
        '  ami           = "ami-0abc1234"\n'
        '}\n'
    )

    violations = [_violation("aws_instance", "web", ["Name", "env"])]
    patched = _build_patched_content(original, violations)

    # A tags block must have been injected
    assert "tags" in patched
    assert "Name" in patched
    assert "env" in patched
    # Original instance_type line must be preserved
    assert 'instance_type = "t3.micro"' in patched
    # Content changed
    assert patched != original


# ── Test 2: inject missing keys into an existing tags block ───────────────────

def test_inject_missing_keys_into_existing_tags() -> None:
    original = (
        'resource "aws_instance" "api" {\n'
        '  instance_type = "t3.small"\n'
        '  tags = {\n'
        '    Name = "api-server"\n'
        '  }\n'
        '}\n'
    )

    violations = [_violation("aws_instance", "api", ["env", "team"])]
    patched = _build_patched_content(original, violations)

    # Existing key must survive unchanged
    assert 'Name = "api-server"' in patched
    # Missing keys must be added
    assert "env" in patched
    assert "team" in patched
    assert patched != original


# ── Test 3: heredoc blocks are skipped, not corrupted ─────────────────────────

def test_heredoc_block_is_skipped(tmp_path: Path) -> None:
    tf = tmp_path / "main.tf"
    original = (
        'resource "aws_iam_policy" "example" {\n'
        '  name   = "example"\n'
        '  policy = <<EOT\n'
        '{\n'
        '  "Version": "2012-10-17"\n'
        '}\n'
        'EOT\n'
        '}\n'
    )
    tf.write_text(original)

    violations = [_violation("aws_iam_policy", "example", ["Name"])]
    diff = patch_file(str(tf), violations)

    # Patcher must return None (no changes) rather than corrupting the file
    assert diff is None, "Heredoc resource should not be patched"
    assert tf.read_text() == original


# ── Test 4: only the targeted resource is patched in a multi-resource file ────

def test_only_targeted_resource_is_patched() -> None:
    original = (
        'resource "aws_instance" "web" {\n'
        '  instance_type = "t3.micro"\n'
        '}\n'
        '\n'
        'resource "aws_instance" "worker" {\n'
        '  instance_type = "t3.large"\n'
        '}\n'
    )

    violations = [_violation("aws_instance", "web", ["Name"])]
    patched = _build_patched_content(original, violations)

    # Both resource declarations must still be present
    assert 'resource "aws_instance" "web"' in patched
    assert 'resource "aws_instance" "worker"' in patched

    # Tags should only appear once (in the web block), not twice
    assert patched.count("tags") == 1


# ── Test 5: generate_rightsizing_diff patches instance_type ───────────────────

def test_generate_rightsizing_diff_changes_instance_type(tmp_path: Path) -> None:
    tf = tmp_path / "ec2.tf"
    tf.write_text(
        'resource "aws_instance" "app" {\n'
        '  instance_type = "m5.xlarge"\n'
        '  ami           = "ami-0abc"\n'
        '}\n'
    )

    diff = generate_rightsizing_diff(
        file_path=str(tf),
        resource_type="aws_instance",
        resource_name="app",
        new_value="m5.large",
    )

    assert diff is not None
    assert "m5.xlarge" in diff   # old value in diff
    assert "m5.large" in diff    # new value in diff


def test_generate_rightsizing_diff_returns_none_when_unchanged(tmp_path: Path) -> None:
    tf = tmp_path / "ec2.tf"
    tf.write_text(
        'resource "aws_instance" "app" {\n'
        '  instance_type = "m5.large"\n'
        '}\n'
    )

    diff = generate_rightsizing_diff(
        file_path=str(tf),
        resource_type="aws_instance",
        resource_name="app",
        new_value="m5.large",  # same value — no change
    )

    assert diff is None


def test_generate_rightsizing_diff_skips_heredoc(tmp_path: Path) -> None:
    tf = tmp_path / "ec2.tf"
    tf.write_text(
        'resource "aws_instance" "app" {\n'
        '  instance_type = "m5.xlarge"\n'
        '  user_data     = <<EOT\n'
        '#!/bin/bash\n'
        'echo hello\n'
        'EOT\n'
        '}\n'
    )

    diff = generate_rightsizing_diff(
        file_path=str(tf),
        resource_type="aws_instance",
        resource_name="app",
        new_value="m5.large",
    )

    assert diff is None, "Heredoc resource should not be patched"


# ── Test 6: diff output is valid unified diff format ─────────────────────────

def test_diff_is_valid_unified_diff(tmp_path: Path) -> None:
    tf = tmp_path / "main.tf"
    tf.write_text(
        'resource "aws_db_instance" "db" {\n'
        '  instance_class = "db.r5.large"\n'
        '  engine         = "postgres"\n'
        '}\n'
    )

    diff = generate_rightsizing_diff(
        file_path=str(tf),
        resource_type="aws_db_instance",
        resource_name="db",
        new_value="db.r5.medium",
    )

    assert diff is not None
    lines = diff.splitlines()
    # Unified diff starts with --- and +++ header lines
    assert any(line.startswith("---") for line in lines)
    assert any(line.startswith("+++") for line in lines)
    # At least one hunk header
    assert any(line.startswith("@@") for line in lines)
    # Old and new values present as removed/added lines
    assert any(line.startswith("-") and "db.r5.large" in line for line in lines)
    assert any(line.startswith("+") and "db.r5.medium" in line for line in lines)
