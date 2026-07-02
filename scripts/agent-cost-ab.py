#!/usr/bin/env python3
"""Agent cost A/B: compare a control run against a nable-governed run.

The honest way to get a real "we measured X% less" number instead of asserting one.
Run the same task twice, once plain (control) and once with the cost_guard
instruction so the agent calls nable's gate before it acts, capture each run's usage
into a small JSON file, and diff them here.

This is the manual harness for before the full runner ships. It only reports what
you feed it, so the number is as honest as your capture.

Per-run JSON (tokens required, the rest optional):
    {"input_tokens": 120000, "output_tokens": 8000, "model": "claude-opus-4-8",
     "tool_calls": 22, "cloud_usd": 4100.0, "wall_clock_s": 180,
     "input_price_per_mtok": 5.0, "output_price_per_mtok": 25.0}

Usage:
    python3 scripts/agent-cost-ab.py control.json governed.json
"""
import json
import sys

# Default prices ($/million tokens). Override per-run in the JSON. Opus 4.8 rates.
_DEF_IN = 5.0
_DEF_OUT = 25.0


def _cost(run: dict) -> dict:
    itok = float(run.get("input_tokens", 0) or 0)
    otok = float(run.get("output_tokens", 0) or 0)
    in_price = float(run.get("input_price_per_mtok", _DEF_IN))
    out_price = float(run.get("output_price_per_mtok", _DEF_OUT))
    model_usd = itok / 1e6 * in_price + otok / 1e6 * out_price
    cloud_usd = float(run.get("cloud_usd", 0) or 0)
    return {
        "model_usd": round(model_usd, 4),
        "cloud_usd": round(cloud_usd, 2),
        "total_usd": round(model_usd + cloud_usd, 4),
        "tokens": int(itok + otok),
        "tool_calls": int(run.get("tool_calls", 0) or 0),
        "wall_clock_s": float(run.get("wall_clock_s", 0) or 0),
    }


def _pct(control: float, governed: float) -> str:
    if control <= 0:
        return "n/a"
    return f"{(control - governed) / control * 100:+.1f}%"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    with open(sys.argv[1]) as f:
        control = _cost(json.load(f))
    with open(sys.argv[2]) as f:
        governed = _cost(json.load(f))

    print("\n  Agent cost A/B  (control vs nable-governed)")
    print("  " + "-" * 56)
    rows = [
        ("model $", "model_usd"),
        ("cloud $", "cloud_usd"),
        ("total $", "total_usd"),
        ("tokens", "tokens"),
        ("tool calls", "tool_calls"),
        ("wall clock s", "wall_clock_s"),
    ]
    for label, key in rows:
        c, g = control[key], governed[key]
        print(f"    {label:<13} {c:>12,}   ->  {g:>12,}   {_pct(float(c), float(g))}")
    print()
    saved = control["total_usd"] - governed["total_usd"]
    print(f"  Governed run saved ${saved:,.2f} on this task "
          f"({_pct(control['total_usd'], governed['total_usd'])}).")
    print("  Model $ is measured. Cloud $ is whatever you captured (avoided or applied);")
    print("  keep the two separate when you quote a number.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
