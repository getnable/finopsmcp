"""
FOCUS 2.0 schema.

FinOps Open Cost and Usage Specification 2.0 defines a vendor-neutral
record format for cloud and SaaS cost data. This module provides the
canonical Python dataclass used throughout nable's FOCUS layer.

Spec reference: https://focus.finops.org/
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FocusRecord:
    """One line item in FOCUS 2.0 format."""

    # Cost columns
    BilledCost: float                   # actual charged amount
    EffectiveCost: float                # amortized cost (includes commitment discounts)
    ListCost: float                     # on-demand list price

    # Resource identity
    ResourceId: str                     # provider resource identifier
    ResourceName: str | None            # human-readable name, if available
    ResourceType: str                   # e.g. "Virtual Machine", "Object Storage"

    # Service classification
    ServiceName: str                    # e.g. "Amazon EC2", "Azure Virtual Machines"
    ServiceCategory: str                # "Compute" | "Storage" | "Database" | "Networking" | "Other"

    # Provider identity
    ProviderName: str                   # "AWS" | "Azure" | "GCP"
    PublisherName: str                  # same as ProviderName for cloud; vendor name for SaaS

    # Location
    RegionId: str | None                # provider region code, e.g. "us-east-1"
    RegionName: str | None              # human-readable region name

    # Billing period (covers the invoice period)
    BillingPeriodStart: datetime
    BillingPeriodEnd: datetime

    # Charge period (when the usage occurred)
    ChargePeriodStart: datetime
    ChargePeriodEnd: datetime

    # Charge classification
    ChargeCategory: str                 # "Usage" | "Purchase" | "Tax" | "Adjustment" | "Credit"
    ChargeDescription: str | None       # free-text line item description

    # Commitment discounts (RIs, Savings Plans, CUDs)
    CommitmentDiscountId: str | None    # RI ARN, SP ARN, or GCP CUD ID
    CommitmentDiscountType: str | None  # "Reserved" | "Savings Plan" | "Committed Use"

    # Tags and sub-account
    Tags: dict[str, str] = field(default_factory=dict)
    SubAccountId: str | None = None     # AWS account ID, Azure subscription, GCP project
    SubAccountName: str | None = None   # human-readable sub-account name


# Valid values for enumerated FOCUS fields
CHARGE_CATEGORIES = {"Usage", "Purchase", "Tax", "Adjustment", "Credit"}
SERVICE_CATEGORIES = {"Compute", "Storage", "Database", "Networking", "AI and Machine Learning", "Other"}
