"""Pydantic models for the ``wait-detached`` query primitive.

The harness-notification bridge for detach-by-contract (design §3, §5;
proving-run-3 finding: the raw-``Popen`` detached worker severs the harness's
completion channel, leaving the driving agent nothing to await, so it falls
back to timed ``/loop`` polling — burning cache on guessed cadences and adding
up to a full poll interval of dead air after the brief is ready).

``wait-detached`` is a BLOCKING query: it returns when the detached worker for
``(run_id, block)`` exits (its lease pid dies) or the timeout elapses. The
driving agent launches it through the harness's native backgrounding
(Claude Code ``run_in_background``) and is woken by the harness exactly once,
at completion — event-driven, no polling.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class WaitDetachedInput(BaseModel):
    """Which detached worker to wait on, and for how long."""

    model_config = ConfigDict(extra="forbid", title="wait-detached input spec")

    run_id: RunIdStrict
    # The block whose worker to await (e.g. ``submit-s2``). Omitted → wait on
    # ANY live lease for the run (the common case: one detached block at a
    # time; with several, the first exit returns and names its block).
    block: str | None = None
    # Wall-clock budget. A detached canary/main watch can legitimately run for
    # hours — default generously; the waiter is cheap (a local pid probe every
    # ``poll_interval_sec``, no SSH).
    timeout_sec: float = Field(default=7200.0, gt=0, le=86400.0)
    poll_interval_sec: float = Field(default=2.0, gt=0, le=60.0)


class WaitDetachedResult(BaseModel):
    """How the wait ended, plus the pointers the wake-up needs next.

    ``outcome``:

    * ``worker_exited`` — the lease pid died: the block finished (or crashed);
      the journal/log carry the verdict. Read the run's journal state next.
    * ``no_live_worker`` — no live lease for ``(run_id, block)`` at call time:
      either the worker already exited (brief likely ready) or none was ever
      launched. Also the immediate return when the lease file is absent.
    * ``timeout`` — the budget elapsed with the worker still alive. NOT an
      anomaly by itself (long queue waits are normal); re-arm another wait or
      consult ``status-snapshot``.
    """

    model_config = ConfigDict(extra="forbid", title="wait-detached output data")

    outcome: Literal["worker_exited", "no_live_worker", "timeout"]
    run_id: str
    # The lease's block when one was found (source of truth over the input's
    # optional filter), else the input's ``block`` passthrough.
    block: str | None
    pid: int | None
    log_path: str | None
    waited_sec: float = Field(ge=0)
    # The wake-up payload (L2, run-14 loop kill): a detached worker parks ITSELF
    # at its decision boundary (writes the §5 pending marker + records its
    # terminal) on the way out, so ``wait-detached`` can hand the woken agent the
    # decision brief DIRECTLY — no extra driver tick to notice the terminal and no
    # journal re-scrape. Populated from the worker's recorded terminal (falling
    # back to the pending-decision marker) once it has exited; ``None`` while the
    # worker is still alive (``timeout``) or when it recorded nothing.
    brief: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The exited worker's code-digested decision brief, read from its "
            "recorded terminal / pending-decision marker. Null on timeout (still "
            "running) or when no terminal was recorded."
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
