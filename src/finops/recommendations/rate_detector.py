"""
Auto-detect effective private rates from Cost Explorer and CUR.

No manual input required. We derive the customer's actual effective discount
by comparing what they paid vs what on-demand would have cost — this
automatically captures EDP, MOSA, private pricing, credits, and negotiated rates.

Sources (in priority order):
  1. CUR via Athena — most granular, per-line-item rates
  2. CUR via S3     — same data, parsed from parquet/CSV
  3. Cost Explorer  — OnDemandCostEquivalent metric vs actual spend

The result is an EffectiveRateProfile used by the commitment optimizer
so savings projections reflect actual contract economics, not public list prices.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class EffectiveRateProfile:
    """
    Derived private rate information for a customer.
    All discounts are expressed as fractions (0.18 = 18% off list).
    """
    # Overall blended effective discount across all services
    overall_discount_pct: float = 0.0

    # Per-service effective discounts (service_name -> discount fraction)
    per_service_discount: dict[str, float] = field(default_factory=dict)

    # Per-region effective discounts
    per_region_discount: dict[str, float] = field(default_factory=dict)

    # Source used to derive rates
    source: str = "public_pricing"  # "cur_athena" | "cur_s3" | "cost_explorer" | "public_pricing"

    # Confidence in the derived rates
    confidence: str = "low"  # "high" | "medium" | "low"

    # Whether any private pricing was detected
    has_private_pricing: bool = False

    # Raw data for debugging
    metadata: dict[str, Any] = field(default_factory=dict)

    def effective_multiplier(self, service: str | None = None) -> float:
        """Return the price multiplier to apply (1.0 = no discount, 0.82 = 18% off)."""
        if service and service in self.per_service_discount:
            return 1.0 - self.per_service_discount[service]
        return 1.0 - self.overall_discount_pct

    def apply_to_public_price(self, public_price: float, service: str | None = None) -> float:
        """Convert a public on-demand price to the customer's effective price."""
        return public_price * self.effective_multiplier(service)


def _date_range(months_back: int = 3) -> tuple[str, str]:
    end = date.today().replace(day=1)
    start = (end - timedelta(days=months_back * 30)).replace(day=1)
    return start.isoformat(), end.isoformat()


def _detect_from_cost_explorer(ce_client: Any) -> EffectiveRateProfile | None:
    """
    Compare OnDemandCostEquivalent (what you'd pay at list prices) vs
    AmortizedCost (what you actually pay including all discounts).
    The difference is your effective discount.

    OnDemandCostEquivalent = list price for all usage
    AmortizedCost = actual amortized cost including RIs, SPs, discounts
    NetAmortizedCost = after credits

    effective_discount = 1 - (AmortizedCost / OnDemandCostEquivalent)
    """
    start, end = _date_range(months_back=3)

    try:
        # Total level first
        resp = ce_client.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["AmortizedCost", "OnDemandCostEquivalent", "NetAmortizedCost"],
        )

        total_amortized = 0.0
        total_od_equivalent = 0.0
        total_net = 0.0

        for period in resp.get("ResultsByTime", []):
            t = period.get("Total", {})
            total_amortized += float(t.get("AmortizedCost", {}).get("Amount", 0))
            total_od_equivalent += float(t.get("OnDemandCostEquivalent", {}).get("Amount", 0))
            total_net += float(t.get("NetAmortizedCost", {}).get("Amount", 0))

        if total_od_equivalent == 0:
            return None

        overall_discount = 1.0 - (total_amortized / total_od_equivalent)
        has_private = overall_discount > 0.05  # >5% suggests negotiated pricing beyond SP/RI

        # Per-service breakdown
        per_service: dict[str, float] = {}
        try:
            svc_resp = ce_client.get_cost_and_usage(
                TimePeriod={"Start": start, "End": end},
                Granularity="MONTHLY",
                Metrics=["AmortizedCost", "OnDemandCostEquivalent"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
            svc_totals: dict[str, dict[str, float]] = {}
            for period in svc_resp.get("ResultsByTime", []):
                for group in period.get("Groups", []):
                    svc = group["Keys"][0]
                    metrics = group.get("Metrics", {})
                    a = float(metrics.get("AmortizedCost", {}).get("Amount", 0))
                    o = float(metrics.get("OnDemandCostEquivalent", {}).get("Amount", 0))
                    if svc not in svc_totals:
                        svc_totals[svc] = {"amortized": 0.0, "od": 0.0}
                    svc_totals[svc]["amortized"] += a
                    svc_totals[svc]["od"] += o

            for svc, vals in svc_totals.items():
                if vals["od"] > 10:  # ignore noise below $10
                    per_service[svc] = round(1.0 - (vals["amortized"] / vals["od"]), 4)
        except Exception as e:
            log.debug("Per-service rate detection failed: %s", e)

        confidence = "high" if total_od_equivalent > 10000 else "medium" if total_od_equivalent > 1000 else "low"

        return EffectiveRateProfile(
            overall_discount_pct=round(overall_discount, 4),
            per_service_discount=per_service,
            source="cost_explorer",
            confidence=confidence,
            has_private_pricing=has_private,
            metadata={
                "total_amortized_3mo": round(total_amortized, 2),
                "total_od_equivalent_3mo": round(total_od_equivalent, 2),
                "total_net_3mo": round(total_net, 2),
                "period": f"{start} to {end}",
            },
        )

    except Exception as e:
        log.warning("Cost Explorer rate detection failed: %s", e)
        return None


def _detect_from_cur_athena(
    database: str,
    table: str,
) -> EffectiveRateProfile | None:
    """
    Query CUR via Athena to get per-line-item rate comparison.
    pricing/publicOnDemandRate vs lineItem/UnblendedRate reveals exact private rates.
    """
    try:
        import boto3
        athena = boto3.client("athena")
        s3_output = os.getenv("CUR_ATHENA_S3_OUTPUT", f"s3://aws-athena-query-results-{boto3.client('sts').get_caller_identity()['Account']}/finops-rates/")

        query = f"""
        SELECT
            "line_item_product_code" AS service,
            AVG(CAST("pricing_public_on_demand_rate" AS DOUBLE)) AS avg_public_rate,
            AVG(CAST("line_item_unblended_rate" AS DOUBLE)) AS avg_actual_rate,
            COUNT(*) AS line_count
        FROM "{database}"."{table}"
        WHERE
            "line_item_line_item_type" = 'Usage'
            AND "pricing_public_on_demand_rate" != ''
            AND "pricing_public_on_demand_rate" != '0'
            AND "line_item_usage_start_date" >= date_add('month', -3, current_date)
        GROUP BY "line_item_product_code"
        HAVING COUNT(*) > 100
        ORDER BY line_count DESC
        """

        resp = athena.start_query_execution(
            QueryString=query,
            QueryExecutionContext={"Database": database},
            ResultConfiguration={"OutputLocation": s3_output},
        )
        execution_id = resp["QueryExecutionId"]

        import time
        for _ in range(30):
            status_resp = athena.get_query_execution(QueryExecutionId=execution_id)
            state = status_resp["QueryExecution"]["Status"]["State"]
            if state == "SUCCEEDED":
                break
            if state in ("FAILED", "CANCELLED"):
                log.warning("Athena CUR query failed: %s", state)
                return None
            time.sleep(2)
        else:
            return None

        results_resp = athena.get_query_results(QueryExecutionId=execution_id)
        rows = results_resp.get("ResultSet", {}).get("Rows", [])
        if len(rows) <= 1:
            return None

        per_service: dict[str, float] = {}
        total_public = 0.0
        total_actual = 0.0

        for row in rows[1:]:  # skip header
            cells = [c.get("VarCharValue", "0") for c in row["Data"]]
            service = cells[0]
            try:
                pub = float(cells[1])
                actual = float(cells[2])
                if pub > 0 and actual >= 0:
                    discount = 1.0 - (actual / pub)
                    per_service[service] = round(discount, 4)
                    total_public += pub
                    total_actual += actual
            except (ValueError, ZeroDivisionError):
                continue

        if not per_service:
            return None

        overall = 1.0 - (total_actual / total_public) if total_public > 0 else 0.0

        return EffectiveRateProfile(
            overall_discount_pct=round(overall, 4),
            per_service_discount=per_service,
            source="cur_athena",
            confidence="high",
            has_private_pricing=overall > 0.05,
            metadata={"athena_database": database, "athena_table": table},
        )

    except Exception as e:
        log.warning("Athena CUR rate detection failed: %s", e)
        return None


def detect_effective_rates() -> EffectiveRateProfile:
    """
    Auto-detect the customer's effective rates. No manual input needed.

    Tries sources in order of accuracy:
      1. CUR via Athena (most accurate — line-item rates)
      2. Cost Explorer OnDemandCostEquivalent (good — amortized vs list)
      3. Public pricing (fallback — no discount applied)
    """
    # Try Athena CUR first (most accurate)
    athena_db = os.getenv("CUR_ATHENA_DATABASE")
    athena_table = os.getenv("CUR_ATHENA_TABLE", "cost_and_usage")
    if athena_db:
        profile = _detect_from_cur_athena(athena_db, athena_table)
        if profile:
            log.info(
                "Rate profile from CUR/Athena: %.1f%% overall discount, confidence=%s",
                profile.overall_discount_pct * 100,
                profile.confidence,
            )
            return profile

    # Fall back to Cost Explorer
    try:
        import boto3
        ce = boto3.client("ce", region_name="us-east-1")
        profile = _detect_from_cost_explorer(ce)
        if profile:
            log.info(
                "Rate profile from Cost Explorer: %.1f%% overall discount, confidence=%s",
                profile.overall_discount_pct * 100,
                profile.confidence,
            )
            return profile
    except Exception as e:
        log.warning("Could not initialize Cost Explorer for rate detection: %s", e)

    # No discount data available — use public pricing
    log.info("No private rate data detected. Using public on-demand pricing.")
    return EffectiveRateProfile(
        source="public_pricing",
        confidence="low",
        has_private_pricing=False,
    )
