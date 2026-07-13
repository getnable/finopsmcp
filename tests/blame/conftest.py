import json
import subprocess
from pathlib import Path

import pytest

_TF = '''resource "aws_instance" "web" {{
  ami           = "ami-123"
  instance_type = "{size}"
  tags = {{
    Name = "web"
  }}
}}
'''


def _run(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _state(size: str) -> str:
    return json.dumps({
        "version": 4,
        "resources": [{
            "type": "aws_instance", "name": "web",
            "instances": [{"attributes": {"id": "i-0abc123", "instance_type": size}}],
        }],
    })


@pytest.fixture
def tf_source():
    return _TF


@pytest.fixture
def state_json():
    return _state


@pytest.fixture
def repo_with_resize(tmp_path, monkeypatch):
    """git repo where aws_instance.web was resized m5.large -> m5.4xlarge, with
    tfstate mapping i-0abc123 to that block."""
    monkeypatch.setenv("GITHUB_TOKEN", "")  # no PR resolution by default
    _run(tmp_path, "git", "init", "-q")
    _run(tmp_path, "git", "config", "user.email", "dev@example.com")
    _run(tmp_path, "git", "config", "user.name", "Dev Example")
    tf = tmp_path / "main.tf"
    tf.write_text(_TF.format(size="m5.large"))
    _run(tmp_path, "git", "add", "-A")
    _run(tmp_path, "git", "commit", "-q", "-m", "initial: web m5.large")
    tf.write_text(_TF.format(size="m5.4xlarge"))
    _run(tmp_path, "git", "add", "-A")
    _run(tmp_path, "git", "commit", "-q", "-m", "bump web to m5.4xlarge for launch")
    (tmp_path / "terraform.tfstate").write_text(_state("m5.4xlarge"))
    return tmp_path
