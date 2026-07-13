"""Pydantic models for the status human-amplification block verbs.

The status flow, decomposed (``docs/design/human-amplification-blocks.md`` §3,
§5) into two **blocks**, at the same grain as the submit S1–S4 blocks
(``_wire/workflows/submit_blocks.py``). Each block chains deterministically in
code as far as code can go, then TERMINATES at a human decision point carrying
code-digested evidence — a *brief*. No decision point is resolved by the LLM:
the block hands back the brief; the LLM drafts a proposal over it; the human
answers ``y`` or a natural-language nudge (§2).

The two blocks:

* **status-snapshot — one-shot digest (no watch).** A cheap journal-first read
  (optionally re-derived from the cluster via ``reconcile-journal``) that
  digests "what is running where / what changed since the human last looked"
  from durable state — the §5 first-class task-state contract. The changed-since
  delta is computed against ``last_seen_by_human_at``; after digesting, the
  watermark is re-stamped via ``mark_seen_by_human``. ``needs_decision`` only
  when evidence demands it: a stalled driver (``find_stalled_runs``) or a run
  already sitting on a failed / abandoned terminal.
* **status-watch — blocking poll to terminal or anomaly.** Composes
  ``monitor-flow`` (which owns the throttled SSH spine and the §5 guaranteed
  harvest in its ``finally``). Terminators: a clean terminal
  (``needs_decision=False`` + a hand-off hint to the harvest block) or an
  anomaly — failed / abandoned / timeout — which raises the ``y``/nudge
  boundary with a drafted-evidence brief (error digest, counts, and a
  structured ``recommendation`` — proposed next-action DATA, never LLM text).

Both blocks share :class:`StatusBlockResult`; the per-block Spec models embed
the existing ``MonitorFlowSpec`` verbatim rather than re-enumerating its knobs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import DetachedHandleFields
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec

# The union of every terminator a status block can stop at, modelled as data
# (#231 decision-points-as-data). ``needs_decision`` on the result says whether
# a human must answer here; ``stage_reached`` carries the finer detail.
StatusBlockStage = Literal[
    # status-snapshot — one-shot digest.
    "snapshot_clean",  # nothing demands a decision (needs_decision=False).
    "snapshot_anomaly",  # stalled driver and/or failed/abandoned run surfaced.
    # status-watch — blocking poll.
    "watch_terminal",  # clean terminal; harvest guaranteed → hand off to harvest.
    "watch_anomaly",  # failed / abandoned → §5 anomaly terminator (evidence brief).
    "watch_timeout",  # wall-clock budget hit; cluster jobs may run on.
    # Detach-by-contract (design §3): status-watch spawned a durable detached
    # worker to own the ONE cold dial per lifetime and returned immediately — the
    # terminal/anomaly brief arrives on completion, read from the journal (never
    # held in a process, never dialed inline on an unattended cron tick).
    "detached",
]


class StatusBlockResult(DetachedHandleFields):
    """Shared ``data`` block for every status block (snapshot / watch).

    The ``brief`` is the code-digested evidence the LLM drafts a proposal over
    (§2): the running-where digest + changed-since-seen delta (+ stalled/anomaly
    evidence) for the snapshot, and the terminal status + harvest hand-off or the
    drafted anomaly brief for the watch. ``needs_decision`` marks a ``y``/nudge
    terminator (True for a snapshot anomaly, a watch anomaly, or a watch timeout;
    False for a clean snapshot and for a watch that reached a clean terminal —
    which simply hands off to the harvest block).
    """

    model_config = ConfigDict(extra="forbid", title="status-block output data")

    block: Literal["snapshot", "watch"] = Field(
        description="Which status block produced this result.",
    )
    stage_reached: StatusBlockStage = Field(
        description="The terminator the block stopped at (decision-as-data, #231).",
    )
    needs_decision: bool = Field(
        description=(
            "True when a human must answer here (the y/nudge terminator). False "
            "for a clean snapshot and for a watch that reached a clean terminal."
        ),
    )
    reason: str = Field(
        default="",
        description="Human-readable one-line summary of the terminator.",
    )
    relay: str = Field(
        default="",
        description=(
            "The human-facing one-liner CODE renders FRESH from the journal "
            "digest — the agent relays it VERBATIM (design §5.3, finding 15). "
            "Rendered from the CURRENT record state on every snapshot, so the "
            "agent relays what the snapshot returns NOW rather than a brief it "
            "cached across a journal transition (the staleness fix). Each run's "
            "counts are rendered from its own digest row, so a canary's 1-task "
            "summary never bleeds the main array's total. Shares the one renderer "
            "with the submit blocks so the two surfaces agree."
        ),
    )
    run_id: str | None = Field(
        default=None,
        description="The run this block operated on (null for a fleet-wide snapshot).",
    )
    brief: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Code-digested evidence the LLM drafts a proposal over (§2). Shape "
            "varies per block; never interpreted raw by the LLM."
        ),
    )
    next_block: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The DETERMINISTICALLY-computed next block — ``{verb, why, spec_hint}`` "
            "— or null at a terminal / human-branch terminator (design §2, the "
            "``_next_step_hint`` pattern generalized). ``verb`` names the next "
            "block's CLI verb; ``spec_hint`` carries the minimal next-spec skeleton "
            "(run_id etc.). Surfaced, greenlit, journaled under "
            "``resolved.next_block``, and enforced by the successor gate — never "
            "free-prose."
        ),
    )


class StatusSnapshotSpec(BaseModel):
    """Inputs to ``status-snapshot`` (one-shot digest).

    Journal-first and cluster-free by default: it digests durable state (the §5
    task-state contract) into "what is running where / what changed since the
    human last looked". Set ``reconcile`` to re-derive ground truth from the
    cluster first (the only path that touches SSH) — that requires ``run_id`` and
    ``scheduler``.
    """

    model_config = ConfigDict(extra="forbid", title="status-snapshot input spec")

    run_id: str | None = Field(
        default=None,
        description=(
            "The run to digest. Null → a fleet digest over every in-flight run "
            "(what is running where, right now)."
        ),
    )
    reconcile: bool = Field(
        default=False,
        description=(
            "Re-derive ground truth from the cluster (via the `reconcile` verb) "
            "before digesting — the only path that touches SSH. Requires run_id + "
            "scheduler."
        ),
    )
    scheduler: str | None = Field(
        default=None,
        description="Backend name — required only when reconcile=True (to query alive job IDs).",
    )
    now_iso: str | None = Field(
        default=None,
        description=(
            "ISO-8601 UTC 'now' for the stalled-driver watchdog check and the "
            "attention watermark. Defaults to the current UTC time."
        ),
    )
    mark_seen: bool = Field(
        default=True,
        description=(
            "Stamp last_seen_by_human_at on each digested run AFTER computing the "
            "changed-since-seen delta, so the next snapshot's delta is measured "
            "from this look. Disable for a peek that must not move the watermark."
        ),
    )


class StatusWatchSpec(BaseModel):
    """Inputs to ``status-watch`` (blocking poll to terminal or anomaly).

    Embeds the full ``monitor-flow`` spec; status-watch runs it to a
    terminal/anomaly state (monitor-flow owns the throttled SSH spine and the §5
    guaranteed harvest), then digests the outcome into a brief. On a timeout —
    the "keep watching?" terminator — it arms the next tick when an
    ``invocation_argv`` is supplied.
    """

    model_config = ConfigDict(extra="forbid", title="status-watch input spec")

    monitor: MonitorFlowSpec = Field(
        description=(
            "The wait-until-terminal monitor spec (run_id + poll cadence + "
            "wall-clock budget). status-watch runs it to terminal/timeout."
        ),
    )
    invocation_argv: str | None = Field(
        default=None,
        description=(
            "The exact /monitor-hpc argv the next tick should re-invoke. When "
            "supplied, a timeout terminator arms the next tick (decide-monitor-arm) "
            "and folds the cron/loop/none decision into the brief. Null skips arming."
        ),
    )
    user_invoked_via_loop: bool | None = Field(
        default=None,
        description="True iff this tick runs under /loop (the user drives cadence; no cron armed).",
    )
    detach: bool = Field(
        default=True,
        description=(
            "Detach-by-contract (design §3): default ON — no UNATTENDED cold dial may "
            "exist (connection-broker.md, 2026-07-07). When True status-watch spawns a "
            "durable detached worker that owns the ONE cold dial per lifetime (warm "
            "engine, lease-single, watchdog-covered, exits at terminal — never an "
            "immortal daemon) and returns immediately with a {started, watch: journal, "
            "detached_pid} handle; the terminal/anomaly brief is read from the journal "
            "on completion. So an unattended `block-drive --workflow status` tick that "
            "chains snapshot→watch spawns-and-returns with ZERO inline ssh (the "
            "snapshot is journal-first; the watch detaches). Set False to run the "
            "monitor poll synchronously in-process (the attended/CLI/tests path)."
        ),
    )
