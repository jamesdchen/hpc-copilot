"""Serial-elision harness — the safety backstop (Layer 2).

Each test runs a fixture experiment whole vs. split and asserts the
harness reaches the right verdict: a correctly-classified axis passes, a
misclassified one is flagged.
"""

from __future__ import annotations

import pytest

from hpc_agent.experiment_kit import (
    MOMENTS,
    SUM,
    Associative,
    BoundedHalo,
    Independent,
    Moments,
    Sequential,
    assert_elision_equivalent,
    check_elision,
    load_series,
    set_series_loader,
)

_SERIES = [float(i) for i in range(60)]
_WINDOW = 4


@pytest.fixture(autouse=True)
def _series_loader() -> None:
    set_series_loader(lambda name: _SERIES)


# ─── fixtures: experiment functions ─────────────────────────────────────────


def run_scaled(scale: float = 1.0) -> list[float]:
    """Stateless map — correctly Independent."""
    return [v * scale for v in load_series("series")]


def run_cumsum() -> list[float]:
    """Running total — unbounded carried state. NOT Independent."""
    out: list[float] = []
    acc = 0.0
    for v in load_series("series"):
        acc += v
        out.append(acc)
    return out


def run_moving_average() -> list[float]:
    """Trailing window mean — bounded look-back of ``_WINDOW`` rows."""
    s = load_series("series")
    out: list[float] = []
    for i in range(len(s)):
        lo = max(0, i - _WINDOW + 1)
        window = s[lo : i + 1]
        out.append(sum(window) / len(window))
    return out


def run_moments() -> Moments:
    """Sufficient statistics — a genuine associative summary."""
    return Moments.of(load_series("series"))


def run_mean() -> float:
    """A bare mean — NOT associative; cannot be a monoid partial."""
    s = load_series("series")
    return sum(s) / len(s)


# ─── tests ──────────────────────────────────────────────────────────────────


def test_independent_correct_passes() -> None:
    report = check_elision(run_scaled, {"scale": 2.0}, Independent(), chunks=5, series_length=60)
    assert report.passed, report.detail


def test_cumsum_misclassified_as_independent_is_flagged() -> None:
    report = check_elision(run_cumsum, {}, Independent(), chunks=5, series_length=60)
    assert not report.passed
    assert "DIVERGED" in report.detail


def test_bounded_halo_with_sufficient_halo_passes() -> None:
    report = check_elision(
        run_moving_average, {}, BoundedHalo(lambda p: _WINDOW), chunks=5, series_length=60
    )
    assert report.passed, report.detail


def test_bounded_halo_too_small_is_flagged() -> None:
    # A zero halo cannot cover a width-4 trailing window.
    report = check_elision(
        run_moving_average, {}, BoundedHalo(lambda p: 0), chunks=5, series_length=60
    )
    assert not report.passed


def test_associative_correct_passes() -> None:
    report = check_elision(run_moments, {}, Associative(MOMENTS), chunks=6, series_length=60)
    assert report.passed, report.detail


def test_associative_misuse_is_flagged() -> None:
    # run_mean returns a (non-associative) mean but the axis declares the
    # additive monoid — folding the chunk means does not recover the whole.
    report = check_elision(run_mean, {}, Associative(SUM), chunks=6, series_length=60)
    assert not report.passed


def test_sequential_axis_passes_trivially() -> None:
    report = check_elision(run_cumsum, {}, Sequential(), chunks=8, series_length=60)
    assert report.passed
    assert report.chunks == 1


def test_assert_elision_equivalent_raises_on_divergence() -> None:
    with pytest.raises(AssertionError, match="serial-elision gate failed"):
        assert_elision_equivalent(run_cumsum, {}, Independent(), chunks=5, series_length=60)


def test_assert_elision_equivalent_returns_report_on_pass() -> None:
    report = assert_elision_equivalent(
        run_scaled, {"scale": 1.0}, Independent(), chunks=4, series_length=60
    )
    assert report.passed
