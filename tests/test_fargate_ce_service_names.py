"""Fargate spend is looked for under the ECS service name too.

Several Cost Explorer reads used "AWS Fargate" as a SERVICE value: the
commitment engine's EC2 + Fargate spend, the Savings Plans waste pattern's
Fargate share, and the "fargate" alias. Nothing in the repo shows Cost
Explorer naming a service "AWS Fargate". Fargate's prices live in the
AmazonECS offer (AWS Price List, AmazonECS us-east-1 20260911124425,
USE1-Fargate-vCPU-Hours:perCPU), and the codebase names ECS spend "Amazon
Elastic Container Service" (connectors/universal.py, anomaly/root_cause.py).
A SERVICE value Cost Explorer does not use matches nothing, so Fargate read
as $0. Both names are now matched; neither is verified against a live
account.
"""
from __future__ import annotations

import pytest

ECS = "Amazon Elastic Container Service"
EC2 = "Amazon Elastic Compute Cloud - Compute"


def test_the_commitment_spend_filters_name_ecs_and_keep_the_alias():
    from finops.recommendations import commitments as c

    seen = []

    class _CE:
        def get_cost_and_usage(self, **kw):
            seen.append(kw["Filter"])
            return {"ResultsByTime": []}

    c._total_ec2_spend(_CE(), "2026-07-01", "2026-10-01")
    c._ec2_spend_for_tag(_CE(), "2026-07-01", "2026-10-01", "team", "web")
    total, tagged = seen
    assert set(total["Dimensions"]["Values"]) == {EC2, ECS, "AWS Fargate"}
    assert set(tagged["And"][0]["Dimensions"]["Values"]) == {EC2, ECS, "AWS Fargate"}


def test_the_savings_plans_pattern_prices_ecs_spend_as_fargate():
    from finops.ml.patterns import PatternContext, _check_savings_plans_coverage

    costs = {EC2: [100.0] * 30, ECS: [50.0] * 30}
    m = _check_savings_plans_coverage(PatternContext(
        daily_costs=costs, by_resource=[], snapshots=[{"savings_plan_coverage_pct": 0.0}]))
    # 3,000 x 0.26 + 1,500 x 0.20, the Fargate rate, not 3,000 x 0.26 alone.
    assert m.monthly_waste == pytest.approx(1080.0)


def test_the_fargate_alias_resolves_to_ecs_and_still_to_the_old_name():
    from finops.connectors.universal import _resolve_aws_service

    assert _resolve_aws_service("fargate", [EC2, ECS]) == ECS
    assert _resolve_aws_service("fargate", [EC2, "AWS Fargate"]) == "AWS Fargate"
