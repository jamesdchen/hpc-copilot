"""Pydantic models for the ``poll-detached`` query primitive.

The NON-blocking sibling of ``wait-detached`` (``ops/monitor/wait_detached.py``).
Where ``wait-detached`` BLOCKS on a detached worker's lease pid so the harness
can wake the agent exactly once at completion, ``poll-detached`` takes a single
INSTANT snapshot and returns — the affordance a caller reaches for when it wants
"where is this worker *right now*?" without holding a turn open (over MCP the
in-process server dispatches synchronously, so a blocking wait would wedge it —
``_kernel/extension/mcp_server.py::_refuse_blocking_over_mcp`` refuses
``wait-detached`` there; this query is the MCP-safe read).

The snapshot fuses the three durable signals a detach-by-contract worker leaves
behind (design §3, ``docs/design/human-amplification-blocks.md``), each read
with ZERO cluster contact:

* the filesystem LEASE the launcher stamps
  (``_kernel/lifecycle/detached.py::_spawn_detached`` writes
  ``<verb>-<run_id>.lease.json`` under the global ``_detached/`` home with the
  worker ``pid``) — presence + pid-liveness say "is a worker process live?";
* the per-run JOURNAL status (``state/journal_poll.read_run_status``) — the
  durable "is this run done?" signal the submit/dedup paths key off;
* the block TERMINAL record (``state/block_terminal.read_terminal_with_fallback``)
  — the durable "this (run_id, block) reached terminal" replay signal.

From those it derives a single ``state`` so a caller need not re-derive the
lease-vs-journal-vs-terminal logic every consumer used to hand-roll. Crucially
``exited_unrecorded`` (pid dead, but no terminal recorded) names the exact
dead-worker gap the run-#12 hunt chased — a worker that died WITHOUT stamping a
terminal — so a caller can escalate to the doctor instead of waiting forever.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class PollDetachedInput(BaseModel):
    """Which detached worker to snapshot.

    ``experiment_dir`` is deliberately NOT a field here: it is supplied through
    the standard ``--experiment-dir`` CLI arg (``experiment_dir_arg=True`` on the
    :class:`CliShape`, injected as an optional MCP input property), exactly as
    every sibling monitor query resolves it (``ops/monitor/{status,reconcile,
    list_in_flight,...}.py``). The architect memo's shorthand lists it beside
    ``run_id``/``block`` as an input, but the sibling-mirroring CliShape it also
    mandates is where it belongs — the journal/terminal reads receive it as the
    primitive's ``experiment_dir`` kwarg.
    """

    model_config = ConfigDict(extra="forbid", title="poll-detached input spec")

    run_id: RunIdStrict
    # The detached block whose worker to snapshot, named by its detach VERB
    # (e.g. ``campaign-run``, ``submit-s2``, ``status-watch``) — the SAME key the
    # launcher stamps the lease under and the terminal store is keyed by
    # (``state/block_terminal.terminal_block_key``). Required: the lease path and
    # the terminal lookup are both ``(run_id, block)``-keyed, so a snapshot with
    # no block would have no worker to point at. NOT constrained to the
    # ``SUPPORTED_DETACHED_BLOCK_VERBS`` frozenset — a plugin may add detachable
    # verbs, and coupling the wire model to that core set would break the
    # library-knowledge boundary.
    block: str = Field(min_length=1)


class PollDetachedResult(BaseModel):
    """A single instant read of a detached worker's liveness + durable records.

    ``state`` fuses the raw signals into the one answer callers act on:

    * ``running`` — a lease exists and its pid is alive: the worker is driving
      the block. Observe further via the journal (``watch``); do NOT relaunch
      (the lease is single, ``_kernel/lifecycle/detached.py::_guard_single_lease``).
    * ``exited_recorded`` — the lease pid is dead AND a block terminal is on disk:
      the worker finished cleanly-or-not and stamped its verdict. Read the
      terminal / journal for the outcome; a re-invoke will REPLAY, not re-spawn.
    * ``exited_unrecorded`` — the lease pid is dead but NO terminal was recorded:
      the dead-worker gap (run-#12). The worker died without stamping a terminal;
      escalate to the doctor / re-arm rather than waiting on a wake that will
      never come.
    * ``no_lease`` — no lease file for ``(run_id, block)``: the worker was never
      launched (or its lease was reclaimed). The journal status still reports
      whatever is on disk.

    ``watch`` is the constant ``"journal"``: every further observation of a
    detached worker is a journal read (never an SSH dial) — the field states the
    contract so a caller polls the right surface.
    """

    model_config = ConfigDict(extra="forbid", title="poll-detached output data")

    run_id: str
    block: str
    # Whether the ``<block>-<run_id>.lease.json`` file exists at all — the
    # absent-vs-present distinction the derived ``no_lease`` state keys off (a
    # present-but-corrupt lease is still ``present``: a worker was launched).
    lease_present: bool
    # The pid recorded in the lease, or None when absent/unreadable/malformed.
    pid: int | None
    # Whether that pid names a live process right now (False whenever ``pid`` is
    # None) — the single liveness probe ``infra.proc.pid_alive``.
    pid_alive: bool
    # The run's journal status (``in_flight``/``complete``/``failed``/
    # ``abandoned``), or None when no journal record exists yet.
    journal_status: str | None
    # Whether a block terminal record exists for ``(run_id, block)`` — the replay
    # signal that separates ``exited_recorded`` from ``exited_unrecorded``.
    terminal_recorded: bool
    state: Literal["running", "exited_recorded", "exited_unrecorded", "no_lease"]
    watch: Literal["journal"] = "journal"
