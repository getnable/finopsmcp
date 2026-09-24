"""
On-demand hourly prices (us-east-1, Linux) for common EC2 instance types.
Used by the Graviton migration scanner to estimate current costs and savings.

The table itself lives in finops.aws_prices, shared with every other module
that prices an EC2 instance. This module keeps the names graviton.py and its
tests import.
"""
from __future__ import annotations

from ..aws_prices import EC2_HOURLY, HOURS_PER_MONTH

# Hourly on-demand price in USD (us-east-1, Linux, no RI/SP)
HOURLY_PRICE: dict[str, float] = EC2_HOURLY

# Fallback savings ratio when both types are in the price table but
# the Graviton type is missing. Graviton is consistently ~20% cheaper.
GRAVITON_SAVINGS_PCT: float = 0.20

__all__ = ["GRAVITON_SAVINGS_PCT", "HOURLY_PRICE", "HOURS_PER_MONTH"]
