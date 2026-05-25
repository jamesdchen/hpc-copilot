"""The :data:`DataAxis` correctness model.

Parallelizing a computation = partitioning a totally-ordered series. The
partition is *fungible* with a serial run iff it does not cut an
unaccounted data dependency. One question classifies every axis: **is
there carried state, and is the state-transition operator associative?**

=====================  ===============  =====================================
``DataAxis``           carried state    strategy
=====================  ===============  =====================================
:class:`Independent`   none             split anywhere (DOALL loop)
:class:`Associative`   yes, associative  prefix-scan: carry a fixed-size
                                        monoid summary (Blelloch scan)
:class:`BoundedHalo`   yes, bounded     halo overlap: replay the last
                       distance         ``halo_fn(params)`` rows, trim the
                                        emitted prefix (ghost cells / MPI)
:class:`Sequential`    unbounded /      do not split — one serial task per
                       order-dependent  sweep point (parallel-in-time)
=====================  ===============  =====================================

When in doubt, classify as :class:`Sequential`: the fail-safe outcome is
slow, not wrong.

Stdlib-only — safe to import at dispatch time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "Independent",
    "Associative",
    "BoundedHalo",
    "Sequential",
    "DataAxis",
    "Monoid",
    "Moments",
    "SUM",
    "MOMENTS",
]


@dataclass(frozen=True)
class Monoid:
    """An associative reduction with an identity element.

    A monoid lets :class:`Associative` chunks be combined in any order:
    each chunk emits a partial summary, and :func:`hpc_agent.experiment_kit.reduce_monoid`
    folds them. Non-associative aggregates (mean, variance, Sharpe,
    QLIKE) are recovered by carrying *sufficient statistics* — see
    :class:`Moments`.
    """

    identity: Any
    combine: Callable[[Any, Any], Any]


@dataclass(frozen=True)
class Independent:
    """No carried state — the loop body is a pure function of its row.

    Splits anywhere; chunks need no warm-up and no reduction beyond
    concatenation. The classic DOALL loop.
    """


@dataclass(frozen=True)
class Associative:
    """Carried state whose transition is associative.

    Each chunk produces a :class:`Monoid` partial; the partials fold in
    any order back to the serial result. This is the parallel-scan
    (Blelloch) case.
    """

    monoid: Monoid


@dataclass(frozen=True)
class BoundedHalo:
    """Carried state with a bounded look-back distance.

    ``halo_fn(params)`` returns the number of rows a chunk must replay
    as warm-up before the rows it emits. Over-wide halos are merely
    wasteful; too-small halos are silent corruption — so bias the
    estimate large.
    """

    halo_fn: Callable[[dict[str, Any]], int]


@dataclass(frozen=True)
class Sequential:
    """Unbounded or order-dependent state — not splittable.

    The series axis is run as a single serial task per sweep point. The
    sweep itself still fans out; only the per-point time axis stays
    whole.
    """


DataAxis = Independent | Associative | BoundedHalo | Sequential


@dataclass(frozen=True)
class Moments:
    """Sufficient statistics for order-invariant mean / variance.

    ``mean`` and ``variance`` are *not* associative, but ``(n, total,
    sumsq)`` is — so a windowed-stat experiment carries this triple as
    its :class:`Associative` monoid element and recovers the scalar at
    the end.
    """

    n: int = 0
    total: float = 0.0
    sumsq: float = 0.0

    @classmethod
    def of(cls, values: Any) -> Moments:
        """Build a :class:`Moments` from a sequence of numbers."""
        vals = [float(v) for v in values]
        return cls(n=len(vals), total=sum(vals), sumsq=sum(v * v for v in vals))

    def merge(self, other: Moments) -> Moments:
        """Associatively combine two :class:`Moments`."""
        return Moments(
            n=self.n + other.n,
            total=self.total + other.total,
            sumsq=self.sumsq + other.sumsq,
        )

    @property
    def mean(self) -> float:
        return self.total / self.n if self.n else 0.0

    @property
    def variance(self) -> float:
        """Population variance (divides by ``n``)."""
        if self.n == 0:
            return 0.0
        return max(0.0, self.sumsq / self.n - self.mean**2)

    @property
    def sample_variance(self) -> float:
        """Unbiased sample variance (divides by ``n - 1``)."""
        if self.n < 2:
            return 0.0
        return max(0.0, (self.sumsq - self.n * self.mean**2) / (self.n - 1))


#: The additive monoid over numbers.
SUM: Monoid = Monoid(identity=0.0, combine=lambda a, b: a + b)

#: The :class:`Moments` monoid — sufficient statistics for mean/variance.
MOMENTS: Monoid = Monoid(identity=Moments(), combine=lambda a, b: a.merge(b))
