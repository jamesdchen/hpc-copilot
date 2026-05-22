---
name: hpc-preflight
description: "Verify the local environment can submit HPC jobs before the first submit of a session."
allowed-tools: Bash Read Write
execution: inline
---

Agent-facing composition over the **[check-preflight](../../docs/primitives/check-preflight.md) primitive** (see that file for full contract). Run this BEFORE the first `hpc-submit` invocation in a session, or any time submissions hang or fail. Catches the most common failure mode: SSH credentials not forwarded into the spawned shell.

## Steps

1. **Determine target cluster** from the active spec or task input. If no cluster is in scope, run check-preflight without `--cluster` (local-only check).

2. **List clusters when a name is needed but unknown**: invoke [clusters-list](../../docs/primitives/clusters-list.md) and parse `data.clusters[].name`.

3. **Invoke** [check-preflight](../../docs/primitives/check-preflight.md) with `--cluster <name>` (or no flag for local-only).

4. **Parse `data.checks[]`** and remediate by check name (this is the agent-specific layer — the primitive surfaces failures as `checks[].ok = false` rather than error envelopes):
   - `ssh_auth_sock == false` — `SSH_AUTH_SOCK` is unset or the agent has no keys. Caller must add a key (`ssh-add ~/.ssh/<key>`) AND export `SSH_AUTH_SOCK` + `SSH_AGENT_PID` into the env passed to `hpc-agent`. **Stop**; do not proceed to submit.
   - `ssh_on_path == false` — install via system package manager. Stop.
   - `file_transfer_on_path == false` — no file-transfer transport found. Install `rsync`, or ensure `scp` + `tar` are on PATH (the runtime falls back to a `tar`/`scp` pipeline when `rsync` is absent — e.g. Windows without WSL/MSYS rsync). Stop.
   - `clusters_yaml_parses == false` — surface `detail` (parse error) and stop.
   - `cluster_known == false` — wrong cluster name; re-invoke clusters-list.
   - `cluster_tcp_22 == false` — cluster offline or hostname wrong; do NOT submit.

5. If `data.all_ok` is true, the environment is ready — continue with the calling workflow. **Write a per-cluster cache marker** at `~/.claude/hpc/<repo_hash>/preflight-<cluster>.json` with `{checked_at: <ISO>, all_ok: true, cluster: <name>}` so `hpc-submit`'s Step 6b gate can skip the re-check for the next 24h. If `data.all_ok` is false, return the failing checks to the caller and stop — do NOT write the marker.

## Notes

- **SSH env passthrough**: the caller MUST forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` into the env when spawning `hpc-agent`. Without these, every cluster call hangs on auth. This skill catches the missing-passthrough case before submission.
- Without `--cluster`, only local-machine checks run. Useful as a smoke test after install.
- Run this skill first whenever a downstream call (`hpc-submit`, `hpc-status`, `hpc-aggregate`) returns `ssh_unreachable` or hangs longer than expected.
- The marker (Step 5) is the single artifact that makes `/submit-hpc` Step 6b skip re-checking; without it, every submit re-runs the SSH probe.
