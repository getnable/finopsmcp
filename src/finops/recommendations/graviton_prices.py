"""
On-demand hourly prices (us-east-1, Linux) for common EC2 instance types.
Used by the Graviton migration scanner to estimate current costs and savings.

Source: AWS pricing as of May 2026. Refresh periodically.
"""
from __future__ import annotations

# Hourly on-demand price in USD (us-east-1, Linux, no RI/SP)
HOURLY_PRICE: dict[str, float] = {
    # General purpose: m5
    "m5.large":     0.096,
    "m5.xlarge":    0.192,
    "m5.2xlarge":   0.384,
    "m5.4xlarge":   0.768,
    "m5.8xlarge":   1.536,
    "m5.12xlarge":  2.304,
    "m5.16xlarge":  3.072,
    "m5.24xlarge":  4.608,
    # General purpose: m5a
    "m5a.large":    0.086,
    "m5a.xlarge":   0.172,
    "m5a.2xlarge":  0.344,
    "m5a.4xlarge":  0.688,
    # General purpose: m6i
    "m6i.large":    0.096,
    "m6i.xlarge":   0.192,
    "m6i.2xlarge":  0.384,
    "m6i.4xlarge":  0.768,
    "m6i.8xlarge":  1.536,
    # General purpose: m6a
    "m6a.large":    0.0864,
    "m6a.xlarge":   0.1728,
    "m6a.2xlarge":  0.3456,
    "m6a.4xlarge":  0.6912,
    # General purpose: m7i
    "m7i.large":    0.1008,
    "m7i.xlarge":   0.2016,
    "m7i.2xlarge":  0.4032,
    "m7i.4xlarge":  0.8064,
    # General purpose: m7g (Graviton)
    "m7g.medium":   0.0408,
    "m7g.large":    0.0816,
    "m7g.xlarge":   0.1632,
    "m7g.2xlarge":  0.3264,
    "m7g.4xlarge":  0.6528,
    "m7g.8xlarge":  1.3056,
    "m7g.12xlarge": 1.9584,
    "m7g.16xlarge": 2.6112,
    # Compute optimized: c5
    "c5.large":     0.085,
    "c5.xlarge":    0.17,
    "c5.2xlarge":   0.34,
    "c5.4xlarge":   0.68,
    "c5.9xlarge":   1.53,
    "c5.18xlarge":  3.06,
    # Compute optimized: c6i
    "c6i.large":    0.085,
    "c6i.xlarge":   0.17,
    "c6i.2xlarge":  0.34,
    "c6i.4xlarge":  0.68,
    "c6i.8xlarge":  1.36,
    # Compute optimized: c6a
    "c6a.large":    0.0765,
    "c6a.xlarge":   0.153,
    "c6a.2xlarge":  0.306,
    "c6a.4xlarge":  0.612,
    # Compute optimized: c7g (Graviton)
    "c7g.medium":   0.0363,
    "c7g.large":    0.0725,
    "c7g.xlarge":   0.145,
    "c7g.2xlarge":  0.29,
    "c7g.4xlarge":  0.58,
    "c7g.8xlarge":  1.16,
    "c7g.12xlarge": 1.74,
    "c7g.16xlarge": 2.32,
    # Memory optimized: r5
    "r5.large":     0.126,
    "r5.xlarge":    0.252,
    "r5.2xlarge":   0.504,
    "r5.4xlarge":   1.008,
    "r5.8xlarge":   2.016,
    "r5.12xlarge":  3.024,
    # Memory optimized: r6i
    "r6i.large":    0.126,
    "r6i.xlarge":   0.252,
    "r6i.2xlarge":  0.504,
    "r6i.4xlarge":  1.008,
    "r6i.8xlarge":  2.016,
    # Memory optimized: r7g (Graviton)
    "r7g.medium":   0.0533,
    "r7g.large":    0.1067,
    "r7g.xlarge":   0.2133,
    "r7g.2xlarge":  0.4267,
    "r7g.4xlarge":  0.8533,
    "r7g.8xlarge":  1.7067,
    "r7g.12xlarge": 2.56,
    "r7g.16xlarge": 3.4133,
    # Burstable: t3
    "t3.nano":      0.0052,
    "t3.micro":     0.0104,
    "t3.small":     0.0208,
    "t3.medium":    0.0416,
    "t3.large":     0.0832,
    "t3.xlarge":    0.1664,
    "t3.2xlarge":   0.3328,
    # Burstable: t3a
    "t3a.nano":     0.0047,
    "t3a.micro":    0.0094,
    "t3a.small":    0.0188,
    "t3a.medium":   0.0376,
    "t3a.large":    0.0752,
    "t3a.xlarge":   0.1504,
    "t3a.2xlarge":  0.3008,
    # Burstable: t4g (Graviton)
    "t4g.nano":     0.0042,
    "t4g.micro":    0.0084,
    "t4g.small":    0.0168,
    "t4g.medium":   0.0336,
    "t4g.large":    0.0672,
    "t4g.xlarge":   0.1344,
    "t4g.2xlarge":  0.2688,
}

# Hours per month (30-day average)
HOURS_PER_MONTH: float = 730.0

# Fallback savings ratio when both types are in the price table but
# the Graviton type is missing. Graviton is consistently ~20% cheaper.
GRAVITON_SAVINGS_PCT: float = 0.20
