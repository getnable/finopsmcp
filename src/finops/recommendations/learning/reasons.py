"""
Turn a free-text dismiss reason into a canonical category the learning loop can use.

When a user dismisses a recommendation they may type anything ("reserved for our
Black Friday peak", "the SRE team owns this", "already in next sprint"). This maps
that to a small stable enum so the signal can learn patterns (e.g. lots of
"reserved_for_peak" on Spot recs -> weight Spot down for bursty workloads). The raw
text is always kept; the canonical value is only a hint, never the sole basis for
suppressing a recommendation.
"""
from __future__ import annotations

# canonical reason -> substrings that imply it (checked in order, first match wins)
_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("reserved_for_peak", ("peak", "black friday", "holiday", "burst", "spike",
                            "seasonal", "headroom", "capacity reserve", "reserved for")),
    ("sla_sensitive",     ("sla", "production critical", "prod critical", "latency",
                           "can't risk", "cannot risk", "uptime", "availability",
                           "mission critical", "customer facing", "customer-facing")),
    ("already_planned",   ("already", "planned", "in progress", "next sprint",
                           "roadmap", "scheduled", "ticket open", "being done", "wip")),
    ("wrong_estimate",    ("wrong", "inaccurate", "overstated", "not that much",
                           "estimate is off", "doesn't save", "does not save",
                           "no savings", "miscalculat")),
    ("not_our_resource",  ("not ours", "another team", "other team", "owned by",
                           "third party", "vendor owns", "not our resource",
                           "different account", "not mine")),
]

VALID_REASONS = {p[0] for p in _PATTERNS} | {"other"}


def classify_dismiss_reason(text: str | None) -> str:
    """Map free-text dismiss reason to a canonical category, or 'other'."""
    if not text:
        return "other"
    t = text.lower()
    for canonical, needles in _PATTERNS:
        if any(n in t for n in needles):
            return canonical
    return "other"
