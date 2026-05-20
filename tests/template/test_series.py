"""``load_series`` halo-aware slicing (Layer 2)."""

from __future__ import annotations

import pytest

from hpc_agent.template import (
    SliceSpec,
    current_slice,
    load_series,
    set_series_loader,
    trim_emission,
)
from hpc_agent.template.series import SeriesNotConfigured, activate_slice, deactivate_slice

_SERIES = list(range(100))


def _with_series() -> None:
    set_series_loader(lambda name: _SERIES)


def test_whole_run_returns_full_series() -> None:
    _with_series()
    assert load_series("x") == _SERIES


def test_chunk_slice_includes_halo_prefix() -> None:
    _with_series()
    token = activate_slice(SliceSpec(start=40, end=60, halo=8))
    try:
        loaded = load_series("x")
        # emit range [40, 60) plus 8 warm-up rows -> [32, 60)
        assert loaded == list(range(32, 60))
        assert current_slice() == SliceSpec(40, 60, 8)
    finally:
        deactivate_slice(token)


def test_halo_clamped_at_series_start() -> None:
    _with_series()
    token = activate_slice(SliceSpec(start=0, end=10, halo=8))
    try:
        # cannot replay before row 0
        assert load_series("x") == list(range(0, 10))
    finally:
        deactivate_slice(token)


def test_end_minus_one_means_to_end() -> None:
    _with_series()
    token = activate_slice(SliceSpec(start=90, end=-1, halo=0))
    try:
        assert load_series("x") == list(range(90, 100))
    finally:
        deactivate_slice(token)


def test_trim_emission_drops_warmup_prefix() -> None:
    token = activate_slice(SliceSpec(start=40, end=60, halo=8))
    try:
        assert trim_emission(list(range(28))) == list(range(8, 28))
    finally:
        deactivate_slice(token)
    # no active slice -> no-op
    assert trim_emission([1, 2, 3]) == [1, 2, 3]


def test_missing_loader_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # The loader global lives in the self-contained _runtime module.
    import hpc_agent.template._runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_series_loader", None)
    monkeypatch.delenv("LOCAL_DATA_DIR", raising=False)
    with pytest.raises(SeriesNotConfigured, match="set_series_loader"):
        load_series("nonexistent_series_xyz_abc")
