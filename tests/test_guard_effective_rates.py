# SPDX-License-Identifier: Apache-2.0
"""The workspace's effective rate on the guard's ask: shown, never judged (D146, D148).

What has to stay true:
  - an ask for an EC2 instance, an RDS instance, an ElastiCache node or a
    saved Terraform plan holding an aws_instance and an aws_db_instance shows
    the list price times the workspace's on-demand factor, "at your effective
    rate", with the factor's window and as_of, and says the ask is judged at
    list price
  - rule 13: wherever list price raises an ask (the threshold, the velocity
    cap, the cloud budget, a team's threshold, a freeze), the decision, the
    rule, the thresholds and every judged figure are identical with and
    without a rates file whose factor puts the shown price under the
    threshold; only the note differs
  - a missing, stale, other-workspace or unmatched rate says "at list price"
  - where a price book prices the SKU, the book's figure stands and no
    effective rate is applied, said once
  - the ledger line carries contract 2 (priced, price_basis, effective) and
    keeps monthly_usd and total_usd as the figures judged; verify-log still
    verifies the chain and every reader reads the line
  - the rates file is read with sockets made to fail, and only on a priced
    path: a silent command and the editor path open no rates file
"""
from __future__ import annotations

import io
import json
import os
import socket
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

import finops.guard as g
import finops.guard_ledger as gl
import finops.guard_plugin as gp
from finops import ai_budget, cloud_rates, cloud_standing, guard_packs, org
from finops.aws_prices import EC2_HOURLY, rds_hourly
from finops.budget import summary as bs
from finops.connectors.terraform_estimate import elasticache_hourly
from finops.org.cli import _who as human

BOX = "https://box.example.com"
WS_A = "ws_" + "a" * 32
WS_B = "ws_" + "b" * 32
AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
LOW = "0.3000"

EC2 = "aws ec2 run-instances --region us-east-1 --instance-type m5.2xlarge --count 2"
EC2_MONTHLY = round(2 * EC2_HOURLY["m5.2xlarge"] * 730, 2)               # over $500
RDS = ("aws rds create-db-instance --region us-east-1 --db-instance-identifier orders "
       "--engine postgres --db-instance-class db.r5.4xlarge")
RDS_MONTHLY = round(rds_hourly("db.r5.4xlarge", "postgres") * 730, 2)
CACHE = ("aws elasticache create-cache-cluster --region us-east-1 --cache-cluster-id c1 "
         "--engine redis --cache-node-type cache.r6g.xlarge --num-cache-nodes 2")
CACHE_MONTHLY = round(2 * elasticache_hourly("cache.r6g.xlarge", "redis") * 730, 2)
SMALL = "aws ec2 run-instances --region us-east-1 --instance-type m5.2xlarge"  # under $500


def _row(product, usage_type, factor=LOW, *, region="us-east-1", blended=LOW, share="0.6000"):
    return {"product_code": product, "region": region, "usage_type": usage_type,
            "unit": "Hrs", "on_demand_factor": factor, "blended_factor": blended,
            "covered_share": share, "days": 30, "line_items": 120}


ROWS = [_row("AmazonEC2", "BoxUsage:m5.2xlarge"), _row("AmazonEC2", "BoxUsage:m5.4xlarge"),
        _row("AmazonRDS", "InstanceUsage:db.r5.4xlarge"),
        _row("AmazonRDS", "InstanceUsage:db.r5.large"),
        _row("AmazonElastiCache", "NodeUsage:cache.r6g.xlarge")]


def _block(ref=WS_A, as_of="2026-10-18", rows=ROWS):
    start = (datetime.fromisoformat(as_of) - timedelta(days=29)).date().isoformat()
    return {"v": 1, "read": True, "workspace_ref": ref, "as_of": as_of,
            "window": {"start": start, "end": as_of},
            "source": "the AWS cost export at s3://acme-bills/cur/, 2026-09-19 to 2026-10-18",
            "columns": ["line_item_unblended_cost", "pricing_public_on_demand_cost"],
            "rows": rows, "more": 0, "not_served": {}}


def keep_rates(ref=WS_A, **kw):
    cloud_standing.bind(BOX, workspace=WS_A)
    got = cloud_rates.keep(_block(ref=ref, **kw), box=BOX, workspace_ref=ref, fetched_at=AT)
    assert got["kept"] is (ref == WS_A), got


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_POLICY_LOOP_COUNT", "FINOPS_GUARD_TEAM", "FINOPS_GUARD_ACCOUNT",
                "FINOPS_POLICY_ON_BUDGET_BREACH", "FINOPS_POLICY_FILE", "FINOPS_PROFILE",
                "FINOPS_DATA_DIR", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE",
                "AWS_DEFAULT_PROFILE", "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL",
                "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "FINOPS_GUARD_BUDGET_MAX_AGE_HOURS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    monkeypatch.setattr(cloud_standing, "now_epoch", lambda: AT.timestamp())
    import sys
    db = sys.modules.get("finops.storage.db")
    if db is not None:
        monkeypatch.setattr(db, "_DATA_DIR", None)


def _records():
    p = gl.ledger_path()
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _usd(x: float) -> str:
    return f"${x:,.0f}"


def _at_factor(listed: float, factor: str = LOW) -> float:
    """A list figure times the factor, rounded up to the cent: what the guard
    shows at an effective rate is never below it (D168, rule 15)."""
    from decimal import ROUND_CEILING, Decimal
    return float((Decimal(str(listed)) * Decimal(factor)).quantize(
        Decimal("0.01"), rounding=ROUND_CEILING))


# ── the ask shows the effective price beside list ───────────────────────────

@pytest.mark.parametrize("cmd, monthly, usage_type", [
    (EC2, EC2_MONTHLY, "BoxUsage:m5.2xlarge"),
    (RDS, RDS_MONTHLY, "InstanceUsage:db.r5.4xlarge"),
    (CACHE, CACHE_MONTHLY, "NodeUsage:cache.r6g.xlarge"),
], ids=["ec2 run-instances", "rds create-db-instance", "elasticache create-cache-cluster"])
def test_an_ask_shows_the_price_at_your_effective_rate_and_is_judged_at_list(
        cmd, monthly, usage_type):
    keep_rates()
    v = g.gate_command(cmd)
    assert v["decision"] == "ask" and v["rule"] == "threshold"
    assert v["monthly_delta_usd"] == monthly and v["estimate"]["monthly_usd"] == monthly
    r = v["reason"]
    shown = _at_factor(monthly)
    assert f"This is ~{_usd(shown)}/mo at your effective rate" in r, r
    assert "over 2026-09-19 to 2026-10-18 (as of 2026-10-18)" in r
    assert "on-demand factor 0.3000 for" in r and usage_type in r
    assert "60% of whose usage ran on commitments" in r
    assert "The guard judges this ask at list price" in r
    assert f"~{_usd(monthly)}/mo" in r, "the list figure is still said"
    assert v["price_basis"] == "effective" and v["effective"]["monthly_usd"] == shown


def _plan(tmp_path, monkeypatch):
    doc = {"format_version": "1.2",
           "configuration": {"provider_config": {"aws": {
               "name": "aws", "expressions": {"region": {"constant_value": "us-east-1"}}}}},
           "resource_changes": [
               {"address": "aws_instance.app", "type": "aws_instance", "mode": "managed",
                "change": {"actions": ["create"], "before": None,
                           "after": {"instance_type": "m5.4xlarge"}}},
               {"address": "aws_db_instance.db", "type": "aws_db_instance", "mode": "managed",
                "change": {"actions": ["create"], "before": None,
                           "after": {"instance_class": "db.r5.large", "engine": "postgres",
                                     "multi_az": False}}}]}
    (tmp_path / "plan.out").write_bytes(b"binary plan")
    monkeypatch.setattr(g, "_plan_read", lambda *a, **k: ("terraform", "plan.out", doc))
    return doc


def test_a_saved_plan_with_an_instance_and_a_database_shows_both_at_your_effective_rate(
        tmp_path, monkeypatch):
    _plan(tmp_path, monkeypatch)
    keep_rates()
    v = g.gate_command("terraform apply plan.out", cwd=str(tmp_path))
    listed = round(EC2_HOURLY["m5.4xlarge"] * 730 + rds_hourly("db.r5.large", "postgres")
                   * 730, 2)
    assert v["decision"] == "ask" and v["monthly_delta_usd"] == pytest.approx(listed, abs=0.01)
    r = v["reason"]
    assert "at your effective rate" in r and "BoxUsage:m5.4xlarge in us-east-1" in r
    assert "InstanceUsage:db.r5.large in us-east-1" in r
    assert [p["usage_type"] for p in v["priced"]] == ["BoxUsage:m5.4xlarge",
                                                      "InstanceUsage:db.r5.large"]
    assert v["effective"]["monthly_usd"] == pytest.approx(listed * 0.3, abs=0.02)


def _aliased_plan(region_west: dict, *, key: str = "aws.west", module: bool = False) -> dict:
    """A plan whose default provider is in us-east-1 and whose aws.west
    provider (`region_west`, its region expression) creates the instance."""
    res = {"address": "aws_instance.web", "mode": "managed", "type": "aws_instance",
           "name": "web", "provider_config_key": key}
    db = {"address": "aws_db_instance.db", "mode": "managed", "type": "aws_db_instance",
          "name": "db", "provider_config_key": "aws"}
    root: dict = {"resources": [db]}
    address = "aws_instance.web"
    if module:
        root["module_calls"] = {"app": {"source": "./app", "module": {"resources": [res]}}}
        address = "module.app[0].aws_instance.web"
    else:
        root["resources"].append(res)
    change = {"mode": "managed", "change": {"actions": ["create"], "before": None}}
    return {"format_version": "1.2", "configuration": {
        "provider_config": {
            "aws": {"name": "aws", "expressions": {"region": {"constant_value": "us-east-1"}}},
            "aws.west": {"name": "aws", "alias": "west", "expressions": {"region": region_west}}},
        "root_module": root},
        "resource_changes": [
            {**change, "address": address, "type": "aws_instance",
             **({"module_address": "module.app[0]"} if module else {}),
             "change": {**change["change"], "after": {"instance_type": "m5.4xlarge"}}},
            {**change, "address": "aws_db_instance.db", "type": "aws_db_instance",
             "change": {**change["change"], "after": {"instance_class": "db.r5.large",
                                                      "engine": "postgres"}}}]}


@pytest.mark.parametrize("module", [False, True], ids=["root", "module"])
def test_a_resource_made_through_an_aliased_provider_is_priced_in_that_providers_region(
        module):
    from finops.connectors.terraform_estimate import priced_skus
    skus = priced_skus(_aliased_plan({"constant_value": "us-west-2"}, module=module))
    assert {s["usage_type"]: s["region"] for s in skus} == {
        "BoxUsage:m5.4xlarge": "us-west-2", "InstanceUsage:db.r5.large": "us-east-1"}


@pytest.mark.parametrize("region", [{"references": ["var.region"]}, {}],
                         ids=["a variable", "no region"])
def test_an_aliased_providers_region_that_is_not_a_constant_names_none(region):
    from finops.connectors.terraform_estimate import priced_skus
    skus = priced_skus(_aliased_plan(region))
    assert {s["usage_type"]: s["region"] for s in skus} == {
        "BoxUsage:m5.4xlarge": None, "InstanceUsage:db.r5.large": "us-east-1"}


def test_a_provider_naming_no_region_is_not_given_the_environments(monkeypatch):
    """The provider then takes the environment's region when it applies; the
    plan's figure is us-east-1's list price, so no factor is matched."""
    from finops.connectors.terraform_estimate import priced_skus
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    doc = _aliased_plan({"constant_value": "us-west-2"})
    doc["configuration"]["provider_config"]["aws"]["expressions"] = {}
    skus = priced_skus(doc)
    assert {s["usage_type"]: s["region"] for s in skus} == {
        "BoxUsage:m5.4xlarge": "us-west-2", "InstanceUsage:db.r5.large": None}


def test_a_plan_through_an_aliased_provider_is_not_matched_to_the_default_regions_rate(
        tmp_path, monkeypatch):
    doc = _aliased_plan({"constant_value": "us-west-2"})
    (tmp_path / "plan.out").write_bytes(b"binary plan")
    monkeypatch.setattr(g, "_plan_read", lambda *a, **k: ("terraform", "plan.out", doc))
    keep_rates()
    v = g.gate_command("terraform apply plan.out", cwd=str(tmp_path))
    assert v["decision"] == "ask"
    by_type = {p["usage_type"]: p for p in v["priced"]}
    assert by_type["BoxUsage:m5.4xlarge"]["region"] == "us-west-2"
    assert by_type["BoxUsage:m5.4xlarge"]["factor"] is None
    assert by_type["InstanceUsage:db.r5.large"]["factor"] == LOW
    assert "BoxUsage:m5.4xlarge in us-east-1" not in v["reason"]


# ── rule 13: the same verdict, with and without the rate ─────────────────────

def _judged(v: dict) -> dict:
    """Everything a verdict was judged by, with the price note taken out of
    its reason."""
    reason = v["reason"].replace(" " + v.get("price_note", ""), "") if v.get(
        "price_note") else v["reason"]
    keep = ("decision", "rule", "history", "org_thresholds", "freeze", "budget_check",
            "monthly_delta_usd", "action_type", "door")
    return {"reason": reason, **{k: v.get(k) for k in keep},
            "judged_usd": (v.get("estimate") or {}).get("monthly_usd")}


def _velocity(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_LOOP_COUNT", "0")
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "700")
    for minutes in (5, 3):
        gl.append({"ts": (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(
            timespec="seconds"), "decision": "allow", "monthly_usd": 300.0,
            "action_type": "infra_apply", "command": "aws ec2 run-instances x"})
    return SMALL


def _budget(monkeypatch):
    today = datetime.now().astimezone().date()
    start = today.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    bs.write_summary([{"name": "AWS Total", "scope_type": "total", "scope_value": "*",
                       "period": "monthly", "period_start": start.isoformat(),
                       "period_end": end.isoformat(), "spent": 49_999.0, "limit": 50_000.0,
                       "pct_used": 99.9, "status": "ok"}],
                     spend_through=today.isoformat(), now=datetime.now(UTC) - timedelta(hours=2))
    return SMALL


def _budget_deny(monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    return _budget(monkeypatch)


def _team(monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_TEAM", "payments")
    org.set_fact(org.make_fact("threshold", "team:payments", {"max_auto_monthly_usd": 100},
                               source="human"), human("maria"))
    return SMALL


def _freeze(monkeypatch):
    now = datetime.now(UTC)
    f = org.make_fact("freeze", "org:org", {"start": (now - timedelta(hours=1)).isoformat(),
                                            "end": (now + timedelta(hours=2)).isoformat(),
                                            "reason": "Black Friday", "mode": "ask"},
                      source="human")
    org.set_fact(f, human("maria"))
    return SMALL


def _threshold(monkeypatch):
    return EC2


SCENARIOS = {"the threshold": _threshold, "the velocity cap": _velocity,
             "the cloud budget": _budget, "the cloud budget's stop": _budget_deny,
             "a team's threshold": _team, "a freeze": _freeze}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_rule_13_an_effective_rate_never_changes_what_list_price_raised(monkeypatch, name):
    cmd = SCENARIOS[name](monkeypatch)
    without = g.gate_command(cmd)
    assert without is not None and without["decision"] in ("ask", "deny"), without
    assert "at list price" in without["price_note"]
    keep_rates()
    with_rate = g.gate_command(cmd)
    assert with_rate["price_basis"] == "effective"
    assert with_rate["effective"]["monthly_usd"] < 500 or name != "the threshold"
    assert _judged(with_rate) == _judged(without)


def test_a_launch_under_the_threshold_stays_silent_with_its_rate_on_the_ledger():
    keep_rates()
    assert g.gate_command(SMALL) is None
    last = _records()[-1]
    assert last["decision"] == "allow" and last["price_basis"] == "effective"


# ── at list price, and why ───────────────────────────────────────────────────

@pytest.mark.parametrize("case, why", [
    ("none kept", "no effective rate from your workspace is kept on this machine"),
    ("stale", "more than 45 days ago"),
    ("another workspace", "another workspace's"),
    ("unmatched", "hold no on-demand factor for BoxUsage:m5.2xlarge in us-east-1"),
    ("no region", "names no region"),
])
def test_without_a_usable_rate_the_ask_says_at_list_price(monkeypatch, case, why):
    cmd = EC2
    if case == "stale":
        keep_rates(as_of="2026-08-01")
    elif case == "another workspace":
        keep_rates()
        cloud_standing.bind(BOX, workspace=WS_B)
    elif case == "unmatched":
        keep_rates(rows=[_row("AmazonRDS", "InstanceUsage:db.r5.4xlarge")])
    elif case == "no region":
        keep_rates()
        cmd = EC2.replace("--region us-east-1 ", "")
    v = g.gate_command(cmd)
    assert v["decision"] == "ask" and v["price_basis"] == "list" and "effective" not in v
    assert "Priced at list price: " in v["reason"] and why in v["reason"], v["reason"]
    assert "at your effective rate" not in v["reason"]
    assert _records()[-1]["price_basis"] == "list" and "effective" not in _records()[-1]


@pytest.mark.parametrize("how", ["the cli config", "AWS_REGION", "--profile",
                                 "an inline AWS_PROFILE"])
def test_a_region_the_command_does_not_name_is_not_matched_to_its_list_price(
        tmp_path, monkeypatch, how):
    """A command that names no region is priced at us-east-1's list price,
    while the AWS CLI launches it in the environment's or the profile's
    region. Neither is matched to a rate: one region's factor is never
    applied to another region's list price, and the ledger names no region
    the price was not figured in."""
    cfg = tmp_path / "aws-config"
    cfg.write_text("[default]\nregion = us-east-1\n[profile eu]\nregion = eu-west-1\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    cmd = EC2.replace("--region us-east-1 ", "")
    if how == "AWS_REGION":
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
    elif how == "--profile":
        cmd = cmd.replace("run-instances", "run-instances --profile eu")
    elif how == "an inline AWS_PROFILE":
        cmd = "AWS_PROFILE=eu " + cmd
    keep_rates(rows=[*ROWS, *(_row("AmazonEC2", "BoxUsage:m5.2xlarge", region=r)
                              for r in ("eu-west-1", "eu-central-1"))])
    v = g.gate_command(cmd)
    assert v["decision"] == "ask" and v["price_basis"] == "list" and "effective" not in v
    assert v["priced"]["region"] is None
    assert v["priced"]["usd_hour"] == EC2_HOURLY["m5.2xlarge"]
    assert "names no region" in v["reason"] and "at your effective rate" not in v["reason"]


def test_a_named_region_is_the_one_the_list_price_and_the_rate_are_for():
    """The region the command names is the one its list price is figured
    in and the one its rate is looked up for. An EC2 rate there is the
    region's floor, not its listed rate, so no factor is applied to it
    (D168): it is at list price, and says why."""
    from finops.aws_prices import EC2_REGION_RANGE
    keep_rates(rows=[*ROWS, _row("AmazonEC2", "BoxUsage:m5.2xlarge", region="eu-central-1")])
    v = g.gate_command(EC2.replace("us-east-1", "eu-central-1"))
    low = EC2_REGION_RANGE["eu-central-1"][0]
    assert v["priced"]["region"] == "eu-central-1"
    assert v["priced"]["usd_hour"] == round(EC2_HOURLY["m5.2xlarge"] * low, 6)
    assert v["price_basis"] == "list" and "effective" not in v
    assert "Priced at list price" in v["reason"] and "floor" in v["reason"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
@pytest.mark.parametrize("cmd", [EC2.replace("--region us-east-1 ", ""),
                                 RDS.replace("--region us-east-1 ", "")])
def test_a_fifo_in_place_of_the_aws_cli_config_never_holds_the_priced_ask(
        tmp_path, monkeypatch, cmd):
    """The hook fails open when it runs out of time, so a file the guard
    waits on would cost the ask."""
    import threading
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    fifo = home / ".aws" / "config"
    os.mkfifo(fifo)
    monkeypatch.delenv("AWS_CONFIG_FILE")
    keep_rates()
    got: dict = {}
    t = threading.Thread(target=lambda: got.update(v=g.gate_command(cmd)), daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))     # let the reader go
        t.join(5)
        pytest.fail("the guard waited on a FIFO in place of ~/.aws/config")
    assert got["v"]["decision"] == "ask" and got["v"]["price_basis"] == "list"


def test_a_price_book_sku_keeps_the_books_figure_and_no_effective_rate(monkeypatch):
    def book(provider, sku, per="hour"):
        if sku == "m5.2xlarge":
            return {"usd": 0.5, "pack": "acme-edp", "sku": sku, "rate": "0.5",
                    "unit": "hour"}
        return None
    monkeypatch.setattr(guard_packs, "rate", book)
    keep_rates()
    v = g.gate_command(EC2)
    assert v["decision"] == "ask" and v["monthly_delta_usd"] == pytest.approx(2 * 0.5 * 730)
    assert v["price_basis"] == "list" and "effective" not in v
    assert v["reason"].count("Your price book prices m5.2xlarge") == 1
    assert "at your effective rate" not in v["reason"]


# ── the ledger: contract 2 ───────────────────────────────────────────────────

def test_the_ledger_line_carries_contract_2_and_keeps_the_judged_figures():
    keep_rates()
    g.gate_command(EC2)
    rec = _records()[-1]
    assert rec["monthly_usd"] == EC2_MONTHLY and rec["total_usd"] is None
    assert rec["price_basis"] == "effective"
    assert rec["priced"] == {"product_code": "AmazonEC2", "region": "us-east-1",
                             "usage_type": "BoxUsage:m5.2xlarge", "quantity": 2,
                             "unit": "Hrs", "hours": 730.0,
                             "usd_hour": EC2_HOURLY["m5.2xlarge"], "path": "cli"}
    assert rec["effective"] == {"monthly_usd": _at_factor(EC2_MONTHLY), "factor": LOW,
                                "blended_factor": LOW, "as_of": "2026-10-18",
                                "window": ["2026-09-19", "2026-10-18"], "workspace_ref": WS_A}


def test_verify_log_and_every_reader_read_the_new_lines():
    keep_rates()
    for cmd in (EC2, RDS, CACHE, SMALL, "terraform destroy", "ls -la"):
        g.gate_command(cmd)
    assert gl.verify()["ok"] is True
    recs = gl.read(outcomes=True)
    assert sum(1 for r in recs if r.get("price_basis")) == 4
    s = gl.summarize(30)
    assert s["by_decision"]["ask"] >= 3
    assert len(gl.export_records()) == len(_records())
    assert gl.ask_outcomes(recs)
    assert gl.count_asks("infra_apply")["asks"] >= 3
    for r in gl.export_records():
        gl.to_cef(r, "0.0.0")


def test_a_line_names_no_workspace_figure_it_did_not_price_at():
    keep_rates()
    g.gate_command("terraform destroy")
    rec = _records()[-1]
    assert not {"priced", "price_basis", "effective"} & set(rec)


# ── read on the priced path only, never the network ──────────────────────────

@pytest.fixture
def opened(monkeypatch):
    """Every open of the rates file, by any route."""
    seen: list[str] = []
    real = os.open

    def spy(path, *a, **k):
        if str(path).endswith(cloud_rates.FILE_NAME):
            seen.append(str(path))
        return real(path, *a, **k)
    monkeypatch.setattr(os, "open", spy)
    return seen


@pytest.fixture
def offline(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the guard opened a connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)


def test_the_priced_path_reads_the_rates_with_sockets_made_to_fail(opened, offline):
    keep_rates()
    opened.clear()
    v = g.gate_command(EC2)
    assert v["price_basis"] == "effective" and opened


def test_a_silent_command_and_the_editor_path_open_no_rates_file(opened, offline, tmp_path):
    keep_rates()
    opened.clear()
    assert g.gate_command("ls -la") is None
    assert g.gate_command("git status") is None
    out = io.StringIO()
    payload = {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Edit",
               "tool_input": {"file_path": str(tmp_path / "notes.txt")}}
    assert gp.run_hook(None, "plugin", io.StringIO(json.dumps(payload)), out,
                       io.StringIO()) == 0
    assert opened == []


def test_a_deny_says_the_call_is_judged_at_list(monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    _budget(monkeypatch)
    keep_rates()
    v = g.gate_command(SMALL)
    assert v["decision"] == "deny"
    assert "The guard judges this call at list price" in v["reason"]
    assert "judges this ask" not in v["reason"]
