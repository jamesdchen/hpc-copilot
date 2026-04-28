# Integrating claude-hpc into MARs

Status: **proposal** — claude-hpc is a standalone tool today; this document
describes a low-friction path for the MARs maintainer to adopt it as the
cluster-execution backend for Tier-2 runs. No MARs code changes have been
made.

## What this is

claude-hpc is a parameter-grid HPC orchestrator with a JSON-in/JSON-out
CLI (`hpc-mapreduce`). It plugs into MARs's `experiment-runner` agent via
the existing `Bash` tool — no new agent type, no plugin API. The proposal:

- **Tier-1 probes stay local.** `uv run python probe.py` is unchanged.
- **Tier-2 runs that exceed local capacity (large grid, GPU, walltime > N min)
  delegate to `hpc-mapreduce`.** Otherwise Tier-2 also stays local.
- The agent decides per-run whether to delegate. claude-hpc is opt-in.

## Adoption cost (what the MARs maintainer changes)

1. Add `claude-hpc` to MARs's `pyproject.toml` so `uv run hpc-mapreduce …`
   works inside the experiment venv:

   ```bash
   uv add claude-hpc
   ```

2. Append the cluster-execution section from
   [`docs/mars/experiment-runner.snippet.md`](mars/experiment-runner.snippet.md)
   to `agents/experiment-runner.md`. Verbatim paste — no rewrite needed.

3. Forward SSH credentials and a couple of env vars when the agent is
   spawned. See the Bun.spawn block below.

That's it. No directory restructuring, no changes to `meta.json`, no
changes to the `results/metrics.json` schema.

## Bun.spawn env block

`Bun.spawn`'s default env is empty unless `env: …` is passed explicitly.
Without `SSH_AUTH_SOCK`, every cluster call hangs on auth — this is the
single most common spawn failure for orchestrators.

```typescript
import { spawn } from "bun";

const proc = spawn({
  cmd: ["uv", "run", "hpc-mapreduce", "preflight", "--cluster", "hoffman2"],
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

`hpc-mapreduce capabilities` returns `data.required_env` so MARs can
introspect the required forwards without parsing this doc.

## Honoring MARs invariants

| MARs rule | claude-hpc behavior |
|---|---|
| `uv run` for all Python | The integration runs `uv run hpc-mapreduce …` inside MARs's venv. Cluster-side dispatch honors the invariant when callers set `runtime: "uv"` on the submit spec — `build_task_manifest(runtime="uv")` prefixes every task `cmd` with `uv run`, and the four shipped templates run a `uv sync` preamble gated on `HPC_RUNTIME=uv`. See `docs/cli-spec.md` § submit. |
| Tier-1 = `probe.py` only | The agent snippet routes Tier-1 to `uv run python probe.py` directly; claude-hpc is never invoked for probes. |
| Tier-2 entrypoints under `scripts/` | `hpc-mapreduce discover --experiment-dir <run-NNN>` finds `scripts/*.py` (it scans `executors/`, `scripts/`, `src/` today; a `meta.json`-aware filter to skip `src/` is a follow-up). |
| `meta.json` is authoritative for `experiment_id` and `seed=42` | The agent reads `meta.json` first and threads `--seed 42` (and any experiment params) through the grid spec. claude-hpc treats them as ordinary CLI flags. |
| Output to `results/metrics.json` with the canonical schema | Per-task outputs go to `results/metrics.<task_id>.json` (or whatever the executor writes). After `aggregate`, the agent reads `<experiment-dir>/_aggregated/<run_id>/` and assembles `results/metrics.json` in MARs's schema. |
| Deterministic seed, single-output convention | Untouched — claude-hpc has no opinion on these. |

## Error code → MARs retry policy

Source of truth: [`/slash_commands/errors.py`](../slash_commands/errors.py).

| `error_code` | `category` | `retry_safe` | What MARs's runner should do |
|---|---|---|---|
| `ssh_unreachable` | network | true | **Halt-and-prompt.** Don't loop; the agent socket is missing or the host is unreachable. Re-run preflight after operator fix. |
| `scheduler_throttled` | cluster | true | Backoff (1s → 2s → 4s, max 4 retries). Schedulers cap at ~1/sec. |
| `cluster_timeout` | cluster | true | Backoff (4s → 8s → 16s, max 3 retries). Likely NFS stall. |
| `combiner_failed` | cluster | true | Single retry after inspecting `stderr_tail`; if it persists, surface to operator. |
| `remote_command_failed` | cluster | false | Surface to operator with `stderr_tail`. Don't auto-retry. |
| `manifest_invalid` | user | false | Surface; the spec is wrong. The agent must regenerate it. |
| `executor_not_found` | user | false | Surface; the executor path is wrong. |
| `cluster_unknown` | user | false | Surface; the cluster name is wrong. Run `clusters list` to recover. |
| `config_invalid` | user | false | Surface; clusters.yaml or hpc.yaml is malformed. |
| `journal_corrupt` | internal | false | Surface; investigate `$HPC_JOURNAL_DIR`. |
| `internal` | internal | false | Surface; bug report. |

Exit codes: `0` ok, `1` user error, `2` cluster/network error, `3` internal.

## Troubleshooting: silent hangs on the first cluster call

This is the single most common failure when MARs spawns `hpc-mapreduce`.

1. From the same env MARs spawns with, run:
   ```bash
   uv run hpc-mapreduce preflight --cluster <your_cluster>
   ```
   Inspect `data.checks[]` for `ssh_auth_sock`, `cluster_tcp_22`.

2. If `ssh_auth_sock` is `false`: the spawn env is missing `SSH_AUTH_SOCK`.
   Update the `env:` block in `Bun.spawn` to forward
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

- **Cancel / abort.** claude-hpc deliberately does not kill cluster jobs
  (`settings.json` denies `scancel`/`qdel`). If MARs decides an
  experiment is bad, stop waiting; cluster jobs run to walltime. This is
  a permanent design choice — do not work around it.
- **Modifying any file in the MARs repo.** This proposal is changes to
  claude-hpc plus one paste into MARs's `agents/experiment-runner.md`.

## Open items for discussion with the MARs maintainer

These are the substantive code items that depend on maintainer feedback,
deferred until adoption is agreed:

1. **`uv run` in the cluster-side dispatch script and job templates.** Today
   templates use plain `python3 -m …`. A `runtime: "uv"` field in the
   spec / hpc.yaml would emit `uv run python …` on the cluster after a
   `uv sync` preamble. Requires uv to be installed on the cluster.
2. **`meta.json`-aware discovery filter.** When `meta.json` exists, treat
   the directory as MARs Tier-2 and exclude `src/` from executor scanning
   (per MARs's "src is modules, not entrypoints" contract).
3. **`results/metrics.json` aggregation adapter.** A documented combiner
   pattern that produces MARs's canonical metrics.json schema directly,
   so the agent doesn't post-process `_aggregated/<run_id>/`.

## References

- CLI contract: [`docs/cli-spec.md`](cli-spec.md)
- Reference snippet for `agents/experiment-runner.md`:
  [`docs/mars/experiment-runner.snippet.md`](mars/experiment-runner.snippet.md)
- claude-hpc README: [`../README.md`](../README.md)
- MARs experiment-runner agent (current):
  https://github.com/FredFang1216/MARs/blob/main/agents/experiment-runner.md
