"""
The slice engine: one query primitive that groups, filters, and excludes over any
FOCUS dimension. It is what makes nable's dashboards moldable instead of rigid: the
agent compiles a natural-language question into a SliceSpec, the engine runs it over
FOCUS records in memory, and the result carries a self-describing CardSpec that any
surface (web Ask tab, Slack, Claude/Cursor) can render and pin.
"""
from .spec import (
    CARD_TEMPLATES,
    FILTER_OPS,
    FOCUS_DIMENSIONS,
    GRANULARITIES,
    METRICS,
    CardSpec,
    FilterClause,
    SliceResult,
    SliceSpec,
    parse_spec,
)
from .engine import run_slice

__all__ = [
    "SliceSpec",
    "FilterClause",
    "SliceResult",
    "CardSpec",
    "parse_spec",
    "run_slice",
    "FOCUS_DIMENSIONS",
    "METRICS",
    "GRANULARITIES",
    "FILTER_OPS",
    "CARD_TEMPLATES",
]
