"""
FOCUS 2.0 normalization layer.

Provides a single normalize() entry point that routes raw provider rows
to the correct translator and returns a FocusRecord.

Usage:
    from finops.focus import normalize
    record = normalize("aws", cur_row_dict)
    record = normalize("azure", cost_export_row_dict)
    record = normalize("gcp", bigquery_row_dict)
"""
from __future__ import annotations

from .schema import FocusRecord
from .translators import aws as _aws_t
from .translators import azure as _azure_t
from .translators import gcp as _gcp_t
from .translators import snowflake as _snowflake_t

__all__ = ["normalize", "FocusRecord"]

_TRANSLATORS = {
    "aws": _aws_t.translate,
    "azure": _azure_t.translate,
    "gcp": _gcp_t.translate,
    "snowflake": _snowflake_t.translate,
}


def normalize(provider: str, raw_row: dict) -> FocusRecord:
    """
    Translate a raw provider row dict into a FocusRecord.

    Args:
        provider: One of "aws", "azure", or "gcp" (case-insensitive).
        raw_row:  Dict representing one line item from the provider's export.

    Returns:
        A FocusRecord with all fields populated. Fields not available in the
        source data are set to None or 0.0 rather than raising.

    Raises:
        ValueError: If provider is not recognized.
    """
    key = provider.lower()
    translator = _TRANSLATORS.get(key)
    if translator is None:
        supported = ", ".join(sorted(_TRANSLATORS.keys()))
        raise ValueError(
            f"Unknown provider {provider!r}. Supported providers: {supported}"
        )
    return translator(raw_row)
