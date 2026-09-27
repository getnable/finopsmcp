# SPDX-License-Identifier: Apache-2.0
"""adapter csv-owners: owners from a CSV, proposed as org facts.

The CSV (path in the EXAMPLE_OWNERS_CSV secret) has a header row with:

    kind,id,team

where kind is an org subject kind (aws_account, repo_path, service, ...).
Subjects the org model already has an owner for (read through the declared
org.owners scope) are skipped. Whatever this returns, nable writes it as a
proposal a person confirms, with a source that starts with this pack's id.
"""
from __future__ import annotations

import csv


def propose(ctx, context: dict):
    path = ctx.secret("EXAMPLE_OWNERS_CSV")
    if not path:
        raise RuntimeError("EXAMPLE_OWNERS_CSV is not set: put the CSV's path in nable's "
                           "vault or environment")
    known = {o["subject"] for o in (ctx.read_data("org.owners") or {}).get("owners", [])}
    facts = []
    with open(path, encoding="utf-8", newline="") as f:
        for n, rec in enumerate(csv.DictReader(f), start=2):
            kind, sid, team = (rec.get(k, "").strip() for k in ("kind", "id", "team"))
            if not (kind and sid and team) or f"{kind}:{sid}" in known:
                continue
            facts.append({"fact": "owner", "subject": {"kind": kind, "id": sid},
                          "value": {"team": team}, "source": f"owners.csv:{n}",
                          "confidence": 0.6})
    return facts
