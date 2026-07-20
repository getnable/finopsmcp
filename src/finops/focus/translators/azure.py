"""
Azure Cost Export to FOCUS 1.2 translator.

Azure Cost Exports with FOCUS schema output these columns:
  BilledCost, EffectiveCost, ResourceId, ResourceName, ServiceName,
  ServiceCategory, RegionId, SubscriptionId, Tags, ChargeCategory, etc.

Native (non-FOCUS) Azure cost exports use:
  CostInBillingCurrency, ServiceName, ResourceLocation, SubscriptionId, Tags
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..schema import FocusRecord

# Maps Azure service names to FOCUS ServiceCategory
_SERVICE_CATEGORY_MAP: dict[str, str] = {
    "Virtual Machines": "Compute",
    "Azure Kubernetes Service": "Compute",
    "App Service": "Compute",
    "Azure Functions": "Compute",
    "Container Instances": "Compute",
    "Azure Batch": "Compute",
    "Storage": "Storage",
    "Azure Blob Storage": "Storage",
    "Azure Files": "Storage",
    "Azure Disk Storage": "Storage",
    "Azure Data Lake Storage": "Storage",
    "SQL Database": "Database",
    "Azure Cosmos DB": "Database",
    "Azure Cache for Redis": "Database",
    "Azure Database for MySQL": "Database",
    "Azure Database for PostgreSQL": "Database",
    "Azure Synapse Analytics": "Database",
    "Azure SQL Managed Instance": "Database",
    "Virtual Network": "Networking",
    "Azure DNS": "Networking",
    "Azure CDN": "Networking",
    "Application Gateway": "Networking",
    "Azure ExpressRoute": "Networking",
    "VPN Gateway": "Networking",
    "Azure Cognitive Services": "AI and Machine Learning",
    "Azure Machine Learning": "AI and Machine Learning",
    "Azure OpenAI Service": "AI and Machine Learning",
    "Azure AI Services": "AI and Machine Learning",
}

# Azure region codes to readable names
_REGION_NAMES: dict[str, str] = {
    "eastus": "East US",
    "eastus2": "East US 2",
    "westus": "West US",
    "westus2": "West US 2",
    "westus3": "West US 3",
    "centralus": "Central US",
    "northcentralus": "North Central US",
    "southcentralus": "South Central US",
    "westcentralus": "West Central US",
    "northeurope": "North Europe",
    "westeurope": "West Europe",
    "uksouth": "UK South",
    "ukwest": "UK West",
    "francecentral": "France Central",
    "germanywestcentral": "Germany West Central",
    "swedencentral": "Sweden Central",
    "switzerlandnorth": "Switzerland North",
    "eastasia": "East Asia",
    "southeastasia": "Southeast Asia",
    "japaneast": "Japan East",
    "japanwest": "Japan West",
    "australiaeast": "Australia East",
    "australiasoutheast": "Australia Southeast",
    "centralindia": "Central India",
    "southindia": "South India",
    "westindia": "West India",
    "canadacentral": "Canada Central",
    "canadaeast": "Canada East",
    "brazilsouth": "Brazil South",
    "southafricanorth": "South Africa North",
    "uaenorth": "UAE North",
    "global": "Global",
}

# FOCUS ChargeCategory values from Azure charge types
_CHARGE_CATEGORY_MAP: dict[str, str] = {
    "Usage": "Usage",
    "Purchase": "Purchase",
    "Refund": "Credit",
    "Credit": "Credit",
    "RoundingAdjustment": "Adjustment",
    "Tax": "Tax",
    "UnusedReservation": "Purchase",
    "UnusedSavingsPlan": "Purchase",
}


def _float(v: Any) -> float:
    try:
        return float(v) if v not in (None, "", "NULL") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _str(v: Any) -> str:
    return str(v).strip() if v not in (None, "", "NULL") else ""


def _parse_dt(v: Any) -> datetime:
    if not v or v in ("", "NULL"):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(v), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _extract_tags(v: Any) -> dict[str, str]:
    """Parse Azure Tags field, which may be a JSON string, a dict, or already flat."""
    if isinstance(v, dict):
        return {str(k): str(val) for k, val in v.items() if val is not None}
    if isinstance(v, str) and v.strip().startswith("{"):
        import json
        try:
            parsed = json.loads(v)
            return {str(k): str(val) for k, val in parsed.items() if val is not None}
        except (json.JSONDecodeError, AttributeError):
            pass
    return {}


def _service_category(service_name: str) -> str:
    if service_name in _SERVICE_CATEGORY_MAP:
        return _SERVICE_CATEGORY_MAP[service_name]
    sl = service_name.lower()
    if any(w in sl for w in ("virtual machine", "compute", "kubernetes", "function", "container", "batch", "app service")):
        return "Compute"
    if any(w in sl for w in ("storage", "blob", "disk", "file", "data lake")):
        return "Storage"
    if any(w in sl for w in ("sql", "cosmos", "database", "redis", "mysql", "postgresql", "synapse")):
        return "Database"
    if any(w in sl for w in ("network", "dns", "cdn", "gateway", "expressroute", "vpn", "firewall")):
        return "Networking"
    if any(w in sl for w in ("cognitive", "machine learning", "openai", "ai service", "bot")):
        return "AI and Machine Learning"
    return "Other"


def _commitment_info(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (CommitmentDiscountId, CommitmentDiscountType) for the row."""
    discount_id = (
        _str(row.get("CommitmentDiscountId"))
        or _str(row.get("ReservationId"))
        or _str(row.get("SavingsPlanId"))
    ) or None

    benefit_name = _str(row.get("BenefitName", "")).lower()
    charge_type = _str(row.get("ChargeType", "") or row.get("ChargeCategory", "")).lower()

    if discount_id or "reservation" in benefit_name or "reservation" in charge_type:
        discount_type: str | None = "Reserved"
    elif "savings" in benefit_name or "savings" in charge_type:
        discount_type = "Savings Plan"
    else:
        discount_type = None

    return discount_id, discount_type


def translate(row: dict[str, Any]) -> FocusRecord:
    """
    Translate a single Azure Cost Export row dict into a FocusRecord.

    Handles both FOCUS-format exports and native Azure cost export formats.
    Missing fields fall back to safe defaults.
    """
    # Service info
    service_name = (
        _str(row.get("ServiceName"))
        or _str(row.get("MeterCategory"))
        or "Unknown"
    )

    # Costs (prefer FOCUS columns, fall back to native Azure columns)
    billed_cost = _float(
        row.get("BilledCost")
        or row.get("CostInBillingCurrency")
        or row.get("Cost")
    )
    effective_cost = _float(
        row.get("EffectiveCost")
        or row.get("EffectiveCostInBillingCurrency")
        or billed_cost
    )
    list_cost = _float(
        row.get("ListCost")
        or row.get("UnitPrice")
        or billed_cost
    )

    # Location
    region_id = (
        _str(row.get("RegionId"))
        or _str(row.get("ResourceLocation"))
        or _str(row.get("Location"))
    ) or None
    if region_id:
        region_id = region_id.lower().replace(" ", "")
    region_name = _REGION_NAMES.get(region_id or "")

    # Dates
    billing_start = _parse_dt(
        row.get("BillingPeriodStart") or row.get("BillingPeriodStartDate")
    )
    billing_end = _parse_dt(
        row.get("BillingPeriodEnd") or row.get("BillingPeriodEndDate")
    )
    charge_start = _parse_dt(
        row.get("ChargePeriodStart") or row.get("UsageDate") or row.get("Date")
    ) or billing_start
    charge_end = _parse_dt(
        row.get("ChargePeriodEnd") or row.get("UsageDate") or row.get("Date")
    ) or billing_end

    # Sub-account
    sub_id = (
        _str(row.get("SubAccountId"))
        or _str(row.get("SubscriptionId"))
        or _str(row.get("SubscriptionGuid"))
    ) or None
    sub_name = (
        _str(row.get("SubAccountName"))
        or _str(row.get("SubscriptionName"))
    ) or sub_id

    # Charge category
    raw_charge_type = _str(row.get("ChargeCategory") or row.get("ChargeType", "Usage"))
    charge_category = _CHARGE_CATEGORY_MAP.get(raw_charge_type, "Usage")

    commitment_id, commitment_type = _commitment_info(row)

    return FocusRecord(
        BilledCost=round(billed_cost, 10),
        EffectiveCost=round(effective_cost, 10),
        ListCost=round(list_cost, 10),
        ResourceId=_str(row.get("ResourceId") or row.get("InstanceId") or row.get("ResourceName") or ""),
        ResourceName=_str(row.get("ResourceName")) or None,
        ResourceType=_str(row.get("ResourceType") or row.get("MeterSubCategory")) or "Unknown",
        ServiceName=service_name,
        ServiceCategory=_service_category(service_name),
        ProviderName="Azure",
        PublisherName=_str(row.get("PublisherName")) or "Azure",
        RegionId=region_id,
        RegionName=region_name,
        BillingPeriodStart=billing_start,
        BillingPeriodEnd=billing_end,
        ChargePeriodStart=charge_start,
        ChargePeriodEnd=charge_end,
        ChargeCategory=charge_category,
        ChargeDescription=_str(row.get("ChargeDescription") or row.get("AdditionalInfo")) or None,
        CommitmentDiscountId=commitment_id,
        CommitmentDiscountType=commitment_type,
        Tags=_extract_tags(row.get("Tags")),
        SubAccountId=sub_id,
        SubAccountName=sub_name,
    )
