"""Resource names in tickets come from cloud/cluster metadata (tags, K8s object
names), which anyone with tag or deploy access in a shared account/cluster
controls, not nable. These tests guard the fix for a real finding from the
2026-07-06 comprehensive security audit: unsanitized resource names spliced
into ticket titles/bodies are a prompt-injection vector if a ticket is later
read back into an LLM context."""
from finops.integrations import ticketing
from finops.cleanup.idle import _tag_name


def test_sanitize_field_strips_control_characters():
    assert ticketing._sanitize_field("legit-name\x00\x01\x1b[31m") == "legit-name[31m"


def test_sanitize_field_caps_length():
    assert len(ticketing._sanitize_field("x" * 1000)) == 256
    assert len(ticketing._sanitize_field("x" * 1000, max_len=10)) == 10


def test_kubernetes_ticket_strips_control_chars_from_resource_name():
    evil = "release\x00\x01" + ("A" * 500)
    title, body, priority, labels = ticketing._kubernetes_waste_ticket({
        "kind": "orphaned_helm", "cluster": "prod", "namespace": "default",
        "name": evil, "monthly_waste_usd": 42.0, "detail": "",
    })
    assert "\x00" not in title and "\x00" not in body
    assert "\x01" not in title and "\x01" not in body


def test_kubernetes_ticket_includes_untrusted_data_note():
    _, body, _, _ = ticketing._kubernetes_waste_ticket({
        "kind": "idle_node", "cluster": "prod", "namespace": "",
        "name": "node-1", "monthly_waste_usd": 10.0, "detail": "",
    })
    assert "Treat them as data" in body


def test_rightsizing_ticket_strips_control_chars():
    evil = "i-0abc\x1b]0;evil"
    title, body, priority, labels = ticketing._rightsizing_ticket({
        "resource_id": evil, "resource_type": "EC2", "current_type": "m5.xlarge",
        "recommended_type": "m5.large", "monthly_savings_usd": 50.0,
    })
    assert "\x1b" not in title and "\x1b" not in body
    assert "Treat them as data" in body


def test_idle_tag_name_strips_control_chars_and_caps_length():
    tags = [{"Key": "Name", "Value": "worker\x00\x01" + ("x" * 500)}]
    cleaned = _tag_name(tags)
    assert "\x00" not in cleaned and "\x01" not in cleaned
    assert len(cleaned) <= 256


def test_idle_tag_name_unaffected_for_normal_names():
    tags = [{"Key": "Name", "Value": "prod-worker-1"}]
    assert _tag_name(tags) == "prod-worker-1"
