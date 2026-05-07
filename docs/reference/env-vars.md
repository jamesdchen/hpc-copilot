# Environment variables

Cross-cutting reference for every `HPC_*` env-var the framework reads.
Set on the local shell before invoking the CLI / slash command;
cluster-side scripts inherit a curated subset (see the per-template
preamble).

## Runtime / behaviour

| Variable | Default | Purpose |
|---|---|---|
| `HPC_CLUSTERS_CONFIG` | `<package>/config/clusters.yaml` | Path to a `clusters.yaml` override. Used by `claude_hpc.infra.clusters.load_clusters_config`. |
| `HPC_JOURNAL_DIR` | `~/.claude/hpc/` | Root of the per-experiment journal tree. MARs and other harnesses set this so their state lives outside the user's `~/.claude`. |
| `HPC_MAX_RUNS` | `500` | Max per-experiment sidecars retained before oldest-by-mtime eviction (`claude_hpc.state.runs`). |
| `HPC_CAMPAIGN_ID` | (unset) | Threaded through to every cluster job by the scheduler templates so `tasks.py` can read the prior iteration's history via `claude_hpc.mapreduce.reduce.history.prior(...)`. |
| `HPC_TELEMETRY_SINK` | `none` | One of `none` / `stderr-jsonl` / `monitor-jsonl`. Routes `claude_hpc._internal.telemetry.record` events. |

## SSH / rsync transport

| Variable | Default | Purpose |
|---|---|---|
| `HPC_SSH_TIMEOUT_SEC` | `60` | Per-call subprocess timeout for `ssh` / `scp` invocations from `claude_hpc.infra.remote`. Raise on slow login nodes; lowering risks false-positive timeouts. |
| `HPC_RSYNC_TIMEOUT_SEC` | `1800` | Per-call subprocess timeout for `rsync` push / pull. Raise when transferring large repos over slow links. |
| `HPC_NO_SSH_MULTIPLEX` | (unset) | Set to `1` to disable OpenSSH connection multiplexing. Some clusters disallow it (e.g. PAM session limits). Without multiplexing, every status poll pays a full SSH handshake. |
| `HPC_SSH_NO_BACKOFF` | (unset) | Set to `1` to disable transient-failure exponential backoff. Used by the test suite when mocking subprocess; production callers should leave this alone. |
| `HPC_SUBMIT_NO_LOCK` | (unset) | Set to `1` to disable the per-cluster submit-flow flock. Allows parallel `submit-flow` calls from different shells against the same cluster — only safe when the operator confirms races are tolerable. |

## Validation thresholds

There are no env-var knobs for validators; per-rule overrides live in
`.hpc/playbook.yaml` (version-controlled, per-project). See
[`config-precedence.md`](config-precedence.md).

## Discovery

Run `hpc-agent capabilities --full` to see the full operations
catalog plus all supported `clusters.yaml` keys (the latter come from
`claude_hpc.infra.clusters.CLUSTER_YAML_KEYS`). Env vars don't appear
there — this doc is the canonical list.
