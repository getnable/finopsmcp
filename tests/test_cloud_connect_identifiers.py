# SPDX-License-Identifier: Apache-2.0
"""`nable cloud connect` and the identifiers a saved plan names (D167, D169).

What has to stay true:
  - the dry run counts the ledger lines whose priced SKUs name a planned
    identifier (an RDS instance's or an ElastiCache cluster's name in a saved
    plan), and how many identifiers, beside the created ids, and says the
    name is matched by name and believed until seen on a real bill
  - an older line (a single priced SKU, a list without path) counts nothing
    and does not stop the count
  - what is sent is byte for byte what was shown: a send through a mock
    transport posts exactly the payload's bytes, the identifiers among them
    as the ledger stored them

No listener and no network: every request goes to a fake opener.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

import pytest

from finops import cloud_connect as cc
from finops import guard_ledger

KEY = "nbl_test_workspace_key_0123456789abcdef"  # pragma: allowlist secret
BOX = "https://box.example.com"
SHA = hashlib.sha256

ORG = {"owners.yaml": ("# nable org model v1\n- fact: owner\n  subject: aws_account:123456789012\n"
                       "  value: {team: payments}\n  status: confirmed\n")}

EC2 = {"product_code": "AmazonEC2", "region": "eu-west-1", "usage_type": "BoxUsage:m5.large",
       "quantity": 1, "unit": "Hrs", "hours": 730.0, "usd_hour": 0.102624, "path": "terraform"}
RDS = {"product_code": "AmazonRDS", "region": "eu-west-1",
       "usage_type": "InstanceUsage:db.r6g.large", "quantity": 1, "unit": "Hrs", "hours": 730.0,
       "usd_hour": 0.252, "path": "terraform", "identifier": "orders-db"}
CACHE = {"product_code": "AmazonElastiCache", "region": "eu-west-1",
         "usage_type": "NodeUsage:cache.r6g.large", "quantity": 1, "unit": "Hrs",
         "hours": 730.0, "usd_hour": 0.229, "path": "terraform", "identifier": "sessions"}


def _verdict(priced, *, minute: int) -> dict:
    return {"ts": f"2026-10-20T10:{minute:02d}:00+00:00", "decision": "ask",
            "action_type": "infra_apply", "harness": "claude-code", "session": "s-1",
            "tool": "Bash", "command": "terraform apply plan.out", "monthly_usd": 426.05,
            "price_basis": "list", "priced": priced}


def write_ledger(recs: list[dict]) -> list[bytes]:
    """Chained exactly as guard_ledger.append chains, at fixture times."""
    path = guard_ledger.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    prev, out = guard_ledger.GENESIS, []
    with path.open("ab") as fh:
        for rec in recs:
            line = json.dumps({"v": 1, **rec, "prev": prev}, separators=(",", ":"),
                              sort_keys=True).encode()
            prev = SHA(line).hexdigest()
            fh.write(line + b"\n")
            out.append(line)
    return out


@pytest.fixture
def org_dir(tmp_path, monkeypatch):
    d = tmp_path / "org"
    d.mkdir()
    for name, text in ORG.items():
        (d / name).write_text(text, encoding="utf-8")
    monkeypatch.setenv("FINOPS_ORG_DIR", str(d))
    return d


@pytest.fixture
def ledger(org_dir):
    old_single = _verdict({k: v for k, v in RDS.items() if k not in ("path", "identifier")},
                          minute=0)
    old_list = _verdict([{k: v for k, v in s.items() if k not in ("path", "identifier")}
                         for s in (EC2, RDS)], minute=1)
    plan = _verdict([EC2, RDS, CACHE], minute=2)
    lines = write_ledger([old_single, old_list, plan])
    outcome = {"v": 1, "kind": "outcome", "outcome": "ran", "harness": "claude-code",
               "tool": "Bash", "verdict": SHA(lines[-1]).hexdigest(),
               "ts": "2026-10-20T10:03:00+00:00",
               "created": [{"kind": "ec2_instance", "id": "i-0123456789abcdef0"}]}
    return lines + write_ledger_tail(outcome)


def write_ledger_tail(rec: dict) -> list[bytes]:
    path = guard_ledger.ledger_path()
    last = path.read_bytes().rstrip(b"\n").rsplit(b"\n", 1)[-1]
    line = json.dumps({**rec, "prev": SHA(last).hexdigest()}, separators=(",", ":"),
                      sort_keys=True).encode()
    with path.open("ab") as fh:
        fh.write(line + b"\n")
    return [line]


class Box:
    """The free tool's endpoints as far as a send relies on them; every
    request is recorded with its body, so the test reads exactly what left."""

    def __init__(self):
        self.requests: list[dict] = []

    def __call__(self, req: urllib.request.Request, timeout: float):
        self.requests.append({"method": req.get_method(), "url": req.full_url,
                              "body": req.data or b""})
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        if req.get_method() == "GET" and path == "api/free-tool/status":
            return 200, json.dumps({"org_model": None, "chains": [],
                                    "totals": {"lines": 0}}).encode()
        if req.get_method() == "POST" and path == "api/free-tool/connect":
            return 200, json.dumps({"accepted": True}).encode()
        return 404, b'{"error": "not_found"}'

    @property
    def posted(self) -> list[bytes]:
        return [r["body"] for r in self.requests if r["method"] == "POST"]


def test_the_dry_run_counts_the_lines_naming_a_planned_identifier(ledger):
    p = cc.build()
    assert p.ledger["records_planning"] == 1 and p.ledger["planned_identifiers"] == 2
    assert p.ledger["records_creating"] == 1 and p.ledger["created_ids"] == 1
    text = cc.describe(p, BOX, None)
    assert ("1 of them names 2 identifiers a saved plan creates an RDS instance or an "
            "ElastiCache cluster with") in text
    assert "matched by name, believed until seen on a real bill" in text
    doc = cc.summary(p, BOX, None)
    assert doc["ledger"]["records_planning"] == 1 and doc["ledger"]["planned_identifiers"] == 2


def test_older_lines_count_nothing_and_stop_nothing(org_dir):
    write_ledger([_verdict({k: v for k, v in RDS.items() if k != "path"} | {"identifier": 7},
                           minute=0),
                  _verdict([{k: v for k, v in s.items() if k != "path"} for s in (EC2,)],
                           minute=1),
                  {"ts": "2026-10-20T10:02:00+00:00", "decision": "ask",
                   "command": "x", "priced": "not a sku"}])
    p = cc.build()
    assert p.ledger["records"] == 3 and p.ledger["ok"] is True
    assert p.ledger["records_planning"] == 0 and p.ledger["planned_identifiers"] == 0
    assert "identifier" not in cc.describe(p, BOX, None)


def test_what_is_sent_is_byte_for_byte_what_was_shown(ledger, tmp_path):
    p = cc.build()
    shown = cc.save(p, tmp_path / "payload.jsonl")
    said = cc.describe(p, BOX, shown)
    assert "names 2 identifiers" in said and "exactly the bytes that would leave" in said
    box = Box()
    result = cc.send(p, BOX, KEY, opener=box)
    sent = b"".join(box.posted)
    assert sent == p.data == Path(shown).read_bytes(), "what left is not what was shown"
    assert result["bytes_sent"] == len(p.data)
    lines = [json.loads(x) for x in sent.split(b"\n") if x]
    stored = [d["line"].encode() for d in lines if d["kind"] == "ledger_line"]
    assert stored == ledger, "a ledger line was not sent as stored"
    plan = json.loads(stored[2])
    assert [s.get("identifier") for s in plan["priced"]] == [None, "orders-db", "sessions"]
    assert [s["path"] for s in plan["priced"]] == ["terraform"] * 3
