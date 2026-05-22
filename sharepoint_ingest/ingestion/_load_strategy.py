"""Load strategy resolution helpers.

Extracted from ``sharepoint_ingest.ingestion_engine`` so callers and tests
can reference the logic without needing the full engine module.
"""

from __future__ import annotations

from typing import Optional


def resolve_load_strategy(
    configured_strategy: Optional[str],
    default_strategy: Optional[str] = "TRUNCATE",
    *,
    force_append: bool = False,
) -> str:
    """Resolve the effective load strategy string.

    Parameters
    ----------
    configured_strategy:
        The raw value from the ingestion config row.
    default_strategy:
        Fallback when *configured_strategy* is blank; defaults to
        ``"TRUNCATE"``.
    force_append:
        When ``True`` the strategy is always ``"APPEND"`` regardless of
        the configured value (used for multi-file batches).

    Returns
    -------
    str
        One of ``"TRUNCATE"`` or ``"APPEND"``.

    Raises
    ------
    ValueError
        If the strategy value is not a recognised token.
    """
    if force_append:
        return "APPEND"

    raw_value = (configured_strategy or default_strategy or "TRUNCATE").strip()
    if not raw_value:
        raw_value = "TRUNCATE"

    normalized = raw_value.replace("-", "_").upper()
    if normalized == "TRUNCATE_RELOAD":
        return "TRUNCATE"
    if normalized in {"TRUNCATE", "APPEND"}:
        return normalized

    raise ValueError(
        f"Unsupported load_strategy '{raw_value}'. Allowed values are TRUNCATE or APPEND."
    )
