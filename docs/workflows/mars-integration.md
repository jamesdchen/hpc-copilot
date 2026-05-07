# Integrating claude-hpc into MARs

Status: **contract** — claude-hpc commits to maintain the wire surface
described here through the end-of-quarter MARs integration. The CLI
binary, JSON envelope shape, exit codes, and error-code semantics are
stable; additive enum entries (new error codes, new primitives) may
land but never with breaking changes to the existing fields.

## What this is

claude-hpc is a parameter-grid HPC orchestrator with a JSON-in/JSON-out
CLI (`hpc-agent`). It plugs into MARs's `experiment-runner` agent
via the existing `Bash` tool — no new agent type, no plugin API. The
adoption split:

- **Tier-1 probes stay local.** `uv run python probe.py` is unchanged.
- **Tier-2 runs that exceed local capacity** (large grid, GPU, walltime > N min)
  delegate to `hpc-agent`. Otherwise Tier-2 also stays local.
- The agent decides per-run whether to delegate. claude-hpc is opt-in.

## Adoption cost (what the MARs maintainer changes)

1. Add `claude-hpc` to MARs's `pyproject.toml` so `uv run hpc-agent …`
   works inside the experiment venv:

   ```bash
   uv add claude-hpc
   ```

2. Append the cluster-execution section from
   [`docs/workflows/mars/experiment-runner.snippet.md`](mars/experiment-runner.snippet.md)
   to `agents/experiment-runner.md`. Verbatim paste — no rewrite needed.

3. Forward SSH credentials and a couple of env vars when the agent is
   spawned. See the `Bun.spawn` block below.

That's it. No directory restructuring, no changes to `meta.json`, no
changes to the `results/metrics.json` schema.

## `Bun.spawn` env block

`Bun.spawn`'s default env is empty unless `env: …` is passed
explicitly. Without `SSH_AUTH_SOCK`, every cluster call hangs on auth —
this is the single most common spawn failure for orchestrators
delegating to claude-hpc.

```typescript
import { spawn } from "bun";

const proc = spawn({
  cmd: ["uv", "run", "hpc-agent", "preflight", "--cluster", "hoffman2"],
  cwd: experimentDir,                  // e.g. experiments/runs/run-042-…
  env: {
    ...process.env,                    // critical: forward parent env
    SSH_AUTH_SOCK: process.env.SSH_AUTH_SOCK ?? "",
    SSH_AGENT_PID: process.env.SSH_AGENT_PID ?? "",
    HPC_JOURNAL_DIR: `${process.env.HOME}/.mars/hpc`,   // per-MARs-run, not shared
    HPC_CLUSTERS_CONFIG: "/path/to/your/clusters.yaml", // optional override
    PATH: process.env.PATH ?? "",
  },
  stdout: "pipe",
  stderr: "pipe",
});

const stdout = await new Response(proc.stdout).text();
const envelope = JSON.parse(stdout);
// envelope: { ok: true, idempotent: true, data: {...} } or
//           { ok: false, error_code, category, retry_safe, remediation, message }
```

`hpc-agent capabilities` returns `data.required_env` so MARs can
introspect the required forwards without parsing this doc. For the
full API surface in one tool call, use `hpc-agent capabilities --full`
(returns a single text blob with the catalog plus every primitive doc,
schemas, envelope shape, and error remediation strings).

## The `.hpc/tasks.py` boundary — MARs writes the task definition

This is the most important section of this doc and the property that
keeps claude-hpc reusable across experiments.

**The split is**: claude-hpc owns the *protocol* (the `total()` /
`resolve(task_id)` interface, the per-run sidecar schema, the SSH
plumbing, the WIP/atomic-promote dispatcher); **MARs owns the
*content*** (which experiment, what parameter axes, what kwargs each
task should receive). The bridge between them is a single user-written
file in the experiment repo:

```
<experiment-dir>/.hpc/tasks.py        # MARs writes this; claude-hpc imports it
<experiment-dir>/.hpc/runs/<id>.json  # claude-hpc writes this each submit
```

`.hpc/tasks.py` exposes exactly two callables:

```python
def total() -> int:
    """How many tasks this experiment fans out into."""

def resolve(task_id: int) -> dict:
    """Return the kwargs for task #i. Eager-materialized (see below)."""
```

The eager-materialization convention — `_TASKS = [...]` at module load,
`total()` returns `len(_TASKS)`, `resolve(i)` indexes — gives free
`cmd_sha` derivation, submit-time error catching, and laptop-side
inspectability. The canonical reference at
`claude_hpc/mapreduce/templates/tasks_example.py` shows three usage
patterns inline (Cartesian product, chunking by row count,
date-window backtests) — MARs picks whichever matches the experiment
it just proposed.

### Why MARs and not claude-hpc

claude-hpc is **experiment-agnostic by design**. It cannot know what a
new experiment's parameter sweep should look like — only the agent
that proposed the experiment has that context. Putting the
parameter-shape decision in claude-hpc would force every new
experiment kind into a framework upgrade. Putting it in MARs (or
whatever meta-agent is driving) keeps claude-hpc reusable.

### Recommended MARs flow

When MARs proposes a new Tier-2 experiment, it scaffolds three files
in the experiment dir as a single atomic step (one git commit):

1. **`meta.json`** — experiment_id, seed, purpose, the parameter
   axes/values MARs decided to sweep.
2. **`scripts/<entrypoint>.py`** — the executor that consumes one
   task's kwargs and writes per-task outputs.
3. **`.hpc/tasks.py`** — the materialized `_TASKS = [...]` translating
   `meta.json`'s axes into per-task kwarg dicts.

Concrete example. Say MARs proposes a hyperparameter sweep with
`{lr: [0.01, 0.001], seed: [42, 1337]}`. The agent writes:

```python
# .hpc/tasks.py — written by MARs once at experiment-proposal time
import itertools
_TASKS = [
    {"lr": lr, "seed": seed}
    for lr, seed in itertools.product([0.01, 0.001], [42, 1337])
]
def total() -> int:
    return len(_TASKS)
def resolve(i: int) -> dict:
    return _TASKS[i]
```

Then submits via `hpc-agent submit`. The cluster-side dispatcher
imports this `tasks.py` at task time, calls `resolve(task_id)` to get
the kwargs, exports them as env vars (uppercased + `HPC_KW_*`), and
runs the executor command from the per-run sidecar.

### Where to teach MARs about this

The MARs agent doc (`agents/experiment-runner.md`) should include —
just above the `hpc-agent submit` invocation — a step that:

1. Reads the canonical example via
   `python -c 'from claude_hpc import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "mapreduce" / "templates" / "tasks_example.py")'`.
2. Drafts the experiment's `_TASKS` list from `meta.json`'s parameter
   axes.
3. Writes `.hpc/tasks.py` and commits it alongside `meta.json`.
4. Verifies locally with
   `python -c 'from claude_hpc import load_tasks_module, tasks_path; m = load_tasks_module(tasks_path(".")); print("total=", m.total(), "sample=", m.resolve(0))'`
   before the first `submit` call.

If `.hpc/tasks.py` is already present (a re-run, a manual edit), MARs
**does not regenerate** — it inspects the existing file, computes
`total()` to confirm cardinality matches expectations, and proceeds.
That's the only way reuse-across-experiments stays clean.

### What stays in `meta.json` vs. what moves to `tasks.py`

- `meta.json` keeps experiment-level metadata (experiment_id, seed,
  purpose, what was tried, what was learned). It is **declarative**;
  it describes the experiment.
- `.hpc/tasks.py` is **operational** — the materialized fan-out shape
  the cluster dispatcher consumes. MARs may regenerate it from
  `meta.json` if the parameter axes change between proposals (a fresh
  experiment id), but on a re-run of the same experiment_id the file
  is read as-is.

There is no schema drift: `meta.json` is the human-readable summary
and the source the agent reads when scaffolding; `.hpc/tasks.py` is
the machine-readable sweep definition the framework consumes. They are
both git-tracked and diffable.

## Honoring MARs invariants

| MARs rule | claude-hpc behavior |
|---|---|
| `uv run` for all Python | The integration runs `uv run hpc-agent …` inside MARs's venv. Cluster-side dispatch honors the invariant when callers set `runtime: "uv"` on the submit spec — the agent then writes the per-run sidecar's `executor` field as `uv run python ...`, and the four shipped templates run a `uv sync` preamble gated on `HPC_RUNTIME=uv`. See `docs/reference/cli-spec.md` § submit. |
| Tier-1 = `probe.py` only | The agent snippet routes Tier-1 to `uv run python probe.py` directly; claude-hpc is never invoked for probes. |
| Tier-2 entrypoints under `scripts/` | `hpc-agent discover --experiment-dir <run-NNN>` finds `scripts/*.py` (it scans `executors/`, `scripts/`, `src/` today; a `meta.json`-aware filter to skip `src/` is a follow-up). |
| `meta.json` is authoritative for `experiment_id` and `seed=42` | The agent reads `meta.json` first and threads `--seed 42` (and any experiment params) through the grid spec. claude-hpc treats them as ordinary CLI flags. |
| Output to `results/metrics.json` with the canonical schema | Per-task outputs go to `results/metrics.<task_id>.json` (or whatever the executor writes). After `aggregate`, the agent reads `<experiment-dir>/_aggregated/<run_id>/` and assembles `results/metrics.json` in MARs's schema. |
| Deterministic seed, single-output convention | Untouched — claude-hpc has no opinion on these. |

## Error code → MARs retry policy

Source of truth: [`src/claude_hpc/errors.py`](../src/claude_hpc/errors.py).
The full enum is also documented in [`docs/reference/cli-spec.md`](cli-spec.md).

| `error_code` | `category` | `retry_safe` | What MARs's runner should do |
|---|---|---|---|
| `ssh_unreachable` | network | true | **Halt-and-prompt.** Don't loop; the agent socket is missing or the host is unreachable. Re-run preflight after operator fix. |
| `scheduler_throttled` | cluster | true | Backoff (1s → 2s → 4s, max 4 retries). Schedulers cap at ~1/sec. |
| `cluster_timeout` | cluster | true | Backoff (4s → 8s → 16s, max 3 retries). Likely NFS stall. |
| `combiner_failed` | cluster | true | Single retry after inspecting `stderr_tail`; if it persists, surface to operator. |
| `preempted` | cluster | true | **Resubmit immediately.** The job was bumped (not failed) by higher-priority work. claude-hpc surfaces `preempted_count` and `preempted_task_ids` in `cmd_failures` output for selective resubmit. |
| `cluster_partially_degraded` | cluster | true | Inspect top-level `partial_errors` array. Continue the polling loop; the cluster is responding but some sub-system (qhost, sacct) is timing out. |
| `remote_command_failed` | cluster | false | Surface to operator with `stderr_tail`. Don't auto-retry. |
| `spec_invalid` | user | false | Surface; the spec is wrong. The agent must regenerate it. |
| `executor_not_found` | user | false | Surface; the executor path is wrong. |
| `cluster_unknown` | user | false | Surface; the cluster name is wrong. Run `clusters list` to recover. |
| `config_invalid` | user | false | Surface; clusters.yaml is malformed. |
| `outputs_missing` | user | false | Surface; the executor produced no per-task outputs. Inspect logs. |
| `journal_corrupt` | internal | false | Surface; investigate `$HPC_JOURNAL_DIR`. |
| `schema_incompat` | internal | false | Surface; the sidecar / runtime-prior schema version isn't supported by this claude-hpc. Pin claude-hpc and the cluster runtime to compatible versions. |
| `internal` | internal | false | Surface; bug report. |

Exit codes: `0` ok, `1` user error, `2` cluster/network error, `3` internal.

## Surfaces MARs can use directly

These primitives may be useful from the agent loop beyond the basic
submit/status/aggregate cycle:

- **`hpc-agent capabilities --full`** — single-call dump of the
  whole API surface (catalog, every primitive doc, schemas, envelope,
  error codes). Use to load full context without piecemeal discovery.
- **`hpc-agent validate --profile <p> --cluster <c>`** — promotes
  the internal `sbatch --test-only` lattice probe to a top-level
  primitive. Returns `{estimated_start_iso, fits_backfill,
  predicted_eta_sec, scheduler_response}` so the agent can branch on
  timing instead of submitting blind.
- **`hpc-agent predict-queue-wait --profile <p> --cluster <c>`** —
  diurnal moving-average + DES-backed predictor for queue wait
  (cold-start aware; returns `confidence: "cold"` when there isn't
  enough data yet).
- **`hpc-agent best-submit-window --profile <p> --cluster <c>`** —
  sweeps the predictor over the next N hours, returns the top-K
  windows by predicted wait.
- **`hpc-agent campaign-health [--campaign-id <id>]`** — structured
  campaign-health summary including `walltime_cliff_rate`,
  `failure_breakdown`, `gpu_utilization` plus a `suggested_prompt`
  string ready to feed to MARs's own LLM for narrative analysis.
- **`hpc-agent failures --run-id <id>`** — surfaces
  `preempted_count` and `preempted_task_ids` at the data top level so
  MARs can selectively resubmit preempted tasks without re-doing the
  ones that completed.

## Troubleshooting: silent hangs on the first cluster call

This is the single most common failure when MARs spawns `hpc-agent`.

1. From the same env MARs spawns with, run:
   ```bash
   uv run hpc-agent preflight --cluster <your_cluster>
   ```
   Inspect `data.checks[]` for `ssh_auth_sock`, `cluster_tcp_22`.

2. If `ssh_auth_sock` is `false`: the spawn env is missing
   `SSH_AUTH_SOCK`. Update the `env:` block in `Bun.spawn` to forward
   `process.env.SSH_AUTH_SOCK` and `process.env.SSH_AGENT_PID`.

3. If `cluster_tcp_22` is `false`: cluster is offline or hostname is
   wrong. This is operator config, not a MARs bug.

As a defense-in-depth, claude-hpc's `status`, `aggregate`, and
`reconcile` subcommands now fail fast with `error_code:
"ssh_unreachable"` (exit 2) instead of hanging when `SSH_AUTH_SOCK` is
unset. `submit` and `resubmit` are journal-only and do not require an
agent.

## Journal coexistence

MARs has its own experiment journal (`src/paper/experiments/journal.ts`)
tracking experiment-level state. claude-hpc's journal at
`$HPC_JOURNAL_DIR` tracks HPC-run-level state (a per-grid submission
record, not the parent experiment).

Different scopes, no overlap. Two rules:

- Set `HPC_JOURNAL_DIR` per MARs run (e.g.
  `~/.mars/hpc/<experiment_id>/`) so concurrent runs don't share state.
- Don't share `HPC_JOURNAL_DIR` between MARs and an interactive Claude
  Code session — they expect different ownership semantics.

## Out of scope

- **Cancel / abort.** claude-hpc deliberately does not kill cluster
  jobs (`settings.json` denies `scancel`/`qdel`). If MARs decides an
  experiment is bad, stop waiting; cluster jobs run to walltime. This
  is a permanent design choice — do not work around it.
- **Modifying any file in the MARs repo.** This is changes to claude-hpc
  plus one paste into MARs's `agents/experiment-runner.md`.

## Wire-contract changes since the original proposal

For maintainers who reviewed the earlier proposal in April 2026, here's
what's changed that touches a Bun.spawn-style consumer:

- **Package import path**: `hpc_mapreduce` was renamed to `claude_hpc`.
  The CLI binary `hpc-agent` is unchanged, so this only affects
  Python imports inside `.hpc/tasks.py` (the framework helpers
  `load_tasks_module`, `tasks_path`, `_PACKAGE_ROOT`, etc. are now
  imported from `claude_hpc`).
- **Error code rename**: `manifest_invalid` → `spec_invalid`. Wire-
  protocol break, but there were no live consumers.
- **New error codes**: `preempted`, `cluster_partially_degraded`,
  `schema_incompat`. All additive — agents that haven't been updated
  fall through to a default action; recommended retry policies in the
  table above.
- **Schemas additive**: `envelope.json` gained a top-level optional
  `partial_errors` array. Output schemas gained `lifecycle_state =
  "timeout"` (was missing in some schemas). Consumers using
  `additionalProperties: false` validators against the April-era schema
  must re-pin.
- **Primitives**: `record-segv-blacklist` was removed (no consumers).
  New primitives: `validate`, `predict-queue-wait`,
  `best-submit-window`, `campaign-health`. None are required for the
  basic submit/status/aggregate flow.
- **`plan_submit.output.json`** lost `blacklist_active_count` (was
  required) and per-candidate `blacklisted_nodes` (was optional). Only
  consumers that parsed plan-submit output are affected.
- **Per-task sidecar field**: `preempted_at: <iso>` moved under
  `preempt: {at: <iso>, grace_sec: <int>}` for forward-compat. Only
  consumers reading sidecar JSON directly (not through the CLI) are
  affected.

## Open items for discussion with the MARs maintainer

These are the substantive code items that depend on maintainer feedback:

1. **`uv run` in the cluster-side dispatch script and job templates.**
   Today templates use plain `python3 -m …`. The `runtime: "uv"` field
   on the submit spec persists to the run sidecar and emits `uv run
   python …` on the cluster after a `uv sync` preamble — but only when
   uv is installed on the cluster. Confirm this matches MARs's
   deployment expectations.
2. **`meta.json`-aware discovery filter.** When `meta.json` exists,
   treat the directory as MARs Tier-2 and exclude `src/` from executor
   scanning (per MARs's "src is modules, not entrypoints" contract).
3. **`results/metrics.json` aggregation adapter.** A documented
   combiner pattern that produces MARs's canonical metrics.json schema
   directly, so the agent doesn't post-process
   `_aggregated/<run_id>/`.

## References

- CLI contract: [`docs/reference/cli-spec.md`](cli-spec.md)
- POSIX-native agent surface design: [`docs/reference/agent-surface.md`](agent-surface.md)
- Reference snippet for `agents/experiment-runner.md`:
  [`docs/workflows/mars/experiment-runner.snippet.md`](mars/experiment-runner.snippet.md)
- claude-hpc README: [`../README.md`](../README.md)
- MARs experiment-runner agent (current):
  https://github.com/FredFang1216/MARs/blob/main/agents/experiment-runner.md
