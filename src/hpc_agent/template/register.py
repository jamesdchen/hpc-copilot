"""``register_run`` — the one decorator a notebook experiment applies.

``@register_run`` does three things at import time:

1. **Export marker.** Its presence flags a function as the experiment
   entry point — :func:`hpc_agent.template.discover_runs` finds it by an
   AST walk.
2. **Flag synthesis.** ``inspect.signature(run)`` is mapped to a
   :class:`~hpc_agent.executor_cli.Flag` list (see
   :mod:`hpc_agent.template.signature`).
3. **Runtime registration.** It records the run in a module-level
   ``_RUNS`` registry and injects a ``compute(args)`` wrapper into the
   defining module — satisfying the hpc-agent executor contract
   (``def compute(args) -> None``) without the researcher writing any
   CLI glue.

Usage — bare or parameterised::

    @register_run
    def run(alpha: float = 1.0) -> dict:
        ...

    @register_run(gpu=True)
    def run(epochs: int = 10) -> dict:
        ...

The injected ``compute`` forwards only the parsed args that match
``run``'s signature, runs it inside the slice + artifact context, and —
when ``run`` returns a ``dict`` — JSON-dumps it to ``args.output_file``
(results-by-return).

Stdlib-only.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import pickle
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpc_agent.executor_cli import Flag
from hpc_agent.template import series
from hpc_agent.template.series import SliceSpec
from hpc_agent.template.signature import flags_for_run

__all__ = ["register_run", "RunSpec", "save_artifact"]


@dataclass(frozen=True)
class RunSpec:
    """Metadata recorded for one ``@register_run`` function."""

    func: Callable[..., Any]
    name: str
    gpu: bool
    flags: tuple[Flag, ...]


_artifact_dir: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "hpc_template_artifact_dir", default=None
)


def register_run(func: Any = None, *, gpu: bool = False) -> Any:
    """Decorator — see the module docstring. Works bare or as ``@register_run(...)``."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = RunSpec(
            func=fn,
            name=fn.__name__,
            gpu=gpu,
            flags=tuple(flags_for_run(fn, gpu=gpu)),
        )
        # Attach metadata to the function and register it in the
        # defining module's namespace. ``fn.__globals__`` IS that
        # module's global dict — robust without frame inspection.
        fn.__dict__["_hpc_run"] = spec
        module_ns = fn.__globals__
        runs: dict[str, RunSpec] = module_ns.setdefault("_RUNS", {})
        runs[fn.__name__] = spec
        # Inject the executor-contract entry point. One @register_run
        # per module is the expected shape; a second registration
        # overwrites ``compute`` (the dispatcher selects the file, and
        # one file = one experiment).
        module_ns["compute"] = _make_compute(spec)
        return fn

    if func is not None and callable(func):
        return decorate(func)
    return decorate


def save_artifact(name: str, obj: Any) -> Path:
    """Persist a large artifact under the current task's output directory.

    Inside a ``compute(args)`` call the directory is derived from
    ``args.output_file``; outside one (a local smoke test) it falls back
    to the current working directory. ``bytes`` and ``str`` are written
    verbatim; anything else is pickled. Returns the path written.
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
    import inspect

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
    slice_token = series.activate_slice(spec)
    try:
        yield
    finally:
        series.deactivate_slice(slice_token)
        _artifact_dir.reset(art_token)
