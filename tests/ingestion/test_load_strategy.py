"""Tests for load strategy resolution."""
from __future__ import annotations

import pytest

from .conftest import make_engine


def test_resolve_load_strategy_forces_append_for_multi_file_processing() -> None:
    engine = make_engine()
    assert engine._resolve_load_strategy("TRUNCATE", force_append=True) == "APPEND"


def test_resolve_load_strategy_rejects_unsupported_merge_value() -> None:
    engine = make_engine()
    with pytest.raises(ValueError, match="Unsupported load_strategy"):
        engine._resolve_load_strategy("merge")
