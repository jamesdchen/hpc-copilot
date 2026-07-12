# Code-driven orchestration: code drives, the LLM only at decision points

The shipped consumption styles put the control flow in **code**, not in
an LLM. The interactive slash commands are a human + Claude translating
at decision points; underneath, the workflow itself is driven by a
stateless code tick (`block-drive`) that chains deterministic spans and
parks at each genuine human boundary. There is no `claude -p` worker
executing a rendered procedure any more — that spawn transport and its
worker-prompt directory were removed
(`docs/design/history/proving-run-2-hardening.md` §6); the LLM's role
shrank to translation at the parks.

This page documents driving that same substrate from **a plain program**:
your loop owns the control flow and consults an LLM only where a typed
judgement point genuinely requires one, with every decision recorded on
the audit trail (the journal / `WorkerReport`).

The seams, at three altitudes. They compose.

## 1. The workflow spine — `block-drive`

`hpc_agent._kernel.lifecycle.block_drive` is the primary code-driven
orchestrator for the human-amplification workflows (submit / status /
aggregate / campaign). One `block_drive.run_tick`:

- **chains the deterministic spans in code** (`block_drive._chain`) — a
  block that returns without a human decision and with a code-determined
  successor is followed immediately, in the same tick, no LLM round-trip;
- **at a decision point, parks and exits** — writes the pending marker
  (`journal.mark_pending_decision`) and returns; the journal + filesystem
  are the only state carried between ticks;
- **on resume, consumes an approved SPEC, never a nudge string** — it
  reads the latest committed greenlight's `resolved` and routes by
  IDENTITY + OWNERSHIP (`block_drive.plan_block_action` +
  `ops/field_ownership.py::route`): advance / rerun / advance_carrying.

The block bodies (`ops/submit_blocks.py`, `meta/campaign/blocks.py`, …)
ARE the execution — code does the SSH, staging, canary, submit, watch,
harvest. **No LLM lives inside a block.** The console / cron / detach-
child entry is `block_drive.block_drive_once` (console script
`hpc-block-drive`); a plain program calls `run_tick` directly. See
[`../internals/submit-sequence.md`](../internals/submit-sequence.md) for
the full submit walkthrough and
[`../design/block-drive.md`](../design/block-drive.md) for the tick's
contract.

## 2. The generic tick-loop — `drive_once` + `StepTable`

`hpc_agent._kernel.lifecycle.drive.drive_once` is a **neutral** loop body
for external autonomous controllers (Optuna / Ax / a custom controller).
It is deliberately not a `@primitive` — primitives are JSON-in/JSON-out
tools an agent invokes; this loop *drives*. Each tick reads the
`delegate` block emitted by `hpc-agent load-context` and dispatches on
its kind (`drive.plan_action`, a pure, unit-testable function):

- `kind == "cli"` → a deterministic step. Your injected `StepTable`
  (`{"monitor": "monitor-flow", "aggregate": "aggregate-flow"}`) maps the
  delegate step to an `hpc-agent` verb, run directly. **No LLM, ever.**
- `kind == "agent"` → a judgement step. Always planned as **skip**: a
  judgement step is a human decision boundary, driven via `block-drive`
  (seam 1). The `claude -p` worker-spawn transport this loop once
  dispatched was removed in the §6 worker removal, so there is no
  resolver to inject — the loop no longer takes one.

One step per invocation: idempotent and cron-friendly. Wrap it in cron or
`/loop`; the on-disk state (run sidecars, journal, cursors) is the only
thing carried between ticks. The mechanism stays neutral; the domain
knowledge is the caller's, injected as the `StepTable` — the same
"loop owns the protocol, caller owns the policy" seam
`_kernel/decision/kernel.py` establishes one level down.

```python
from pathlib import Path

from hpc_agent._kernel.lifecycle.drive import drive_once

# One deterministic step per call; agent (judgement) steps skip to block-drive.
code = drive_once(
    Path("~/experiments/tune_lr").expanduser(),
    step_table={"monitor": "monitor-flow", "aggregate": "aggregate-flow"},
)
```

## 3. The pure-CLI seam — escalation-as-data from any language

No Python required: every judgement point is enumerated and typed, so a
shell/Go/Rust loop can drive `hpc-agent` verbs and consult an LLM only
when an envelope says so.

- `DECISION_POINTS` (`hpc_agent/_wire/spawn_contract.py`) enumerates each
  workflow's choice points and tags each `decided_by: "code"` (a
  primitive computes it — call the verb) or `"judgement"` (the
  genuinely-LLM tail: `axis_class`, `resubmit`, `partial_handling`,
  campaign `path`/`decide`/`concurrency`). `judgement_point_ids` returns
  the judgement subset per workflow.
- Primitives return **escalation-as-data**: an `Escalation{decided_by,
  reason, failure_features, candidate_actions}` on held failure clusters,
  `needs_decision` / `stage_reached` refusals (`resolve-submit-inputs`,
  `submit-pipeline`'s `parents_not_ready`), a `safe_default` on
  `decide-resubmit`. `candidate_actions` is a closed menu — the LLM
  picks, your code acts (e.g. `resubmit --spec` with the chosen
  overrides).

The integrator workflow (`find-prior-run` → `submit` →
`monitor-summary` → `verify-aggregation-complete`) is documented in
[`../integrations/CONTRACT.md`](../integrations/CONTRACT.md); the JSON
envelope and exit codes in
[`../reference/cli-spec.md`](../reference/cli-spec.md).

**Putting an LLM at a decision point in your own code.** There is no
shipped resolver that adjudicates a parked residue for you — the block
architecture parks to the *human* at genuine judgement points, and the
LLM only translates the brief. If your controller wants to make a bounded
LLM call at a decision point, `_kernel/lifecycle/structured.py`
(`structured`, `get_model`, the `ChatModel` messages-in/completion-out
protocol) is the primitive to build on: constrain the completion to a
closed menu of `candidate_actions`, feed the choice back as a spec, and
own the `messages` list across turns for multi-shot decisions. Your code
keeps control flow; the model picks from a menu you authored.

## 4. Detach-by-contract — no LLM in the connection loop

The seams above keep the LLM out of *control flow*; this one keeps it out
of the *connection loop*. The 0.10.63 cluster ban traced to an LLM
**driving SSH**: a `claude -p --bare` worker was spawned to run a
wait-until-terminal poll; it auto-backgrounded at 2 min, ended its turn
mid-poll (so the run reported "no report"), and a fallback inline
subagent retried SSH in prose for ~21 min. The fix is the
`infra/retry.py` principle carried to the drive layer: the poll loop runs
in plain code with one process owning the connection, and the model is
out of the loop.

**Detach-by-contract** runs each cluster-bound block as a DETACHED
`hpc-agent <verb>` subprocess — **not** a `claude -p` worker — that owns
the connection and runs to terminal, while the orchestrator learns the
outcome by **reading the journal**. This is DPDispatcher's "submit and
poke until they finish" loop / jobflow-remote's Runner daemon applied to
the drive layer. The detach-supported verbs are
`detached.SUPPORTED_DETACHED_BLOCK_VERBS` (the S2 canary-wait, S3 main-
array watch, speculative canary, S4 harvest, plus `status-watch` and the
`aggregate-run` / `campaign-run` iteration blocks). The parent runs its
synchronous gate + drift guards, forces the spec's `detach` field OFF,
and spawns the child on the SAME verb body so the child owns the SSH poll
to terminal (stamping the journal as it goes) while the parent returns a
`DetachedLaunch` handle immediately:

```python
# In the parent verb (e.g. ops/submit_blocks.py): gate → drift → detach.
from hpc_agent._kernel.lifecycle.detached import launch_submit_block_detached

launch = launch_submit_block_detached(
    verb="submit-s3", experiment_dir=exp_dir, spec=spec_with_detach_off
)
# → DetachedLaunch(run_id=..., pid=4242, log_path=".../submit-s3-...log", argv=[...])
```

```python
# Poll the JOURNAL (cluster-free) until terminal — the child writes it as
# it polls; the orchestrator only reads. The model schedules nothing.
from pathlib import Path
from hpc_agent.state.journal_poll import poll_until_terminal

snap = poll_until_terminal(Path("~/experiments/tune_lr").expanduser(), launch.run_id)
if snap.terminal:
    ...  # snap.status ∈ {complete, failed, abandoned}; act on it
```

The detached child uses `start_new_session` (POSIX) /
`DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP` (Windows, plus
`CREATE_BREAKAWAY_FROM_JOB` to escape a kill-on-close Job Object) so it
**outlives the orchestrator** — the crash that killed the auto-
backgrounded `submit-pipeline` ~1s after `qsub` in 0.10.63 no longer
kills the poll. The child's stdout/stderr is captured to `log_path` for a
post-mortem that never re-opens SSH. The launch is idempotent-single: a
filesystem lease keyed by `(run_id, block)`
(`detached._guard_single_lease`) refuses a second LIVE worker
(`DetachedLeaseHeld`) while self-healing on a dead pid — the proving-
run-#2 race where two `submit-s2` workers hit one run.

Implementation: `hpc_agent._kernel.lifecycle.detached`
(`launch_submit_block_detached`, `_spawn_detached`, the lease/pid guards)
+ `hpc_agent.state.journal_poll`. Wired into the blocks in
`ops/submit_blocks.py` / `ops/submit_speculate.py`. In-session callers
block on the lease pid (no SSH) via `hpc-agent wait-detached`
(`ops/monitor/wait_detached.py`).

## 5. Fully mechanized async — the campaign refill actor

A campaign is greenlit **once** at start and then runs fully
asynchronously — there is no per-iteration human boundary
(`meta/campaign/blocks.py`). The iteration loop is entirely code, on the
same `block-drive` spine:

- `campaign-advance` (`meta/campaign/atoms/advance.py::campaign_advance`)
  is the pure **authority** — each tick it decides whether a greenlit
  campaign has a free pool slot with budget headroom
  (`decision == "refill"`, carrying `refill_count`). It never submits.
- `campaign-refill` (`ops/campaign_refill.py`) is the side-effecting
  **actor** that consumes that decision and tops the pool back up: per
  slot, sequentially, `resolve-submit-inputs` →
  `campaign_run(detach=True)`. It is a first-class primitive — the refill
  arm the deleted `deterministic_resolver` used to carry, now sitting on
  the block-drive spine like `campaign-run`.

The strictly-sequential, sidecar-between-slots discipline is load-bearing
(the async scaffold indexes proposals by the campaign sidecar count); see
`ops/campaign_refill.py`'s module docstring and
[`../design/campaign-async-refill.md`](../design/campaign-async-refill.md).

## Composing with the DAG kernel

For multi-stage pipelines, the same loop walks the run graph:
`hpc-agent dag-frontier` (`ops/dag_frontier.py`) returns the complete-
runs frontier (which nodes' parents are all terminal); your code fires
`submit-pipeline` (`ops/submit_pipeline.py`) per ready node (it composes
`validate-parents-ready` — `ops/validate/parents_ready.py` — mechanically);
LLM calls happen only where a node's submit genuinely escalates. Note for
contributors: recorded walks of exactly this shape are the evidence
[`../design/dag-kernel.md`](../design/dag-kernel.md)'s earn-it rule
requires before any framework-side graph runner is considered — if you
build this loop, keep the tick records.

## Cost model

| Path | LLM spend per tick |
|---|---|
| `block-drive` chaining a deterministic span (`kind: "cli"` step) | zero |
| `drive_once` running a mapped CLI verb | zero |
| a campaign refill / advance tick (fully mechanized) | zero |
| parking at a human decision point (the rendezvous) | zero — the tick exits; the LLM only translates the brief in chat |
| a decision your own controller chose to adjudicate via `structured()` | one schema-constrained completion (+ bounded repairs) you authored |

The LLM is never in control flow and never in the connection loop. The
only spend is the translation at a park (chat, human-paced) or a
completion your own code explicitly chose to make against a closed menu.
</content>
