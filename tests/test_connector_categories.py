"""Each SaaS connector must tag its spend with the intended FOCUS ServiceCategory.

Datadog/New Relic/PagerDuty are observability, GitHub is developer tooling. Before
this they all fell into "Other". This guards the wiring so a connector can't quietly
regress to an undifferentiated bucket.
"""
import re
from pathlib import Path

import pytest

CONNECTORS = Path(__file__).parent.parent / "src" / "finops" / "connectors"

EXPECTED = {
    "saas/datadog.py": "Observability",
    "saas/new_relic.py": "Observability",
    "saas/pagerduty.py": "Observability",
    "saas/github.py": "Developer Tools",
    "saas/vercel.py": "Compute",
    "saas/cloudflare.py": "Networking",
    "saas/langfuse.py": "AI and Machine Learning",
    "saas/mongodb_atlas.py": "Database",
    "saas/snowflake.py": "Database",
    "databricks.py": "Compute",
}


@pytest.mark.parametrize("rel,category", EXPECTED.items())
def test_connector_uses_expected_focus_category(rel, category):
    src = (CONNECTORS / rel).read_text()
    cats = set(re.findall(r'category="([^"]+)"', src))
    assert category in cats, f"{rel} should tag spend as {category!r}, found {cats}"


def test_new_categories_are_registered():
    from finops.focus.schema import SERVICE_CATEGORIES
    assert {"Observability", "Developer Tools"} <= SERVICE_CATEGORIES
