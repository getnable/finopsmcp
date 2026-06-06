"""
Stripe -> business-metrics auto-population.

Connecting Stripe should make unit economics (cost per customer, AI as % of MRR)
fire on the first question, with no manual data entry. These tests cover the
three risky parts:
  1. _normalize_monthly: per-interval MRR math, and skipping metered prices.
  2. fetch_business_snapshot: pagination, multi-item sums, customer dedup, the
     metered-skip caveat (parsing a faked Stripe API).
  3. resolve_business_metrics: precedence (manual entry wins; Stripe fills the
     gap and persists; nothing-available degrades cleanly).
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.finops.connectors.saas.stripe import (
    StripeConnector,
    _normalize_monthly,
)


# ── _normalize_monthly (pure math) ────────────────────────────────────────────

def test_monthly_price_is_unchanged():
    assert _normalize_monthly(5000, 1, "month", 1) == 50.0


def test_quantity_multiplies():
    assert _normalize_monthly(3000, 2, "month", 1) == 60.0


def test_yearly_divided_by_twelve():
    assert _normalize_monthly(120000, 1, "year", 1) == 100.0


def test_interval_count_every_three_months():
    # $90 billed every 3 months -> $30/mo
    assert _normalize_monthly(9000, 1, "month", 3) == 30.0


def test_weekly_and_daily_are_scaled_up():
    assert round(_normalize_monthly(1000, 1, "week", 1), 2) == 43.45
    assert round(_normalize_monthly(100, 1, "day", 1), 2) == 30.44


def test_metered_price_is_skipped():
    # No fixed unit_amount -> cannot price reliably -> None (floor, not a guess).
    assert _normalize_monthly(None, 1, "month", 1) is None


def test_unknown_interval_is_skipped():
    assert _normalize_monthly(5000, 1, "decade", 1) is None


# ── fetch_business_snapshot (faked Stripe API) ────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Serves canned subscription pages in order, ignoring request args."""

    def __init__(self, pages):
        self._pages = pages
        self._i = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, auth=None, params=None):
        page = self._pages[min(self._i, len(self._pages) - 1)]
        self._i += 1
        return _FakeResp(page)


_SUB_SEQ = [0]


def _sub(customer, items, items_has_more=False):
    _SUB_SEQ[0] += 1
    return {
        "id": f"sub_{_SUB_SEQ[0]}",
        "customer": customer,
        "items": {"data": items, "has_more": items_has_more},
    }


def _item(unit_amount, interval, quantity=1, interval_count=1, currency="usd"):
    return {
        "quantity": quantity,
        "price": {
            "unit_amount": unit_amount,
            "currency": currency,
            "recurring": {"interval": interval, "interval_count": interval_count},
        },
    }


def test_snapshot_sums_mrr_dedups_customers_and_notes_metered():
    # Driven via asyncio.run (not a bare `async def`) so the end-to-end loop
    # coverage runs on every interpreter, including ones where pytest-asyncio's
    # auto mode is not active.
    import asyncio

    pages = [
        {  # page 1
            "has_more": True,
            "data": [
                _sub("cus_1", [_item(5000, "month")]),          # +50
                _sub("cus_2", [_item(120000, "year")]),         # +100
            ],
        },
        {  # page 2
            "has_more": False,
            "data": [
                _sub("cus_1", [_item(3000, "month", quantity=2)]),  # +60, dup customer
                _sub("cus_3", [_item(None, "month")]),              # metered -> skipped
            ],
        },
    ]
    sc = StripeConnector()
    sc._secret_key = "sk_test_x"

    with patch(
        "src.finops.connectors.saas.stripe.httpx.AsyncClient",
        lambda *a, **k: _FakeClient(pages),
    ):
        snap = asyncio.run(sc.fetch_business_snapshot())

    assert snap["mrr_usd"] == 210.0
    assert snap["paying_customers"] == 3          # cus_1 counted once
    assert snap["source"] == "stripe_active_subscriptions"
    assert any("metered" in c for c in snap["caveats"])
    assert snap["as_of"]


def test_snapshot_empty_without_key():
    import asyncio

    sc = StripeConnector()
    sc._secret_key = ""
    assert asyncio.run(sc.fetch_business_snapshot()) == {}


# ── resolve_business_metrics (precedence + persistence) ───────────────────────

def _with_temp_db(coro_fn):
    import asyncio

    with tempfile.TemporaryDirectory() as td:
        with patch.dict(os.environ, {"FINOPS_DB_PATH": str(Path(td) / "test.db")}):
            from src.finops.storage import db as db_mod

            db_mod._ENGINE = None
            try:
                return asyncio.run(coro_fn())
            finally:
                db_mod._ENGINE = None


def test_manual_metrics_win_and_stripe_is_not_called():
    async def body():
        from src.finops.connectors import business_metrics as bm

        bm.save_metrics(metric_date="2026-06-01", mrr_usd=45_000, paying_customers=340)

        called = {"stripe": False}

        async def _never(*a, **k):
            called["stripe"] = True
            return {"mrr_usd": 1, "paying_customers": 1}

        with patch.object(bm, "_stripe_snapshot", _never):
            out = await bm.resolve_business_metrics()

        assert out["_source"] == "stored"
        assert out["mrr_usd"] == 45_000
        assert out["paying_customers"] == 340
        assert called["stripe"] is False  # manual entry short-circuits Stripe

    _with_temp_db(body)


def test_stripe_fills_gap_and_persists():
    async def body():
        from src.finops.connectors import business_metrics as bm

        async def _snap(*a, **k):
            return {
                "mrr_usd": 12_000.0,
                "paying_customers": 88,
                "as_of": "2026-06-05T00:00:00+00:00",
                "caveats": ["1 metered/usage-based item(s) skipped (no fixed price); MRR is a floor."],
            }

        with patch.object(bm, "_stripe_snapshot", _snap):
            out = await bm.resolve_business_metrics()

        assert out["_source"] == "stripe"
        assert out["mrr_usd"] == 12_000.0
        assert out["paying_customers"] == 88
        assert out["_stripe_caveats"]

        # Persisted so it trends and the next call short-circuits to "stored".
        latest = bm.get_latest_metrics(n=1)[0]
        assert latest["mrr_usd"] == 12_000.0
        assert latest["paying_customers"] == 88

    _with_temp_db(body)


def test_partial_stored_metrics_merge_with_stripe():
    async def body():
        from src.finops.connectors import business_metrics as bm

        # Headcount only, no revenue signal.
        bm.save_metrics(metric_date="2026-05-01", employees=12)

        async def _snap(*a, **k):
            return {"mrr_usd": 9_000.0, "paying_customers": 50, "as_of": "x", "caveats": []}

        with patch.object(bm, "_stripe_snapshot", _snap):
            out = await bm.resolve_business_metrics()

        assert out["_source"] == "stored+stripe"
        assert out["employees"] == 12       # preserved from the stored row
        assert out["mrr_usd"] == 9_000.0     # filled by Stripe
        assert out["paying_customers"] == 50

    _with_temp_db(body)


def test_nothing_available_degrades_to_none():
    async def body():
        from src.finops.connectors import business_metrics as bm

        async def _empty(*a, **k):
            return {}

        with patch.object(bm, "_stripe_snapshot", _empty):
            out = await bm.resolve_business_metrics()

        assert out["_source"] == "none"

    _with_temp_db(body)
