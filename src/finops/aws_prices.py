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
on, and every figure built from them says so. That label matters because the
regional spread is not small. In the AWS Price List (AmazonEC2 offer, version
20261012193925, read 2026-10-13) m5.large is $0.096/hr in us-east-1 and
us-west-2, $0.107 in eu-west-1 (11% more) and $0.12 in ap-southeast-1 (25%
more); p3.2xlarge is $4.234 in ap-southeast-1, 38% over us-east-1; a NAT
gateway hour is $0.059 there against $0.045. A us-east-1 figure is therefore a
floor for most other regions, never a quote for them.

PRICE_SOURCES below names the offer file, version and filter each table was
read from, and the date it was read. tests/test_price_sources.py fails on a
priced constant here that has no entry, and on an entry without a date.

RDS is the one service priced outside us-east-1 too: rds_rate() answers from
price_data/rds_on_demand.json, sixteen regions' listed on-demand rows with
their SKUs, Multi-AZ as its own listed row (see rds_rate).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

# The date every AWS row below was last compared with the public Price List
# (https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/index.json).
AS_OF = "2026-10-13"

_EC2_OFFER = "AWS Price List, AmazonEC2 us-east-1 offer, version 20261012193925"
_RDS_OFFER = "AWS Price List, AmazonRDS us-east-1 offer, version 20261006224050"

# Where each priced constant came from. "source" is the offer file and the
# filter or SKU; "as_of" is the day it was compared with that file. A value
# that is a billing rule rather than a Price List row says where the rule is
# written down instead.
PRICE_SOURCES: dict[str, dict[str, str]] = {
    "ALB_HOURLY": {"source": "AWS Price List, AWSELB us-east-1 offer, version "
                             "20260911124544, SKU 37CUWUT8GSNQEPUV (LoadBalancerUsage)",
                   "as_of": AS_OF},
    "NLB_HOURLY": {"source": "AWSELB us-east-1 offer 20260911124544, SKU YKBGGP5EBGDU9T5W",
                   "as_of": AS_OF},
    "CLB_HOURLY": {"source": "AWSELB us-east-1 offer 20260911124544, SKU 6XT9C7FFNQ3T39YK",
                   "as_of": AS_OF},
    "GWLB_HOURLY": {"source": "AWSELB us-east-1 offer 20260911124544, SKU 4XCKHVEY2F7FVHE3",
                    "as_of": AS_OF},
    "NAT_GATEWAY_HOURLY": {"source": f"{_EC2_OFFER}, SKU M2YSHUBETB3JX4M4 (NatGateway-Hours)",
                           "as_of": AS_OF},
    "NAT_GATEWAY_PER_GB": {"source": f"{_EC2_OFFER}, SKU 59S5R83GFPUAGVR5 (NatGateway-Bytes)",
                           "as_of": AS_OF},
    "COST_EXPLORER_PER_REQUEST": {"source": "AWS Price List, AWSCostExplorer us-east-1 offer, "
                                            "version 20260911124538, SKU BX68HKNBJP73AFBV "
                                            "(USE1-APIRequest)", "as_of": AS_OF},
    "ATHENA_PER_TB_SCANNED": {"source": "AWS Price List, AmazonAthena us-east-1 offer, version "
                                        "20260911124408, SKU B6WHE3FUNCDQVVX4 "
                                        "(USE1-DataScannedInTB)", "as_of": AS_OF},
    "ATHENA_MIN_BYTES_BILLED": {"source": "the same Athena SKU's description: a minimum 10MB "
                                          "for each successful or cancelled query",
                                "as_of": AS_OF},
    "S3_LIST_PER_1000": {"source": "AWS Price List, AmazonS3 us-east-1 offer, version "
                                   "20261012231102, SKU E9YHNFENF4XQBZR6 (Requests-Tier1)",
                         "as_of": AS_OF},
    "S3_GET_PER_1000": {"source": "AmazonS3 us-east-1 offer 20261012231102, SKU "
                                  "ZWQ6Q48CRJXX4FXE (Requests-Tier2)", "as_of": AS_OF},
    "CLOUDWATCH_PER_1000_METRICS": {"source": "AWS Price List, AmazonCloudWatch us-east-1 "
                                              "offer, version 20261007115540, SKU "
                                              "ST83NCNZMVRKWEVJ (CW:GMD-Metrics)",
                                    "as_of": AS_OF},
    "CLOUDWATCH_FREE_REQUESTS_PER_MONTH": {"source": "AmazonCloudWatch us-east-1 offer "
                                                     "20261007115540, SKU VE63M6QDRWUAS2VN "
                                                     "(Global-CW:Requests, 0 to 1,000,000 free)",
                                           "as_of": AS_OF},
    "S3_EGRESS_PER_GB": {"source": "AWS Price List, AWSDataTransfer us-east-1 offer, version "
                                   "20260916132208, SKU HQEH3ZWJVT46JHRG (DataTransfer-Out-"
                                   "Bytes, first 10 TB)", "as_of": AS_OF},
    "PUBLIC_IPV4_HOURLY": {"source": "AWS Price List, AmazonVPC us-east-1 offer, version "
                                     "20260917190528, SKUs 4GQUNXTFWVSGPUZK (InUseAddress) and "
                                     "T6YDQKTMVWKNJFJ8 (IdleAddress)", "as_of": AS_OF},
    "EBS_PER_GB_MONTH": {"source": f"{_EC2_OFFER}, Product Family Storage, "
                                   "EBS:VolumeUsage.* (gp2 HY3BZPP2B6K8MSJF, gp3 "
                                   "JG3KUJMBRGHV3N8G, io1 JR4AM7VS63CTEPMN, io2 "
                                   "6V576P37PS7K7KYU, st1 YQT3C842QHBG6XCU, sc1 "
                                   "6U5VMCFHHMTH3QW6, standard 269VXUCZZ7E6JNXT)",
                         "as_of": AS_OF},
    "EBS_SNAPSHOT_PER_GB_MONTH": {"source": f"{_EC2_OFFER}, SKU 7U7TWP44UP36AT3R "
                                            "(EBS:SnapshotUsage)", "as_of": AS_OF},
    "EBS_GP3_FREE_IOPS": {"source": "not in the Price List: the gp3 baseline in the EBS "
                                    "volume-types documentation, not re-read in this audit",
                          "as_of": AS_OF, "verified": "no"},
    "EBS_GP3_PER_IOPS_MONTH": {"source": f"{_EC2_OFFER}, SKU 7Q58NR58VQEASA4W "
                                         "(EBS:VolumeP-IOPS.gp3)", "as_of": AS_OF},
    "EBS_GP3_FREE_MIBPS": {"source": "not in the Price List: the gp3 baseline in the EBS "
                                     "volume-types documentation, not re-read in this audit",
                           "as_of": AS_OF, "verified": "no"},
    "EBS_GP3_PER_MIBPS_MONTH": {"source": f"{_EC2_OFFER}, SKU SQUFRQX4K92S4SBB "
                                          "(EBS:VolumeP-Throughput.gp3, $40.96 per GiBps-mo)",
                                "as_of": AS_OF},
    "EBS_IO1_PER_IOPS_MONTH": {"source": f"{_EC2_OFFER}, SKU D8RDVC722HKNR55G "
                                         "(EBS:VolumeP-IOPS.piops)", "as_of": AS_OF},
    "EBS_IO2_IOPS_TIERS": {"source": f"{_EC2_OFFER}, SKUs X9AGQNCQXFA9Q8XZ, KDKG2R3W4NMV7CHP "
                                     "(32001-64000) and AX8G343U7JZBRKJB (over 64000)",
                           "as_of": AS_OF},
    "EC2_HOURLY": {"source": f"{_EC2_OFFER}, OnDemand, Linux, Shared tenancy, No License "
                             "required, Pre Installed S/W NA, CapacityStatus Used; all 174 "
                             "rows matched", "as_of": AS_OF},
    "RDS_HOURLY": {"source": f"{_RDS_OFFER}, OnDemand, Single-AZ, MySQL and MariaDB, No "
                             "license required", "as_of": AS_OF},
    "RDS_HOURLY_POSTGRES": {"source": f"{_RDS_OFFER}, OnDemand, Single-AZ, PostgreSQL",
                            "as_of": AS_OF},
    "RDS_ON_DEMAND_JSON": {"source": "AWS Price List, AmazonRDS regional offers, version "
                                     "20261006224050 (published 2026-10-06), 16 regions, "
                                     "OnDemand, MySQL, PostgreSQL and MariaDB, Single-AZ and "
                                     "Multi-AZ, No license required, current generation; each "
                                     "row carries its SKU (scripts/build_rds_price_tables.py)",
                           "as_of": "2026-10-17"},
}

# Constants here that are not read from a price source: a convention, or a
# figure computed from the sourced ones above.
DERIVED = frozenset({
    "HOURS_PER_MONTH", "LB_HOURLY_BY_TYPE", "ALB_PER_MONTH", "NLB_PER_MONTH",
    "CLB_PER_MONTH", "NAT_GATEWAY_PER_MONTH", "PUBLIC_IPV4_PER_MONTH", "EC2_MONTHLY",
    "RDS_MONTHLY",
})

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
# carry 500), with NO free tier. The free tier below covers the standard API
# requests (GetMetricStatistics, ListMetrics and the like), not GetMetricData.
# AWS Price List, AmazonCloudWatch offer file, us-east-1, checked 2026-09-24:
# CW:GMD-Metrics is $0.01 per 1,000 metrics; CW:Requests is 1,000,000 free a
# month (global), then $0.01 per 1,000.
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

# Every public IPv4 address, attached or not, since February 2024.
PUBLIC_IPV4_HOURLY = 0.005
PUBLIC_IPV4_PER_MONTH = round(PUBLIC_IPV4_HOURLY * HOURS_PER_MONTH, 2)   # 3.65

# ── EBS ──────────────────────────────────────────────────────────────────────
#
# AWS Price List, AmazonEC2 offer, us-east-1, version 20260924165117. Five
# tables used to carry these, all with sc1 at $0.025 (the list rate is $0.015),
# and the unattached-volume finding priced every volume at the gp2 rate: a
# 500 GB gp3 volume read $50/mo against $40, and a 50 GB io1 volume with 1,000
# provisioned IOPS read $5/mo against $71.25, because IOPS were never counted.
EBS_PER_GB_MONTH: dict[str, float] = {
    "gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125,
    "st1": 0.045, "sc1": 0.015, "standard": 0.05,
}
EBS_SNAPSHOT_PER_GB_MONTH = 0.05
# gp3 includes 3,000 IOPS and 125 MiB/s; provisioning above that is billed.
EBS_GP3_FREE_IOPS = 3000
EBS_GP3_PER_IOPS_MONTH = 0.005
EBS_GP3_FREE_MIBPS = 125
EBS_GP3_PER_MIBPS_MONTH = 0.04
# io1 is flat per provisioned IOPS; io2 is tiered at 32,000 and 64,000.
EBS_IO1_PER_IOPS_MONTH = 0.065
EBS_IO2_IOPS_TIERS: tuple[tuple[float, float], ...] = (
    (32000, 0.065), (64000, 0.0455), (float("inf"), 0.03185),
)


def as_number(value: object, default: float | None = 0.0) -> float | None:
    """A float from whatever an IaC parser handed over, or `default` (0.0).

    The PR-comment parser passes attribute values through as raw text, so a
    Terraform reference (var.data_iops) or a value with a trailing comment
    (3000 # burst) reaches the price functions as a string. float() on that
    raised, and one unreadable attribute cost the whole PR comment. An
    unreadable quantity is priced as nothing provisioned, which for IOPS and
    throughput is the free baseline. A caller that needs to tell "not a
    number" from zero passes default=None.
    """
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).split("#", 1)[0].split("//", 1)[0].strip().strip("\"'").strip()
        try:
            number = float(text)
        except ValueError:
            return default
    return number if math.isfinite(number) else default


def ebs_volume_monthly(vol_type: str | None, size_gb: float,
                       iops: float | None = None, throughput: float | None = None) -> float:
    """List-price monthly cost of one EBS volume: storage plus any billed IOPS
    and throughput. Unknown types price as gp2, the most common legacy default.
    A size, IOPS or throughput that is not a number counts as 0."""
    vt = str(vol_type or "gp2").strip().lower()
    total = as_number(size_gb) * EBS_PER_GB_MONTH.get(vt, EBS_PER_GB_MONTH["gp2"])
    iops = as_number(iops)
    if vt == "gp3":
        total += max(0.0, iops - EBS_GP3_FREE_IOPS) * EBS_GP3_PER_IOPS_MONTH
        total += max(0.0, as_number(throughput) - EBS_GP3_FREE_MIBPS) * EBS_GP3_PER_MIBPS_MONTH
    elif vt == "io1":
        total += iops * EBS_IO1_PER_IOPS_MONTH
    elif vt == "io2":
        floor = 0.0
        for ceiling, rate in EBS_IO2_IOPS_TIERS:
            if iops <= floor:
                break
            total += (min(iops, ceiling) - floor) * rate
            floor = ceiling
    return total

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
# us-east-1 on-demand listing and is left out rather than guessed. Re-read on
# 2026-10-13 (AmazonEC2 offer 20261012193925, AmazonRDS 20261006224050): all
# 174 EC2 rows still match, in us-west-2 as well; the one RDS row off was
# db.m5.8xlarge (see RDS_HOURLY). Monthly is never typed in; EC2_MONTHLY and RDS_MONTHLY below are derived. A new type
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

# RDS, single-AZ, MySQL / MariaDB (the two are priced identically). Multi-AZ
# is twice this, applied by the caller, which is the one that knows whether the
# instance is Multi-AZ. The offer lists Multi-AZ at two Single-AZ hours to
# within its third decimal, not exactly: db.t4g.medium Multi-AZ is $0.129
# against 2 x $0.065, db.r6g.xlarge $0.859 against 2 x $0.43, db.t4g.small
# $0.065 against 2 x $0.032. Doubling is within 0.8% either way; rds_rate below
# has the listed Multi-AZ row itself. Callers that do not know the engine use this table;
# PostgreSQL has its own rates below (rds_hourly picks between them).
RDS_HOURLY: dict[str, float] = {
    "db.t3.micro": 0.017, "db.t3.small": 0.034, "db.t3.medium": 0.068,
    "db.t3.large": 0.136, "db.t3.xlarge": 0.272, "db.t3.2xlarge": 0.544,
    "db.t4g.micro": 0.016, "db.t4g.small": 0.032, "db.t4g.medium": 0.065,
    "db.t4g.large": 0.129,
    "db.m5.large": 0.171, "db.m5.xlarge": 0.342, "db.m5.2xlarge": 0.684,
    # db.m5.8xlarge is listed at $2.74, not the $2.736 that 16x the large's
    # $0.171 gives: AmazonRDS offer 20261006224050, SKU D5NU4Q3STSENKMSM (MySQL)
    # and K9FM4VKJBEDZ49XX (MariaDB), read 2026-10-13. RDS sizes are not exact
    # multiples, so each row is the listed one, never scaled from another.
    "db.m5.4xlarge": 1.368, "db.m5.8xlarge": 2.74, "db.m5.12xlarge": 4.104,
    "db.m6i.large": 0.171, "db.m6i.xlarge": 0.342, "db.m6i.2xlarge": 0.684,
    "db.m6g.large": 0.152, "db.m6g.xlarge": 0.304, "db.m6g.2xlarge": 0.608,
    "db.r5.large": 0.24, "db.r5.xlarge": 0.48, "db.r5.2xlarge": 0.96,
    "db.r5.4xlarge": 1.92, "db.r5.8xlarge": 3.84,
    "db.r6i.large": 0.24, "db.r6i.xlarge": 0.48, "db.r6i.2xlarge": 0.96,
    "db.r6g.large": 0.215, "db.r6g.xlarge": 0.43, "db.r6g.2xlarge": 0.859,
    "db.r7g.large": 0.239, "db.r7g.xlarge": 0.478, "db.r7g.2xlarge": 0.956,
}

# RDS for PostgreSQL, single-AZ. Not the MySQL table: from the AWS Price List
# (AmazonRDS offer, version 20260924211011, us-east-1, OnDemand, Single-AZ,
# Database Engine "PostgreSQL"), PostgreSQL runs 4-7% above MySQL/MariaDB on
# the t3, m5, m6i, m6g, r5, r6i and r6g classes, and the same on t4g and r7g.
# The same offer's MySQL and MariaDB rates match RDS_HOURLY exactly.
RDS_HOURLY_POSTGRES: dict[str, float] = {
    "db.t3.micro": 0.018, "db.t3.small": 0.036, "db.t3.medium": 0.072,
    "db.t3.large": 0.145, "db.t3.xlarge": 0.29, "db.t3.2xlarge": 0.579,
    "db.t4g.micro": 0.016, "db.t4g.small": 0.032, "db.t4g.medium": 0.065,
    "db.t4g.large": 0.129,
    "db.m5.large": 0.178, "db.m5.xlarge": 0.356, "db.m5.2xlarge": 0.712,
    "db.m5.4xlarge": 1.424, "db.m5.8xlarge": 2.848, "db.m5.12xlarge": 4.272,
    "db.m6i.large": 0.178, "db.m6i.xlarge": 0.356, "db.m6i.2xlarge": 0.712,
    "db.m6g.large": 0.159, "db.m6g.xlarge": 0.318, "db.m6g.2xlarge": 0.636,
    "db.r5.large": 0.25, "db.r5.xlarge": 0.5, "db.r5.2xlarge": 1.0,
    "db.r5.4xlarge": 2.0, "db.r5.8xlarge": 4.0,
    "db.r6i.large": 0.25, "db.r6i.xlarge": 0.5, "db.r6i.2xlarge": 1.0,
    "db.r6g.large": 0.225, "db.r6g.xlarge": 0.45, "db.r6g.2xlarge": 0.899,
    "db.r7g.large": 0.239, "db.r7g.xlarge": 0.478, "db.r7g.2xlarge": 0.956,
}

_RDS_BY_ENGINE: dict[str, dict[str, float]] = {
    "mysql": RDS_HOURLY, "mariadb": RDS_HOURLY, "postgres": RDS_HOURLY_POSTGRES,
}


def rds_hourly(instance_class: str, engine: str) -> float | None:
    """The single-AZ hourly rate for a class on an engine (mysql, mariadb or
    postgres), or None for a class or engine the tables do not hold: Aurora,
    SQL Server, Oracle and Db2 bill at other rates, and get no figure rather
    than a MySQL one."""
    return _RDS_BY_ENGINE.get(engine.lower(), {}).get(instance_class)


# ── RDS by region, Multi-AZ included ─────────────────────────────────────────
#
# The tables above are us-east-1 Single-AZ, and a Multi-AZ figure built from
# them doubles the hour. The offer lists Multi-AZ as its own row, and it is
# not always double: PostgreSQL db.r5d.xlarge in us-east-1 is $0.571 Single-AZ
# and $1.143 Multi-AZ. scripts/build_rds_price_tables.py writes every listed
# row for sixteen regions into price_data/rds_on_demand.json (MySQL,
# PostgreSQL and MariaDB, No license required, current generation), each with
# its SKU and the offer version it came from. It is read on first use: the
# guard's hook imports this module and must not pay for a table it never asks.
RDS_ON_DEMAND_JSON = "price_data/rds_on_demand.json"
_RDS_REGIONAL: dict[str, dict] | None = None


@dataclass(frozen=True)
class RdsRate:
    """One listed RDS on-demand rate.

    hourly_usd is the USD price per instance hour exactly as the offer lists
    it (a decimal string such as "0.1890000000"): parse it with Decimal, not
    float, where the digits matter. engine is mysql, mariadb or postgres;
    deployment is Single-AZ or Multi-AZ; source names the regional offer, its
    version and the SKU; as_of is the day that offer was read."""

    hourly_usd: str
    region: str
    engine: str
    deployment: str
    sku: str
    source: str
    as_of: str


def _rds_regional() -> dict[str, dict]:
    """{region: {"version", "read", "rows": {(engine, deployment, class): (usd, sku)}}},
    read once. A table that cannot be read is an empty one: no figure, never
    a guessed one."""
    global _RDS_REGIONAL
    if _RDS_REGIONAL is None:
        import json
        table: dict[str, dict] = {}
        try:
            raw = json.loads((Path(__file__).parent / RDS_ON_DEMAND_JSON)
                             .read_text(encoding="utf-8"))
            for region, entry in raw["regions"].items():
                table[region] = {
                    "version": str(entry["version"]), "read": str(entry["read"]),
                    "rows": {(e, d, c): (usd, sku) for e, d, c, usd, sku in entry["rows"]},
                }
        except (OSError, ValueError, KeyError, TypeError):
            table = {}
        _RDS_REGIONAL = table
    return _RDS_REGIONAL


def rds_rate(instance_class: str, engine: str, region: str, *,
             multi_az: bool = False) -> RdsRate | None:
    """The listed on-demand rate for one RDS instance class, or None.

    engine is RDS's own id (mysql, mariadb or postgres, any case); region a
    region code. None for anything the table does not hold: Aurora, Oracle,
    SQL Server and Db2 (other rates, licence included or brought), a region
    outside the sixteen, a class not listed there, a previous-generation
    class. multi_az picks the offer's Multi-AZ row, which is its own listed
    price, never twice the Single-AZ hour."""
    if not all(isinstance(x, str) for x in (instance_class, engine, region)):
        return None
    entry = _rds_regional().get(region.strip().lower())
    if entry is None:
        return None
    eng = engine.strip().lower()
    deployment = "Multi-AZ" if multi_az else "Single-AZ"
    hit = entry["rows"].get((eng, deployment, instance_class.strip()))
    if hit is None:
        return None
    usd, sku = hit
    where = region.strip().lower()
    return RdsRate(hourly_usd=usd, region=where, engine=eng, deployment=deployment, sku=sku,
                   source=(f"AWS Price List, AmazonRDS {where} offer, version "
                           f"{entry['version']}, SKU {sku}"),
                   as_of=entry["read"])


# Rounded to cents at definition, for the reason given above ALB_PER_MONTH.
EC2_MONTHLY: dict[str, float] = {
    t: round(h * HOURS_PER_MONTH, 2) for t, h in EC2_HOURLY.items()
}
RDS_MONTHLY: dict[str, float] = {
    t: round(h * HOURS_PER_MONTH, 2) for t, h in RDS_HOURLY.items()
}


# For each region, the least and the most it charges over us-east-1 for any
# EC2 type nable lists (Linux on-demand, AWS Price List regional offer files,
# October 2026). The guard added it to judge a command in its region; it lives
# here so a scanner prices an instance from the same table. eu-west-1 was
# checked on 2026-10-17 against AmazonEC2 eu-west-1 20261012193925 over the
# 150 EC2_HOURLY types it lists: least 1.0689x (c7g.medium, $0.0388 against
# $0.0363), most 1.25x (inf2.24xlarge). A region not listed has no figure.
EC2_REGION_RANGE: dict[str, tuple[float, float]] = {
    "af-south-1": (1.286, 1.428), "ap-east-1": (1.253, 1.595), "ap-east-2": (1.052, 1.178),
    "ap-northeast-1": (1.169, 1.5), "ap-northeast-2": (1.124, 1.45),
    "ap-northeast-3": (1.169, 1.45), "ap-south-1": (0.611, 1.3), "ap-south-2": (0.611, 1.201),
    "ap-southeast-1": (1.149, 1.45), "ap-southeast-2": (1.198, 1.45),
    "ap-southeast-3": (1.149, 1.45), "ap-southeast-4": (1.198, 1.45),
    "ap-southeast-5": (0.975, 1.305), "ap-southeast-7": (0.975, 1.305),
    "ca-central-1": (1.088, 1.15), "ca-west-1": (1.088, 1.115), "eu-central-1": (1.135, 1.5),
    "eu-central-2": (1.251, 1.54), "eu-north-1": (1.024, 1.14), "eu-south-1": (1.143, 1.26),
    "eu-south-2": (1.054, 1.2), "eu-west-1": (1.069, 1.25), "eu-west-2": (1.119, 1.5),
    "eu-west-3": (1.119, 1.4), "il-central-1": (1.121, 1.296), "me-central-1": (1.18, 1.247),
    "me-south-1": (1.19, 1.375), "mx-central-1": (1.047, 1.058), "sa-east-1": (1.529, 1.95),
    "us-east-2": (0.994, 1.0), "us-gov-east-1": (1.167, 1.267),
    "us-gov-west-1": (1.167, 1.267), "us-west-1": (1.099, 1.3), "us-west-2": (1.0, 1.0),
}

_LIST = "us-east-1 Linux on-demand list rate"


def ec2_hourly_in_region(hourly: float, region: str | None) -> tuple[float, str]:
    """(one EC2 instance's hourly rate in `region`, the basis to print).

    `hourly` is the us-east-1 list rate. Where the region charges more than
    us-east-1 for every listed type, the rate is that rate times the least it
    charges over us-east-1: a floor for every listed type, as the guard judges
    a command. Anywhere else the us-east-1 figure stands and the basis says
    why. A difference between two types is not bounded by a floor on each
    side, so a caller pricing one (a Graviton move) uses us_east_1_list_note.
    """
    if not region or region == "us-east-1":
        return hourly, _LIST
    span = EC2_REGION_RANGE.get(region)
    if span is None:
        return hourly, f"{_LIST}; nable holds no {region} rate"
    low, high = span
    if low <= 1.0:
        return hourly, (f"{_LIST}; {region} lists EC2 at {low:g}x to {high:g}x "
                        "us-east-1, so this is the us-east-1 figure")
    return hourly * low, (f"{region} floor: the {_LIST} x{low:g}, the least {region} "
                          f"charges over us-east-1 for the EC2 types nable lists (its rates "
                          f"run {low:g}x to {high:g}x)")


def us_east_1_list_note(region: str | None) -> str | None:
    """What a figure priced on a us-east-1 rate says outside us-east-1, or None."""
    if not region or region in ("us-east-1", "global"):
        return None
    return f"priced at us-east-1 list rates; nable holds no {region} rate for it"
