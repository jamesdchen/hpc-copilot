# Environment variables

Cross-cutting reference for every `HPC_*` env-var the framework reads.
Set on the local shell before invoking the CLI / slash command;
cluster-side scripts inherit a curated subset (see the per-template
preamble).

## Runtime / behaviour

| Variable | Default | Purpose |
|---|---|---|
| `HPC_CLUSTERS_CONFIG` | `<package>/config/clusters.yaml` | Path to a `clusters.yaml` override. Used by `hpc_agent.infra.clusters.load_clusters_config`. |
| `HPC_JOURNAL_DIR` | `~/.claude/hpc/` | Root of the per-experiment journal tree. External harnesses set this so their state lives outside the user's `~/.claude`. |
| `HPC_MAX_RUNS` | `500` | Max per-experiment sidecars retained before oldest-by-mtime eviction (`hpc_agent.state.runs`). |
| `HPC_CAMPAIGN_ID` | (unset) | Threaded through to every cluster job by the scheduler templates so `tasks.py` can read the prior iteration's history via `hpc_agent.models.mapreduce.reduce.history.prior(...)`. |
| `HPC_TELEMETRY_SINK` | `none` | One of `none` / `stderr-jsonl` / `monitor-jsonl`. Routes `hpc_agent._kernel.extension.telemetry.record` events. |

## SSH / rsync transport

| Variable | Default | Purpose |
|---|---|---|
| `HPC_SSH_TIMEOUT_SEC` | `60` | Per-call subprocess timeout for `ssh` / `scp` invocations from `hpc_agent.infra.remote`. Raise on slow login nodes; lowering risks false-positive timeouts. |
| `HPC_RSYNC_TIMEOUT_SEC` | `1800` | Per-call subprocess timeout for `rsync` push / pull. Raise when transferring large repos over slow links. |
| `HPC_NO_SSH_MULTIPLEX` | (unset) | Set to `1` to disable OpenSSH connection multiplexing. Some clusters disallow it (e.g. PAM session limits). Without multiplexing, every status poll pays a full SSH handshake. |
| `HPC_SSH_BINARY` | (auto) | Path to the `ssh` binary to invoke. On native Windows, when unset, hpc-agent prefers `C:\Windows\System32\OpenSSH\ssh.exe` over Git Bash's bundled `ssh` (Git's ssh can't reach the Windows OpenSSH named-pipe agent). Elsewhere it falls back to bare `ssh` on `PATH`. Set explicitly to pin a specific binary on any platform. |
| `HPC_SCP_BINARY` | (auto) | As `HPC_SSH_BINARY`, for `scp` (prefers `C:\Windows\System32\OpenSSH\scp.exe` on Windows when present). |
| `RSYNC_RSH` | (auto) | Standard rsync variable naming the remote shell. hpc-agent sets it to the resolved `HPC_SSH_BINARY` for rsync transfers when that isn't the bare `ssh` (e.g. native Windows OpenSSH), so rsync's ssh matches the rest of the transport. A value you set yourself is respected. |
| `HPC_SSH_NO_BACKOFF` | (unset) | Set to `1` to disable transient-failure exponential backoff. Used by the test suite when mocking subprocess; production callers should leave this alone. |
| `HPC_SUBMIT_NO_LOCK` | (unset) | Set to `1` to disable the per-repo submit-flow advisory flock. The lock serializes concurrent `submit-flow` / `submit-flow-batch` calls against the same experiment dir so two shells don't both fan out N qsubs at the cluster's sshd. Retained for two narrow callers: (a) the test suite, where `submit_flow` is exercised in parallel with mocked subprocess (no real qsub to race), and (b) operators who deliberately want concurrent submits (different specs, different shells) and have confirmed the cluster can absorb the burst. Disabling outside those two cases risks a scheduler-throttling stampede. |

## Validation thresholds

There are no env-var knobs for validators; per-rule overrides live in
`.hpc/playbook.yaml` (version-controlled, per-project). See
[`config-precedence.md`](config-precedence.md).

## Discovery

Run `hpc-agent capabilities --full` to see the full operations
catalog plus all supported `clusters.yaml` keys (the latter come from
`hpc_agent.infra.clusters.CLUSTER_YAML_KEYS`). Env vars don't appear
there — this doc is the canonical list.
