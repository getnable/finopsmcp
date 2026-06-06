"""CSV exports must neutralize spreadsheet formula injection (CWE-1236): a cell
starting with =, +, -, @, tab, or CR is a live formula in Excel/Sheets, and these
cells come from resource/tag names a lower-privileged user can set."""
from finops.reporting.exporter import _csv_safe as exporter_safe
from finops.server_web import _csv_safe as web_safe
import pytest


@pytest.mark.parametrize("fn", [exporter_safe, web_safe])
def test_formula_triggers_are_neutralized(fn):
    for payload in ('=HYPERLINK("http://evil")', "+1+1", "-2", "@SUM(A1)", "\tcmd", "\rx"):
        out = fn(payload)
        assert out.startswith("'"), f"{payload!r} not neutralized -> {out!r}"
        assert out[1:] == payload  # original value preserved after the quote


@pytest.mark.parametrize("fn", [exporter_safe, web_safe])
def test_safe_values_pass_through(fn):
    for ok in ("EC2", "us-east-1", "my-bucket", "$5,361", "i-0463ff880c54fdf44", ""):
        assert fn(ok) == ok


@pytest.mark.parametrize("fn", [exporter_safe, web_safe])
def test_non_strings_unchanged(fn):
    assert fn(42) == 42
    assert fn(3.14) == 3.14
    assert fn(None) is None
