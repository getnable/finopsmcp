# SPDX-License-Identifier: Apache-2.0
"""The workspace's own effective rates, kept on the laptop for the guard (D145 to D148).

The box sends a `rates` block (contract 1) in the status answer the laptop
already reads. `nable cloud status` and `nable cloud connect --send` keep it
as cloud-rates.json, 0600, beside the guard ledger, only when it comes from
the box and the workspace this machine last sent to (cloud_standing.bound());
otherwise it is shown and not kept, with why. A block that is not contract 1
is refused whole. The guard uses it until STALE_DAYS past its as_of.

No listener and no network: the box is a fake opener.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import re
import stat
from datetime import UTC, date, datetime, timedelta

import pytest

from finops import cli_cloud, cloud_rates, cloud_standing
from finops import cloud_connect as cc
from tests.test_cloud_connect import BOX, KEY, Box, records, write_ledger

OTHER_BOX = "https://other-box.example.com"
WS_A = "ws_" + "a" * 32
WS_B = "ws_" + "b" * 32
AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
# The other box's host named as a whole word (CodeQL reads a bare substring
# test of a host as incomplete URL sanitization).
_OTHER_HOST_NAMED = re.compile(r"(?<![\w.-])other-box\.example\.com(?![\w-])")


def block(*, ref=WS_A, as_of="2026-10-18", rows=None, **over):
    """A rates block exactly as the box's workspace_costs.rates.block writes
    it (contract 1)."""
    doc = {"v": 1, "read": True, "workspace_ref": ref, "as_of": as_of,
           "window": {"start": "2026-09-19", "end": as_of},
           "source": ("the AWS cost export at s3://acme-bills/cur/, EC2, RDS and ElastiCache "
                      "line items from 2026-09-19 to 2026-10-18"),
           "columns": ["line_item_line_item_type", "line_item_unblended_cost",
                       "pricing_public_on_demand_cost", "reservation_effective_cost"],
           "rows": rows if rows is not None else [
               {"product_code": "AmazonEC2", "region": "us-east-1",
                "usage_type": "BoxUsage:m5.large", "unit": "Hrs", "on_demand_factor": "0.9200",
                "blended_factor": "0.7040", "covered_share": "0.7500", "days": 30,
                "line_items": 90},
               {"product_code": "AmazonRDS", "region": "eu-west-1",
                "usage_type": "InstanceUsage:db.r6g.large", "unit": "Hrs",
                "on_demand_factor": "0.8000", "blended_factor": None,
                "covered_share": "0.0000", "days": 12, "line_items": 12}],
           "more": 0, "not_served": {"under_7_days": 1}}
    doc.update(over)
    return doc


class RatesBox(Box):
    """The fake box, its status naming a workspace and carrying a rates block."""

    def __init__(self, *, ref=WS_A, rates=None, **kw):
        super().__init__(**kw)
        self.ref = ref
        self.rates = block(ref=ref) if rates is None else rates

    def status(self) -> dict:
        out = {**super().status(), "reads": [], "workspace_ref": self.ref,
               "accepts": ["org_model", "ledger_line", "agent_usage", "org_file_delete"]}
        if self.rates is not False:
            out["rates"] = self.rates
        return out


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    """A sandboxed HOME and data dir, git's and the AWS CLI's config
    locations cleared, the clock fixed."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                "GIT_CONFIG_NOSYSTEM", "FINOPS_GUARD_PERSON", "FINOPS_AIRGAP", cc.KEY_ENV,
                "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
                "AWS_CONFIG_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(cloud_standing, "now_epoch", lambda: AT.timestamp())
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _parse(*argv):
    p = argparse.ArgumentParser(prog="nable")
    sub = p.add_subparsers(dest="cmd")
    cli_cloud.add_parser(sub)
    return p.parse_args(["cloud", *argv])


def _status(box, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli_cloud.run(_parse("status", "--box", BOX, *argv), opener=box,
                         environ={cc.KEY_ENV: KEY}, prompt=None, stdout=out, stderr=err,
                         terminal=True, now=AT)
    return code, out.getvalue(), err.getvalue()


def _kept() -> dict | None:
    p = cloud_rates.path()
    return json.loads(p.read_text()) if p.exists() else None


# ── contract 1, checked whole ────────────────────────────────────────────────

def test_a_contract_1_block_is_accepted_as_it_is():
    b = block()
    assert cloud_rates.check(b, workspace_ref=WS_A) is b


def _mutations():
    def drop(k):
        return lambda b: b.pop(k)

    def row(k, v):
        def f(b):
            b["rows"][0][k] = v
        return f
    return {
        "a missing key": drop("source"),
        "an extra key": lambda b: b.__setitem__("extra", 1),
        "another version": lambda b: b.__setitem__("v", 2),
        "a factor of 1.2": row("on_demand_factor", "1.2000"),
        "a factor below 0.30": row("on_demand_factor", "0.2999"),
        "a factor of 3 places": row("on_demand_factor", "0.920"),
        "a factor as a number": row("on_demand_factor", 0.92),
        "a row with no factor": lambda b: b["rows"][0].update(on_demand_factor=None,
                                                               blended_factor=None),
        "a region that is not one": row("region", "us-east-1; rm -rf"),
        "a usage type the guard does not price": row("usage_type", "SpotUsage:m5.large"),
        "a product it does not price": row("product_code", "AmazonS3"),
        "a share over one": row("covered_share", "1.5000"),
        "days as text": row("days", "30"),
        "a repeated key": lambda b: b["rows"].append(copy.deepcopy(b["rows"][0])),
        "a window backwards": lambda b: b.__setitem__("window", {"start": "2026-10-18",
                                                                 "end": "2026-09-01"}),
        "an as_of that is not a day": lambda b: b.__setitem__("as_of", "yesterday"),
        "a reason code with spaces": lambda b: b.__setitem__("not_served", {"too few": 1}),
        "more rows than 2,000": lambda b: b.__setitem__("rows", [
            {**b["rows"][0], "usage_type": f"BoxUsage:m{i}.large"} for i in range(2001)]),
        "a source of two lines": lambda b: b.__setitem__("source", "a\nb"),
    }


@pytest.mark.parametrize("name", list(_mutations()))
def test_a_block_that_is_not_contract_1_is_refused_whole(name):
    b = block()
    _mutations()[name](b)
    with pytest.raises(cloud_rates.RatesError):
        cloud_rates.check(b, workspace_ref=WS_A)


def test_a_block_naming_another_workspace_than_its_status_is_refused():
    with pytest.raises(cloud_rates.RatesError, match="another workspace"):
        cloud_rates.check(block(ref=WS_B), workspace_ref=WS_A)


def test_a_block_over_64_kb_is_refused():
    big = block(source="x" * 300, rows=[
        {"product_code": "AmazonEC2", "region": "us-east-1",
         "usage_type": f"BoxUsage:{'m' * 40}{i}.large", "unit": "Hrs",
         "on_demand_factor": "0.9200", "blended_factor": "0.7040", "covered_share": "0.7500",
         "days": 30, "line_items": 90} for i in range(600)])
    assert len(json.dumps(big).encode()) > cloud_rates.BLOCK_MAX_BYTES
    with pytest.raises(cloud_rates.RatesError, match="bytes"):
        cloud_rates.check(big, workspace_ref=WS_A)


# ── kept only from the bound box and workspace ───────────────────────────────

def test_a_send_keeps_the_block_0600_from_the_box_and_workspace_it_bound(laptop):
    write_ledger(records(2))
    out = cc.send(cc.build(agent_usage=False), BOX, KEY, opener=RatesBox())
    assert out["rates"]["kept"] is True, out["rates"]
    p = cloud_rates.path()
    assert p.parent == cloud_standing.box_path().parent
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    kept = _kept()
    assert kept["box"] == BOX and kept["block"] == block()
    assert KEY not in p.read_text()


def test_status_keeps_the_block_only_from_the_box_and_workspace_last_sent_to(laptop):
    code, out, _err = _status(RatesBox())
    assert code == 0 and _kept() is None
    assert "Not kept for the guard" in out and "has sent nothing to a box yet" in out
    cloud_standing.bind(BOX, workspace=WS_A)
    code, out, _err = _status(RatesBox())
    assert code == 0 and _kept()["block"]["workspace_ref"] == WS_A
    assert "Kept in" in out and "judges it at list price" in out


def test_two_workspaces_on_one_box_each_keep_only_their_own(laptop):
    """A URL is not a workspace: B's key reaches B's rates on the same box,
    and this machine, bound to A, keeps none of them."""
    cloud_standing.bind(BOX, workspace=WS_A)
    _status(RatesBox())
    mine = _kept()
    code, out, _err = _status(RatesBox(ref=WS_B))
    assert code == 0 and _kept() == mine, "B's rates replaced A's"
    assert "another workspace's" in out


def test_another_box_is_shown_and_not_kept(laptop):
    cloud_standing.bind(OTHER_BOX, workspace=WS_A)
    _code, out, _err = _status(RatesBox())
    assert _kept() is None and "last sent to" in out and _OTHER_HOST_NAMED.search(out)


def test_a_status_naming_no_workspace_keeps_nothing(laptop):
    cloud_standing.bind(BOX, workspace=WS_A)
    got = cloud_rates.keep(block(), box=BOX, workspace_ref=None, fetched_at=AT)
    assert got["kept"] is False and _kept() is None


def test_a_refused_block_is_said_and_keeps_the_last_good_one(laptop):
    cloud_standing.bind(BOX, workspace=WS_A)
    _status(RatesBox())
    good = _kept()
    bad = block()
    bad["rows"][0]["on_demand_factor"] = "1.5000"
    _code, out, _err = _status(RatesBox(rates=bad))
    assert _kept() == good and "refused whole" in out


def test_read_false_from_the_bound_workspace_removes_the_kept_rates(laptop):
    cloud_standing.bind(BOX, workspace=WS_A)
    _status(RatesBox())
    assert _kept() is not None
    gone = {"v": 1, "read": False, "workspace_ref": WS_A,
            "reason": "Effective rates are part of Cloud and above on an active plan."}
    _code, out, _err = _status(RatesBox(rates=gone))
    assert _kept() is None and "part of Cloud and above" in out


def test_read_false_from_another_workspace_removes_nothing(laptop):
    cloud_standing.bind(BOX, workspace=WS_A)
    _status(RatesBox())
    other = {"v": 1, "read": False, "workspace_ref": WS_B, "reason": "x"}
    got = cloud_rates.keep(other, box=BOX, workspace_ref=WS_A, fetched_at=AT)
    assert got["kept"] is False and _kept() is not None


def test_an_older_box_without_a_block_is_said(laptop):
    cloud_standing.bind(BOX, workspace=WS_A)
    _code, out, _err = _status(RatesBox(rates=False))
    assert _kept() is None and "carries no rates block" in out


def test_the_json_status_carries_what_happened_to_the_rates(laptop):
    cloud_standing.bind(BOX, workspace=WS_A)
    _code, out, _err = _status(RatesBox(), "--json")
    got = json.loads(out)
    assert got["rates_kept"]["kept"] is True and got["rates"]["workspace_ref"] == WS_A


# ── what the guard reads ─────────────────────────────────────────────────────

def _bound_and_kept(b=None):
    cloud_standing.bind(BOX, workspace=WS_A)
    got = cloud_rates.keep(b or block(), box=BOX, workspace_ref=WS_A, fetched_at=AT)
    assert got["kept"], got


def test_the_guard_uses_the_rates_until_45_days_past_their_as_of(laptop):
    _bound_and_kept()
    doc, why = cloud_rates.for_guard(today=date(2026, 10, 18) + timedelta(days=45))
    assert why is None and doc is not None
    doc, why = cloud_rates.for_guard(today=date(2026, 10, 18) + timedelta(days=46))
    assert doc is None and "more than 45 days" in why


def test_a_rates_file_from_another_workspace_than_this_machine_sends_to_is_not_used(laptop):
    _bound_and_kept()
    cloud_standing.bind(BOX, workspace=WS_B)
    doc, why = cloud_rates.for_guard(today=date(2026, 10, 19))
    assert doc is None and "another workspace's" in why


@pytest.mark.parametrize("raw, why", [(b"{", "not rates"), (b'{"v": 2}', "not rates"),
                                      (b"x" * (cloud_rates.MAX_BYTES + 1), "KiB")])
def test_a_file_that_is_not_kept_rates_is_not_used(laptop, raw, why):
    _bound_and_kept()
    cloud_rates.path().write_bytes(raw)
    doc, said = cloud_rates.for_guard(today=date(2026, 10, 19))
    assert doc is None and why in said


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="no O_NOFOLLOW here")
def test_a_symlinked_rates_file_is_not_followed(laptop, tmp_path):
    _bound_and_kept()
    real = tmp_path / "elsewhere.json"
    real.write_bytes(cloud_rates.path().read_bytes())
    cloud_rates.path().unlink()
    cloud_rates.path().symlink_to(real)
    doc, why = cloud_rates.for_guard(today=date(2026, 10, 19))
    assert doc is None and "could not be opened" in why


def test_the_rates_file_is_a_guard_file(laptop):
    from finops import guard_paths
    names = {os.path.basename(e.path) for e in guard_paths.protected()}
    assert cloud_rates.FILE_NAME in names


SKU = {"product_code": "AmazonEC2", "region": "us-east-1", "usage_type": "BoxUsage:m5.large",
       "quantity": 4, "unit": "Hrs", "hours": 730.0, "usd_hour": 0.096, "monthly_usd": 280.32,
       "price_book": False}


def test_effective_is_list_times_the_on_demand_factor(laptop):
    _bound_and_kept()
    got = cloud_rates.effective([SKU], judged_monthly=280.32)
    assert got["price_basis"] == "effective"
    assert got["effective"] == {"monthly_usd": 257.89, "factor": "0.9200",
                                "blended_factor": "0.7040", "as_of": "2026-10-18",
                                "window": ["2026-09-19", "2026-10-18"], "workspace_ref": WS_A}
    assert got["priced"] == {k: SKU[k] for k in ("product_code", "region", "usage_type",
                                                 "quantity", "unit", "hours", "usd_hour")}
    note = got["note"]
    assert "~$258/mo at your effective rate" in note
    assert "2026-09-19 to 2026-10-18" in note and "as of 2026-10-18" in note
    assert "75% of whose usage ran on commitments" in note
    assert "judges this ask at list price" in note


def test_what_the_bill_names_under_another_usage_type_stays_at_list(laptop):
    """An instance's EBS volumes are in the judged figure: only the
    instance's own hours take the factor."""
    _bound_and_kept()
    got = cloud_rates.effective([SKU], judged_monthly=280.32 + 100)
    assert got["effective"]["monthly_usd"] == 357.89


@pytest.mark.parametrize("case, why", [
    ("none kept", "no effective rate from your workspace is kept on this machine"),
    ("another region", "hold no on-demand factor for BoxUsage:m5.large in us-west-2"),
    ("no region", "names no region"),
    ("stale", "more than 45 days"),
])
def test_without_a_matching_rate_it_says_at_list_price(laptop, monkeypatch, case, why):
    sku = dict(SKU)
    if case != "none kept":
        _bound_and_kept()
    if case == "another region":
        sku["region"] = "us-west-2"
    if case == "no region":
        sku["region"] = None
    if case == "stale":
        monkeypatch.setattr(cloud_standing, "now_epoch",
                            lambda: datetime(2026, 12, 20, tzinfo=UTC).timestamp())
    got = cloud_rates.effective([sku], judged_monthly=280.32)
    assert got["price_basis"] == "list" and got["effective"] is None
    assert "at list price" in got["note"] and why in got["note"]


def test_a_price_book_sku_keeps_the_books_figure_and_says_so_once(laptop):
    _bound_and_kept()
    got = cloud_rates.effective([{**SKU, "price_book": True}], judged_monthly=280.32)
    assert got["price_basis"] == "list" and got["effective"] is None
    assert got["note"].count("Your price book prices m5.large") == 1
    assert "at your effective rate" not in got["note"]


def test_several_skus_are_priced_each_and_recorded_as_a_list(laptop):
    _bound_and_kept()
    rds = {"product_code": "AmazonRDS", "region": "eu-west-1",
           "usage_type": "InstanceUsage:db.r6g.large", "quantity": 1, "unit": "Hrs",
           "hours": 730.0, "usd_hour": 0.24, "monthly_usd": 175.2, "price_book": False}
    got = cloud_rates.effective([SKU, rds], judged_monthly=455.52)
    assert isinstance(got["priced"], list) and len(got["priced"]) == 2
    assert [p["factor"] for p in got["priced"]] == ["0.9200", "0.8000"]
    assert got["effective"]["factor"] is None
    # 455.52 - 280.32 * 0.08 - 175.20 * 0.20 = 398.0544
    assert got["effective"]["monthly_usd"] == 398.05


def test_the_region_comes_from_the_environment_then_the_cli_config(laptop, monkeypatch):
    cfg = laptop / "aws-config"
    cfg.write_text("[default]\nregion = eu-west-1\n[profile ops]\nregion = ap-south-1\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    assert cloud_rates.env_region() == "eu-west-1"
    monkeypatch.setenv("AWS_PROFILE", "ops")
    assert cloud_rates.env_region() == "ap-south-1"
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    assert cloud_rates.env_region() == "us-west-2"
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    assert cloud_rates.env_region() == "us-east-2"
    monkeypatch.setenv("AWS_REGION", "not a region")
    assert cloud_rates.env_region() == "us-west-2"


def test_a_cli_config_that_does_not_parse_names_no_region(laptop, monkeypatch):
    cfg = laptop / "aws-config"
    cfg.write_text("region = eu-west-1\n[default\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    assert cloud_rates.env_region() is None


def test_the_module_names_no_network_client():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(cloud_rates))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not names & {"socket", "urllib", "urllib.request", "http", "http.client",
                        "requests", "httpx"}
