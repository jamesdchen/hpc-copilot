# /preflight — Verify the local environment can submit HPC jobs

`/submit-hpc` Step 6b auto-runs this same gate (cached per cluster for 24 h via `~/.claude/hpc/<repo_hash>/preflight-<cluster>.json`), so you usually don't need to invoke `/preflight` directly. Reasons to invoke it standalone:

- Cluster diagnostics without a pending submission ("is Hoffman2 up?")
- Force-refresh the cache after fixing your SSH agent (this command writes the same marker `/submit-hpc` reads)
- First-time-user smoke test on a new machine before any executor or `tasks.py` exists

Catches the most common first-time-user failure mode: SSH credentials not forwarded into the current shell.

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
   write the shared marker so `/submit-hpc`'s Step 6b gate skips the
   re-check for the next 24 h:

   ```python
   from datetime import datetime, timezone
   from pathlib import Path
   import json
   marker = Path.home() / ".claude/hpc" / repo_hash / f"preflight-{cluster}.json"
   marker.parent.mkdir(parents=True, exist_ok=True)
   marker.write_text(json.dumps({
       "checked_at": datetime.now(timezone.utc).isoformat(),
       "all_ok": True,
       "cluster": cluster,
   }))
   ```

   If false, list the failing checks with their `detail` fields and stop.
   Do not write the marker. Do not advance to `/submit-hpc`.

## Notes

- Idempotent. Safe to call as many times as you want.
- Read-only. Touches no files on the cluster.
- If `--cluster` is omitted, the cluster-specific checks are skipped; only
  the local-machine checks run. Useful as a `pip install`-time smoke test.
