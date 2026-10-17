"""One fix per resource, without letting an inference crowd out a measurement.

collapse_per_resource kept the largest saving per resource whatever its
evidence. A resource with a measured $10 fix (an unattached volume read as
unattached) and an inferred $25 alternative (a scanner's estimate it files as
an investigation) kept the $25 row, which split_total then counted as $25 to
confirm and $0 recoverable: the measured $10 was dropped from recoverable.

Now, per resource: recoverable is the largest measured saving, and to
confirm is what the largest inferred saving claims beyond it, max(0, inferred
- measured). The headline is still one fix per resource: $10 recoverable plus
$15 to confirm, $25 in all, never $35.
"""
from __future__ import annotations

import asyncio

import pytest

from finops import server as srv
from finops.recommendations import sweep as S
from finops.recommendations.sweep import collapse_per_resource, split_total


def _row(title, usd, rid="vol-1", inferred=False):
    return {"title": title, "monthly_savings": usd, "resource_id": rid, "category": "X",
            **({"to_confirm": True} if inferred else {})}


def test_a_measured_ten_and_an_inferred_twenty_five():
    kept, n = collapse_per_resource([_row("estimate", 25.0, inferred=True),
                                     _row("delete", 10.0)])
    assert n == 1 and len(kept) == 1
    assert split_total(kept) == (pytest.approx(10.0), pytest.approx(15.0), 1)
    [row] = kept
    assert row["title"] == "delete" and not row.get("to_confirm")
    assert row["to_confirm_above_usd"] == pytest.approx(15.0)
    assert [a["title"] for a in row["alternatives"]] == ["estimate"]


@pytest.mark.parametrize(("rows", "expected"), [
    ([_row("m", 30.0), _row("i", 25.0, inferred=True)], (30.0, 0.0, 0)),
    ([_row("i1", 25.0, inferred=True), _row("i2", 5.0, inferred=True)], (0.0, 25.0, 1)),
    ([_row("m1", 4.0), _row("m2", 9.0)], (9.0, 0.0, 0)),
    ([_row("m", 10.0), _row("i", 25.0, inferred=True), _row("x", 8.0, rid="vol-2")],
     (18.0, 15.0, 1)),
])
def test_each_resource_counts_once(rows, expected):
    kept, _ = collapse_per_resource(rows)
    rec, conf, n = split_total(kept)
    assert (round(rec, 2), round(conf, 2), n) == expected


def test_the_audit_headline_splits_the_resource(monkeypatch):
    from finops.cleanup.idle import IdleResource
    from finops.tools import cost_queries as cq

    class _AWS:
        async def is_configured(self):
            return True

    monkeypatch.setitem(srv.CLOUD_CONNECTORS, "aws", _AWS())
    monkeypatch.setattr(srv, "require_role", lambda *a, **k: None)
    monkeypatch.setattr(S, "build_specs", lambda aws, regions: [
        ("idle_resources", lambda **k: [IdleResource(
            resource_type="ebs_volume", resource_id="vol-1", region="us-east-1",
            account_id="1", name="vol-1", idle_since="2026-08-03", idle_days=75,
            monthly_cost_usd=10.0, reason="Unattached gp3 volume")], {}),
        ("graviton", lambda **k: [{
            "instance_id": "vol-1", "instance_type": "m5.large",
            "graviton_equivalent": "m7g.large", "savings_estimate": 25.0,
            "savings_pct": 15.0, "region": "us-east-1",
            "finding": {"kind": "investigation"}}], {}),
    ])
    out = asyncio.run(getattr(cq.run_full_cost_audit, "fn", cq.run_full_cost_audit)())
    assert "Estimated monthly saving: $10.00 |" in out, out
    assert "$15.00/mo to confirm" in out, out
    assert "$35.00" not in out and "$25.00/mo to confirm" not in out, out
    assert "| $10.00 (+$15.00 to confirm) |" in out, out
