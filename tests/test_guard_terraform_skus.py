# SPDX-License-Identifier: Apache-2.0
"""A saved plan's several SKUs, each with its path and planned name (D166, D167, D169).

What has to stay true:
  - a saved plan holding an aws_instance m5.large, an aws_db_instance
    db.r6g.large postgres (identifier "orders-db") and an
    aws_elasticache_cluster cache.r6g.large in eu-west-1 asks, and its ledger
    line's `priced` is a list of three SKUs, each with region eu-west-1 and
    path "terraform"; the RDS and cache SKUs carry their identifier, and are
    priced at eu-west-1's listed rates from the committed tables
  - a shell command's SKU carries path "cli"
  - an identifier the plan does not know (identifier_prefix, or computed at
    apply) and a replace record no identifier; an identifier not in AWS's
    form records none
  - an older ledger line (a single priced dict, or a list without path)
    still reads in verify-log and every reader
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

import finops.guard as g
import finops.guard_ledger as gl
from finops import ai_budget, aws_prices, cloud_standing
from finops.aws_prices import EC2_HOURLY, EC2_REGION_RANGE, HOURS_PER_MONTH
from finops.connectors.terraform_estimate import parse_plan, planned_identifier, priced_skus

AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_GUARD_TEAM", "FINOPS_POLICY_FILE", "FINOPS_PROFILE", "FINOPS_DATA_DIR",
                "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "XDG_CONFIG_HOME",
                "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    monkeypatch.setattr(cloud_standing, "now_epoch", lambda: AT.timestamp())


def _rc(address, rtype, after, actions=("create",), unknown=None):
    change = {"actions": list(actions), "before": None, "after": after}
    if unknown is not None:
        change["after_unknown"] = unknown
    return {"address": address, "type": rtype, "mode": "managed", "change": change}


def _plan(resources, region="eu-west-1"):
    return {"format_version": "1.2",
            "configuration": {"provider_config": {"aws": {
                "name": "aws", "expressions": {"region": {"constant_value": region}}}}},
            "resource_changes": resources}


THREE = [
    _rc("aws_instance.app", "aws_instance", {"instance_type": "m5.large"}),
    _rc("aws_db_instance.orders", "aws_db_instance",
        {"instance_class": "db.r6g.large", "engine": "postgres", "identifier": "orders-db",
         "multi_az": False}),
    _rc("aws_elasticache_cluster.sessions", "aws_elasticache_cluster",
        {"node_type": "cache.r6g.large", "engine": "redis", "num_cache_nodes": 1,
         "cluster_id": "sessions"}),
]


def _records():
    p = gl.ledger_path()
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_a_saved_plan_of_three_resources_asks_with_three_skus(tmp_path, monkeypatch):
    doc = _plan(THREE)
    (tmp_path / "plan.out").write_bytes(b"binary plan")
    monkeypatch.setattr(g, "_plan_read", lambda *a, **k: ("terraform", "plan.out", doc))
    monkeypatch.setenv("FINOPS_POLICY_MAX_AUTO_USD", "100")
    v = g.gate_command("terraform apply plan.out", cwd=str(tmp_path))
    assert v["decision"] == "ask"
    rec = [r for r in _records() if r.get("priced")][-1]
    priced = rec["priced"]
    assert isinstance(priced, list) and len(priced) == 3
    assert {p["region"] for p in priced} == {"eu-west-1"}
    assert {p["path"] for p in priced} == {"terraform"}
    by = {p["product_code"]: p for p in priced}
    assert "identifier" not in by["AmazonEC2"]
    assert by["AmazonRDS"]["identifier"] == "orders-db"
    assert by["AmazonElastiCache"]["identifier"] == "sessions"
    rds = aws_prices.rds_rate("db.r6g.large", "postgres", "eu-west-1")
    cache = aws_prices.elasticache_rate("cache.r6g.large", "redis", "eu-west-1")
    assert by["AmazonRDS"]["usd_hour"] == float(rds.hourly_usd)
    assert by["AmazonElastiCache"]["usd_hour"] == float(cache.hourly_usd)
    assert by["AmazonEC2"]["usd_hour"] == round(
        EC2_HOURLY["m5.large"] * EC2_REGION_RANGE["eu-west-1"][0], 6)
    listed = round((float(rds.hourly_usd) + float(cache.hourly_usd)
                    + by["AmazonEC2"]["usd_hour"]) * HOURS_PER_MONTH, 2)
    assert rec["monthly_usd"] == pytest.approx(listed, abs=0.02)
    assert "eu-west-1" in rec["basis"]


def test_a_shell_commands_sku_carries_the_cli_path():
    est = g.estimate_command_monthly_cost(
        "aws rds create-db-instance --region eu-west-1 --db-instance-identifier orders-db "
        "--engine postgres --db-instance-class db.r6g.large")
    assert est["sku"]["path"] == "cli" and "identifier" not in est["sku"]


@pytest.mark.parametrize("after, unknown, actions", [
    ({"instance_class": "db.r6g.large", "engine": "postgres", "identifier": None,
      "identifier_prefix": "orders-"}, {"identifier": True}, ("create",)),
    ({"instance_class": "db.r6g.large", "engine": "postgres"}, {"identifier": True},
     ("create",)),
    ({"instance_class": "db.r6g.large", "engine": "postgres", "identifier": "orders-db"}, None,
     ("delete", "create")),
    ({"instance_class": "db.r6g.large", "engine": "postgres", "identifier": "orders-db"}, None,
     ("create", "delete")),
    ({"instance_class": "db.r6g.large", "engine": "postgres", "identifier": "1-not-a-name"},
     None, ("create",)),
    ({"instance_class": "db.r6g.large", "engine": "postgres", "identifier": "x" * 64}, None,
     ("create",)),
], ids=["identifier_prefix", "computed", "replace delete first", "replace create first",
        "not aws form", "too long"])
def test_an_identifier_the_plan_does_not_know_records_none(after, unknown, actions):
    rc = _rc("aws_db_instance.orders", "aws_db_instance", after, actions, unknown)
    [change] = parse_plan(_plan([rc]))
    assert planned_identifier(change) is None
    for sku in priced_skus(_plan([rc])):
        assert "identifier" not in sku


def test_an_elasticache_cluster_id_is_its_identifier():
    [change] = parse_plan(_plan([THREE[2]]))
    assert planned_identifier(change) == "sessions"
    long = _rc("aws_elasticache_cluster.c", "aws_elasticache_cluster",
               {"node_type": "cache.r6g.large", "cluster_id": "c" * 51})
    [change] = parse_plan(_plan([long]))
    assert planned_identifier(change) is None


def test_a_replication_group_or_an_instance_names_no_identifier():
    group = _rc("aws_elasticache_replication_group.g", "aws_elasticache_replication_group",
                {"node_type": "cache.r6g.large", "replication_group_id": "g1"})
    [change] = parse_plan(_plan([group]))
    assert planned_identifier(change) is None


# ── older lines read as before ───────────────────────────────────────────────

OLD_SINGLE = {"product_code": "AmazonRDS", "region": "eu-west-1",
              "usage_type": "InstanceUsage:db.r6g.large", "quantity": 1, "unit": "Hrs",
              "hours": 730.0, "usd_hour": 0.252}
OLD_LIST = [OLD_SINGLE, {"product_code": "AmazonEC2", "region": "eu-west-1",
                         "usage_type": "BoxUsage:m5.large", "quantity": 1, "unit": "Hrs",
                         "hours": 730.0, "usd_hour": 0.102624}]


def test_older_lines_still_read_in_verify_log_and_every_reader(tmp_path, monkeypatch):
    for priced in (OLD_SINGLE, OLD_LIST):
        assert gl.append({"harness": "claude-code", "tool": "Bash",
                          "command": "terraform apply plan.out", "door": "two_way",
                          "action_type": "infra_apply", "decision": "ask",
                          "monthly_usd": 258.92, "total_usd": None, "basis": "fixture",
                          "reason": "nable guard: fixture", "price_basis": "list",
                          "priced": priced, "outcome": None})
    doc = _plan(THREE)
    (tmp_path / "plan.out").write_bytes(b"binary plan")
    monkeypatch.setattr(g, "_plan_read", lambda *a, **k: ("terraform", "plan.out", doc))
    g.gate_command("terraform apply plan.out", cwd=str(tmp_path))
    assert gl.verify()["ok"] is True
    recs = gl.read(outcomes=True)
    assert sum(1 for r in recs if r.get("priced")) == 3
    s = gl.summarize(30)
    assert s["by_decision"]["ask"] >= 2
    assert len(gl.export_records()) == len(_records())
    assert gl.ask_outcomes(recs) is not None
    assert gl.count_asks("infra_apply")["asks"] >= 2
    from finops import cloud_connect as cc
    led = cc._ledger(None)
    assert led["ok"] is True and led["records_planning"] == 1
    assert led["planned_identifiers"] == 2
