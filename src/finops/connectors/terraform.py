"""
Terraform state reader and tag auditor.

Reads Terraform state (via `terraform show -json` or a .tfstate file) and
compares each taggable resource's tags against FINOPS_REQUIRED_TAGS.
Violations are stored in the `terraform_tag_audits` DB table.

Not a BaseConnector subclass — this module deals with IaC metadata, not
cost data.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import date
from fnmatch import fnmatch
from typing import Any

log = logging.getLogger(__name__)

# Resource types that cannot carry tags (skip them)
_UNTAGGABLE_EXACT = {
    "null_resource",
    "terraform_remote_state",
}
_UNTAGGABLE_GLOB = [
    "data.*",
    "random_*",
    "local_*",
    "time_*",
]


# ── State loading ─────────────────────────────────────────────────────────────

def _load_state(tf_dir: str, state_path: str | None = None) -> dict:
    """Run `terraform show -json` or read a .tfstate file. Returns parsed JSON."""
    if state_path:
        with open(state_path) as f:
            return json.load(f)
    result = subprocess.run(
        [os.environ.get("TERRAFORM_BIN", "terraform"), "show", "-json"],
        cwd=tf_dir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"terraform show -json failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


# ── Configuration ─────────────────────────────────────────────────────────────

def _required_tags() -> list[str]:
    """Return required tag keys from FINOPS_REQUIRED_TAGS env var.

    Defaults to 'team,environment,service' if not set.
    """
    raw = os.environ.get("FINOPS_REQUIRED_TAGS", "team,environment,service")
    return [t.strip() for t in raw.split(",") if t.strip()]


def _is_taggable(resource_type: str) -> bool:
    """Return False for resource types that do not support tags."""
    if resource_type in _UNTAGGABLE_EXACT:
        return False
    for pattern in _UNTAGGABLE_GLOB:
        if fnmatch(resource_type, pattern):
            return False
    return True


# ── State parsing ─────────────────────────────────────────────────────────────

def _extract_resources(state: dict) -> list[dict]:
    """Walk state values.root_module.resources (+ child_modules recursively).

    Returns a list of dicts: {address, type, name, provider, tags, file_path}.
    Tags are extracted from attributes.tags (AWS/Azure) or attributes.labels (GCP).
    """
    resources: list[dict] = []

    def _walk(module: dict) -> None:
        for res in module.get("resources", []):
            attrs = res.get("values", {})
            tags: Any = attrs.get("tags") or attrs.get("labels") or {}
            if not isinstance(tags, dict):
                tags = {}
            resources.append({
                "address": res.get("address", ""),
                "type": res.get("type", ""),
                "name": res.get("name", ""),
                "provider": res.get("provider_name", ""),
                "tags": tags,
                # tfstate doesn't expose source file; hcl_patcher will scan .tf files
                "file_path": "",
            })
        for child in module.get("child_modules", []):
            _walk(child)

    root = state.get("values", {}).get("root_module", {})
    _walk(root)
    return resources


# ── Public API ────────────────────────────────────────────────────────────────

def audit_tags(tf_dir: str, state_path: str | None = None) -> list[dict]:
    """Scan Terraform state and return tag violations.

    Returns a list of dicts:
        address       "module.vpc.aws_subnet.private[0]"
        type          "aws_subnet"
        name          "private"
        current_tags  {"Name": "private", "env": "prod"}
        missing_tags  ["team", "service"]
        file_path     "" (populated later by hcl_patcher)
    """
    state = _load_state(tf_dir, state_path)
    resources = _extract_resources(state)
    required = _required_tags()
    violations: list[dict] = []

    for res in resources:
        if not _is_taggable(res["type"]):
            continue
        current = res["tags"]
        missing = [t for t in required if t not in current]
        if missing:
            violations.append({
                "address": res["address"],
                "type": res["type"],
                "name": res["name"],
                "current_tags": current,
                "missing_tags": missing,
                "file_path": res["file_path"],
            })

    log.info(
        "terraform tag audit: tf_dir=%s resources=%d violations=%d",
        tf_dir, len(resources), len(violations),
    )
    return violations


def persist_violations(tf_dir: str, violations: list[dict]) -> int:
    """Insert violations into the terraform_tag_audits table.

    Returns the number of rows inserted.
    """
    if not violations:
        return 0

    from ..storage.db import terraform_tag_audits, get_engine  # lazy to avoid circular

    engine = get_engine()
    today = date.today().isoformat()
    rows = [
        {
            "tf_dir": tf_dir,
            "audit_date": today,
            "resource_address": v["address"],
            "resource_type": v["type"],
            "resource_name": v["name"],
            "current_tags": json.dumps(v["current_tags"]),
            "missing_tags": json.dumps(v["missing_tags"]),
            "status": "open",
            "pr_url": None,
            "file_path": v.get("file_path", ""),
        }
        for v in violations
    ]

    with engine.begin() as conn:
        conn.execute(terraform_tag_audits.insert(), rows)

    log.info("persisted %d terraform tag violations for %s", len(rows), tf_dir)
    return len(rows)
