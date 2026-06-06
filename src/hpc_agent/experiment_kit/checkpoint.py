"""Checkpoint-aware recovery helpers for long executors (#294 PR1).

A 30-second array task can be rerun on preemption — ``reconcile`` + ``resubmit``
already handle that. A long, many-iteration solve cannot: rerunning from scratch
throws away hours. These helpers are the checkpoint primitives an executor uses
to *resume* instead of restart.

Stdlib-only (like the rest of :mod:`hpc_agent.experiment_kit`) so they are safe
to import at dispatch time on a cluster that has hpc-agent on its PATH. The
default backend is :mod:`pickle`; richer backends (torch / safetensors / HDF5)
are a deliberate follow-up.

Convention
----------
Checkpoints live as ``checkpoint-<iteration>.pkl`` under the directory the
dispatcher exports as ``HPC_CHECKPOINT_DIR`` — the STABLE per-task dir (the
final result dir's ``_checkpoints/``), NOT the WIP dir that is renamed to
``_wip_*_failed_*`` / recreated on retry. That stability is what lets a killed
run's checkpoints survive to a ``resubmit --from-checkpoint`` (#294). Outside a
dispatched task (a bare local run) it falls back to
``$HPC_RESULT_DIR/_checkpoints`` then ``./_checkpoints``.

An executor loop:

    from hpc_agent.experiment_kit.checkpoint import (
        read_latest_checkpoint,
        should_checkpoint,
        write_checkpoint,
    )

    state, start_iter = read_latest_checkpoint()  # (None, 0) on a fresh run
    for it in range(start_iter, n_iters):
        state = step(state)
        if should_checkpoint(strategy="walltime_margin", margin_min=10):
            write_checkpoint(state, iteration=it)

Existing executors that ignore checkpointing are unaffected — nothing here runs
unless an executor opts in.

Scope (PR1)
-----------
This is the helper layer. The framework auto-injecting ``args.resume_from`` /
``args.checkpoint_dir`` (PR1's executor-convention half), classifying the kill
reason (PR2), and ``resubmit --from-checkpoint`` (PR3) build on these. Until the
template exports a walltime deadline (``HPC_WALLTIME_END_EPOCH``), the
``walltime_margin`` strategy degrades to a no-op unless the caller passes an
explicit ``deadline_epoch`` — the ``interval`` strategy works today with no
framework plumbing.
"""

from __future__ import annotations

import contextlib
import os
import pickle
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = [
    "checkpoint_dir",
    "write_checkpoint",
    "read_checkpoint",
    "latest_checkpoint",
    "read_latest_checkpoint",
    "checkpoint_iteration",
    "should_checkpoint",
    "run_iterations",
]

# Per-task checkpoint subdir + filename shape. Kept deliberately simple so the
# files sort by iteration and a human inspecting a result dir can read them.
_CHECKPOINT_SUBDIR = "_checkpoints"
_CHECKPOINT_RE = re.compile(r"^checkpoint-(\d+)\.pkl$")

# Env var carrying the unix epoch (seconds) at which the job's walltime expires,
# read by the ``walltime_margin`` strategy. The shared array preamble
# (hpc_preamble.sh) stamps it as job-start + HPC_WALLTIME_SEC when the submit set
# a walltime; absent it (no walltime set, or a caller-supplied ``deadline_epoch``)
# ``walltime_margin`` degrades to a no-op rather than guessing.
_WALLTIME_END_ENV = "HPC_WALLTIME_END_EPOCH"


def _resolve_result_dir(result_dir: str | os.PathLike[str] | None) -> Path:
    """The per-task result dir checkpoints hang off.

    Priority: an explicit *result_dir*, then ``HPC_RESULT_DIR`` /
    ``RESULT_DIR`` (the dispatcher's per-task WIP dir), then the CWD — so the
    helpers work both inside a dispatched task and in a bare local run.
    """
    if result_dir is not None:
        return Path(result_dir)
    env = os.environ.get("HPC_RESULT_DIR") or os.environ.get("RESULT_DIR")
    return Path(env) if env else Path.cwd()


def checkpoint_dir(result_dir: str | os.PathLike[str] | None = None) -> Path:
    """The directory checkpoints live in (not created).

    With an explicit *result_dir*, it's ``<result_dir>/_checkpoints``. Otherwise
    the dispatcher-provided ``HPC_CHECKPOINT_DIR`` wins — that's the STABLE
    per-task dir (the final result dir, not the WIP dir that's renamed/cleaned on
    retry), so a killed run's checkpoints survive to a ``resubmit
    --from-checkpoint`` (#294). Falls back to ``HPC_RESULT_DIR/_checkpoints``
    then CWD when no stable dir was provided.
    """
    if result_dir is not None:
        return Path(result_dir) / _CHECKPOINT_SUBDIR
    env_ckpt = os.environ.get("HPC_CHECKPOINT_DIR")
    if env_ckpt:
        return Path(env_ckpt)
    return _resolve_result_dir(None) / _CHECKPOINT_SUBDIR


def checkpoint_iteration(path: str | os.PathLike[str]) -> int | None:
    """The iteration encoded in a ``checkpoint-<n>.pkl`` filename, or None."""
    m = _CHECKPOINT_RE.match(Path(path).name)
    return int(m.group(1)) if m else None


def write_checkpoint(
    state: Any, *, iteration: int, result_dir: str | os.PathLike[str] | None = None
) -> Path:
    """Atomically pickle *state* to ``<result_dir>/_checkpoints/checkpoint-<iteration>.pkl``.

    Writes to a sibling tempfile then ``os.replace``\\ s into place, so a reader
    (or a resume after a mid-write kill) never sees a half-written checkpoint —
    the same atomic-promote discipline the dispatcher uses for outputs. Returns
    the path written.
    """
    d = checkpoint_dir(result_dir)
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"checkpoint-{int(iteration)}.pkl"
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(d))
    try:
        with os.fdopen(fd, "wb") as fh:
            pickle.dump(state, fh, protocol=pickle.HIGHEST_PROTOCOL)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return target


def read_checkpoint(path: str | os.PathLike[str]) -> Any:
    """Load and return the state pickled at *path*."""
    with open(path, "rb") as fh:
        return pickle.load(fh)


def _checkpoints_descending(
    result_dir: str | os.PathLike[str] | None,
) -> list[tuple[int, Path]]:
    """``(iteration, path)`` for every non-empty checkpoint, highest first."""
    d = checkpoint_dir(result_dir)
    if not d.is_dir():
        return []
    items: list[tuple[int, Path]] = []
    for p in d.iterdir():
        m = _CHECKPOINT_RE.match(p.name)
        if not m:
            continue
        try:
            if p.is_file() and p.stat().st_size > 0:
                items.append((int(m.group(1)), p))
        except OSError:
            continue
    items.sort(reverse=True)
    return items


def latest_checkpoint(result_dir: str | os.PathLike[str] | None = None) -> Path | None:
    """The highest-iteration non-empty checkpoint path, or None if none exist."""
    items = _checkpoints_descending(result_dir)
    return items[0][1] if items else None


def read_latest_checkpoint(
    result_dir: str | os.PathLike[str] | None = None,
) -> tuple[Any, int]:
    """Load the latest valid checkpoint; return ``(state, next_iteration)``.

    ``next_iteration`` is the loop index to RESUME at — the latest checkpoint's
    iteration + 1 — so ``for it in range(next_iteration, N)`` skips the work
    already done. Returns ``(None, 0)`` on a fresh run (no checkpoints).

    Resilient to an unreadable latest checkpoint (e.g. a pickle written by an
    incompatible interpreter): it walks checkpoints newest-to-oldest and returns
    the first that loads, so one bad file doesn't force a from-scratch restart.
    """
    for it, p in _checkpoints_descending(result_dir):
        try:
            return read_checkpoint(p), it + 1
        except (pickle.UnpicklingError, EOFError, OSError, AttributeError, ImportError):
            continue
    return None, 0


# Process-local arming time for the ``interval`` strategy. ``should_checkpoint``
# carries no caller state, so the cadence timer lives here. One executor process
# runs one task, so a module global is the right scope; tests reset it.
_interval_armed_mono: float | None = None


def _reset_should_checkpoint_state() -> None:
    """Test hook: clear the process-local ``interval`` arming time."""
    global _interval_armed_mono
    _interval_armed_mono = None


def _walltime_deadline_epoch() -> float | None:
    raw = os.environ.get(_WALLTIME_END_ENV, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def should_checkpoint(
    *,
    strategy: str = "walltime_margin",
    margin_min: float = 10.0,
    interval_min: float = 30.0,
    deadline_epoch: float | None = None,
    _now_mono: float | None = None,
    _now_epoch: float | None = None,
) -> bool:
    """Whether the executor should write a checkpoint now.

    Two strategies:

    * ``"walltime_margin"`` — True once the remaining walltime drops to
      *margin_min* minutes. The deadline is *deadline_epoch* if given, else the
      framework-exported ``HPC_WALLTIME_END_EPOCH``. When neither is available
      the deadline is unknown and this returns False (it never guesses).
    * ``"interval"`` — True once *interval_min* minutes have elapsed since the
      previous True (the first call arms the timer and returns False, so a loop
      doesn't checkpoint on iteration 0). Process-local; works with no framework
      plumbing.

    The ``_now_*`` parameters are test seams.
    """
    if strategy == "walltime_margin":
        deadline = deadline_epoch if deadline_epoch is not None else _walltime_deadline_epoch()
        if deadline is None:
            return False
        now = time.time() if _now_epoch is None else _now_epoch
        return (deadline - now) <= margin_min * 60.0

    if strategy == "interval":
        global _interval_armed_mono
        now = time.monotonic() if _now_mono is None else _now_mono
        if _interval_armed_mono is None:
            _interval_armed_mono = now
            return False
        if now - _interval_armed_mono >= interval_min * 60.0:
            _interval_armed_mono = now
            return True
        return False

    raise ValueError(
        f"unknown checkpoint strategy: {strategy!r} (expected 'walltime_margin' or 'interval')"
    )


def run_iterations(
    step: Callable[[Any, int], Any],
    *,
    init: Any,
    n: int,
    result_dir: str | os.PathLike[str] | None = None,
    checkpoint_every: int | None = None,
    strategy: str = "interval",
    interval_min: float = 30.0,
    margin_min: float = 10.0,
) -> Any:
    """Drive an iterative computation with framework-owned checkpoint + resume.

    The ``register_run`` principle extended to durability: you write the
    per-step transition and the starting state; the framework owns the loop, the
    checkpoint *writes*, AND the *resume* — so an executor becomes
    preemption-safe with NO hand-rolled checkpoint plumbing. This is the
    convention an agent can target (or generate from a plain loop), so that
    eventually nothing checkpoint-specific is hand-written at all.

    Parameters
    ----------
    step:
        ``step(state, iteration) -> new_state`` — one iteration's transition.
        Should be deterministic in *state* so a resumed run reproduces the
        un-checkpointed tail exactly.
    init:
        Starting state for a FRESH run (used only when no checkpoint exists).
        A value, or a zero-arg callable (called lazily, so an expensive init is
        skipped entirely on resume).
    n:
        Total iterations; the loop runs ``range(resume_point, n)``.
    result_dir:
        Passed through to the checkpoint helpers (defaults to the dispatcher's
        ``HPC_CHECKPOINT_DIR`` / ``HPC_RESULT_DIR``).
    checkpoint_every:
        When truthy, checkpoint every N completed iterations (deterministic
        cadence). When None/0 (default), cadence is driven by
        :func:`should_checkpoint` with *strategy* — so a long solve checkpoints
        on the time interval / walltime margin rather than every step.

    Returns the final state. On resume (a checkpoint exists) it loads the latest
    and continues from the next iteration, skipping already-done work — the
    executor side of ``resubmit --from-checkpoint``.
    """
    state, resume_point = read_latest_checkpoint(result_dir=result_dir)
    if state is None:
        state = init() if callable(init) else init
        resume_point = 0

    total = int(n)
    start = int(resume_point)
    last_checkpointed = -1
    for i in range(start, total):
        state = step(state, i)
        if checkpoint_every:
            due = (i + 1) % int(checkpoint_every) == 0
        else:
            due = should_checkpoint(
                strategy=strategy, interval_min=interval_min, margin_min=margin_min
            )
        if due:
            write_checkpoint(state, iteration=i, result_dir=result_dir)
            last_checkpointed = i

    # Always persist the FINAL state when the loop ran — so a resume landing
    # after the last iteration (e.g. killed during output promotion) has nothing
    # to redo, and the last iteration's work is never lost to a cadence that
    # happened not to fire on it.
    if total > start and last_checkpointed != total - 1:
        write_checkpoint(state, iteration=total - 1, result_dir=result_dir)
    return state
