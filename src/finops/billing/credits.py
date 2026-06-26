"""Managed-AI credit ledger.

Meters what an account spends on *managed AI* (the model calls nable makes on the
customer's behalf, on our key) and exposes the remaining prepaid balance. The
efficiency router reads that balance to degrade, then block, before spend ever
runs past what was paid for. That clamp is what keeps managed AI from eating
margin, so the ledger is the half of the router that makes the budget real.

Design for the single-tenant deployment: one server process per instance, so the
store is a JSON file in the instance data dir guarded by an in-process lock plus
an atomic replace. No external DB, no cross-process coordination. Pooled
multi-tenant keying is deliberately later (see the hosting posture).

Two states, and the default matters:

* **Unmetered** (no budget configured): the ledger still *tracks* every turn's
  cost so the profile can show spend, but ``remaining``/``total`` are ``None`` and
  the router never degrades or blocks. This is the default so nobody gets locked
  out by surprise.
* **Metered** (a monthly budget is set, via env today or a Stripe credit purchase
  later): ``remaining = budget + rollover - spent``; the router clamps to it.

Cost is estimated from token counts at list prices. Reconciling that estimate
against Anthropic's actual billing (Admin/Cost API) is a later accuracy pass; the
estimate is what gates spend in real time.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# List prices per 1M tokens (input, output) for the models the router can pick.
# Public Anthropic pricing; override the whole tier→provider mapping via the
# router's env hooks, not here. Unknown models price as the mid tier so an
# estimate is never silently zero.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
}
_DEFAULT_PRICE = (3.00, 15.00)

_LOCK = threading.Lock()


def _price_for(model: str) -> tuple[float, float]:
    m = (model or "").strip()
    if m in _PRICES:
        return _PRICES[m]
    # Date-suffixed or versioned IDs (claude-opus-4-8-20260101): longest prefix.
    for key in sorted(_PRICES, key=len, reverse=True):
        if m.startswith(key):
            return _PRICES[key]
    return _DEFAULT_PRICE


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Dollar cost of one turn at list prices. Never negative; tolerant of junk."""
    in_price, out_price = _price_for(model)
    it = max(0, int(input_tokens or 0))
    ot = max(0, int(output_tokens or 0))
    return (it / 1_000_000.0) * in_price + (ot / 1_000_000.0) * out_price


def _current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _ledger_file() -> Path:
    from ..storage.db import data_dir

    return data_dir() / "managed_ai_ledger.json"


def _load() -> dict:
    try:
        return json.loads(_ledger_file().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(d: dict) -> None:
    f = _ledger_file()
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2, default=str))
    os.replace(tmp, f)  # atomic: a crash mid-write can't corrupt the ledger


def _default_budget() -> float | None:
    """Monthly managed-AI budget from env. Absent/zero/garbage => unmetered."""
    raw = os.environ.get("FINOPS_MANAGED_AI_BUDGET_USD", "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v > 0 else None


def _effective_budget(d: dict) -> float | None:
    """An explicitly set budget (set_monthly_budget) wins over the env default."""
    b = d.get("budget_usd")
    if b is not None:
        try:
            return float(b)
        except (TypeError, ValueError):
            return None
    return _default_budget()


def _advance(d: dict, period: str) -> bool:
    """Roll the ledger to ``period`` if it isn't there yet. Returns True if it
    rolled (so the caller persists). Credits are use-it-or-lose-it: the monthly
    allowance does not carry forward, so rollover resets to zero each period."""
    cur = d.get("current_period")
    if cur == period:
        return False
    if cur is not None:
        budget = _effective_budget(d)
        spent = float(d.get("spent_usd") or 0.0)
        roll = float(d.get("rollover_usd") or 0.0)
        d.setdefault("history", {})[cur] = {
            "spent_usd": round(spent, 6),
            "budget_usd": budget,
            "rollover_usd": round(roll, 6),
        }
        # Use-it-or-lose-it: the monthly allowance does not carry forward. The
        # prior period's spend and budget are preserved in history above; the
        # live rollover always resets to zero.
        d["rollover_usd"] = 0.0
    d["current_period"] = period
    d["spent_usd"] = 0.0
    d["turns"] = 0
    return True


def record_spend(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    surface: str = "",
    requested_by: str = "",
    period: str | None = None,
) -> float:
    """Add one turn's cost to the current period. Returns the dollar cost."""
    cost = cost_usd(model, input_tokens, output_tokens)
    p = period or _current_period()
    with _LOCK:
        d = _load()
        _advance(d, p)
        d["spent_usd"] = round(float(d.get("spent_usd") or 0.0) + cost, 6)
        d["turns"] = int(d.get("turns") or 0) + 1
        _save(d)
    return cost


def budget_status(period: str | None = None) -> dict:
    """Current balance. ``metered`` False / ``remaining`` None means the router
    must not degrade or block (its documented None contract)."""
    p = period or _current_period()
    with _LOCK:
        d = _load()
        if _advance(d, p):
            _save(d)
        spent = round(float(d.get("spent_usd") or 0.0), 6)
        roll = round(float(d.get("rollover_usd") or 0.0), 6)
        budget = _effective_budget(d)
        turns = int(d.get("turns") or 0)
    metered = budget is not None
    total = round(budget + roll, 6) if metered else None
    remaining = round(max(0.0, total - spent), 6) if metered else None
    return {
        "period": p,
        "spent": spent,
        "budget": budget,
        "rollover": roll,
        "total": total,
        "remaining": remaining,
        "turns": turns,
        "metered": metered,
    }


def set_monthly_budget(usd: float | None) -> None:
    """Set (or clear, with None/0) the monthly managed-AI allowance for this
    instance. A real Stripe credit purchase will call this; today it's config."""
    with _LOCK:
        d = _load()
        _advance(d, _current_period())
        try:
            v = float(usd) if usd is not None else None
        except (TypeError, ValueError):
            v = None
        d["budget_usd"] = v if (v is not None and v > 0) else None
        _save(d)


def reset() -> None:
    """Wipe the ledger file. For tests and a clean reinstall."""
    with _LOCK:
        try:
            _ledger_file().unlink()
        except FileNotFoundError:
            pass
