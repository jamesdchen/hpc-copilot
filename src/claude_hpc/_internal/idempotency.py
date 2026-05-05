"""Unified idempotency-key resolver.

The framework has five idempotency mechanisms with no shared shape:

1. Frontmatter ``idempotent: true|false`` (advisory; per-primitive).
2. Envelope ``idempotent`` (hardcoded per call site, ~47 callers).
3. ``run_id``-keyed dedup in :func:`claude_hpc.runner.submit_and_record`.
4. ``cmd_sha``-keyed :func:`claude_hpc.state.runs.find_run_by_cmd_sha`
   (wired to ``submit_and_record`` in item A5).
5. ``request_id``-keyed resubmit dedup in ``slash_commands/runner.py``.

This module collapses the *lookup* side of (3), (4), and (5) onto one
typed shape — a small ABC for the key plus a stateless
:func:`dedup_check` resolver that reads journal + sidecar without
mutating state.

Why a typed key rather than a dict
----------------------------------

The key carries the *kind* alongside the *value*: a function that
accepts ``Optional[str]`` for each of three keys ends up with
``if/elif`` ladders at each callsite. With :class:`IdempotencyKey`
subclasses, ``isinstance`` does the dispatch and the type checker
flags missed cases. The resolver returns a :class:`PriorResult` so the
caller knows *which* mechanism matched (journal vs sidecar vs request
log) for diagnostics.

This is the *resolver*, not the *writer*. Writes still happen in their
respective modules; this module is the read-side aggregator that
:func:`submit_and_record` and friends consult before issuing fresh
work.
"""

from __future__ import annotations

import abc
import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class IdempotencyKey(abc.ABC):
    """Abstract base for the three idempotency-key shapes in use.

    Subclasses are :class:`RunIdKey`, :class:`CmdShaKey`, and
    :class:`RequestIdKey`. Each carries the value as its sole field;
    the dispatcher (:func:`dedup_check`) uses ``isinstance`` to pick
    the right read path.
    """

    @abc.abstractmethod
    def origin(self) -> str:
        """Return a stable diagnostic label (``"run_id"`` etc.)."""


@dataclasses.dataclass(frozen=True)
class RunIdKey(IdempotencyKey):
    """Lookup by deterministic run_id (the canonical key).

    Used by :func:`submit_and_record` — a re-submit with the same
    arguments derives the same run_id and short-circuits if the
    journal already has a record.
    """

    run_id: str

    def origin(self) -> str:
        return "run_id"


@dataclasses.dataclass(frozen=True)
class CmdShaKey(IdempotencyKey):
    """Lookup by cmd_sha (the executor + args hash).

    Covers the case where the journal at
    ``~/.claude/hpc/<repo_hash>/runs/`` has been wiped but the per-run
    sidecar at ``<exp>/.hpc/runs/<run_id>.json`` still exists. Wired
    into :func:`submit_and_record` by item A5.
    """

    cmd_sha: str

    def origin(self) -> str:
        return "cmd_sha"


@dataclasses.dataclass(frozen=True)
class RequestIdKey(IdempotencyKey):
    """Lookup by user-supplied request_id (resubmit dedup).

    The slash-command surface accepts a free-form ``request_id`` so a
    user retrying a flaky resubmit gets the same outcome twice.
    """

    request_id: str

    def origin(self) -> str:
        return "request_id"


@dataclasses.dataclass(frozen=True)
class PriorResult:
    """Outcome of a successful :func:`dedup_check`.

    *origin*: one of ``"journal"``, ``"sidecar"``, ``"request_log"`` —
    tells the caller which read path matched, useful for debugging
    why a re-submission was treated as idempotent.
    *run_id*: the canonical run_id of the prior submission.
    *details*: an opaque dict the caller can decode based on origin
    (e.g. for sidecar matches the dict is the sidecar JSON).
    """

    origin: str
    run_id: str
    details: dict


def dedup_check(experiment_dir: Path, key: IdempotencyKey) -> PriorResult | None:
    """Resolve *key* against the journal + sidecar in priority order.

    Read-only — never mutates state. Returns ``None`` if no prior
    submission matches; otherwise returns a :class:`PriorResult`
    describing where the match was found.

    Priority:

    1. Journal (``~/.claude/hpc/<repo_hash>/runs/<run_id>.json``).
       Fast and authoritative when present.
    2. Per-experiment sidecar (``<exp>/.hpc/runs/<run_id>.json``).
       Survives journal wipes (machine swap, rm -rf), accessed via
       ``find_run_by_cmd_sha`` for :class:`CmdShaKey`.
    3. Request log (per-request_id resubmit dedup).

    Cancelled records are not treated as a dedup hit — the caller
    should be free to re-submit work that was explicitly cancelled.
    """
    # Local imports keep this module's import graph cheap and avoid
    # circular dependencies with the runner module which imports
    # session lazily.
    from claude_hpc._internal import session

    if isinstance(key, RunIdKey):
        record = session.load_run(experiment_dir, key.run_id)
        if record is None:
            return None
        if (record.status or "").lower() == "cancelled":
            return None
        return PriorResult(
            origin="journal",
            run_id=record.run_id,
            details={"status": record.status},
        )

    if isinstance(key, CmdShaKey):
        from claude_hpc.state.runs import find_run_by_cmd_sha, read_run_sidecar

        sidecar_path = find_run_by_cmd_sha(experiment_dir, key.cmd_sha)
        if sidecar_path is None:
            return None
        run_id = sidecar_path.stem
        try:
            data = read_run_sidecar(experiment_dir, run_id)
        except (FileNotFoundError, OSError):
            return None
        if (data.get("status") or "").lower() == "cancelled":
            return None
        return PriorResult(origin="sidecar", run_id=run_id, details=data)

    if isinstance(key, RequestIdKey):
        # Request-log dedup is owned by claude_hpc.runner's
        # _request_log helpers; the resolver hands off the lookup
        # rather than duplicate that file format here.
        try:
            from claude_hpc import runner as _runner_mod
        except ImportError:
            return None
        _lookup_request_id = getattr(_runner_mod, "_lookup_request_id", None)
        if _lookup_request_id is None:
            return None
        try:
            run_id = _lookup_request_id(experiment_dir, key.request_id)
        except Exception:  # noqa: BLE001 — defensive read
            return None
        if run_id is None:
            return None
        return PriorResult(
            origin="request_log",
            run_id=run_id,
            details={"request_id": key.request_id},
        )

    raise TypeError(f"unknown IdempotencyKey subclass: {type(key).__name__}")


__all__ = [
    "CmdShaKey",
    "IdempotencyKey",
    "PriorResult",
    "RequestIdKey",
    "RunIdKey",
    "dedup_check",
]
