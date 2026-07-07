"""
Per-customer recommendation learning.

This is the adaptive half of nable's moat: instead of blanket recommendations
("buy a savings plan", "rightsize anything under 40% CPU"), the agent learns from
THIS customer's ledger, which recommendation types they actually act on, and how
close predicted savings landed to measured realized savings, and re-shapes future
proposals to fit. It NEVER executes anything; the rescorer only reorders, annotates,
and suppresses proposals a human still has to approve.

Modules:
  signal.py    customer_signal(): per-source act-rate + accuracy with Bayesian
               shrinkage and a COLD/WARMING/WARM cold-start ladder.
  rescorer.py  rescore(): the propose-only choke point. Reorders, annotates,
               suppresses. Imports no cloud client; cannot change state.
  reasons.py   classify_dismiss_reason(): free-text dismiss reason -> canonical enum.
"""
from .reasons import classify_dismiss_reason
from .signal import approval_profile, customer_signal
from .rescorer import rescore

__all__ = ["customer_signal", "approval_profile", "rescore", "classify_dismiss_reason"]
