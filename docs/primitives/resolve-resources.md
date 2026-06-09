---
name: resolve-resources
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent resolve-resources --cluster <cluster> [--experiment-dir <experiment_dir>]
    [--profile <profile>] [--cmd-sha <cmd_sha>] [--walltime-sec <walltime_sec>] [--gpu-type
    <gpu_type>] [--safety-mult <safety_mult>] [--partition <partition>] [--user-preferred-partition
    <user_preferred_partition>] [--mpi-pe <mpi_pe>] [--mpi-ranks <mpi_ranks>]
  python: hpc_agent.ops.resolve_resources.resolve_resources
---
# resolve-resources

Collapses `hpc-submit` SKILL.md Step 6's silent multi-step resource
resolution — runtime-prior walltime + cluster gpu default + partition —
into one CLI verb. The agent's role at Step 6 shrinks to a single tool
call; the three resolutions (each with its own caller-override seam and
auto-resolution rule) stop being prose the agent must remember to walk.

## Inputs / outputs

See `hpc_agent/schemas/resolve_resources.{input,output}.json`. Input
requires only `cluster`. Output carries the three resolved fields
(`walltime_sec`, `gpu_type`, `partition`) plus a `provenance` map
recording HOW each was resolved, so a caller can audit which values were
caller-supplied vs. auto-resolved vs. cold-start.

## The three resolutions

Each field resolves from a caller override first, then an
auto-resolution rule:

- **`walltime_sec`** — caller (`--walltime-sec`), else the optional
  `read-runtime-prior` verb's p95 × `safety_mult` (default 1.30). On
  every cold-start path it stays `null` and the caller falls back to the
  cluster cold-start walltime.
- **`gpu_type`** — caller (`--gpu-type`), else
  `clusters.<cluster>.gpu_types[0]` (the first declared GPU). `null` when
  the cluster declares none. Resolved *before* walltime so the
  runtime-prior probe can select the matching per-gpu quantile row.
- **`partition`** — caller (`--partition`), else delegated verbatim to
  the existing [`recommend-partition`](recommend-partition.md) primitive
  when the caller supplies the cluster's partition list. `null`
  (`no_partitions_supplied`) when no partition config is available.
  Partition routing logic is NOT reimplemented here.

## Cold-start is not an error

`read-runtime-prior` is an **optional-plugin-only** verb. On a core
install (e.g. plain PyPI `hpc-agent`) it is not a registered subcommand,
and on the very first submit there is no prior anyway. The probe treats
ALL of the following as cold-start — `walltime_sec` stays `null`, never
an error:

- the verb is unregistered (argparse "invalid choice", exit 2, non-JSON
  stdout) → `cold_start_prior_verb_unavailable`;
- the subprocess errors or times out → `cold_start_prior_verb_unavailable`;
- the envelope is not `ok` → `cold_start_prior_verb_unavailable`;
- the verb reports `needs_canary` (no samples yet) or returns no matching
  quantile row → `cold_start_no_samples`;
- no `profile` lookup key was supplied → `cold_start_no_profile`.

This mirrors `get_default_walltime_sec` (`hpc_agent.infra.clusters`),
which ALWAYS resolves a usable cold-start walltime — so a core install
never stalls waiting on a feature that may not be installed.

## Provenance

`data.provenance` maps each field to its resolution source:

- `walltime_sec`: `caller` / `prior_p95` / `cold_start_no_profile` /
  `cold_start_no_samples` / `cold_start_prior_verb_unavailable`.
- `gpu_type`: `caller` / `cluster_default` / `cluster_declares_none`.
- `partition`: `caller` / `no_partitions_supplied` /
  `recommend_partition:<rationale>` (carrying recommend-partition's own
  rationale, e.g. `debug_short_walltime`, `debug_overrun_refused`,
  `user_preference_honoured`).

## requires_ssh: False

The only subprocess call is the `read-runtime-prior` probe, which reads
the local on-disk runtime-prior store; `clusters.yaml` is a local file
and `recommend-partition` is a pure local primitive. Nothing here touches
the cluster.

## Why this exists

Step 6 used to be three silent resolution rules the agent walked in
prose — easy to skip the prior probe, mis-handle the missing-plugin case
as an error, or hand-roll partition routing instead of calling
`recommend-partition`. Folding all three into one verb makes the
auto-resolution deterministic and the cold-start contract structural.
