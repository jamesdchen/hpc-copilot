"""The halo-aware series loader — the single seam the planner controls.

A parallel task is a contiguous slice of a totally-ordered series. For a
*stateful* computation (a walk-forward backtest, an online-learning
scan) a chunk is only correct if it replays enough warm-up — the *halo*
— before the rows it is actually responsible for emitting.

:func:`load_series` is how the framework hands a task its slice without
the experiment knowing it has been chunked at all:

- On a whole-series run (``start=0, end=-1, halo=0``) it returns the
  entire series.
- On a chunked task it returns ``series[start - halo : end]`` — the
  emit range plus its warm-up prefix.

The active slice is carried in a :class:`contextvars.ContextVar` that
the ``compute(args)`` wrapper injected by :func:`hpc_agent.template.register_run`
sets from ``args.start`` / ``args.end`` / ``args.halo``. The
serial-elision harness sets it directly.

Stdlib-only — safe to import at dispatch time on a stdlib-only cluster.
"""

from __future__ import annotations

import contextvars
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "SliceSpec",
    "SeriesNotConfigured",
    "load_series",
    "set_series_loader",
    "current_slice",
    "trim_emission",
    "activate_slice",
    "deactivate_slice",
]


class SeriesNotConfigured(RuntimeError):
    """Raised when :func:`load_series` has no loader and no on-disk fallback."""


@dataclass(frozen=True)
class SliceSpec:
    """One task's view of a series.

    Attributes
    ----------
    start:
        First row index this task emits (inclusive).
    end:
        One past the last row this task emits; ``-1`` means "to the end
        of the series".
    halo:
        Number of warm-up rows replayed *before* ``start``. The loaded
        slice is ``series[start - halo : end]``; the first ``halo``
        emitted outputs are discarded (see :func:`trim_emission`).
    """

    start: int = 0
    end: int = -1
    halo: int = 0

    @property
    def is_whole(self) -> bool:
        """True for the canonical whole-series slice (``0 .. -1`` no halo)."""
        return self.start == 0 and self.end < 0 and self.halo == 0


_active_slice: contextvars.ContextVar[SliceSpec | None] = contextvars.ContextVar(
    "hpc_template_active_slice", default=None
)

_series_loader: Callable[[str], Any] | None = None


def set_series_loader(loader: Callable[[str], Any]) -> None:
    """Register the function that loads a *whole* series by name.

    ``loader(name)`` must return an indexable, sliceable sequence (a
    list, a numpy array, a pandas Series — anything supporting
    ``len()`` and ``seq[a:b]``). :func:`load_series` applies the active
    slice on top of whatever it returns.
    """
    global _series_loader
    _series_loader = loader


def current_slice() -> SliceSpec | None:
    """Return the :class:`SliceSpec` active for the current task, if any."""
    return _active_slice.get()


def activate_slice(spec: SliceSpec) -> contextvars.Token[SliceSpec | None]:
    """Make *spec* the active slice; returns a token for :func:`deactivate_slice`."""
    return _active_slice.set(spec)


def deactivate_slice(token: contextvars.Token[SliceSpec | None]) -> None:
    """Restore the slice context to its state before :func:`activate_slice`."""
    _active_slice.reset(token)


def load_series(name: str) -> Any:
    """Load series *name*, sliced to the current task's haloed window.

    On a whole-series run returns the entire series; on a chunked task
    returns ``series[start - halo : end]``. The experiment calls this
    exactly as it would a plain loader — the chunking is invisible.
    """
    full = _load_full(name)
    spec = _active_slice.get()
    if spec is None or spec.is_whole:
        return full
    n = len(full)
    end = n if spec.end < 0 else min(spec.end, n)
    lo = max(0, spec.start - max(0, spec.halo))
    return full[lo:end]


def trim_emission(values: Any) -> Any:
    """Drop the warm-up prefix from a per-row output sequence.

    A chunked task computes over its haloed slice and therefore emits
    ``halo`` extra leading rows. Pass the raw per-row output here to get
    back just the rows this task is responsible for. A no-op on a
    whole-series run.
    """
    spec = _active_slice.get()
    if spec is None or spec.halo <= 0:
        return values
    return values[spec.halo :]


def _load_full(name: str) -> Any:
    if _series_loader is not None:
        return _series_loader(name)
    root = Path(os.environ.get("LOCAL_DATA_DIR", "."))
    candidate = root / f"{name}.json"
    if candidate.is_file():
        return json.loads(candidate.read_text(encoding="utf-8"))
    raise SeriesNotConfigured(
        f"no series loader registered and no {name}.json found under {root}. "
        "Call hpc_agent.template.set_series_loader(fn) where fn(name) returns "
        "the whole series."
    )
