"""The non-prod scheduler finds an environment tag whatever its key's case.

_ENV_TAG_KEYS listed "Environment", "Env", "environment", "Stage", "stage"
and compared keys exactly, so an instance tagged env=dev (lower-case key, the
most common spelling after Environment) or ENV=dev was not seen: on a fixture
account whose dev and staging instances all carry env=<value>,
identify_nonprod_resources found no instance at all.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from finops.analyzers.cloudwatch import GET_METRIC_DATA_ENV
from finops.recommendations.nonprod_scheduler import _get_env_tag, identify_nonprod_resources


@pytest.fixture(autouse=True)
def _free_path(monkeypatch):
    monkeypatch.delenv(GET_METRIC_DATA_ENV, raising=False)


@pytest.mark.parametrize("key", ["env", "Env", "ENV", "environment", "Environment",
                                 "ENVIRONMENT", "stage", "STAGE"])
def test_any_case_of_the_key_is_read(key):
    assert _get_env_tag([{"Key": key, "Value": "dev"}]) == "dev"


def test_a_prod_value_under_any_key_case_is_still_prod():
    assert _get_env_tag([{"Key": "ENV", "Value": "prod"}]) is None
    assert _get_env_tag([{"Key": "envy", "Value": "dev"}]) is None


@pytest.mark.parametrize("extra", [None, ["preview"]])
def test_identify_finds_an_instance_tagged_with_a_lower_case_env_key(extra):
    value = "preview" if extra else "dev"
    inst = {"InstanceId": "i-1", "InstanceType": "m5.large",
            "Tags": [{"Key": "Name", "Value": "dev-box"}, {"Key": "env", "Value": value}]}
    ec2, cw = MagicMock(), MagicMock()
    ec2.get_paginator.return_value.paginate.return_value = [
        {"Reservations": [{"Instances": [inst]}]}]
    cw.get_metric_statistics.return_value = {"Datapoints": []}
    boto3 = MagicMock()
    boto3.client.side_effect = lambda svc, **kw: ec2 if svc == "ec2" else cw
    with patch("finops.recommendations.nonprod_scheduler.boto3", boto3):
        out = asyncio.run(identify_nonprod_resources(
            aws_client=MagicMock(), regions=["us-east-1"], env_tags=extra))
    assert [i["instance_id"] for i in out["schedulable_instances"]] == ["i-1"], out
    assert out["schedulable_instances"][0]["environment"] == value
