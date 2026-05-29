"""
Terraform state resolver.

Reads terraform.tfstate (or runs `terraform show -json`) to build a mapping
from cloud resource IDs to Terraform resource addresses.

This lets nable resolve AWS instance IDs like "i-0a3f12345678" to their
Terraform resource type and name (e.g. aws_instance.api_server) automatically,
without the user having to specify the mapping manually.

Supported resource types and their ID attributes:

  aws_instance                    → id (EC2 instance ID)
  aws_db_instance                 → id (RDS instance identifier)
  aws_rds_cluster_instance        → id
  aws_elasticache_cluster         → id
  aws_elasticache_replication_group → id
  aws_redshift_cluster            → id
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Resource types we care about for rightsizing.
# Maps resource_type -> list of attribute names that hold the cloud resource ID.
_ID_ATTRS: dict[str, list[str]] = {
    "aws_instance":                       ["id"],
    "aws_db_instance":                    ["id"],
    "aws_rds_cluster_instance":           ["id"],
    "aws_elasticache_cluster":            ["id", "cluster_id"],
    "aws_elasticache_replication_group":  ["id"],
    "aws_redshift_cluster":               ["id", "cluster_identifier"],
}


def _read_state_json(tf_dir: str) -> dict[str, Any]:
    """Return parsed Terraform state JSON.

    Tries in order:
      1. terraform.tfstate in tf_dir (fastest, no CLI needed)
      2. `terraform show -json` (current workspace state via CLI)

    Raises RuntimeError if neither works.
    """
    state_file = Path(tf_dir) / "terraform.tfstate"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except json.JSONDecodeError as exc:
            log.warning("Could not parse terraform.tfstate: %s", exc)

    # Fall back to CLI
    tf_bin = os.environ.get("TERRAFORM_BIN", "terraform")
    result = subprocess.run(
        [tf_bin, "show", "-json"],
        cwd=tf_dir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`terraform show -json` failed in {tf_dir}:\n{result.stderr[:1000]}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse `terraform show -json` output: {exc}") from exc


def _iter_resources(state: dict[str, Any]):
    """Yield (resource_type, resource_name, module_path, instances) from state JSON.

    Handles both flat state (v4) and plan-output format.
    """
    # Flat state format (terraform.tfstate, format version 4)
    for resource in state.get("resources", []):
        rtype = resource.get("type", "")
        rname = resource.get("name", "")
        module = resource.get("module", "")
        instances = resource.get("instances", [])
        if rtype and rname:
            yield rtype, rname, module, instances

    # Plan output format: values.root_module.resources
    root = state.get("values", {}).get("root_module", {})
    for resource in root.get("resources", []):
        rtype = resource.get("type", "")
        rname = resource.get("name", "")
        attrs = resource.get("values", {})
        if rtype and rname:
            # Wrap in instances list for uniform handling
            yield rtype, rname, "", [{"attributes": attrs}]

    # Plan output: child modules
    for child in root.get("child_modules", []):
        for resource in child.get("resources", []):
            rtype = resource.get("type", "")
            rname = resource.get("name", "")
            attrs = resource.get("values", {})
            if rtype and rname:
                yield rtype, rname, child.get("address", ""), [{"attributes": attrs}]


def build_id_map(tf_dir: str) -> dict[str, dict[str, str]]:
    """Return a mapping of cloud_resource_id -> {tf_resource_type, tf_resource_name, module}.

    Example:
        {
            "i-0a3f12345678": {
                "tf_resource_type": "aws_instance",
                "tf_resource_name": "api_server",
                "module": "",
                "instance_type": "m5.4xlarge",
            },
            "mydb": {
                "tf_resource_type": "aws_db_instance",
                "tf_resource_name": "postgres_main",
                "module": "module.rds",
                "instance_class": "db.r5.large",
            },
        }

    Raises RuntimeError if state cannot be loaded.
    """
    state = _read_state_json(tf_dir)
    id_map: dict[str, dict[str, str]] = {}

    for rtype, rname, module, instances in _iter_resources(state):
        id_attrs = _ID_ATTRS.get(rtype)
        if not id_attrs:
            continue  # not a rightsizing-relevant resource

        for instance in instances:
            attrs = instance.get("attributes", {})
            if not attrs:
                continue

            # Find the cloud resource ID
            cloud_id = None
            for attr in id_attrs:
                val = attrs.get(attr)
                if val and isinstance(val, str):
                    cloud_id = val
                    break

            if not cloud_id:
                continue

            entry: dict[str, str] = {
                "tf_resource_type": rtype,
                "tf_resource_name": rname,
                "module": module,
            }

            # Carry the current sizing attribute for context
            for size_attr in ("instance_type", "instance_class", "node_type"):
                val = attrs.get(size_attr)
                if val:
                    entry[size_attr] = val
                    break

            id_map[cloud_id] = entry

            # Also index by secondary IDs (e.g. RDS identifier vs ARN)
            for extra_attr in ("db_instance_identifier", "cluster_identifier", "identifier"):
                extra_val = attrs.get(extra_attr)
                if extra_val and isinstance(extra_val, str) and extra_val not in id_map:
                    id_map[extra_val] = entry

    return id_map


def resolve_recommendation(
    tf_dir: str,
    resource_id: str,
    resource_name: str | None = None,
    id_map: dict[str, dict[str, str]] | None = None,
) -> dict[str, str] | None:
    """Resolve a single recommendation's resource_id to its Terraform address.

    Tries three strategies in order:
      1. Direct ID match in state (e.g. "i-0a3f...")
      2. Name match in state (e.g. RDS identifiers which are human-readable)
      3. Returns None if unresolvable

    Args:
        tf_dir:        Terraform working directory.
        resource_id:   AWS/cloud resource ID from the recommendation.
        resource_name: Optional human-readable name (used as fallback).
        id_map:        Pre-built map from build_id_map(). Pass this to avoid
                       re-reading state for each recommendation.

    Returns dict with tf_resource_type, tf_resource_name (and optionally module),
    or None if the resource couldn't be found in state.
    """
    if id_map is None:
        try:
            id_map = build_id_map(tf_dir)
        except RuntimeError as exc:
            log.warning("Could not build Terraform ID map: %s", exc)
            return None

    # Direct ID match
    match = id_map.get(resource_id)
    if match:
        return match

    # Name match (useful for RDS where resource_name might be the DB identifier)
    if resource_name:
        match = id_map.get(resource_name)
        if match:
            return match

    return None
