from finops.blame.git_blame import (
    _parse_porcelain, blame_sizing_commit, previous_sizing_value,
)
from finops.tagging.hcl_patcher import find_sizing_attr_line


def test_parse_porcelain_uncommitted():
    out = ("0000000000000000000000000000000000000000 1 1 1\n"
           "\tinstance_type = \"x\"\n")
    assert _parse_porcelain(out) is None


def test_parse_porcelain_fields():
    out = (
        "abc123 3 3 1\n"
        "author Sam Smith\n"
        "author-mail <sam@example.com>\n"
        "author-time 1699900000\n"
        "author-tz +0000\n"
        "summary bump web to m5.4xlarge\n"
        "filename main.tf\n"
        "\tinstance_type = \"m5.4xlarge\"\n"
    )
    ci = _parse_porcelain(out)
    assert ci.sha == "abc123"
    assert ci.author == "Sam Smith"
    assert ci.author_email == "sam@example.com"
    assert ci.summary == "bump web to m5.4xlarge"
    assert ci.authored_date is not None


def test_blame_and_previous_value(repo_with_resize):
    file_path = str(repo_with_resize / "main.tf")
    line_no, value = find_sizing_attr_line(file_path, "aws_instance", "web")
    assert value == "m5.4xlarge"
    commit = blame_sizing_commit(str(repo_with_resize), file_path, line_no)
    assert commit is not None
    assert "bump web to m5.4xlarge" in commit.summary
    prev = previous_sizing_value(str(repo_with_resize), commit.sha, file_path,
                                 "aws_instance", "web")
    assert prev == "m5.large"
