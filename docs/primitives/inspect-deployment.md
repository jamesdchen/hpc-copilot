---
name: inspect-deployment
verb: query
side_effects:
- ssh: <cluster> (one read-only depth-bounded listing probe)
idempotent: true
idempotency_key: none
error_codes:
- code: remote_command_failed
  category: cluster
  retry_safe: false
- code: spec_invalid
  category: user
  retry_safe: false
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent inspect-deployment [--experiment-dir <dir>] --cluster <cluster> [--run-id
    <run_id>] [--path <path>] [--depth <depth>]
  python: hpc_agent.ops.inspect_deployment.inspect_deployment
---
# inspect-deployment

Inspect a deployed experiment tree on the cluster, **read-only and
throttled** — `ls`/`find` under `REPO_DIR` (or an explicit scratch path)
through the single connection-storm-safe SSH seam. This is the verb to reach
for when you need to *see what was deployed* for an experiment, instead of
issuing **raw `ssh`** (`ssh usc-discovery "ls /scratch1/..."`).

Raw ssh bypasses the entire #346 connection-storm hardening —
`ConnectTimeout`, `IdentitiesOnly`, and the per-host `safe_interval` throttle
(`infra/ssh_throttle.py`; CARC: `HPC_SSH_SAFE_INTERVAL=30`). Those guards only
protect the cluster if **all** SSH goes through `infra.remote.ssh_run`; a
raw-ssh side channel reopens the hole that earned the CARC fail2ban ban. This
verb is the general case of S5's single-file `preflight_executor_exists`
existence check, broadened to a depth-bounded listing over the same throttled
transport.

## Inputs / outputs

See `hpc_agent/schemas/inspect_deployment.{input,output}.json`.

- `--cluster` (required) resolves `ssh_target` + `scratch` from
  `clusters.yaml`.
- Exactly one target: `--run-id` derives `REPO_DIR` from the run's journaled
  `remote_path` (the canonical `deploy_target_for` derivation, so "where you
  inspect" can't drift from "where rsync deployed"), **or** `--path` probes an
  explicit absolute path.
- `--depth` (1..4, default 1) bounds `find -maxdepth`.

Output carries the resolved target, `exists`, and the sorted, cluster-side
capped `entries` (with `truncated` when the cap was hit).

## Read-only, throttled, scratch-confined — by construction

- **One connection per call**, through `infra.remote.ssh_run` — so it honors
  `safe_interval` / `ConnectTimeout` like every other verb. Polling/inspection
  stays inside the connection-storm envelope.
- **Not a general remote exec.** The probe is a fixed `test -e` + `find
  -maxdepth N`; there is **no caller-supplied command string** (that would
  just be raw ssh with extra steps). No write ops.
- **Scratch-confined.** The probed path MUST resolve strictly under the
  cluster's scratch root, reusing `validate_remote_path_under_scratch` — the
  same guard `build_submit_spec` uses. A path outside scratch →
  `spec_invalid`; it is never probed.

## Errors

- `cluster_unknown` — the `--cluster` key is not in `clusters.yaml`.
- `spec_invalid` — neither/both of `--run-id`/`--path`; a `--depth` outside
  1..4; a path outside scratch; or a `--run-id` with no journal record /
  no `remote_path`.
- `remote_command_failed` — the SSH transport itself failed (a non-existent
  *target* is **not** an error — it returns `exists: false`).

## Idempotency

Pure read; re-running is always safe and returns the current listing.
