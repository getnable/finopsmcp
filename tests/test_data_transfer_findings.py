"""get_data_transfer_costs: what the data transfer finding may claim.

check_data_transfer_costs read the spend on each transfer usage type from Cost
Explorer, which is real, and then wrote 30% of it into
estimated_monthly_savings with no source. The tool summed that into
estimated_reducible_savings. Transfer is not automatically waste; cutting it
is an architecture change whose saving nothing here measures.

It also asked for MONTHLY granularity over a 30-day window. A window that
crosses a month boundary comes back as two partial months, so one usage type
became two findings, each called a monthly cost and each held to the $50
threshold on its own: an $80 line split $40/$40 was not reported at all.
"""
from __future__ import annotations

from finops.analyzers import waste


class _CE:
    def __init__(self, periods: list[dict[str, float]]):
        self.periods = periods

    def get_cost_and_usage(self, **_kw):
        return {"ResultsByTime": [
            {"Groups": [{"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": str(v)}}}
                        for k, v in p.items()]}
            for p in self.periods]}


def test_transfer_spend_is_not_presented_as_a_30_percent_saving():
    ce = _CE([{"USE1-DataTransfer-Out-Bytes": 1000.0}])
    [f] = waste.check_data_transfer_costs(ce, "2026-09-01", "2026-10-01")
    assert f["estimated_monthly_savings"] is None
    assert f["unpriced"] is True
    assert f["period_cost"] == 1000.0


def test_a_window_across_two_months_is_one_line_per_usage_type():
    ce = _CE([{"USE1-DataTransfer-Regional-Bytes": 40.0},
              {"USE1-DataTransfer-Regional-Bytes": 40.0}])
    [f] = waste.check_data_transfer_costs(ce, "2026-09-13", "2026-10-13")
    assert f["period_cost"] == 80.0
    assert f["period"] == {"start": "2026-09-13", "end": "2026-10-13"}


def test_nat_matching_is_the_nat_gateway_usage_type_not_any_word_with_nat_in_it():
    ce = _CE([{"USE1-NatGateway-Bytes": 300.0, "USE1-SMS-International": 300.0}])
    got = waste.check_data_transfer_costs(ce, "2026-09-01", "2026-10-01")
    assert [f["usage_type"] for f in got] == ["USE1-NatGateway-Bytes"]
    assert "VPC endpoints" in got[0]["detail"]
