"""Lever-2 token cap: idle_resources_summary must bound the detail list while
keeping the totals computed over the full population."""
from finops.cleanup.idle import IdleResource, idle_resources_summary
from finops.token_budget import estimate_tokens, DEFAULT_MAX_TOKENS


def _mk(i: int) -> IdleResource:
    return IdleResource(
        resource_type="ebs_volume",
        resource_id=f"vol-{i:06d}",
        region="us-east-1",
        account_id="111111111111",
        name=f"orphan-volume-{i}-with-a-longish-descriptive-name",
        idle_since="2026-01-01",
        idle_days=90,
        monthly_cost_usd=float(1000 - i),  # descending cost
        reason="Unattached for 90 days; no snapshot dependency found.",
    )


def test_totals_cover_all_but_detail_list_is_capped():
    resources = [_mk(i) for i in range(800)]
    out = idle_resources_summary(resources)

    # Totals are over the full 800, not just the kept rows.
    assert out["total_resources_found"] == 800
    expected_total = round(sum(r.monthly_cost_usd for r in resources), 2)
    assert out["total_monthly_waste_usd"] == expected_total

    # Detail list is bounded and flagged as truncated.
    assert len(out["resources"]) < 800
    assert out["resources_truncated"] is True
    assert out["resources_omitted"] == 800 - len(out["resources"])

    # The kept rows are the costliest (sorted desc), and the payload fits budget.
    assert out["resources"][0]["monthly_cost_usd"] == 1000.0
    assert estimate_tokens(out["resources"]) <= DEFAULT_MAX_TOKENS


def test_small_result_is_not_truncated():
    out = idle_resources_summary([_mk(i) for i in range(5)])
    assert len(out["resources"]) == 5
    assert "resources_truncated" not in out
