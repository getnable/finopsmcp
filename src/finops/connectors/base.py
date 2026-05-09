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
