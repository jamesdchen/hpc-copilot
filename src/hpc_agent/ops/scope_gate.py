"""Scope gate — refuse a reduction over a run whose evidence-scope is locked.

The precondition on the one reduction seam (rigor-primitives T3): before
``aggregate-flow`` does any SSH / combine / reduce work on a run, assert that
none of the run's caller-attached scopes is currently locked. A *scope* is an
opaque caller-owned tag the framework never interprets (see
:mod:`hpc_agent.state.scopes`); a *lock* is deliberate human state, and
reducing against a locked scope would spend a look the human meant to reserve.

Block-gate family (the sibling of :mod:`hpc_agent.ops.block_gate`): a pure
LOCAL read — the run sidecar's ``scopes`` and each scope's decision journal, no
SSH. Fail-safe by construction: a missing sidecar or a scope-less run PASSES,
so the gate can never false-trip on a run that carries no scopes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent.state.runs import read_run_sidecar
from hpc_agent.state.scopes import is_scope_locked

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["assert_scopes_unlocked"]

# The scope decision journal's lock/unlock sentinels (mirrors the private
# constants in :mod:`hpc_agent.state.scopes`). Used ONLY to fish the locking
# record's ``ts`` out for the loud message — the lock VERDICT itself always
# routes through the canonical ``is_scope_locked`` predicate, never this set.
_SCOPE_ACTIONS = frozenset({"lock", "unlock"})


def _locked_at(experiment_dir: Path, tag: str) -> str | None:
    """Newest lock/unlock decision's ``ts`` for *tag* (best-effort, for the message).

    Called only after :func:`is_scope_locked` returned ``True``, so the newest
    lock/unlock record is a ``lock`` and its ``ts`` is the lock timestamp.
    ``None`` when no such record is found (defensive — the message just omits
    the timestamp).
    """
    from hpc_agent.state.decision_journal import read_decisions

    for record in reversed(read_decisions(experiment_dir, "scope", tag)):
        resolved = record.get("resolved")
        action = resolved.get("scope_action") if isinstance(resolved, dict) else None
        if action in _SCOPE_ACTIONS:
            ts = record.get("ts")
            return str(ts) if ts else None
    return None


def assert_scopes_unlocked(experiment_dir: Path, run_id: str) -> None:
    """Refuse if any scope on *run_id*'s sidecar is currently locked.

    Reads the run sidecar's ``scopes`` list; for each tag,
    :func:`hpc_agent.state.scopes.is_scope_locked` decides. The FIRST locked
    tag raises :class:`errors.ScopeLocked` naming the tag, the lock timestamp,
    and the single human-journaled-unlock exit.

    Never a false trip: a missing sidecar or an absent / empty ``scopes`` key
    passes silently. Pure local reads — no SSH.
    """
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except FileNotFoundError:
        return  # no sidecar → no scopes to gate; never a false trip
    scopes = sidecar.get("scopes")
    if not scopes:
        return  # scope-less run (absent or empty) → PASS
    for tag in scopes:
        if is_scope_locked(experiment_dir, str(tag)):
            raise errors.ScopeLocked.for_tag(
                str(tag), locked_at=_locked_at(experiment_dir, str(tag))
            )
