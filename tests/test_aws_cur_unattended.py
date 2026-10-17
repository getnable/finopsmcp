"""`nable aws-cur` with nobody to answer deploys nothing.

Its "Deploy now via AWS CLI? [Y/n]" took the default on a closed stdin, so a
script, a cron job or an agent running `nable aws-cur < /dev/null` with AWS
credentials created a CloudFormation stack (an S3 bucket, a CUR report, a Glue
crawler, an Athena workgroup, an IAM role) that no person had agreed to. A
closed stdin is no answer, and no answer is not a yes.
"""
from __future__ import annotations

import builtins

from finops.setup import cloud_infra


def test_a_closed_stdin_does_not_deploy_the_stack(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))

    def closed(*a, **k):
        raise EOFError
    monkeypatch.setattr(builtins, "input", closed)
    deployed = []
    monkeypatch.setattr(cloud_infra, "_deploy_cf_stack", lambda *a, **k: deployed.append(a) or {})
    assert cloud_infra._open_cf_console("Resources: {}", "123456789012") == {}
    assert deployed == [], "deployed a stack nobody agreed to"
    assert "aws cloudformation deploy" in capsys.readouterr().out


def test_a_yes_still_deploys(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "y")
    deployed = []
    monkeypatch.setattr(cloud_infra, "_deploy_cf_stack",
                        lambda *a, **k: deployed.append(a) or {"CUR_S3_BUCKET": "b"})

    class _Boto:
        @staticmethod
        def client(*a, **k):
            return object()
    monkeypatch.setitem(__import__("sys").modules, "boto3", _Boto)
    assert cloud_infra._open_cf_console("Resources: {}", "123456789012") == {"CUR_S3_BUCKET": "b"}
    assert len(deployed) == 1
