from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class CostEntry:
    provider: str          # "aws" | "azure" | "gcp"
    account_id: str        # account / subscription / billing-account id
    account_name: str
    service: str           # normalized service name
    region: str            # "" if not applicable
    amount: float          # USD
    currency: str = "USD"
    tags: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CostSummary:
    provider: str
    start_date: date
    end_date: date
    total_usd: float
    by_service: dict[str, float]   # service -> USD
    by_account: dict[str, float]   # account_id -> USD
    by_region: dict[str, float]    # region -> USD
    entries: list[CostEntry]
    currency: str = "USD"          # billing currency reported by the provider;
                                   # "MIXED" if rows span more than one currency.
                                   # nable does not convert — non-USD is surfaced, not relabeled.


def returned_no_rows(summary: Any) -> bool:
    """True when the provider answered but sent back no cost rows at all.

    That is not a $0 bill. A $0 bill is rows that sum to zero (AWS flags it as
    _zero_spend_account); no rows is Cost Explorer not yet backfilled, a
    billing export that has not landed, or a query that matched nothing, and
    the tools used to print both as "$0.00".
    """
    if getattr(summary, "_zero_spend_account", False) is True:
        return False
    return (not getattr(summary, "entries", None)
            and not getattr(summary, "by_service", None)
            and not getattr(summary, "total_usd", 0))


def no_rows_message(provider: str) -> str:
    """What to tell a reader when `provider` was read and returned no cost rows."""
    name = {"aws": "AWS Cost Explorer", "gcp": "GCP billing", "azure": "Azure Cost Management"}.get(
        provider, provider)
    hint = (" On a new account Cost Explorer can take up to 24 hours after it is "
            "enabled to show data; check Billing > Cost Explorer in the AWS console."
            if provider == "aws" else "")
    return (f"{name} was read but returned no cost rows for this period, so "
            f"there is no spend figure to report.{hint}")


def combined_currency(summaries: list[CostSummary]) -> str:
    """The currency label for a merge of per-account summaries.

    Only summaries that actually carried rows vote: an empty account defaults
    to "USD" and would otherwise turn a single-currency JPY rollup into MIXED.
    """
    found = {s.currency for s in summaries if s.entries and s.currency}
    if len(found) == 1:
        return found.pop()
    return "MIXED" if found else "USD"


class BaseConnector(ABC):
    provider: str = ""

    @abstractmethod
    async def is_configured(self) -> bool:
        """Return True if required credentials are present."""

    @abstractmethod
    async def get_costs(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",  # "DAILY" | "MONTHLY"
        group_by: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> CostSummary:
        """Fetch cost data for the given date range."""

    @abstractmethod
    async def list_accounts(self) -> list[dict[str, str]]:
        """Return list of {id, name} dicts for all accessible accounts."""
