---
name: hpc-preflight
description: "Verify the local environment can submit HPC jobs before the first submit of a session."
allowed-tools: Bash Read Write
---

Run this BEFORE the first `hpc-submit` invocation in a session, or any time submissions hang or fail. Catches the most common failure mode: SSH credentials not forwarded into the spawned shell.

## Steps

1. Determine target cluster from the active spec or task input. If no cluster is in scope, run the local-only check (skip `--cluster`).

2. List clusters when a name is needed but unknown:
   ```bash
   hpc-mapreduce clusters list
   ```
   Parse `data.clusters[].name` from the JSON envelope.

3. Run preflight:
   ```bash
   hpc-mapreduce preflight --cluster <name>
   ```
   Or for local-only:
   ```bash
   hpc-mapreduce preflight
   ```

4. Parse the single-line JSON envelope on stdout. On `ok: true`, inspect `data.all_ok` and `data.checks[]`:
   - `ssh_auth_sock` — if false, `SSH_AUTH_SOCK` is unset or the agent has no keys. The caller must add a key (`ssh-add ~/.ssh/<key>`) AND export `SSH_AUTH_SOCK` + `SSH_AGENT_PID` into the env passed to `hpc-mapreduce`. Stop; do not proceed to submit.
   - `ssh_on_path`, `rsync_on_path` — if false, install via system package manager. Stop.
   - `clusters_yaml_parses` — if false, surface `detail` (parse error) and stop.
   - `cluster_known` — if false, the cluster name is wrong; re-run `clusters list`.
   - `cluster_tcp_22` — if false, cluster is offline or hostname is wrong; do NOT submit.

5. On `ok: false` envelope (exit 1/2/3), read `error_code` and `remediation`. Common: `config_invalid` (clusters.yaml malformed), `cluster_unknown` (bad `--cluster`).

6. If `data.all_ok` is true, the environment is ready. Continue with the calling workflow. If false, return the failing checks to the caller and stop — do not advance to `hpc-submit`.

## Notes

- **SSH env passthrough**: the caller MUST forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` into the env when spawning `hpc-mapreduce`. Without these, every cluster call hangs on auth. Preflight catches this before submission.
- Idempotent. Safe to call repeatedly. Read-only — touches no files on the cluster.
- Without `--cluster`, only local-machine checks run. Useful as a smoke test after install.
- Exit code: 0 if all checks pass, 2 if any fail.
- Run preflight first whenever a downstream call (`submit`, `status`, `aggregate`) returns `ssh_unreachable` or hangs longer than expected.
