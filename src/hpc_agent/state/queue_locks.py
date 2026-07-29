"""THE durable per-campaign dispatch lock — E4's sidecar-between-slots rule.

One advisory flock per campaign id,

    <experiment_dir>/.hpc/queue/locks/<campaign_id>.lock

held across the whole ``resolve → sidecar write → start the lifecycle`` window
of ONE slot (``docs/plans/run-queue-placement-2026-07-28.md`` §10.S2.4).

Why a lock at all
-----------------
``ops/campaign_refill.py``'s module docstring (E4) states the rule this module
mechanizes: refill slots are strictly sequential, and each one's sidecar must
land before the next one resolves. The reason is that ``resolve-submit-inputs``
consumes the optuna scaffold's sidecar-derived ``_submitted_count`` — two slots
that resolve at the same count read the SAME cached proposal, compute the same
``cmd_sha``, and therefore the same ``run_id``. In-process that is harmless
(the second stops at ``prior_run_found``); across processes it is a duplicated
trial or a pair of dispatchers fighting over one run id.

Until Phase 2 that rule was an in-process ``for`` loop convention — true only
because exactly one refill loop ran at a time. Phase 2 gives the ledger a
second submitter path (``queue-dispatch``, which any session may fire), so the
convention has to become a fact two unrelated processes can both observe. This
is that fact.

Scope: the CRITICAL SECTION, not the file
-----------------------------------------
The lock must span the whole window, because the sidecar write lands near the
END of ``resolve-submit-inputs``, not at its start. A holder that released
after resolving and re-acquired to start would leave exactly the gap the rule
exists to close.

It is emphatically NOT the ledger's lock: ``infra.io.append_jsonl_line``
already takes its own flock (with the in-lock replay-dedup probe) around every
intake append, so arrival and transition records are serialized whether or not
anyone holds this. And it is not the claim either — the claim is the shipped
detached lease keyed on the COMPUTED ``run_id``
(``_kernel/lifecycle/detached.py::_guard_single_lease``, §10.S2). Three
different jobs, three different locks, deliberately: this one orders trial
CHOICE, the lease arbitrates run OWNERSHIP, the ledger flock orders WRITES.

Ordering rule: this lock is the OUTERMOST of the three. A holder goes on to
append to the ledger and to spawn under the detached lease, both of which take
their own locks on other paths; taking them in the other order across two
processes would deadlock.

Re-entrant within one process, exclusive across processes
---------------------------------------------------------
:func:`hpc_agent.infra.io.advisory_flock` is deliberately non-reentrant — a
second acquire on the same path contends at the OS lock even from the same
process. That is right for its callers and wrong here: Phase 2 composes a
refill slot that holds this lock around a ``queue-dispatch`` call that must
also hold it, so a naive re-acquire would self-deadlock into a timeout and
report a phantom peer.

So this module tracks depth per ``(thread, lock path)`` in-process: the
outermost frame of a thread takes the real flock, inner frames of that SAME
thread pass straight through, and the flock is released when that outermost
frame exits. The context manager yields ``True`` when THIS frame acquired the
OS lock and ``False`` when it inherited it — both mean the caller holds it, and
the value exists so a diagnostic can say which.

The key includes the THREAD identity, and that is load-bearing rather than
defensive. Re-entrancy is a property of one call stack: the nested frame D5
composes is literally inside its holder's frame, on its thread. A second thread
is a different stack that has proven nothing, so it must contend at the OS lock
and be refused at the timeout — E4 forbids parallel slots outright ("do NOT
parallelize slots"), and two threads inside this window would resolve at the
same ``_submitted_count``, cache the same optuna proposal and compute the same
``run_id``: precisely the collision the lock exists to make impossible. Keying
on the path ALONE would wave that second thread straight through holding
nothing, and would report it as "nested" so the violation looked healthy.

Depth is also decremented symmetrically rather than cleared by the frame that
happens to exit first: with a per-path key a nested frame that outlived its
holder raised ``KeyError`` out of its own ``finally``. Per-thread keys make that
interleaving unreachable, and the symmetric decrement means it could not throw
even if it were.

Why ``state/`` and not ``ops/queue/``
-------------------------------------
Same argument as ``state/queue_occupancy.py``: ``hpc_agent.state.*`` is one of
the two roots any subject may import (``scripts/lint_subject_imports.py``), and
this lock must be reachable from ``ops/queue/`` (the dispatcher) AND from
``ops/campaign_refill.py`` (the refill slot loop). Parking it under either
subject would make the other's import a cross-subject violation — the exact
pressure that produces a second, subtly different lock path.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from hpc_agent import errors

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "DISPATCH_LOCK_TIMEOUT_SEC",
    "campaign_dispatch_lock",
    "dispatch_lock_path",
]

#: Bounded acquire, in seconds. Bounded rather than infinite for the reason
#: ``_spawn_detached`` is (run-#12 finding 16: a successor sat 15 minutes at 0%
#: CPU behind a wedged holder, invisible to everyone). Generous rather than
#: tight because the window it guards contains a real ``resolve-submit-inputs``
#: — source-audit and pack-receipt gates, a ``tasks.py`` re-exec, a sidecar
#: write — so a healthy peer legitimately holds it for tens of seconds, and a
#: timeout must mean "something is wrong", not "somebody was busy".
DISPATCH_LOCK_TIMEOUT_SEC = 300.0

#: ``(thread ident, lock path)`` -> how many nested frames of THAT THREAD hold
#: it. See the module docstring: the OS lock is per-process, so re-entrancy has
#: to be tracked in-process — but only a frame on the SAME call stack has
#: inherited anything, so the thread is half the key. Keying on the path alone
#: would classify a concurrent thread as "nested" and wave it through.
_HELD_DEPTH: dict[tuple[int, str], int] = {}
_DEPTH_GUARD = threading.Lock()


def dispatch_lock_path(experiment_dir: Path, campaign_id: str) -> Path:
    """``.hpc/queue/locks/<campaign_id>.lock`` — computed, never created here.

    Resolved off ``experiment_dir`` the same way the intake ledger's paths are,
    so two processes launched from different cwds lock the SAME file (a lock on
    two paths is not a lock). The parent directory is materialized by
    :func:`hpc_agent.infra.io.advisory_flock` at acquire time, so this accessor
    stays safe to call from a read-only path (F46).

    *campaign_id* is used verbatim as the filename stem. It is a composed
    ``<base>_<clusterkey>`` id whose wire alias is ``^[A-Za-z0-9._\\-]+$``, so
    it carries no separator — but the charset is re-checked here rather than
    assumed, because this value reaches the filesystem and a caller that built
    a cid by hand must fail loudly instead of locking ``../something``.
    """
    cid = (campaign_id or "").strip()
    if not cid:
        raise ValueError("campaign_id must be a non-empty string to key a dispatch lock")
    if cid in {".", ".."} or any(ch in cid for ch in "/\\\0"):
        raise ValueError(
            f"campaign_id {campaign_id!r} is not usable as a lock filename — a "
            "campaign id is composed as '<base>_<clusterkey>' and carries no "
            "path separator."
        )
    return Path(experiment_dir).resolve() / ".hpc" / "queue" / "locks" / f"{cid}.lock"


def _release_depth(key: tuple[int, str]) -> None:
    """Drop one nesting level for *key*, deleting the entry at zero.

    SYMMETRIC with the bump, and never a bare ``del``/``pop`` by whichever frame
    exits: the bookkeeping runs in a ``finally``, so an unbalanced release would
    throw a bookkeeping error out of a context manager on its way out of an
    unrelated failure — the one place an exception is hardest to read.
    """
    with _DEPTH_GUARD:
        remaining = _HELD_DEPTH.get(key, 0) - 1
        if remaining > 0:
            _HELD_DEPTH[key] = remaining
        else:
            _HELD_DEPTH.pop(key, None)


@contextlib.contextmanager
def campaign_dispatch_lock(
    experiment_dir: Path,
    campaign_id: str,
    *,
    timeout_sec: float = DISPATCH_LOCK_TIMEOUT_SEC,
) -> Iterator[bool]:
    """Serialize one campaign's resolve → sidecar → start window across processes.

    Yields ``True`` when this frame took the OS lock, ``False`` when this
    process already held it and the frame is nested (module docstring). Either
    way the caller holds it for the duration of the block and it is released
    only when the OUTERMOST frame exits — including on an exception, which is
    the case that matters: a slot that raises mid-window must not leave the
    campaign locked against every later tick.

    Raises :class:`hpc_agent.errors.QueueDispatchLockHeld` when *timeout_sec*
    expires with a peer inside the window. That is a disclosed outcome, not a
    crash: the caller is expected to catch it and report the item as
    refused-because-claimed (R4 — no item is ever dropped without a reason).

    Take it around the WHOLE window::

        with campaign_dispatch_lock(experiment_dir, cid):
            rr = resolve_submit_inputs(...)   # consumes the sidecar index
            queue_run(...)                    # durable-first enqueue
            campaign_run(..., detach=True)    # starts the gated lifecycle

    Narrowing it to any one of those three re-opens the window E4 closes.
    """
    from hpc_agent.infra import io  # noqa: PLC0415 — keep the io import lazy, as its peers do

    lock_path = dispatch_lock_path(experiment_dir, campaign_id)
    # THIS thread's frames only (module docstring): a frame on another thread has
    # inherited nothing and must contend at the OS lock like any other process.
    key = (threading.get_ident(), str(lock_path))

    with _DEPTH_GUARD:
        nested = _HELD_DEPTH.get(key, 0) > 0
        if nested:
            _HELD_DEPTH[key] += 1
    if nested:
        try:
            yield False
        finally:
            _release_depth(key)
        return

    try:
        lock_cm = io.advisory_flock(lock_path, timeout_sec=timeout_sec)
        lock_cm.__enter__()
    except TimeoutError as exc:
        # NAME the lock and the campaign: the operator's next move is to find
        # the peer, and a message that says only "timed out" sends them looking
        # for a bug in the queue instead of for a process.
        raise errors.QueueDispatchLockHeld(
            f"could not acquire the dispatch lock for campaign {campaign_id!r} at "
            f"{lock_path} within {timeout_sec:.0f}s — another process is inside this "
            "campaign's resolve->start window. Resolving a trial consumes the optuna "
            "sidecar index, so overlapping windows would propose the SAME trial twice "
            "(§10.S2.4 / E4); refusing is the point."
        ) from exc

    with _DEPTH_GUARD:
        _HELD_DEPTH[key] = 1
    try:
        yield True
    finally:
        _release_depth(key)
        lock_cm.__exit__(None, None, None)
