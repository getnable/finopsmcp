"""Feed each cloud its authentic native billing format through the real FOCUS
normalizer and show the unified view. Three providers publish cost in three totally
different shapes (AWS CUR 2.0 flat columns, Azure Cost Management export, GCP
BigQuery export with nested structs and a credits array). This proves they collapse
to one schema, and that commitment discounts, priced three different ways, all land
in EffectiveCost.
"""
from __future__ import annotations

from finops.focus import normalize

# ── AWS: CUR 2.0 row. Flat snake_case columns, Savings-Plan-covered EC2 usage. ──
AWS_CUR_ROW = {
    "line_item_usage_account_id": "123456789012",
    "bill_billing_period_start_date": "2026-07-01T00:00:00Z",
    "bill_billing_period_end_date": "2026-08-01T00:00:00Z",
    "line_item_usage_start_date": "2026-07-04T00:00:00Z",
    "line_item_usage_end_date": "2026-07-04T01:00:00Z",
    "line_item_line_item_type": "SavingsPlanCoveredUsage",
    "product_servicename": "Amazon EC2",
    "line_item_product_code": "AmazonEC2",
    "product_instance_type": "m5.xlarge",
    "product_region": "us-east-1",
    "line_item_resource_id": "i-0abc123def456",
    "line_item_unblended_cost": "3.84",
    "pricing_public_on_demand_cost": "4.61",
    "savingsplan_savings_plan_effective_cost": "2.98",
    "savings_plan_savings_plan_a_r_n": "arn:aws:savingsplans::123456789012:savingsplan/9f3",
    "line_item_line_item_description": "$0.192 per On Demand m5.xlarge Instance Hour",
    "resource_tags_user_team": "platform",
    "resource_tags_user_env": "prod",
}

# ── Azure: Cost Management export row. PascalCase columns, reserved VM. ──────────
AZURE_EXPORT_ROW = {
    "SubscriptionId": "0000-1111-2222-3333",
    "SubscriptionName": "Production",
    "ServiceName": "Virtual Machines",
    "MeterCategory": "Virtual Machines",
    "MeterSubCategory": "Dv3 Series",
    "ResourceId": "/subscriptions/0000/resourceGroups/rg-prod/providers/Microsoft.Compute/virtualMachines/web-01",
    "ResourceName": "web-01",
    "ResourceLocation": "eastus",
    "CostInBillingCurrency": "5.12",
    "EffectiveCostInBillingCurrency": "3.90",
    "UnitPrice": "6.40",
    "BillingPeriodStartDate": "2026-07-01",
    "BillingPeriodEndDate": "2026-07-31",
    "UsageDate": "2026-07-04",
    "ChargeType": "Usage",
    "BenefitName": "Reserved VM Instance",
    "ReservationId": "res-abc-123",
    "Tags": {"team": "data", "env": "prod"},
}

# ── GCP: BigQuery billing export row. Nested structs + a credits[] array (CUD). ──
GCP_BQ_ROW = {
    "service": {"description": "Compute Engine"},
    "sku": {"description": "N1 Predefined Instance Core running in Americas"},
    "location": {"region": "us-central1"},
    "project": {"id": "acme-prod", "name": "Acme Prod"},
    "resource": {
        "name": "instance-1",
        "global_name": "//compute.googleapis.com/projects/acme-prod/zones/us-central1-a/instances/instance-1",
    },
    "cost": 6.30,
    "credits": [{
        "name": "Committed use discount: CPU",
        "full_name": "Committed Use Discount: N1 predefined vCPUs",
        "amount": -1.85,
        "type": "COMMITTED_USAGE_DISCOUNT",
        "id": "cud-n1-cpu",
    }],
    "usage_start_time": "2026-07-04T00:00:00Z",
    "usage_end_time": "2026-07-04T01:00:00Z",
    "invoice": {"month": "202607"},
    "labels": [{"key": "team", "value": "frontend"}, {"key": "env", "value": "prod"}],
}

CASES = [("aws", AWS_CUR_ROW), ("azure", AZURE_EXPORT_ROW), ("gcp", GCP_BQ_ROW)]


def _fmt(v):
    if v is None:
        return "-"
    if hasattr(v, "isoformat"):
        return v.date().isoformat() if hasattr(v, "date") else v.isoformat()
    return v


def main() -> None:
    print(f"\n{'='*94}")
    print("RAW provider format  ->  normalized FOCUS record (one schema for all three clouds)")
    print('='*94)

    rows = []
    for provider, raw in CASES:
        rec = normalize(provider, raw)
        rows.append((provider, rec))
        top_raw_keys = list(raw.keys())[:4]
        print(f"\n[{provider.upper()}] raw shape: {len(raw)} native fields "
              f"(e.g. {', '.join(top_raw_keys)}...)")

    fields = [
        ("ProviderName", lambda r: r.ProviderName),
        ("ServiceName", lambda r: r.ServiceName),
        ("ServiceCategory", lambda r: r.ServiceCategory),
        ("BilledCost", lambda r: f"${r.BilledCost:.2f}"),
        ("EffectiveCost", lambda r: f"${r.EffectiveCost:.2f}"),
        ("ListCost", lambda r: f"${r.ListCost:.2f}"),
        ("commitment saved", lambda r: f"${r.ListCost - r.EffectiveCost:.2f}"),
        ("RegionId", lambda r: r.RegionId),
        ("RegionName", lambda r: r.RegionName),
        ("SubAccountId", lambda r: (r.SubAccountId or "-")[:20]),
        ("CommitmentType", lambda r: r.CommitmentDiscountType),
        ("ChargeCategory", lambda r: r.ChargeCategory),
        ("Tags", lambda r: dict(r.Tags) if r.Tags else {}),
    ]

    print(f"\n{'='*94}")
    label_w = 17
    col_w = 24
    header = "FOCUS field".ljust(label_w) + "".join(p.upper().ljust(col_w) for p, _ in rows)
    print(header)
    print("-" * len(header))
    for name, getter in fields:
        line = name.ljust(label_w)
        for _, rec in rows:
            line += str(_fmt(getter(rec))).ljust(col_w)
        print(line)
    print('='*94)
    print("Same schema, three raw formats. EffectiveCost reflects each provider's own "
          "commitment mechanic:\n  AWS Savings Plan effective-cost column, Azure reservation "
          "effective-cost column, GCP credits[] math.\n")


if __name__ == "__main__":
    main()
