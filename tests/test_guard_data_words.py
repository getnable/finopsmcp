"""A word inside a commit message or a search pattern is data, not a shell.

Blanking a commit message that mentions `terraform destroy` used to be turned
off by any shell or interpreter name anywhere in the command, the message
included: "from the staging env", "the node pool", "source the creds" each
named one, and the commit asked to confirm a destroy. And `git grep` and
`git log -S` searches were never data at all. The names now count only
outside the quoted data; whatever could run the data still turns it off.
"""
from __future__ import annotations

import pytest

import finops.guard as g


@pytest.mark.parametrize("cmd", [
    'git commit -m "Remove terraform destroy step from the staging env"',
    'git commit -m "Drop kubectl delete from the node pool runbook"',
    'git commit -m "Document how to source creds before terraform destroy"',
    "gh pr create --title 'Destroy legacy env' --body 'Runs terraform destroy on legacy'",
    "git grep -n 'aws ec2 terminate-instances'",
    "git log --oneline -S 'terraform destroy'",
    "git log --grep 'kubectl delete ns' --oneline | head",
])
def test_a_message_or_a_search_is_not_a_change(cmd):
    assert g.classify_command(cmd) is None, cmd


@pytest.mark.parametrize("cmd", [
    'git commit -m "staging env" && bash -c "terraform destroy"',
    "echo 'terraform destroy' | env bash",
    "echo 'terraform destroy' > x.sh; sh x.sh",
    "git grep -O\"sh -c 'terraform destroy'\" pattern",
    'git commit -m "$(terraform destroy -auto-approve)"',
    "git -c core.pager='terraform destroy' log",
    "git log -S 'terraform destroy' | sh",
])
def test_data_something_could_run_still_asks(cmd):
    hit = g.classify_command(cmd)
    assert hit == ("one_way", "delete_resource"), (cmd, hit)
