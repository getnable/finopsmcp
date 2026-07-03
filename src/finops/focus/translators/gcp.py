"""
GCP Billing Export to FOCUS 1.2 translator.

GCP Billing Export (BigQuery) schema reference:
  https://cloud.google.com/billing/docs/how-to/export-data-bigquery-tables

Key GCP fields:
  cost                  -> BilledCost
  service.description   -> ServiceName
  location.region       -> RegionId
  project.id            -> SubAccountId
  project.name          -> SubAccountName
  labels                -> Tags (list of {key, value} structs)
  usage_start_time      -> ChargePeriodStart
  usage_end_time        -> ChargePeriodEnd
  invoice.month         -> BillingPeriodStart / BillingPeriodEnd
  credits               -> list of credit structs (summed for EffectiveCost)
  resource.name         -> ResourceName
  resource.global_name  -> ResourceId
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..schema import FocusRecord

# GCP service name to FOCUS ServiceCategory
_SERVICE_CATEGORY_MAP: dict[str, str] = {
    "Compute Engine": "Compute",
    "Google Kubernetes Engine": "Compute",
    "Cloud Run": "Compute",
    "Cloud Functions": "Compute",
    "App Engine": "Compute",
    "Cloud Batch": "Compute",
    "VMware Engine": "Compute",
    "Cloud Storage": "Storage",
    "Filestore": "Storage",
    "Cloud SQL": "Database",
    "Cloud Spanner": "Database",
    "Firestore": "Database",
    "BigQuery": "Database",
    "Bigtable": "Database",
    "Memorystore": "Database",
    "AlloyDB": "Database",
    "Cloud DNS": "Networking",
    "Cloud CDN": "Networking",
    "Cloud NAT": "Networking",
    "Cloud Interconnect": "Networking",
    "Cloud VPN": "Networking",
    "Network Intelligence Center": "Networking",
    "Vertex AI": "AI and Machine Learning",
    "Cloud AI Platform": "AI and Machine Learning",
    "Document AI": "AI and Machine Learning",
    "Cloud Natural Language API": "AI and Machine Learning",
    "Cloud Vision API": "AI and Machine Learning",
    "Cloud Speech-to-Text": "AI and Machine Learning",
    "Cloud Text-to-Speech": "AI and Machine Learning",
    "Cloud Translation": "AI and Machine Learning",
}

# GCP region code to readable name
_REGION_NAMES: dict[str, str] = {
    "us-central1": "Iowa",
    "us-east1": "South Carolina",
    "us-east4": "Northern Virginia",
    "us-east5": "Columbus",
    "us-south1": "Dallas",
    "us-west1": "Oregon",
    "us-west2": "Los Angeles",
    "us-west3": "Salt Lake City",
    "us-west4": "Las Vegas",
    "northamerica-northeast1": "Montreal",
    "northamerica-northeast2": "Toronto",
    "southamerica-east1": "Sao Paulo",
    "southamerica-west1": "Santiago",
    "europe-central2": "Warsaw",
    "europe-north1": "Finland",
    "europe-southwest1": "Madrid",
    "europe-west1": "Belgium",
    "europe-west2": "London",
    "europe-west3": "Frankfurt",
    "europe-west4": "Netherlands",
    "europe-west6": "Zurich",
    "europe-west8": "Milan",
    "europe-west9": "Paris",
    "europe-west10": "Berlin",
    "europe-west12": "Turin",
    "asia-east1": "Taiwan",
    "asia-east2": "Hong Kong",
    "asia-northeast1": "Tokyo",
    "asia-northeast2": "Osaka",
    "asia-northeast3": "Seoul",
    "asia-south1": "Mumbai",
    "asia-south2": "Delhi",
    "asia-southeast1": "Singapore",
    "asia-southeast2": "Jakarta",
    "australia-southeast1": "Sydney",
    "australia-southeast2": "Melbourne",
    "me-central1": "Doha",
    "me-west1": "Tel Aviv",
    "africa-south1": "Johannesburg",
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
    if isinstance(v, datetime):
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(v), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _extract_labels(labels: Any) -> dict[str, str]:
    """
    GCP labels arrive as a list of {key: str, value: str} dicts in BigQuery exports,
    or as a plain dict in some SDK representations.
    """
    if isinstance(labels, dict):
        return {str(k): str(v) for k, v in labels.items() if v is not None}
    if isinstance(labels, list):
        result: dict[str, str] = {}
        for item in labels:
            if isinstance(item, dict):
                k = item.get("key") or item.get("Key", "")
                v = item.get("value") or item.get("Value", "")
                if k:
                    result[str(k)] = str(v) if v is not None else ""
        return result
    return {}


def _effective_cost(cost: float, credits: Any) -> float:
    """
    Compute effective cost by subtracting credits.
    GCP credits are a list of {amount: float, ...} structs; each amount is negative.
    """
    if not credits:
        return cost
    total_credit = 0.0
    if isinstance(credits, list):
        for c in credits:
            if isinstance(c, dict):
                total_credit += _float(c.get("amount", 0))
    return cost + total_credit  # credits are negative, so addition reduces cost


def _service_category(service_name: str) -> str:
    if service_name in _SERVICE_CATEGORY_MAP:
        return _SERVICE_CATEGORY_MAP[service_name]
    sl = service_name.lower()
    if any(w in sl for w in ("compute", "kubernetes", "cloud run", "function", "app engine", "batch")):
        return "Compute"
    if any(w in sl for w in ("storage", "filestore", "gcs")):
        return "Storage"
    if any(w in sl for w in ("sql", "spanner", "firestore", "bigquery", "bigtable", "memorystore", "alloy", "database")):
        return "Database"
    if any(w in sl for w in ("network", "dns", "cdn", "nat", "interconnect", "vpn", "load balanc")):
        return "Networking"
    if any(w in sl for w in ("vertex", "ai platform", "document ai", "language", "vision", "speech", "translate", "ml")):
        return "AI and Machine Learning"
    return "Other"


def _billing_period_from_invoice_month(invoice_month: str | None) -> tuple[datetime, datetime]:
    """
    Parse GCP invoice.month (format: YYYYMM) into billing period start/end datetimes.
    Falls back to epoch on failure.
    """
    if invoice_month and len(str(invoice_month)) == 6:
        try:
            year = int(str(invoice_month)[:4])
            month = int(str(invoice_month)[4:6])
            start = datetime(year, month, 1, tzinfo=timezone.utc)
            # End is the first day of the next month
            if month == 12:
                end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
            return start, end
        except (ValueError, TypeError):
            pass
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return epoch, epoch


def _commitment_info(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """Detect Committed Use Discounts from GCP credit types."""
    credits = row.get("credits") or []
    if isinstance(credits, list):
        for c in credits:
            if isinstance(c, dict):
                ctype = str(c.get("type", "")).lower()
                if "committed" in ctype or "cud" in ctype:
                    return c.get("id") or c.get("name"), "Committed Use"
                if "sustained" in ctype:
                    return c.get("id") or c.get("name"), "Committed Use"
    return None, None


def translate(row: dict[str, Any]) -> FocusRecord:
    """
    Translate a single GCP Billing Export BigQuery row into a FocusRecord.

    Row may arrive as a plain dict (from BigQuery query result) or a BigQuery
    Row object (which supports dict-like access). Missing fields use safe defaults.
    """
    # GCP BigQuery exports nest some fields; handle both nested and flat forms.
    service_info = row.get("service") or {}
    location_info = row.get("location") or {}
    project_info = row.get("project") or {}
    resource_info = row.get("resource") or {}

    service_name = (
        _str(service_info.get("description") if isinstance(service_info, dict) else row.get("service_description"))
        or _str(row.get("service_description"))
        or _str(row.get("ServiceName"))
        or "Unknown"
    )

    region_id = (
        _str(location_info.get("region") if isinstance(location_info, dict) else row.get("location_region"))
        or _str(row.get("location_region"))
        or _str(row.get("RegionId"))
    ) or None
    region_name = _REGION_NAMES.get(region_id or "", None)

    project_id = (
        _str(project_info.get("id") if isinstance(project_info, dict) else row.get("project_id"))
        or _str(row.get("project_id"))
        or _str(row.get("SubAccountId"))
    ) or None
    project_name = (
        _str(project_info.get("name") if isinstance(project_info, dict) else row.get("project_name"))
        or _str(row.get("project_name"))
        or _str(row.get("SubAccountName"))
    ) or project_id

    # Costs
    billed_cost = _float(row.get("cost") or row.get("total_cost") or row.get("BilledCost"))
    credits = row.get("credits")
    effective_cost = _float(row.get("EffectiveCost") or _effective_cost(billed_cost, credits))
    list_cost = _float(row.get("ListCost") or billed_cost)

    # Dates
    invoice_month = row.get("invoice", {}).get("month") if isinstance(row.get("invoice"), dict) else row.get("invoice_month")
    billing_start, billing_end = _billing_period_from_invoice_month(invoice_month)

    charge_start = _parse_dt(row.get("usage_start_time") or row.get("ChargePeriodStart"))
    charge_end = _parse_dt(row.get("usage_end_time") or row.get("ChargePeriodEnd"))
    if charge_start.year == 1970:
        charge_start = billing_start
    if charge_end.year == 1970:
        charge_end = billing_end

    # Resource
    resource_name = (
        _str(resource_info.get("name") if isinstance(resource_info, dict) else row.get("resource_name"))
        or _str(row.get("resource_name"))
        or _str(row.get("ResourceName"))
    ) or None
    resource_id = (
        _str(resource_info.get("global_name") if isinstance(resource_info, dict) else row.get("resource_global_name"))
        or _str(row.get("resource_global_name"))
        or _str(row.get("ResourceId"))
        or resource_name
        or ""
    )

    # Labels (tags)
    labels_raw = row.get("labels") or row.get("Tags")
    tags = _extract_labels(labels_raw)

    # Charge type
    charge_type = _str(row.get("ChargeCategory") or row.get("type", "Usage"))
    if charge_type not in ("Usage", "Purchase", "Tax", "Adjustment", "Credit"):
        charge_type = "Usage"

    commitment_id, commitment_type = _commitment_info(row)

    return FocusRecord(
        BilledCost=round(billed_cost, 10),
        EffectiveCost=round(effective_cost, 10),
        ListCost=round(list_cost, 10),
        ResourceId=resource_id,
        ResourceName=resource_name,
        ResourceType=_str(row.get("ResourceType") or row.get("sku", {}).get("description") if isinstance(row.get("sku"), dict) else row.get("sku_description")) or "Unknown",
        ServiceName=service_name,
        ServiceCategory=_service_category(service_name),
        ProviderName="GCP",
        PublisherName="GCP",
        RegionId=region_id,
        RegionName=region_name,
        BillingPeriodStart=billing_start,
        BillingPeriodEnd=billing_end,
        ChargePeriodStart=charge_start,
        ChargePeriodEnd=charge_end,
        ChargeCategory=charge_type,
        ChargeDescription=_str(row.get("ChargeDescription") or row.get("description")) or None,
        CommitmentDiscountId=commitment_id,
        CommitmentDiscountType=commitment_type,
        Tags=tags,
        SubAccountId=project_id,
        SubAccountName=project_name,
    )
