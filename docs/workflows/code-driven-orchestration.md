# Code-driven orchestration: your loop, LLM calls only at decision points

The two shipped consumption styles put the control flow inside an agent:
the interactive slash commands (a human + Claude drive), and the
delegated `claude -p` worker (`hpc-agent run`, the headless default —
the whole multi-step procedure runs inside one spawned agent). This page
documents the third style: **a plain program owns the loop and shells
out to an LLM only at the typed judgement points** — one-shot or
multi-shot, any provider, with every decision recorded on the
`WorkerReport` audit trail.

Three seams exist for this, at three altitudes. They compose.

## 1. The tick-loop seam — `drive_once` + `StepTable` + `JudgementResolver`

`hpc_agent._kernel.lifecycle.drive.drive_once` is the programmatic loop
body (#220). Each tick reads `hpc-agent load-context`'s `delegate`
block and dispatches:

- `kind == "cli"` → a deterministic step. Your injected `StepTable`
  (`{"monitor": "monitor-flow", "aggregate": "aggregate-flow"}`) maps it
  to an `hpc-agent` verb. **No LLM, ever.**
- `kind == "agent"` → a judgement step, handed to your injected
  `JudgementResolver`: any callable
  `(spawn_request, experiment_dir) -> (WorkerReport, exit_code)`.

The two shipped resolvers are the extremes:

- `default_judgement_resolver` spawns a whole fresh-context worker
  (`run_workflow` → `claude -p`).
- `DeterministicCampaignResolver` (`meta.campaign`) runs the same steps
  in pure code by chaining registry primitives
  (`classify-campaign-path` → `campaign-advance` →
  `resolve-submit-inputs` → `submit-flow`) and **halts-and-parks** on
  any genuine judgement (its *residue*: exit 3, escalation surfaced as
  data in the report).

## 2. The bridge — `LlmJudgementResolver` (code first, LLM at the parks)

`hpc_agent._kernel.lifecycle.llm_resolver.LlmJudgementResolver` is the
middle: it wraps any code resolver, and when the inner parks it makes
**one bounded `structured()` call** to adjudicate the residue against a
closed menu of candidate outcomes, feeds the choice back through the
`fields.resolved` channel, and retries the inner resolver. Control flow
stays in your code; the LLM picks from a menu you authored; its
rationale lands as a contract-valid judgement `WorkerDecision`
(`parse_worker_report` enforces the non-empty `why`).

```python
from pathlib import Path

from hpc_agent._kernel.lifecycle.drive import drive_once
from hpc_agent._kernel.lifecycle.llm_resolver import LlmJudgementResolver
from hpc_agent._kernel.lifecycle.structured import get_model
from hpc_agent.meta.campaign.deterministic_resolver import DeterministicCampaignResolver

resolver = LlmJudgementResolver(
    inner=DeterministicCampaignResolver(),     # code decides everything it can
    model=get_model("openai-compat"),          # HPC_AGENT_MODEL_* env config
    menu={
        # Which residue points are adjudicable AT ALL, and the closed
        # candidate list for each. "park" is always offered and honored.
        "path": ["manual", "strategy"],
    },
)

while True:
    code = drive_once(
        Path("~/experiments/tune_lr").expanduser(),
        step_table={"monitor": "monitor-flow", "aggregate": "aggregate-flow"},
        resolver=resolver,
        allow_agent_steps=True,   # "agent" here means YOUR resolver, not a worker
    )
    if code != 0:
        break   # a real park (or terminal stop) — read the printed report
```

Protocol guarantees, all pinned by tests
(`tests/_kernel/lifecycle/test_llm_resolver.py`,
`tests/meta/campaign/test_deterministic_resolver_e2e.py`):

- **A success or non-residue failure passes through untouched** — zero
  LLM calls on the common path.
- **An un-menued residue parks with no LLM call.** Genuine interviews
  (cold-start context, credentials) are not menu-shaped; don't put them
  on the menu and the park reaches you intact.
- **The choice must be on the menu** — `structured()`'s
  `post_validate` rejects anything else and the repair loop feeds the
  error back; an exhausted budget parks gracefully.
- **No-progress guard**: if the inner re-emits the residue the decision
  was supposed to resolve (it ignored `fields.resolved`, or the hint
  wasn't enough), the wrapper parks instead of spinning and spending.
- **The hint never overrides confident code.**
  `DeterministicCampaignResolver` consults `fields.resolved["path"]`
  only when `classify-campaign-path` itself escalated — deterministic
  evidence always wins; the adjudication exists to break the tie code
  could not.

Multi-shot decisions: implement your own `JudgementResolver` (or
`apply_decision`) and own the `messages` list across turns —
`structured()`'s `ChatModel` protocol is messages-in/completion-out, so
conversation state belongs to your loop.

## 3. The pure-CLI seam — escalation-as-data from any language

No Python required: every judgement point is enumerated and typed, so a
shell/Go/Rust loop can drive `hpc-agent` verbs and consult an LLM only
when an envelope says so.

- `DECISION_POINTS` (`hpc_agent/_wire/spawn_contract.py`) enumerates
  each workflow's choice points and tags each `decided_by: "code"`
  (a primitive computes it — call the verb) or `"judgement"` (the
  genuinely-LLM tail: `axis_class`, `resubmit`, `partial_handling`,
  campaign `path`/`decide`/`concurrency`).
- Primitives return **escalation-as-data**: `Escalation{decided_by,
  reason, failure_features, candidate_actions}` on held failure
  clusters, `needs_decision`/`stage_reached` refusals
  (`resolve-submit-inputs`, `submit-pipeline`'s `parents_not_ready`),
  `safe_default` on `decide-resubmit`. `candidate_actions` is the same
  closed-menu idea as the bridge's `menu` — the LLM picks, your code
  acts (e.g. `resubmit --spec` with the chosen overrides).

The integrator workflow (`find-prior-run` → `submit` →
`monitor-summary` → `verify-aggregation-complete`) is documented in
[`../integrations/CONTRACT.md`](../integrations/CONTRACT.md); the JSON
envelope and exit codes in
[`../reference/cli-spec.md`](../reference/cli-spec.md).

## 4. Detach-by-contract — no LLM in the connection loop

The seams above keep the LLM out of *control flow*; this one keeps it out
of the *connection loop*. The recent cluster ban traced to an LLM
**driving SSH**: a `claude -p --bare` worker was spawned to run a
wait-until-terminal poll; the worker auto-backgrounds at 2 min, ends its
turn mid-poll (so the run reports "no report"), and a fallback inline
subagent retries SSH in prose for ~21 min. The composite the worker was
driving already runs the whole poll loop in plain code with one process
owning the connection — the `infra/retry.py` principle. The miss was the
drive layer sitting an LLM on top of it. (The old opt-in `run --workflow
status --detached` launcher that first fixed this for the `status` path
has since been removed along with the `run --workflow` spawn transport;
the surviving, generalized mechanism is the block-verb path below.)

**Detach-by-contract** runs each cluster-bound submit block as a DETACHED
`hpc-agent <verb>` subprocess — **not** a `claude -p` worker — that owns
the connection and runs to terminal, while the orchestrator learns the
outcome by **reading the journal**. This is DPDispatcher's "submit and
poke until they finish" loop / jobflow-remote's Runner daemon, applied to
the drive layer. The blocks whose wall-clock is cluster-bound
(`docs/design/human-amplification-blocks.md` §3, "Blocks never block the
chat") are the S2 canary-wait, the S3 main-array watch, the speculative
canary, and the S4 harvest — `detached.SUPPORTED_DETACHED_BLOCK_VERBS =
{submit-s2, submit-s3, submit-s4, submit-speculate}`. The parent verb runs
its synchronous gate + drift guards, forces the spec's `detach` field OFF,
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
**outlives the orchestrator** — the crash that killed the auto-backgrounded
`submit-pipeline` ~1s after qsub in 0.10.63 no longer kills the poll. The
child's stdout/stderr (the composite envelope) is captured to `log_path`
for a post-mortem that never re-opens SSH. The launch is idempotent-single:
a filesystem lease keyed by `(run_id, block)` refuses a second LIVE worker
for the same key (`DetachedLeaseHeld`) while self-healing on a dead pid —
the proving-run-#2 race where two `submit-s2` workers hit one run.

Implementation: `hpc_agent._kernel.lifecycle.detached`
(`launch_submit_block_detached`, `_spawn_detached`, the lease/pid guards) +
`hpc_agent.state.journal_poll`. Wired into the submit blocks in
`ops/submit_blocks.py` / `ops/submit_speculate.py`. Pinned by
`tests/_kernel/lifecycle/test_detached_drive.py`.

`build_status_pipeline_spec` survives in the same module as the pure
`status-pipeline` spec builder (run fields → the composite's spec dict, no
LLM renders it), pinned by `tests/integration/test_spec_contract.py`.

## Composing with the DAG kernel

For multi-stage pipelines, the same loop walks the run graph:
`hpc-agent dag-frontier` returns the complete-runs frontier (which
nodes' parents are all terminal); your code fires `submit-pipeline` per
ready node (it composes `validate-parents-ready` mechanically); LLM
calls happen only where a node's submit genuinely escalates. Note for
contributors: recorded walks of exactly this shape are the evidence
[`../design/dag-kernel.md`](../design/dag-kernel.md)'s earn-it rule
requires before any framework-side graph runner is considered — if you
build this loop, keep the tick records.

## Cost model

| Path | LLM spend per tick |
|---|---|
| `monitor` / `aggregate` ticks (`kind: "cli"`) | zero |
| `decide` / `submit` ticks, code-resolvable | zero |
| a menued residue | one `structured()` call (+ bounded repairs) |
| an un-menued residue | zero — parked to you |

Compare the default resolver: every judgement tick spawns a full agent
worker with the rendered procedure prompt. The bridge replaces that
with at most one schema-constrained completion per *genuine* judgement.
