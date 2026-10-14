"""The CLIs' own container images run the CLI.

`docker run amazon/aws-cli ec2 terminate-instances` is how AWS documents
running its CLI in a container, and `docker run hashicorp/terraform:1.9
destroy` how HashiCorp documents terraform's. With a tag after the program
name (`terraform:1.9`) or a name that is not the program's (`aws-cli`), the
guard did not see the command at all.
"""
from __future__ import annotations

import pytest

import finops.guard as g


@pytest.mark.parametrize("cmd,want", [
    ("docker run --rm -v ~/.aws:/root/.aws amazon/aws-cli ec2 terminate-instances "
     "--instance-ids i-1", ("one_way", "terminate_instance")),
    ("docker run --rm public.ecr.aws/aws-cli/aws-cli:2.17.0 s3 rb s3://old --force",
     ("one_way", "delete_resource")),
    ("docker run --rm -v $PWD:/w -w /w hashicorp/terraform:1.9 destroy -auto-approve",
     ("one_way", "delete_resource")),
    ("podman run --rm docker.io/bitnami/kubectl:1.30 delete ns staging",
     ("one_way", "delete_resource")),
    ("docker run --rm alpine/helm:3.15.2 uninstall api -n staging",
     ("one_way", "delete_resource")),
    ("docker run --rm amazon/aws-cli ec2 describe-instances", None),
    ("docker run --rm hashicorp/terraform:1.9 version", None),
    ("docker pull hashicorp/terraform:1.9", None),
])
def test_a_cli_image_is_read_as_its_cli(cmd, want):
    assert g.classify_command(cmd) == want


def test_a_cli_image_launch_is_priced_like_the_cli():
    est = g.estimate_command_monthly_cost(
        "docker run --rm amazon/aws-cli ec2 run-instances --image-id ami-1 "
        "--instance-type p4d.24xlarge --count 2")
    assert est is not None and est["count"] == 2
