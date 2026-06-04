"""Regression tests for idle-EC2 vCPU sizing (finops.analyzers.waste)."""
from finops.analyzers.waste import _vcpus_from_type, _SIZE_VCPU


def test_known_sizes_map_correctly():
    assert _vcpus_from_type("m5.large") == 2
    assert _vcpus_from_type("m5.4xlarge") == 16
    assert _vcpus_from_type("c6g.16xlarge") == 64
    assert _vcpus_from_type("t3.micro") == 1


def test_metal_is_not_assumed_96():
    # Regression: mapping every '*.metal' to 96 vCPU over-estimated idle savings
    # for smaller metal types (mac1.metal=12, z1d.metal=48, i3.metal=72). 'metal'
    # is intentionally not in the table, so it falls to the conservative default.
    assert "metal" not in _SIZE_VCPU
    assert _vcpus_from_type("mac1.metal") == 2
    assert _vcpus_from_type("z1d.metal") == 2


def test_unknown_suffix_falls_to_conservative_default():
    # An unrecognized size must under-estimate (2) rather than fabricate a number.
    assert _vcpus_from_type("future9.42xlarge") == 2
    assert _vcpus_from_type("garbage") == 2
