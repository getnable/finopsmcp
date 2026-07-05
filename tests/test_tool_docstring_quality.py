"""Docstring-quality ratchet for MCP tools.

External MCP directories grade each tool's description, and the model itself
picks tools by docstring. The bar: a real description (25+ words), documented
args when the tool takes params, and natural-language examples. Existing weak
docstrings are grandfathered below; the list may only SHRINK. A new tool that
ships weak fails here.
"""
import ast
from pathlib import Path

SERVER = Path(__file__).parent.parent / "src" / "finops" / "server.py"

# Empty since 2026-07-05: every tool meets the bar. Keep it empty.
GRANDFATHERED: set[str] = set()


def _tools():
    tree = ast.parse(SERVER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            if any(getattr(getattr(d, "func", None), "attr", "") == "tool"
                   for d in node.decorator_list if isinstance(d, ast.Call)):
                yield node


def _weak(node) -> str | None:
    doc = ast.get_docstring(node) or ""
    params = [a.arg for a in node.args.args if a.arg != "self"]
    if len(doc.split()) < 25:
        return "description under 25 words"
    if params and "Args:" not in doc:
        return "takes params but has no Args: section"
    if "Example" not in doc:
        return "no Examples"
    return None


def test_no_new_weak_tool_docstrings():
    failures = []
    for node in _tools():
        reason = _weak(node)
        if reason and node.name not in GRANDFATHERED:
            failures.append(f"{node.name}: {reason}")
    assert not failures, (
        "New/changed tools must ship a full docstring (25+ words, Args:, Examples): "
        + "; ".join(failures)
    )


def test_grandfathered_list_only_shrinks():
    # Entries that no longer exist or are no longer weak should be removed.
    weak_now = {n.name for n in _tools() if _weak(n)}
    stale = GRANDFATHERED - weak_now
    assert not stale, f"These are fixed or gone; remove from GRANDFATHERED: {sorted(stale)}"
