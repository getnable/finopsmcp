# SPDX-License-Identifier: Apache-2.0
"""One price per AWS resource, in one place.

This module exists because of a measured defect, not a tidiness preference.
analyzers/waste.py priced an idle Application Load Balancer at $0.008/hr * 730 =
$5.84/month. cleanup/idle.py priced the identical resource at $16.20/month. Both
numbers shipped, and which one a customer saw depended only on which tool they
happened to call: get_idle_load_balancers, audit_aws_waste and `nable scan` took
the waste.py path, while list_idle_resources and run_full_cost_audit took
idle.py's. Two answers 2.8x apart for one load balancer in one session, and
waste_evidence labelled the low one MEASURED at high confidence.

idle.py was right, and waste.py's constant was not a typo but a category error:
$0.008 is the LCU-hour price and $0.0225 is the hourly base charge every ALB
pays whether or not it serves a single request. An idle load balancer has no
LCU usage by definition, so the LCU rate was precisely the wrong half of the
bill to charge for it.

The general shape, which is the point of this file: a duplicated constant is not
a bug until the copies drift, and nothing in a test suite notices drift between
two literals in two files. Detecting it needs a place where they cannot be two.
Prices that more than one module needs belong here.

Rates are us-east-1 on-demand list, the basis every estimate in nable is quoted
on. Regional variation is real but small for these fixed hourly charges, and a
per-region table would be a false precision on top of an estimate the customer
is told is an estimate.
"""
from __future__ import annotations

# AWS's own convention for a "month" in pricing examples: 365 * 24 / 12. Using
# 720 (30 days) instead understates every monthly figure by 1.4%, which is
# invisible per resource and material across a fleet.
HOURS_PER_MONTH = 730.0

# Elastic Load Balancing, hourly base charge. Application and Network load
# balancers are the same $0.0225; Classic is $0.025. These are the "exists at
# all" rates, exclusive of LCU/NLCU and data processing, which is the correct
# basis for an IDLE resource: capacity units are what it would cost if it were
# doing work, and the finding is that it is not.
ALB_HOURLY = 0.0225
NLB_HOURLY = 0.0225
CLB_HOURLY = 0.025
GWLB_HOURLY = 0.0125

# Terraform's aws_lb covers all three v2 types through load_balancer_type, and
# defaults to "application" when the attribute is absent.
LB_HOURLY_BY_TYPE = {"application": ALB_HOURLY, "network": NLB_HOURLY, "gateway": GWLB_HOURLY}


def lb_hourly(load_balancer_type: str | None) -> float:
    """Hourly base charge for an aws_lb of this type. Unknown types price as ALB."""
    return LB_HOURLY_BY_TYPE.get((load_balancer_type or "application").lower(), ALB_HOURLY)


# NAT gateway: an hourly charge while it exists plus a per-GB charge on every
# byte it processes. The hourly part is the floor; the per-GB part is usually
# larger for a gateway carrying real egress.
NAT_GATEWAY_HOURLY = 0.045
NAT_GATEWAY_PER_GB = 0.045

# ── what reading the bill costs the customer ─────────────────────────────────
#
# nable's claim is that scanning never adds to the bill. That is only checkable
# if the price of every read we perform is written down somewhere, so these are
# here to be summed rather than asserted.
#
# The contrast that motivates the CUR-direct reader: Cost Explorer bills per
# REQUEST and Athena bills per BYTE SCANNED, while an S3 GET of a file the
# customer already pays to store is three orders of magnitude cheaper than
# either. Same data, same detail, different meter.

# Cost Explorer, per API request. The only one of these that recurs per QUESTION
# rather than per byte, which is what makes it expensive on a timer.
COST_EXPLORER_PER_REQUEST = 0.01

# Athena, per TB scanned, with a 10MB per-query floor AWS bills regardless.
ATHENA_PER_TB_SCANNED = 5.00
ATHENA_MIN_BYTES_BILLED = 10 * 1024 * 1024

# S3 request pricing, us-east-1 Standard. LIST is ~12x a GET, which is why the
# reader lists once per period and then reads only what changed.
S3_LIST_PER_1000 = 0.005
S3_GET_PER_1000 = 0.0004

# CloudWatch GetMetricData, per METRIC requested (not per request: one call can
# carry 500). The first million API requests a month are free, which no other
# billing-data source offers, and it is why AWS/Billing EstimatedCharges is the
# only "what has today cost so far" figure that does not put a meter on asking.
CLOUDWATCH_PER_1000_METRICS = 0.01
CLOUDWATCH_FREE_REQUESTS_PER_MONTH = 1_000_000

# S3 data transfer OUT to the internet, per GB, first 10TB. Zero when the reader
# runs in the same region as the bucket, which is the hosted case and the reason
# a hosted box can honestly claim it adds nothing to the bill. A laptop pays
# this, so it is counted rather than assumed away.
S3_EGRESS_PER_GB = 0.09

# Rounded to cents at definition, not at each use. An hourly rate times 730 lands
# on fractions of a cent (0.0225 * 730 = 16.425), and a caller that rounds for
# display then no longer equals the constant it came from. Every comparison
# downstream would need the same tolerance, and the first one that forgot would
# be an off-by-half-a-cent failure that looks like a real disagreement. Money is
# denominated in cents; the constant is too.
ALB_PER_MONTH = round(ALB_HOURLY * HOURS_PER_MONTH, 2)   # 16.43
NLB_PER_MONTH = round(NLB_HOURLY * HOURS_PER_MONTH, 2)   # 16.43
CLB_PER_MONTH = round(CLB_HOURLY * HOURS_PER_MONTH, 2)   # 18.25
NAT_GATEWAY_PER_MONTH = round(NAT_GATEWAY_HOURLY * HOURS_PER_MONTH, 2)   # 32.85

# ── EC2 and RDS instances ────────────────────────────────────────────────────
#
# The same failure at fleet scale. These rates used to live in about fourteen
# tables: five identical hourly EC2 copies (rightsizing, nonprod_scheduler,
# spot_adoption, graviton_prices, terraform_estimate) plus a sixth that had
# drifted on r7g, two RDS copies that disagreed on every Graviton class, and two
# monthly tables typed in by hand that were not 730x the hourly rates they came
# from. Which figure a customer saw for one db.r6g.large depended on whether
# they asked for a rightsizing finding or a Terraform estimate.
#
# Hourly on-demand list, us-east-1, Linux, shared tenancy, no RI or Savings
# Plan. Checked entry by entry against the public AWS Price List (AmazonEC2
# and AmazonRDS us-east-1 offer files, version 20260924211011). That check
# found the P4/P5 rows still at pre-cut prices (p5.48xlarge 98.32, now 55.04),
# i4i ten percent low and the Graviton RDS classes wrong; p5e.48xlarge has no
# us-east-1 on-demand listing and is left out rather than guessed. Monthly
# is never typed in; EC2_MONTHLY and RDS_MONTHLY below are derived. A new type
# goes here, not in the module that first needs it:
# tests/test_instance_price_tables.py fails on an instance-keyed price dict
# anywhere else.
#
# Within a family AWS charges every size the same rate per vCPU, so a large is
# exactly twice a medium and half an xlarge. GPU and accelerator families are
# the exception (GPU count does not scale with vCPU). The same test holds every
# other family to it, which is how a mistyped size gets noticed.
EC2_HOURLY: dict[str, float] = {
    # Burstable
    "t3.nano": 0.0052, "t3.micro": 0.0104, "t3.small": 0.0208,
    "t3.medium": 0.0416, "t3.large": 0.0832, "t3.xlarge": 0.1664,
    "t3.2xlarge": 0.3328,
    "t3a.nano": 0.0047, "t3a.micro": 0.0094, "t3a.small": 0.0188,
    "t3a.medium": 0.0376, "t3a.large": 0.0752, "t3a.xlarge": 0.1504,
    "t3a.2xlarge": 0.3008,
    "t4g.nano": 0.0042, "t4g.micro": 0.0084, "t4g.small": 0.0168,
    "t4g.medium": 0.0336, "t4g.large": 0.0672, "t4g.xlarge": 0.1344,
    "t4g.2xlarge": 0.2688,
    # General purpose
    "m5.large": 0.096, "m5.xlarge": 0.192, "m5.2xlarge": 0.384,
    "m5.4xlarge": 0.768, "m5.8xlarge": 1.536, "m5.12xlarge": 2.304,
    "m5.16xlarge": 3.072, "m5.24xlarge": 4.608,
    "m5a.large": 0.086, "m5a.xlarge": 0.172, "m5a.2xlarge": 0.344,
    "m5a.4xlarge": 0.688,
    "m6i.large": 0.096, "m6i.xlarge": 0.192, "m6i.2xlarge": 0.384,
    "m6i.4xlarge": 0.768, "m6i.8xlarge": 1.536, "m6i.12xlarge": 2.304,
    "m6a.large": 0.0864, "m6a.xlarge": 0.1728, "m6a.2xlarge": 0.3456,
    "m6a.4xlarge": 0.6912,
    "m6g.large": 0.077, "m6g.xlarge": 0.154, "m6g.2xlarge": 0.308,
    "m6g.4xlarge": 0.616, "m6g.8xlarge": 1.232, "m6g.12xlarge": 1.848,
    "m7i.large": 0.1008, "m7i.xlarge": 0.2016, "m7i.2xlarge": 0.4032,
    "m7i.4xlarge": 0.8064,
    "m7g.medium": 0.0408, "m7g.large": 0.0816, "m7g.xlarge": 0.1632,
    "m7g.2xlarge": 0.3264, "m7g.4xlarge": 0.6528, "m7g.8xlarge": 1.3056,
    "m7g.12xlarge": 1.9584, "m7g.16xlarge": 2.6112,
    # Compute optimised
    "c5.large": 0.085, "c5.xlarge": 0.17, "c5.2xlarge": 0.34,
    "c5.4xlarge": 0.68, "c5.9xlarge": 1.53, "c5.18xlarge": 3.06,
    "c6i.large": 0.085, "c6i.xlarge": 0.17, "c6i.2xlarge": 0.34,
    "c6i.4xlarge": 0.68, "c6i.8xlarge": 1.36,
    "c6a.large": 0.0765, "c6a.xlarge": 0.153, "c6a.2xlarge": 0.306,
    "c6a.4xlarge": 0.612,
    "c6g.large": 0.068, "c6g.xlarge": 0.136, "c6g.2xlarge": 0.272,
    "c7g.medium": 0.0363, "c7g.large": 0.0725, "c7g.xlarge": 0.145,
    "c7g.2xlarge": 0.29, "c7g.4xlarge": 0.58, "c7g.8xlarge": 1.16,
    "c7g.12xlarge": 1.74, "c7g.16xlarge": 2.32,
    "c7i.large": 0.08925, "c7i.xlarge": 0.1785, "c7i.2xlarge": 0.357,
    # Memory optimised
    "r5.large": 0.126, "r5.xlarge": 0.252, "r5.2xlarge": 0.504,
    "r5.4xlarge": 1.008, "r5.8xlarge": 2.016, "r5.12xlarge": 3.024,
    "r5.16xlarge": 4.032,
    "r6i.large": 0.126, "r6i.xlarge": 0.252, "r6i.2xlarge": 0.504,
    "r6i.4xlarge": 1.008, "r6i.8xlarge": 2.016,
    "r6g.large": 0.1008, "r6g.xlarge": 0.2016, "r6g.2xlarge": 0.4032,
    "r7g.medium": 0.0536, "r7g.large": 0.1071, "r7g.xlarge": 0.2142,
    "r7g.2xlarge": 0.4284, "r7g.4xlarge": 0.8568, "r7g.8xlarge": 1.7136,
    "r7g.12xlarge": 2.5704, "r7g.16xlarge": 3.4272,
    "x1e.xlarge": 0.834, "x1e.2xlarge": 1.668, "x1e.4xlarge": 3.336,
    "x2idn.16xlarge": 6.669,
    # GPU. List price, not a discounted or Spot rate.
    "p3.2xlarge": 3.06, "p3.8xlarge": 12.24, "p3.16xlarge": 24.48,
    "p4d.24xlarge": 21.957642, "p4de.24xlarge": 27.44705,
    "p5.48xlarge": 55.04, "p5en.48xlarge": 63.296,
    "g4dn.xlarge": 0.526, "g4dn.2xlarge": 0.752,
    "g4dn.4xlarge": 1.204, "g4dn.8xlarge": 2.176, "g4dn.12xlarge": 3.912,
    "g4dn.16xlarge": 4.352, "g4dn.metal": 7.824,
    "g5.xlarge": 1.006, "g5.2xlarge": 1.212, "g5.4xlarge": 1.624,
    "g5.8xlarge": 2.448, "g5.12xlarge": 5.672, "g5.16xlarge": 4.096,
    "g5.24xlarge": 8.144, "g5.48xlarge": 16.288,
    "g6.xlarge": 0.8048, "g6.2xlarge": 0.9776, "g6.4xlarge": 1.3232,
    "g6.8xlarge": 2.0144, "g6.12xlarge": 4.6016, "g6.16xlarge": 3.3968,
    "g6.24xlarge": 6.6752, "g6.48xlarge": 13.3504,
    "g6e.xlarge": 1.861, "g6e.2xlarge": 2.24208, "g6e.4xlarge": 3.00424,
    "g6e.8xlarge": 4.52856, "g6e.12xlarge": 10.49264, "g6e.16xlarge": 7.57719,
    "g6e.24xlarge": 15.06559, "g6e.48xlarge": 30.13118,
    # Trainium / Inferentia accelerators
    "trn1.2xlarge": 1.34375, "trn1.32xlarge": 21.50, "trn1n.32xlarge": 24.78,
    "inf1.xlarge": 0.228, "inf1.2xlarge": 0.362, "inf1.6xlarge": 1.180,
    "inf1.24xlarge": 4.721,
    "inf2.xlarge": 0.7582, "inf2.8xlarge": 1.96786, "inf2.24xlarge": 6.49063,
    "inf2.48xlarge": 12.98127,
    # Storage optimised
    "i3.large": 0.156, "i3.xlarge": 0.312, "i3.2xlarge": 0.624,
    "i3.4xlarge": 1.248, "i3.8xlarge": 2.496,
    "i4i.large": 0.172, "i4i.xlarge": 0.343, "i4i.2xlarge": 0.686,
}

# RDS, single-AZ, MySQL / PostgreSQL / MariaDB. Multi-AZ is twice this, applied
# by the caller, which is the one that knows whether the instance is Multi-AZ.
RDS_HOURLY: dict[str, float] = {
    "db.t3.micro": 0.017, "db.t3.small": 0.034, "db.t3.medium": 0.068,
    "db.t3.large": 0.136, "db.t3.xlarge": 0.272, "db.t3.2xlarge": 0.544,
    "db.t4g.micro": 0.016, "db.t4g.small": 0.032, "db.t4g.medium": 0.065,
    "db.t4g.large": 0.129,
    "db.m5.large": 0.171, "db.m5.xlarge": 0.342, "db.m5.2xlarge": 0.684,
    "db.m5.4xlarge": 1.368, "db.m5.8xlarge": 2.74, "db.m5.12xlarge": 4.104,
    "db.m6i.large": 0.171, "db.m6i.xlarge": 0.342, "db.m6i.2xlarge": 0.684,
    "db.m6g.large": 0.152, "db.m6g.xlarge": 0.304, "db.m6g.2xlarge": 0.608,
    "db.r5.large": 0.24, "db.r5.xlarge": 0.48, "db.r5.2xlarge": 0.96,
    "db.r5.4xlarge": 1.92, "db.r5.8xlarge": 3.84,
    "db.r6i.large": 0.24, "db.r6i.xlarge": 0.48, "db.r6i.2xlarge": 0.96,
    "db.r6g.large": 0.215, "db.r6g.xlarge": 0.43, "db.r6g.2xlarge": 0.859,
    "db.r7g.large": 0.239, "db.r7g.xlarge": 0.478, "db.r7g.2xlarge": 0.956,
}

# Rounded to cents at definition, for the reason given above ALB_PER_MONTH.
EC2_MONTHLY: dict[str, float] = {
    t: round(h * HOURS_PER_MONTH, 2) for t, h in EC2_HOURLY.items()
}
RDS_MONTHLY: dict[str, float] = {
    t: round(h * HOURS_PER_MONTH, 2) for t, h in RDS_HOURLY.items()
}
