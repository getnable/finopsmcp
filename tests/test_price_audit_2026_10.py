"""Prices corrected by the 2026-10-13 audit, pinned at their primary-source value.

Each test names the offer file (or page) and the SKU the value was read from,
and would have failed on the value the code held before. Downloaded offer files
are not committed; the SKUs are enough to find each row again at
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<offer>/<version>/us-east-1/index.csv.
"""
from __future__ import annotations

from finops.aws_prices import HOURS_PER_MONTH, RDS_HOURLY, rds_hourly

# ── RDS ──────────────────────────────────────────────────────────────────────

def test_rds_m5_8xlarge_is_the_listed_rate_not_sixteen_larges():
    # AmazonRDS offer 20261006224050, us-east-1, Single-AZ: SKU D5NU4Q3STSENKMSM
    # (MySQL) and K9FM4VKJBEDZ49XX (MariaDB) are $2.74. The table held $2.736,
    # 16 x the db.m5.large rate.
    assert RDS_HOURLY["db.m5.8xlarge"] == 2.74
    assert rds_hourly("db.m5.8xlarge", "mysql") == 2.74
    assert rds_hourly("db.m5.8xlarge", "mariadb") == 2.74


def test_guard_prices_rds_m5_8xlarge_at_the_listed_rate():
    from finops.guard import estimate_command_monthly_cost

    est = estimate_command_monthly_cost(
        "aws rds create-db-instance --db-instance-identifier x "
        "--db-instance-class db.m5.8xlarge --engine mysql")
    assert est["hourly_usd"] == 2.74
    assert est["monthly_usd"] == round(2.74 * HOURS_PER_MONTH, 2)
