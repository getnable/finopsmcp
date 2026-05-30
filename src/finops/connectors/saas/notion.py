"""
Notion connector: writes cost reports to a shared Notion page.

Setup: create an integration at notion.so/my-integrations, share
the target page with the integration, then set NOTION_API_KEY and
NOTION_PAGE_ID env vars (or store them with: finops setup notion).

What this provides:
  - Creates or updates a dated cost report child page under your target page
  - Writes a summary callout, opportunity table, and footer block
  - Returns the URL of the published page
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

import httpx

_NOTION_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


class NotionConnector:
    """
    Writes cost reports and recommendations to a Notion page.

    Setup: create an integration at notion.so/my-integrations, share
    the target page with the integration, set NOTION_API_KEY and
    NOTION_PAGE_ID env vars.
    """

    def __init__(self) -> None:
        self._api_key = os.getenv("NOTION_API_KEY", "")
        self._page_id = os.getenv("NOTION_PAGE_ID", "")

    async def is_configured(self) -> bool:
        """Return True if both required env vars are set."""
        return bool(self._api_key and self._page_id)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def _find_child_page(self, client: httpx.AsyncClient, title: str) -> str | None:
        """
        Search for a child page with the given title under NOTION_PAGE_ID.
        Returns the page ID if found, None otherwise.
        """
        r = await client.post(
            f"{_NOTION_API_BASE}/search",
            headers=self._headers(),
            json={
                "query": title,
                "filter": {"value": "page", "property": "object"},
                "page_size": 20,
            },
        )
        r.raise_for_status()
        data = r.json()

        for result in data.get("results", []):
            # Only match pages that are children of our target page
            parent = result.get("parent", {})
            if parent.get("page_id") != self._page_id.replace("-", ""):
                # Notion returns IDs without hyphens sometimes; normalize both sides
                canonical_parent = parent.get("page_id", "").replace("-", "")
                canonical_target = self._page_id.replace("-", "")
                if canonical_parent != canonical_target:
                    continue

            # Check title match
            props = result.get("properties", {})
            title_prop = props.get("title", {})
            title_parts = title_prop.get("title", [])
            page_title = "".join(p.get("plain_text", "") for p in title_parts)
            if page_title == title:
                return result["id"]

        return None

    def _build_report_blocks(self, report: dict, report_date: str) -> list[dict[str, Any]]:
        """
        Build the list of Notion block objects for a cost report.

        Blocks:
          - Heading 1: report title
          - Callout: total savings summary
          - Heading 2: "Top Opportunities"
          - Table: one row per finding (up to 20)
          - Divider
          - Paragraph: generated-by footer
        """
        findings = report.get("findings", [])[:20]
        monthly = report.get("total_monthly_savings", 0.0)
        annual = report.get("total_annual_savings", monthly * 12)
        timestamp = report.get("scan_timestamp", datetime.utcnow().isoformat())
        account = report.get("account", "")

        heading_text = f"Cost Report: {report_date}"
        if account:
            heading_text = f"Cost Report: {report_date} ({account})"

        callout_text = (
            f"Total estimated saving: ${monthly:,.2f}/mo  "
            f"${annual:,.2f}/yr"
        )

        blocks: list[dict[str, Any]] = []

        # Heading 1
        blocks.append({
            "object": "block",
            "type": "heading_1",
            "heading_1": {
                "rich_text": [{"type": "text", "text": {"content": heading_text}}]
            },
        })

        # Callout
        blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": callout_text}}],
                "icon": {"type": "emoji", "emoji": "💰"},
                "color": "green_background",
            },
        })

        # Heading 2
        blocks.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "Top Opportunities"}}]
            },
        })

        # Table
        # Notion table rows: first row is the header row
        table_rows: list[dict[str, Any]] = []

        # Header row
        table_rows.append({
            "object": "block",
            "type": "table_row",
            "table_row": {
                "cells": [
                    [{"type": "text", "text": {"content": "Opportunity"}}],
                    [{"type": "text", "text": {"content": "Category"}}],
                    [{"type": "text", "text": {"content": "Monthly Saving"}}],
                    [{"type": "text", "text": {"content": "Annual Saving"}}],
                ]
            },
        })

        # Data rows
        for f in findings:
            monthly_s = f.get("monthly_savings", 0.0)
            annual_s = monthly_s * 12
            table_rows.append({
                "object": "block",
                "type": "table_row",
                "table_row": {
                    "cells": [
                        [{"type": "text", "text": {"content": f.get("title", "")}}],
                        [{"type": "text", "text": {"content": f.get("category", "")}}],
                        [{"type": "text", "text": {"content": f"${monthly_s:,.2f}"}}],
                        [{"type": "text", "text": {"content": f"${annual_s:,.2f}"}}],
                    ]
                },
            })

        blocks.append({
            "object": "block",
            "type": "table",
            "table": {
                "table_width": 4,
                "has_column_header": True,
                "has_row_header": False,
                "children": table_rows,
            },
        })

        # Divider
        blocks.append({"object": "block", "type": "divider", "divider": {}})

        # Footer
        footer = f"Generated by nable  {timestamp}"
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": footer},
                        "annotations": {"color": "gray"},
                    }
                ]
            },
        })

        return blocks

    async def append_to_page(self, page_id: str, blocks: list[dict]) -> None:
        """Append Notion blocks to an existing page."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.patch(
                f"{_NOTION_API_BASE}/blocks/{page_id}/children",
                headers=self._headers(),
                json={"children": blocks},
            )
            r.raise_for_status()

    async def write_cost_report(self, report: dict) -> str:
        """
        Create or update a cost report page under NOTION_PAGE_ID.

        Returns the URL of the published Notion page.
        """
        report_date = date.today().isoformat()
        page_title = f"nable Cost Report {report_date}"

        blocks = self._build_report_blocks(report, report_date)

        async with httpx.AsyncClient(timeout=30) as client:
            existing_id = await self._find_child_page(client, page_title)

            if existing_id:
                # Archive all existing blocks then re-add fresh content.
                # Notion does not have a "replace all content" API, so we
                # retrieve children and archive them individually.
                children_r = await client.get(
                    f"{_NOTION_API_BASE}/blocks/{existing_id}/children",
                    headers=self._headers(),
                )
                children_r.raise_for_status()
                child_ids = [b["id"] for b in children_r.json().get("results", [])]

                for child_id in child_ids:
                    await client.delete(
                        f"{_NOTION_API_BASE}/blocks/{child_id}",
                        headers=self._headers(),
                    )

                # Append fresh blocks
                await client.patch(
                    f"{_NOTION_API_BASE}/blocks/{existing_id}/children",
                    headers=self._headers(),
                    json={"children": blocks},
                )
                page_id = existing_id
            else:
                # Create a new child page
                create_r = await client.post(
                    f"{_NOTION_API_BASE}/pages",
                    headers=self._headers(),
                    json={
                        "parent": {"page_id": self._page_id},
                        "properties": {
                            "title": {
                                "title": [
                                    {"type": "text", "text": {"content": page_title}}
                                ]
                            }
                        },
                        "children": blocks,
                    },
                )
                create_r.raise_for_status()
                page_data = create_r.json()
                page_id = page_data["id"]

        # Notion page URLs use the ID without hyphens
        clean_id = page_id.replace("-", "")
        return f"https://notion.so/{clean_id}"
