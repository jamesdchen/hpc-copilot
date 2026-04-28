# /preflight — Verify the local environment can submit HPC jobs

Run this BEFORE the first `/submit` of a session, or any time submissions
mysteriously hang. Catches the most common first-time-user failure mode:
SSH credentials not forwarded into the current shell.

## Steps

1. Ask the user (or accept a `--cluster <name>` argument) which cluster to
   target. If they don't know, run `python -m hpc_mapreduce clusters list`
   first and present the names.

2. Invoke the CLI:
   ```bash
   python -m hpc_mapreduce preflight --cluster <name>
   ```
   Output is a single-line JSON envelope on stdout. Parse it.

3. For each `data.checks` entry:
   - `ssh_auth_sock` — `SSH_AUTH_SOCK` is set and `ssh-add -l` returns at
     least one key. If false, tell the user to run `ssh-add ~/.ssh/<key>`
     and ensure their terminal forwards SSH_AUTH_SOCK (tmux/screen quirks).
   - `ssh_on_path`, `rsync_on_path` — binaries are present. If missing,
     install them via the system package manager.
   - `clusters_yaml_parses` — the active clusters.yaml is valid yaml. If
     false, surface the parse error and stop — nothing else will work.
   - `cluster_known` (only if `--cluster` was passed) — the named cluster
     exists in clusters.yaml.
   - `cluster_tcp_22` (only if `--cluster` was passed) — TCP probe to the
     cluster's port 22 succeeded. If false, the cluster is offline or the
     hostname is wrong; do NOT try to submit.

4. If `data.all_ok` is true, summarise the green checks for the user and
   continue with the workflow they originally invoked. If false, list the
   failing checks with their `detail` fields and stop. Do not advance to
   `/submit`.

## Notes

- Idempotent. Safe to call as many times as you want.
- Read-only. Touches no files on the cluster.
- If `--cluster` is omitted, the cluster-specific checks are skipped; only
  the local-machine checks run. Useful as a `pip install`-time smoke test.
