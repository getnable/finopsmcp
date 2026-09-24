"""EC2 and RDS on-demand prices have one source: finops.aws_prices.

They used to live in about fourteen tables. Five hourly EC2 copies were
identical, which is the dangerous state: nothing fails while they agree, and
nothing fails when they stop. graviton_prices had already stopped (r7g about
0.4% low), the two RDS copies disagreed on every Graviton class, and the two
monthly tables had rows that were not 730x any hourly rate in the repo.

These tests pin what each consumer computed before the tables were merged, so
the merge is shown to change nothing except the rows it corrected on purpose,
and those are asserted at their corrected value.
"""
from __future__ import annotations

import pytest

from finops import aws_prices
from finops.aws_prices import EC2_HOURLY, EC2_MONTHLY, HOURS_PER_MONTH, RDS_HOURLY, RDS_MONTHLY

# What every EC2 copy agreed on before the merge. Spread across the families and
# sizes each copy carried, including the ones only one copy had.
_EC2_BEFORE = {
    "t3.micro": 0.0104, "t3.2xlarge": 0.3328, "t3a.medium": 0.0376,
    "t4g.large": 0.0672, "m5.large": 0.096, "m5.24xlarge": 4.608,
    "m5a.xlarge": 0.172, "m6i.2xlarge": 0.384, "m6a.large": 0.0864,
    "m6g.xlarge": 0.154, "m7g.large": 0.0816, "m7g.16xlarge": 2.6112,
    "c5.9xlarge": 1.53, "c6i.8xlarge": 1.36, "c6a.large": 0.0765,
    "c7g.medium": 0.0363, "c7i.large": 0.08925, "r5.8xlarge": 2.016,
    "r6i.large": 0.126, "r6g.2xlarge": 0.4032, "x1e.xlarge": 0.834,
    "p3.2xlarge": 3.06, "g4dn.xlarge": 0.526, "g5.48xlarge": 16.288,
    "inf2.xlarge": 0.7582, "i4i.large": 0.156,
}

_RDS_BEFORE = {
    "db.t3.micro": 0.017, "db.t3.medium": 0.068, "db.t3.2xlarge": 0.544,
    "db.t4g.large": 0.13, "db.m5.large": 0.171, "db.m5.4xlarge": 1.368,
    "db.r5.large": 0.24, "db.r7g.large": 0.204,
}


@pytest.mark.parametrize("itype,hourly", sorted(_EC2_BEFORE.items()))
def test_ec2_rate_is_what_every_copy_used(itype, hourly):
    assert EC2_HOURLY[itype] == hourly


@pytest.mark.parametrize("itype,hourly", sorted(_RDS_BEFORE.items()))
def test_rds_rate_is_what_every_copy_used(itype, hourly):
    assert RDS_HOURLY[itype] == hourly


def test_r7g_is_the_terraform_and_vscode_rate_not_graviton_prices():
    # graviton_prices had r7g.large at 0.1067, terraform_estimate and the VS Code
    # extension at 0.1071. 0.1071 is the one that is exactly half of xlarge and
    # a quarter of 2xlarge; the 0.1067 column was thirds of a cent rounded, and
    # sat 0.4% under every r7g rate the other two copies quoted.
    assert EC2_HOURLY["r7g.large"] == 0.1071
    assert EC2_HOURLY["r7g.xlarge"] == 0.2142
    assert EC2_HOURLY["r7g.2xlarge"] == 0.4284
    # The sizes only graviton_prices carried, rescaled from that rate.
    assert EC2_HOURLY["r7g.4xlarge"] == 0.8568
    assert EC2_HOURLY["r7g.16xlarge"] == 0.4284 * 8


def test_r5_16xlarge_is_the_rate_the_kubernetes_monthly_row_implied():
    # Only kubernetes.py priced it, as 2943.36/month, which is 4.032 x 730.
    assert EC2_HOURLY["r5.16xlarge"] == 4.032
    assert EC2_MONTHLY["r5.16xlarge"] == 2943.36


# ── internal consistency ──────────────────────────────────────────────────────

_SIZE_UNITS = {"nano": 0.125, "micro": 0.25, "small": 0.5, "medium": 1, "large": 2, "xlarge": 4}
# GPU count, not vCPU, sets these prices, so size does not scale linearly.
_NONLINEAR_FAMILIES = {"g4dn", "g5", "g6", "g6e", "inf1", "inf2", "trn1", "trn1n", "p3", "p4d",
                       "p4de", "p5", "p5e", "p5en"}


def _units(size: str) -> float | None:
    if size in _SIZE_UNITS:
        return _SIZE_UNITS[size]
    if size.endswith("xlarge") and size[:-6].isdigit():
        return 4 * int(size[:-6])
    return None


def test_every_cpu_family_is_priced_per_vcpu():
    # AWS publishes four decimal places, so a medium can be off by the half a
    # hundredth of a cent that rounding adds (c7g.medium 0.0363 = 0.0725 / 2).
    # 0.2% allows that and nothing bigger. The r7g drift was 0.4%.
    by_family: dict[str, list[tuple[str, float]]] = {}
    for itype, hourly in EC2_HOURLY.items():
        family, size = itype.split(".", 1)
        units = _units(size)
        if family in _NONLINEAR_FAMILIES or units is None:
            continue
        by_family.setdefault(family, []).append((itype, hourly / units))
    off = []
    for family, rates in by_family.items():
        lo = min(r for _, r in rates)
        hi = max(r for _, r in rates)
        if hi / lo > 1.002:
            off.append((family, rates))
    assert not off, off


def test_monthly_is_derived_never_typed():
    assert HOURS_PER_MONTH == 730.0
    assert EC2_MONTHLY == {t: round(h * 730, 2) for t, h in EC2_HOURLY.items()}
    assert RDS_MONTHLY == {t: round(h * 730, 2) for t, h in RDS_HOURLY.items()}


# ── consumers read the shared table ──────────────────────────────────────────

def test_terraform_estimate_prices_from_aws_prices():
    from finops.connectors import terraform_estimate as te

    assert te._EC2_HOURLY is EC2_HOURLY
    assert te._RDS_HOURLY is RDS_HOURLY
    assert te.HOURS_PER_MONTH is aws_prices.HOURS_PER_MONTH


def test_terraform_estimate_quotes_what_it_used_to():
    from finops.connectors.terraform_estimate import ResourceChange, _estimate_ec2, _estimate_rds

    ec2 = _estimate_ec2(ResourceChange("aws_instance.a", "aws_instance", ["create"], None,
                                       {"instance_type": "m5.large"}))
    assert ec2.monthly_delta == pytest.approx(0.096 * 730)
    rds = _estimate_rds(ResourceChange("aws_db_instance.a", "aws_db_instance", ["create"], None,
                                       {"instance_class": "db.m5.large", "multi_az": True}))
    assert rds.monthly_delta == pytest.approx(0.171 * 2 * 730)


def test_graviton_scanner_prices_from_aws_prices():
    from finops.recommendations import graviton, graviton_prices

    assert graviton_prices.HOURLY_PRICE is EC2_HOURLY
    assert graviton._estimate_monthly_cost("m5.large") == round(0.096 * 730, 2)
    # r5.large -> r7g.large now uses the corrected r7g rate.
    current, savings, _ = graviton._compute_savings("r5.large", "r7g.large")
    assert current == round(0.126 * 730, 2)
    assert savings == round(round(0.126 * 730, 2) - round(0.1071 * 730, 2), 2)
