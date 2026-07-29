"""Pydantic models for the ``wait-any-detached`` query primitive.

The FLEET form of ``wait-detached`` (``_wire/queries/wait_detached.py``). A drain
pass over N running items today holds N chunked ``wait-detached`` relays — one
subagent seat each — because each waiter watches exactly one ``(run_id, block)``
lease. ``wait-any-detached`` collapses that to ONE holder: it watches the whole
target set and returns the moment ANY watched worker resolves, so fleet-scale
waiting costs one seat instead of N (run-queue plan
``docs/plans/run-queue-placement-2026-07-28.md`` §7, "``wait-any-detached``
(Phase 2 efficiency)").

Identity, vocabulary, and budget discipline are ``wait-detached``'s, unchanged:
a target is the same ``(run_id[, block])`` pair the single waiter takes, the
outcome literals are the same three, and ``timeout_sec`` / ``poll_interval_sec``
carry the same defaults and clamps — so a caller can branch on one vocabulary
whether it waited on one run or forty.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import RunIdStrict


class DetachedTarget(BaseModel):
    """One watched detached worker — the identity ``wait-detached`` takes singly."""

    model_config = ConfigDict(extra="forbid", title="wait-any-detached target")

    run_id: RunIdStrict
    # The block whose worker to await (e.g. ``submit-s2``). Omitted → any live
    # lease for the run, exactly as ``wait-detached``'s optional ``block`` does.
    block: str | None = None


class WaitAnyDetachedInput(BaseModel):
    """Which detached workers to watch as a set, and for how long."""

    model_config = ConfigDict(extra="forbid", title="wait-any-detached input spec")

    # REQUIRED and non-empty: an empty watch set has no wait to perform and
    # would burn the full budget doing nothing — the caller must be told, not
    # parked. (The all-optional spec-optional precedent, ``net-triage``, does
    # not apply: this spec has a required field.) Capped so a pathological spec
    # can't turn one poll tick into an unbounded directory sweep; a drain pass
    # watches at most its own loop count.
    targets: list[DetachedTarget] = Field(min_length=1, max_length=128)
    # Wall-clock budget — same default/clamp as ``wait-detached``: a detached
    # canary/main watch can legitimately run for hours, and the waiter is cheap
    # (local pid probes only, no SSH).
    timeout_sec: float = Field(default=7200.0, gt=0, le=86400.0)
    poll_interval_sec: float = Field(default=2.0, gt=0, le=60.0)

    @model_validator(mode="after")
    def _reject_duplicate_targets(self) -> WaitAnyDetachedInput:
        """A ``(run_id, block)`` pair may appear at most once.

        Two rows for one identity would poll the same lease twice and report the
        same worker twice under two rows — a caller bug that silently corrupts
        the per-target snapshot the caller branches on, so it is refused at the
        wire boundary rather than tolerated.
        """
        seen: set[tuple[str, str | None]] = set()
        for target in self.targets:
            key = (target.run_id, target.block)
            if key in seen:
                label = f"{target.run_id}" + (f"/{target.block}" if target.block else "")
                raise ValueError(f"duplicate target {label!r} in targets")
            seen.add(key)
        return self


class DetachedTargetState(BaseModel):
    """One target's state at the moment the wait returned.

    ``state`` uses ``wait-detached``'s vocabulary for the two resolved cases and
    adds the one a single waiter never has to name (a target that is still
    running while a SIBLING target is what returned):

    * ``worker_exited`` — this target's lease pid died during the wait.
    * ``no_live_worker`` — no live lease for this target at the first poll: it
      already exited, or was never launched.
    * ``still_running`` — this target's lease pid was alive when the wait
      returned (another target triggered, or the budget elapsed).
    """

    model_config = ConfigDict(extra="forbid", title="wait-any-detached target state")

    run_id: str
    # The lease's block when one was found (source of truth over the target's
    # optional filter), else the target's ``block`` passthrough.
    block: str | None
    pid: int | None
    log_path: str | None
    state: Literal["worker_exited", "no_live_worker", "still_running"]
    # True for the target(s) whose resolution ENDED the wait. Always false on a
    # ``timeout`` return. Several targets can trigger together (two pids dying
    # between the same pair of polls), which is why this is a per-row flag and
    # not a single top-level pointer.
    triggered: bool = False
    # The wake payload, on the same terms ``wait-detached`` returns it: read from
    # the exited worker's recorded terminal (falling back to the §5
    # pending-decision marker), populated only for a target whose worker is gone
    # — a ``still_running`` row has no terminal to read, so it stays null.
    brief: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The exited worker's code-digested decision brief, read from its "
            "recorded terminal / pending-decision marker. Null for a "
            "still_running target or when no terminal was recorded."
        ),
    )
    relay: str | None = Field(
        default=None,
        description=(
            "The human-facing one-liner CODE rendered for the worker's terminal "
            "(the SubmitBlockResult.relay), relayed VERBATIM — never reconstructed "
            "from memory. Null when the worker recorded no relay."
        ),
    )
    next_verb: str | None = Field(
        default=None,
        description=(
            "The deterministically-computed next block verb the worker's terminal "
            "suggests (its next_block.verb), or null at a human-branch / terminal "
            "boundary."
        ),
    )


class WaitAnyDetachedResult(BaseModel):
    """How the fleet wait ended, plus every watched target's state.

    ``outcome`` is ``wait-detached``'s vocabulary VERBATIM — the drain plan
    branches on the same three literals whether it waited on one run or forty:

    * ``worker_exited`` — at least one watched worker's pid died during the
      wait. The triggering rows carry the wake payload; read those runs'
      journals next.
    * ``no_live_worker`` — at least one target had no live worker at the FIRST
      poll (already exited, or never launched), so there was already something
      actionable and the wait returned at once rather than holding a seat. The
      degenerate all-dead set lands here too.
    * ``timeout`` — the budget elapsed with every watched worker still alive.
      NOT an anomaly by itself (long queue waits are normal); re-arm the wait
      with the same set or consult ``queue-status`` / ``status-snapshot``.

    ``targets`` is the FULL snapshot in the input's order — the triggering rows
    plus every other target's current state — so one return tells the caller
    what to act on AND what is still in flight.
    """

    model_config = ConfigDict(extra="forbid", title="wait-any-detached output data")

    outcome: Literal["worker_exited", "no_live_worker", "timeout"]
    targets: list[DetachedTargetState]
    # Count of rows with ``triggered=true`` — the branch a caller reads before
    # scanning rows (zero exactly on ``timeout``).
    triggered_count: int = Field(ge=0)
    waited_sec: float = Field(ge=0)
