"""The durable per-campaign dispatch lock (run-queue plan §10.S2.4 / D3).

``state/queue_locks.campaign_dispatch_lock`` mechanizes E4's
sidecar-between-slots rule: resolving a trial consumes the optuna scaffold's
sidecar-indexed proposal, so two windows that overlap propose the SAME trial.
The lock must therefore be exactly two things at once —

* RE-ENTRANT for a nested frame on the SAME call stack, because §10.S3's refill
  slot holds it across ``resolve → enqueue → dispatch`` and the ``queue-dispatch``
  call nested inside takes it again; a naive re-acquire self-deadlocks into a
  300s timeout and reports a phantom peer;
* EXCLUSIVE against everything else, including another THREAD of this process.
  E4 forbids parallel slots outright, and a second thread waved through would
  resolve at the same ``_submitted_count`` — the precise collision the lock
  exists to make impossible, made invisible by reporting itself as "nested".

Both directions are pinned here, because the first cut keyed re-entrancy on the
lock PATH alone and got the second one backwards.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent.state.queue_locks import (
    _HELD_DEPTH,
    campaign_dispatch_lock,
    dispatch_lock_path,
)

if TYPE_CHECKING:
    from pathlib import Path

_CID = "tune_hoffman2"


def test_a_nested_frame_inherits_the_lock_and_does_not_deadlock(tmp_path: Path) -> None:
    """D5's composition: a refill slot holds it, the nested dispatch takes it too."""
    with (
        campaign_dispatch_lock(tmp_path, _CID) as outer,
        campaign_dispatch_lock(tmp_path, _CID) as inner,
    ):
        assert (outer, inner) == (True, False)
    assert _HELD_DEPTH == {}


def test_the_flock_is_released_only_by_the_outermost_frame(tmp_path: Path) -> None:
    """An inner frame exiting must not free the campaign for a peer mid-window."""
    with campaign_dispatch_lock(tmp_path, _CID):
        with campaign_dispatch_lock(tmp_path, _CID):
            pass
        # Still held: a second acquirer (here, another thread standing in for
        # another process) is still refused.
        assert _held_elsewhere(tmp_path, _CID) is False
    assert _held_elsewhere(tmp_path, _CID) is True


def test_a_second_thread_contends_and_is_refused(tmp_path: Path) -> None:
    """E4: a concurrent thread has inherited NOTHING and must not be waved through.

    Keyed on the lock path alone, the second thread read ``depth > 0``, was
    classified as a nested frame, and entered the resolve→sidecar→start window
    concurrently with the holder — both would then read the same
    ``_submitted_count``, cache the same optuna proposal, and derive the same
    ``run_id``. Observed before the fix: ``[('main', True), ('thread', False)]``.
    """
    with campaign_dispatch_lock(tmp_path, _CID):
        assert _held_elsewhere(tmp_path, _CID) is False


def test_an_inner_frame_outliving_its_holder_raises_nothing(tmp_path: Path) -> None:
    """The bookkeeping runs in a ``finally`` and must never throw from there.

    With a per-path key, a thread that "nested" onto another thread's depth and
    exited after it executed ``depth -= 1`` on an entry the holder had already
    popped — a ``KeyError`` out of a context manager's ``finally``, unrelated to
    anything that thread was doing. Per-thread keys make the interleaving
    unreachable; the symmetric release means it could not throw even if it were.
    """
    key = (threading.get_ident(), str(dispatch_lock_path(tmp_path, _CID)))
    with campaign_dispatch_lock(tmp_path, _CID):
        with campaign_dispatch_lock(tmp_path, _CID):
            assert _HELD_DEPTH[key] == 2
        assert _HELD_DEPTH[key] == 1
    assert key not in _HELD_DEPTH


def test_an_exception_inside_the_window_releases_the_campaign(tmp_path: Path) -> None:
    """A slot that raises mid-window must not lock the campaign against every tick."""
    with pytest.raises(RuntimeError), campaign_dispatch_lock(tmp_path, _CID):
        raise RuntimeError("slot blew up")
    assert _HELD_DEPTH == {}
    with campaign_dispatch_lock(tmp_path, _CID) as took:
        assert took is True


def test_a_blank_or_pathish_campaign_id_is_refused(tmp_path: Path) -> None:
    """The value reaches a FILENAME, so the charset is re-checked, not assumed."""
    for bad in ("", "  ", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError, match="campaign"):
            dispatch_lock_path(tmp_path, bad)


def _held_elsewhere(experiment_dir: Path, campaign_id: str) -> bool:
    """Can a DIFFERENT thread take this lock right now?

    A thread stands in for the second process the lock really guards against:
    :func:`hpc_agent.infra.io.advisory_flock` opens its own fd per call, so a
    genuine second thread contends at the OS lock exactly as a second process
    does — which is the whole reason the path-only key was able to hide the bug.
    """
    seen: list[Any] = []

    def probe() -> None:
        try:
            with campaign_dispatch_lock(experiment_dir, campaign_id, timeout_sec=0.5) as took:
                seen.append(("acquired", took))
        except errors.QueueDispatchLockHeld:
            seen.append(("refused", None))

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(timeout=30)
    assert seen, "the probe thread never reported"
    return seen[0][0] == "acquired"
