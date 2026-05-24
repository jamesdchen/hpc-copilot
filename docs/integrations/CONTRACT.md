# Integration contract

The wire surface external orchestrators (any agent harness that calls
`hpc-agent` via a shell tool) compose against. This document is
integrator-agnostic: nothing here names a specific consumer. If you're
building a new harness on top of hpc-agent, read this file plus
[`docs/reference/cli-spec.md`](../reference/cli-spec.md) and you have
the full surface.

## Spawn environment

hpc-agent is invoked as a subprocess. The integrator forwards a small
set of env vars from its own shell:

| Caller-side env var | What it does |
|---|---|
| `SSH_AUTH_SOCK` | Path to the ssh-agent socket. Cluster-touching subcommands fail fast with `error_code: "ssh_unreachable"` (exit 2) when missing — they do not hang on auth. |
| `SSH_AGENT_PID` | Companion to `SSH_AUTH_SOCK`. Forward both. |
| `HPC_JOURNAL_DIR` | Per-harness journal root (defaults to `~/.claude/hpc/`). Set to an isolated path (e.g. `~/.<harness>/hpc/<run_id>/`) so concurrent harness runs don't share state, and so the integrator's journal doesn't collide with an interactive Claude Code session against the same repo. |
| `HPC_CLUSTERS_CONFIG` | Optional override of `clusters.yaml`. Useful when the harness ships its own cluster catalog rather than the package default. |
| `HPC_SSH_TIMEOUT_SEC` | Optional override of the per-call SSH/scp timeout. Raise on slow login nodes. |
| `HPC_TELEMETRY_SINK` | Optional. One of `none` / `stderr-jsonl` / `monitor-jsonl`. |

Spawn-helper APIs (`Bun.spawn`, Python `subprocess.run(env=...)`,
Rust `Command::env_clear`) typically start with an empty env unless
told otherwise. Forward `process.env` (or the equivalent) explicitly,
or at minimum the variables above plus `PATH`.

## Stdout envelope

Every subcommand writes exactly one line of JSON to stdout. Two
shapes, discriminated by `ok`:

```json
{"ok": true, "idempotent": <bool>, "data": {...}}
```

Optional top-level `partial_errors: [{code, detail}, ...]` when the
operation succeeded but a sub-system was degraded
(e.g. `qhost_failed`, `scontrol_failed`).

```json
{
  "ok": false,
  "error_code": "<one of 15>",
  "message": "<human-readable>",
  "category": "user|cluster|network|internal",
  "retry_safe": <bool>,
  "remediation": "<optional>"
}
```

Exit codes: `0` ok, `1` user error, `2` cluster/network, `3`
internal. Dispatch on exit code BEFORE parsing JSON if you want a
cheap pre-check; the envelope is the full story.

Full schema: `hpc_agent/schemas/envelope.json`. JSON Schema 2020-12.

## Workflow: submit → monitor → aggregate → verify

The minimum loop for a one-shot fan-out:

1. **`hpc-agent preflight --cluster <name>`** — verify SSH agent and
   cluster reachability before anything else.
2. **`hpc-agent find-prior-run --cmd-sha <sha>`** — resume detection.
   Returns the most-recent matching `run_id` (or empty data) so the
   integrator can branch on resume-vs-fresh before submitting.
3. **`hpc-agent submit --spec <path>`** — emit a `run_id`. Pass
   `--dry-run` to validate the spec without writing journal state.
4. **`hpc-agent verify-canary --canary-run-id <id> --wait-budget-sec <n>`** —
   gate before fan-out: wait for a 1-task canary to clear,
   grep for outputs, return whether to proceed.
5. **`hpc-agent monitor-summary --run-id <id>`** — canonical
   user-facing tick summary; byte-stable framing.
6. **`hpc-agent status --run-id <id>`** — one-shot snapshot. Poll on
   the integrator's own cadence.
7. **`hpc-agent failures --run-id <id>`** — per-fingerprint failure
   breakdown plus `preempted_count` / `preempted_task_ids` for
   selective resubmit.
8. **`hpc-agent logs --run-id <id> --task-ids <ids> --lines <n>`** —
   raw stdout/stderr tail for human inspection.
9. **`hpc-agent resubmit --run-id <id> --spec <path>`** — relaunch a
   subset (e.g. `--category preempted`, `--all-failed`, or explicit
   `--task-ids`).
10. **`hpc-agent aggregate --run-id <id> --wave <N>`** — combiner +
    rsync pull, per wave.
11. **`hpc-agent verify-aggregation-complete --run-id <id> --combiner-dir <dir>`** —
    all-waves-combined / all-tasks-present / no-cross-run-contamination
    invariant check.
12. **`hpc-agent reconcile --run-id <id>`** — reconcile journal vs
    scheduler when the cluster diverges from local belief.

One extra primitive is useful but not required for the basic loop:

- **`hpc-agent clusters list`** — discoverable cluster catalog.

## `error_code` → retry policy

Source of truth: `src/hpc_agent/errors.py`. Full list also in
[`docs/reference/cli-spec.md`](../reference/cli-spec.md).

| `error_code` | `category` | `retry_safe` | Recommended action |
|---|---|---|---|
| `ssh_unreachable` | network | true | **Halt-and-prompt.** Don't loop; the agent socket is missing or the host is unreachable. Re-run `preflight` after operator fix. |
| `scheduler_throttled` | cluster | true | Backoff (1s → 2s → 4s, max 4 retries). Schedulers cap at ~1/sec. |
| `cluster_timeout` | cluster | true | Backoff (4s → 8s → 16s, max 3 retries). Likely NFS stall. |
| `combiner_failed` | cluster | true | Single retry after inspecting `stderr_tail`; if it persists, surface to operator. |
| `preempted` | cluster | true | **Resubmit immediately.** The job was bumped (not failed) by higher-priority work. `failures` surfaces `preempted_count` and `preempted_task_ids` for selective resubmit. |
| `cluster_partially_degraded` | cluster | true | Inspect top-level `partial_errors`. Continue polling; the cluster is responding but some sub-system (qhost, sacct) is timing out. |
| `remote_command_failed` | cluster | false | Surface to operator with `stderr_tail`. Don't auto-retry. |
| `spec_invalid` | user | false | Surface; the spec is wrong. Regenerate it. |
| `executor_not_found` | user | false | Surface; the executor path is wrong. |
| `cluster_unknown` | user | false | Surface; run `clusters list` to recover. |
| `config_invalid` | user | false | Surface; clusters.yaml is malformed. |
| `outputs_missing` | user | false | Surface; the executor produced no per-task outputs. Inspect logs. |
| `journal_corrupt` | internal | false | Surface; investigate `$HPC_JOURNAL_DIR`. |
| `schema_incompat` | internal | false | Surface; the sidecar / runtime-prior schema version isn't supported by this hpc-agent. Pin hpc-agent and the cluster runtime to compatible versions. |

## The `.hpc/tasks.py` boundary

The integrator writes the task definition. hpc-agent owns the
*protocol* (interface, sidecar schema, SSH plumbing, dispatcher);
the integrator owns the *content* (which experiment, what parameter
axes, what kwargs each task receives). The bridge is a single
user-written file in the experiment repo:

```
<experiment-dir>/.hpc/tasks.py        # integrator writes this; hpc-agent imports it
<experiment-dir>/.hpc/runs/<id>.json  # hpc-agent writes this each submit
```

`.hpc/tasks.py` exposes exactly two callables:

```python
def total() -> int:
    """How many tasks this experiment fans out into."""

def resolve(task_id: int) -> dict:
    """Return the kwargs for task #i. Eager-materialized (see below)."""
```

The **eager-materialization convention** —
`_TASKS = [...]` at module load, `total()` returns `len(_TASKS)`,
`resolve(i)` indexes — gives free `cmd_sha` derivation, submit-time
error catching, and laptop-side inspectability. The canonical
reference at
[`tasks_example.py`](../../src/hpc_agent/models/mapreduce/templates/scaffolds/tasks_example.py)
shows three usage patterns inline (Cartesian product, chunking by row
count, date-window backtests). Pick whichever matches your sweep.

hpc-agent is experiment-agnostic by design. Only the agent that
proposed the experiment knows what its parameter sweep should look
like; pushing the parameter-shape decision into hpc-agent would force
every new experiment kind into a framework upgrade. The integrator
owns it.

## Executor import boundary

Templates copied into experiment repos may import from a narrow
allowlist of "runtime modules" that `deploy_runtime` stages on the
compute node alongside the executor. The current allowlist:

- `hpc_agent.models.mapreduce.metrics_io.write_metrics` — per-task sidecar
  writer. Stdlib-only.
- `hpc_agent.models.mapreduce.metrics_io.read_kw_env` — kwargs-from-env
  helper for executors that consume the dispatcher's `HPC_KW_*`
  exports.
- `hpc_agent.executor_cli.flag` — single-flag declaration helper for
  the new pure-`compute(args)` contract.
- `hpc_agent.executor_cli.generic_args` /
  `hpc_agent.executor_cli.gpu_args` — common arg-group bundles.
- `hpc_agent.executor_cli.build_parser_from_flags` — argparse
  builder for the auto-generated `.hpc/cli.py`.

Nothing else from `hpc_agent` is importable from
`hpc_agent/models/mapreduce/templates/**`. The boundary is enforced by
`tests/test_boundary_contract.py`. To extend it, the new module must
(a) be deployed by `deploy_runtime`, (b) be stdlib-only or
self-contained, and (c) be added to both the allowlist constant in
the lint test and this doc in the same PR.

## Dispatcher-side env vars (don't rename)

The dispatcher exports the following on every task; executors read
them as ordinary env vars. These names are part of the contract and
must not change across releases:

| Env var | What it is |
|---|---|
| `RESULT_DIR` | Per-task `_wip_<task_id>/` tempdir that atomically promotes to the final dir on exit-0. Write outputs here. |
| `HPC_KW_<KEY>` | One per kwarg returned by `tasks.resolve(task_id)`. Uppercased key, JSON-encoded value. The legacy bare-uppercase form `<KEY>=<value>` is exported by default for back-compat; set `HPC_KW_NAMESPACE_ONLY=1` to disable. |
| `LOCAL_DATA_DIR` | Optional cluster-side data root. Templates honor it when set; executors that read data files key off it. |
| `HPC_TASK_ID` | 0-based task index. |
| `HPC_RUN_ID` | The current run_id. Locates `.hpc/runs/<run_id>.json`. |
| `HPC_CAMPAIGN_ID` | Optional. When set, marks the run as part of a closed-loop campaign. The user's `tasks.py` can read this to call `hpc_agent.models.mapreduce.reduce.history.prior(experiment_dir, campaign_id)` for prior iterations. |
| `HPC_RUNTIME` | Optional. When `uv`, the template runs `uv sync` before dispatch. |

Constants are also exposed as Python attributes under
`hpc_agent.integration`:

```python
from hpc_agent.integration import (
    RESULT_DIR_ENV,
    HPC_KW_PREFIX,
    LOCAL_DATA_DIR_ENV,
    JOURNAL_DIR_ENV,
    CLUSTERS_CONFIG_ENV,
    LIFECYCLE_STATES,
    ERROR_CODES,
)
```

Import these instead of copy-pasting strings.

## `lifecycle_state` values

The terminal/observable values returned by status / monitor / reconcile:

| Value | Meaning |
|---|---|
| `in_flight` | Submitted, monitoring active. The default. |
| `complete` | Terminal. Every task reported complete and any combiner waves finished. |
| `failed` | Terminal. At least one failure with nothing running/pending. |
| `timeout` | Terminal. Wall-clock budget exceeded; cluster jobs may still be running. |
| `abandoned` | Terminal. Recorded `job_ids` no longer known to the scheduler. |

hpc-agent deliberately does not kill cluster jobs (`settings.json`
denies `scancel`/`qdel`). If the integrator decides a run is bad,
stop polling and let it expire.

## Cancel / abort: out of scope

hpc-agent has no cancel primitive. Cluster jobs run to walltime.
This is a permanent design choice — do not work around it. If your
loop needs to abandon a run, simply stop polling; the journal records
mark themselves `abandoned` on reconcile when the scheduler no longer
knows about the run.

## Capabilities introspection

```bash
hpc-agent capabilities       # JSON envelope: subcommands, schemas dir, required env, …
hpc-agent capabilities --full  # multi-section text dump: catalog + every primitive doc + schemas + error remediations
```

Use `--full` for one-shot LLM context loading. Use the JSON form to
gate features programmatically (e.g. "does this install support
`summarize-submit-plan`?"); the `subcommands` array is the authoritative
list.

## See also

- [`docs/reference/cli-spec.md`](../reference/cli-spec.md) — envelope shape and exit-code contract.
- [`docs/reference/agent-surface.md`](../reference/agent-surface.md) — design rationale for the POSIX-native surface.
- [`docs/reference/boundary-contract.md`](../reference/boundary-contract.md) — what hpc-agent owns vs. what experiment repos own.
- [`docs/reference/env-vars.md`](../reference/env-vars.md) — every `HPC_*` env var the framework reads.
- [`docs/primitives/`](../primitives/) — one file per subcommand.
