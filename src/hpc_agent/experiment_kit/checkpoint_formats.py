"""Format-aware checkpoint discovery + verification — the seam over backends.

:mod:`hpc_agent.experiment_kit.checkpoint` is the pickle backend: helpers an
executor's own loop calls. The solver adapters introduced a second on-disk
format (PETSc binary Vec dumps), which the framework's *verification and
resume* surfaces must also understand — the checkpoint canary verifier asserts
"a restorable checkpoint survived the kill" regardless of who wrote it, and
``resubmit --from-checkpoint`` hands the executor whatever resume point
exists.

This module is that seam. It owns the format-agnostic contract — a
:class:`CheckpointFormat` is "how to find the newest artifact" plus "how to
verify one" — and assembles the known formats:

* ``pickle`` — delegates to the :mod:`checkpoint` helpers; verification is a
  real deserialization (``level: "loadable"``), with the exact semantics the
  canary verifier's remote snippet had when it was pickle-only (newest file
  reported; loading walks newest→oldest, so one corrupt file does not fail
  the verdict).
* ``petsc_binary`` — delegates to
  :mod:`~hpc_agent.experiment_kit.solver_adapters.petsc`; verification is
  structural (``level: "structural"`` — the Vec class-id/block walk), because
  loading requires petsc4py, which the verifying environment may not have.

Boundary note: this assembly list is the ONE core location that names
adapter formats. The contract above is library-agnostic; everything
PETSc-specific stays in the adapter module. A new format = one adapter
module + one entry here.

Stdlib-only, like everything under :mod:`hpc_agent.experiment_kit` — the
canary verifier imports this ON THE CLUSTER (in the run's own env) via its
remote probe snippet.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hpc_agent.experiment_kit import checkpoint as _ck
from hpc_agent.experiment_kit.solver_adapters import petsc as _petsc

__all__ = [
    "CheckpointFormat",
    "checkpoint_formats",
    "describe_latest_checkpoint",
]


class CheckpointFormat:
    """One on-disk checkpoint format: discovery + verification.

    ``latest`` returns ``(path, iteration | None)`` for the newest artifact
    under a result dir's checkpoint location, or None when the format has no
    artifact there. ``verify`` returns the verdict dict for one artifact:
    ``{"status": "ok" | "unloadable", "level": "loadable" | "structural",
    ...}`` (``next_iteration`` and ``detail`` are per-format extras).
    """

    __slots__ = ("name", "latest", "verify")

    def __init__(
        self,
        *,
        name: str,
        latest: Callable[[str | os.PathLike[str] | None], tuple[Path, int | None] | None],
        verify: Callable[[Path], dict[str, Any]],
    ) -> None:
        self.name = name
        self.latest = latest
        self.verify = verify


def _pickle_latest(
    result_dir: str | os.PathLike[str] | None,
) -> tuple[Path, int | None] | None:
    p = _ck.latest_checkpoint(result_dir)
    if p is None:
        return None
    return p, _ck.checkpoint_iteration(p)


def _pickle_verify(path: Path) -> dict[str, Any]:
    # Preserved semantics from the canary verifier's pickle-only snippet:
    # ``read_latest_checkpoint`` walks newest→oldest and returns the first
    # checkpoint that LOADS, so next_iteration>0 means "a loadable checkpoint
    # exists" even if the very newest file is corrupt — distinct from a
    # legitimately pickled ``None`` state, which still yields
    # next_iteration>0. The walk needs the directory, not the file.
    _, nxt = _ck.read_latest_checkpoint(_result_dir_of(path))
    if int(nxt) <= 0:
        return {
            "status": "unloadable",
            "level": "loadable",
            "detail": "present but no checkpoint deserializes (wrong/non-portable format)",
        }
    return {"status": "ok", "level": "loadable", "next_iteration": int(nxt)}


def _result_dir_of(path: Path) -> Path:
    """The result dir whose ``_checkpoints/`` contains *path*.

    The pickle helpers take a *result_dir* and append ``_checkpoints``
    themselves, so hand them the grandparent. Falls back to the parent when
    the artifact does not sit under a ``_checkpoints/`` dir (bare local runs).
    """
    parent = path.parent
    return parent.parent if parent.name == "_checkpoints" else parent


def _petsc_verify(path: Path) -> dict[str, Any]:
    return _petsc.verify_petsc_binary(path)


def checkpoint_formats() -> tuple[CheckpointFormat, ...]:
    """The known formats, in tie-break preference order (pickle first)."""
    return (
        CheckpointFormat(name="pickle", latest=_pickle_latest, verify=_pickle_verify),
        CheckpointFormat(
            name="petsc_binary", latest=_petsc.latest_petsc_artifact, verify=_petsc_verify
        ),
    )


def describe_latest_checkpoint(
    result_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Find and verify the newest checkpoint across all known formats.

    The verdict the canary verifier's remote probe emits:

    * ``{"status": "missing"}`` — no format has any artifact;
    * ``{"status": "ok", "path", "format", "level", ...}`` — a restorable
      checkpoint exists (``next_iteration`` present for the pickle format);
    * ``{"status": "unloadable", "path", "format", "level", "detail"}`` —
      artifacts exist but none verifies.

    When several formats have artifacts (rare — different instrumentation
    modes), the newest by mtime wins, ties going to the format order of
    :func:`checkpoint_formats`. JSON-serializable by construction (paths are
    strings) so the remote probe can ``json.dumps`` it verbatim.
    """
    candidates: list[tuple[float, int, CheckpointFormat, Path]] = []
    for index, fmt in enumerate(checkpoint_formats()):
        found = fmt.latest(result_dir)
        if found is None:
            continue
        path, _ = found
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        candidates.append((mtime, -index, fmt, path))
    if not candidates:
        return {"status": "missing"}

    _, _, fmt, path = max(candidates)
    verdict = fmt.verify(path)
    out: dict[str, Any] = {"path": str(path), "format": fmt.name}
    out.update(verdict)
    return out
