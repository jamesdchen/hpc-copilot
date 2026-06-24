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
import re
import types
import typing
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
    "mpi_rank_world",
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
    mpi: bool = False


_artifact_dir: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "hpc_template_artifact_dir", default=None
)


# Launcher environment variables that carry an MPI rank's identity, in
# detection-preference order: OpenMPI (mpirun), MPICH / Intel MPI via Hydra
# (mpirun), then SLURM (srun). Each pair is (rank_var, size_var); a size_var
# of None means "no world-size companion" and world falls back to 1.
_MPI_RANK_ENV: tuple[tuple[str, str | None], ...] = (
    ("OMPI_COMM_WORLD_RANK", "OMPI_COMM_WORLD_SIZE"),
    ("PMI_RANK", "PMI_SIZE"),
    ("SLURM_PROCID", "SLURM_NTASKS"),
)


def mpi_rank_world() -> tuple[int, int]:
    """Return ``(rank, world_size)`` from the launcher env; ``(0, 1)`` if none.

    A non-MPI task runs its executor directly (no launcher), so none of the
    rank vars are set and this returns ``(0, 1)`` — making the rank-0 output
    gate in :func:`_make_compute` a no-op for ordinary single-process runs.
    Under ``srun`` / ``mpirun`` / ``aprun`` each rank's process inherits its
    own rank var, so the same ``compute`` body discovers its identity.
    """
    for rank_var, size_var in _MPI_RANK_ENV:
        raw = os.environ.get(rank_var)
        if raw is None or raw == "":
            continue
        try:
            rank = int(raw)
        except ValueError:
            continue
        world = 1
        if size_var:
            try:
                world = int(os.environ.get(size_var, "") or 1)
            except ValueError:
                world = 1
        return rank, world
    return 0, 1


def register_run(func: Any = None, *, gpu: bool = False, mpi: bool = False) -> Any:
    """Mark the experiment entry point. Works bare or as ``@register_run(gpu=True)``.

    At import time it records the run in a module-level ``_RUNS``
    registry and injects a ``compute(args)`` wrapper into the defining
    module — satisfying the hpc-agent executor contract without the
    researcher writing any CLI glue. One ``@register_run`` per module is
    the expected shape.

    ``mpi=True`` (#293) marks a multi-rank entry point: the function may
    declare ``rank`` / ``world_size`` parameters, which the injected
    ``compute`` fills from the launcher env (:func:`mpi_rank_world`) rather
    than from CLI flags, and only rank 0 writes the per-task output. A
    non-mpi run is unaffected — its ``compute`` never injects those params
    and its single process is always rank 0.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = RunSpec(func=fn, name=fn.__name__, gpu=gpu, mpi=mpi)
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


# Env-var kwargs (HPC_KW_*) reach a @register_run function as STRINGS. Coerce
# each to its parameter annotation before the call so a `samples: int` executor
# receives `1000000`, not `"1000000"` (which blows up at `range("1000000")`).
# Only the four scalar types env vars realistically carry get a coercer; an
# unannotated or non-scalar parameter is left as the raw string (unchanged
# behaviour). Stdlib-only — this file is inlined into the cluster executor.
_UNION_TYPE = getattr(types, "UnionType", None)  # `X | None` on 3.10+
_TRUE_STRINGS = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "f", "no", "n", "off", ""})


def _coerce_bool(value: str) -> bool:
    """Parse a string to bool by token — ``bool("false")`` is wrongly True."""
    token = value.strip().lower()
    if token in _TRUE_STRINGS:
        return True
    if token in _FALSE_STRINGS:
        return False
    raise ValueError(f"cannot interpret {value!r} as a boolean")


_SCALAR_BY_NAME: dict[str, Callable[[str], Any]] = {
    "bool": _coerce_bool,
    "int": int,
    "float": float,
    "str": str,
}


def _strip_optional_str(annotation: str) -> str:
    """Reduce a string annotation to its inner scalar name.

    Handles the `from __future__ import annotations` fallback path where
    ``get_type_hints`` couldn't resolve and the raw annotation is text:
    ``Optional[int]`` / ``int | None`` / ``builtins.int`` → ``int``.
    """
    text = annotation.strip()
    for pattern in (
        r"Optional\[\s*([\w.]+)\s*\]",
        r"([\w.]+)\s*\|\s*None",
        r"None\s*\|\s*([\w.]+)",
    ):
        match = re.fullmatch(pattern, text)
        if match:
            text = match.group(1)
            break
    return text.rsplit(".", 1)[-1]


def _unwrap_optional(annotation: Any) -> Any:
    """``Optional[T]`` / ``T | None`` → ``T`` (single non-None member only)."""
    origin = typing.get_origin(annotation)
    union_origins = (typing.Union, _UNION_TYPE) if _UNION_TYPE is not None else (typing.Union,)
    if origin in union_origins:
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _scalar_coercer(annotation: Any) -> Callable[[str], Any] | None:
    """Return a ``str -> value`` coercer for a scalar annotation, else None."""
    annotation = _unwrap_optional(annotation)
    if annotation is bool:
        return _coerce_bool
    if annotation is int:
        return int
    if annotation is float:
        return float
    if annotation is str:
        return str
    if isinstance(annotation, str):
        return _SCALAR_BY_NAME.get(_strip_optional_str(annotation))
    return None


def _make_compute(spec: RunSpec) -> Callable[[Any], None]:
    sig = inspect.signature(spec.func)
    accepted = {
        p.name
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    # Resolve annotations once (handles `from __future__ import annotations`);
    # fall back to the raw signature annotation if resolution fails.
    try:
        hints = typing.get_type_hints(spec.func)
    except Exception:
        hints = {}
    coercers: dict[str, Callable[[str], Any]] = {}
    for name in accepted:
        annotation = hints.get(name, sig.parameters[name].annotation)
        coercer = _scalar_coercer(annotation)
        if coercer is not None:
            coercers[name] = coercer

    def compute(args: Any) -> None:
        ns: dict[str, Any] = dict(vars(args)) if hasattr(args, "__dict__") else dict(args)
        # #294: surface the framework's checkpoint resume point to executors that
        # opt in by declaring a ``resume_from`` / ``checkpoint_dir`` parameter.
        # The dispatcher sets HPC_RESUME_FROM (latest checkpoint, on `resubmit
        # --from-checkpoint`) and HPC_CHECKPOINT_DIR (stable per-task dir). Absent
        # → None (fresh start). ``setdefault`` so an explicit arg still wins, and
        # the ``accepted`` filter drops these for functions that don't take them
        # (so existing executors are unaffected).
        ns.setdefault("resume_from", os.environ.get("HPC_RESUME_FROM") or None)
        ns.setdefault("checkpoint_dir", os.environ.get("HPC_CHECKPOINT_DIR") or None)
        # #293: a multi-rank run learns its identity from the launcher env. Inject
        # rank / world_size for mpi runs only — the ``accepted`` filter then hands
        # them to a func that declares them. ``rank`` is also the output gate
        # below; for a non-mpi run it stays 0 so the gate is a no-op.
        rank, world_size = mpi_rank_world()
        if spec.mpi:
            ns.setdefault("rank", rank)
            ns.setdefault("world_size", world_size)
        kwargs: dict[str, Any] = {}
        for k in accepted:
            if k not in ns:
                continue
            value = ns[k]
            coercer = coercers.get(k)
            # Only env-var-sourced (string) values need coercion; framework-
            # injected typed values (rank:int, resume_from:None) pass through.
            if coercer is not None and isinstance(value, str):
                try:
                    value = coercer(value)
                except (ValueError, TypeError) as exc:
                    raise ValueError(
                        f"could not coerce {k}={ns[k]!r} (from HPC_KW_{k.upper()}) "
                        f"to {spec.func.__name__}'s annotated parameter type: {exc}"
                    ) from exc
            kwargs[k] = value
        output_file = ns.get("output_file")
        with _run_context(ns):
            result = spec.func(**kwargs)
        # Only rank 0 writes the per-task output. Non-mpi runs are single-process
        # (rank 0), so this is unchanged for them; an MPI job's N ranks would
        # otherwise race to write the same metrics.json (the reducer expects one).
        if isinstance(result, dict) and output_file and rank == 0:
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
