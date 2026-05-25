"""Self-contained cluster runtime for hpc-agent experiment notebooks.

This module is the single home of the *runtime* surface a notebook
experiment needs on a compute node: the ``@register_run`` decorator and
the ``compute(args)`` wrapper it injects, plus the halo-aware
``load_series`` seam and ``save_artifact``.

It is deliberately **stdlib-only** and imports nothing from
``hpc_agent`` — so :func:`hpc_agent.experiment_kit.export_notebook` can inline
this file's source verbatim into an exported executor. The exported
``.py`` then runs on a stdlib-only cluster with no ``hpc-agent``
install, exactly the way ``.hpc/cli.py`` carries an inlined copy of
``Flag``.

Because the export inlines *this exact source*, there is no second copy
to keep in lock-step — the authoring API (``hpc_agent.experiment_kit``) and
the inlined cluster runtime are the same bytes by construction.

The richer authoring surface — flag synthesis, the parallelization
planner, the notebook exporter, the serial-elision harness — lives in
the other ``hpc_agent.experiment_kit`` submodules and is **not** part of this
runtime.
"""

from __future__ import annotations

import contextlib
import contextvars
import inspect
import json
import os
import pickle
from collections.abc import Callable, Iterator
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
    "register_run",
    "RunSpec",
    "save_artifact",
]


# ─── series slicing ─────────────────────────────────────────────────────────


class SeriesNotConfigured(RuntimeError):
    """Raised when :func:`load_series` has no loader and no on-disk fallback."""


@dataclass(frozen=True)
class SliceSpec:
    """One task's view of a series.

    ``start`` / ``end`` are the emit range (``end`` of ``-1`` means "to
    the end of the series"); ``halo`` is the count of warm-up rows
    replayed before ``start``. The loaded slice is
    ``series[start - halo : end]``.
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

    ``loader(name)`` must return an indexable, sliceable sequence;
    :func:`load_series` applies the active slice on top of it.
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

    A chunked task computes over its haloed slice and so emits ``halo``
    extra leading rows; this returns just the rows it is responsible
    for. A no-op on a whole-series run.
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
        "Call set_series_loader(fn) where fn(name) returns the whole series."
    )


# ─── the experiment decorator + compute wrapper ─────────────────────────────


@dataclass(frozen=True)
class RunSpec:
    """Metadata recorded for one ``@register_run`` function.

    Flag synthesis is an authoring concern (see
    ``hpc_agent.experiment_kit.flags_for_run``) and deliberately not eager
    here — keeping this dataclass dependency-free is what lets the
    runtime stay inline-able.
    """

    func: Callable[..., Any]
    name: str
    gpu: bool


_artifact_dir: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "hpc_template_artifact_dir", default=None
)


def register_run(func: Any = None, *, gpu: bool = False) -> Any:
    """Mark the experiment entry point. Works bare or as ``@register_run(gpu=True)``.

    At import time it records the run in a module-level ``_RUNS``
    registry and injects a ``compute(args)`` wrapper into the defining
    module — satisfying the hpc-agent executor contract without the
    researcher writing any CLI glue. One ``@register_run`` per module is
    the expected shape.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = RunSpec(func=fn, name=fn.__name__, gpu=gpu)
        fn.__dict__["_hpc_run"] = spec
        module_ns = fn.__globals__
        runs: dict[str, RunSpec] = module_ns.setdefault("_RUNS", {})
        runs[fn.__name__] = spec
        module_ns["compute"] = _make_compute(spec)
        return fn

    if func is not None and callable(func):
        return decorate(func)
    return decorate


def save_artifact(name: str, obj: Any) -> Path:
    """Persist a large artifact under the current task's output directory.

    Inside a ``compute(args)`` call the directory is derived from
    ``args.output_file``; outside one it falls back to the current
    working directory. ``bytes`` / ``str`` are written verbatim;
    anything else is pickled. Returns the path written.
    """
    base = _artifact_dir.get() or Path.cwd()
    base.mkdir(parents=True, exist_ok=True)
    path = base / name
    if isinstance(obj, bytes):
        path.write_bytes(obj)
    elif isinstance(obj, str):
        path.write_text(obj, encoding="utf-8")
    else:
        with open(path, "wb") as fh:
            pickle.dump(obj, fh)
    return path


def _make_compute(spec: RunSpec) -> Callable[[Any], None]:
    sig = inspect.signature(spec.func)
    accepted = {
        p.name
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }

    def compute(args: Any) -> None:
        ns: dict[str, Any] = dict(vars(args)) if hasattr(args, "__dict__") else dict(args)
        kwargs = {k: ns[k] for k in accepted if k in ns}
        output_file = ns.get("output_file")
        with _run_context(ns):
            result = spec.func(**kwargs)
        if isinstance(result, dict) and output_file:
            target = Path(output_file)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(result, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )

    return compute


@contextlib.contextmanager
def _run_context(ns: dict[str, Any]) -> Iterator[None]:
    """Bind the artifact directory and the parallelization slice for one run."""
    output_file = ns.get("output_file")
    art = Path(output_file).parent if output_file else None
    art_token = _artifact_dir.set(art)

    end = ns.get("end")
    spec = SliceSpec(
        start=int(ns.get("start") or 0),
        end=int(end) if end is not None else -1,
        halo=int(ns.get("halo") or 0),
    )
    slice_token = activate_slice(spec)
    try:
        yield
    finally:
        deactivate_slice(slice_token)
        _artifact_dir.reset(art_token)
