# /preflight — Verify the local environment can submit HPC jobs

Invoke the `hpc-preflight` skill via the Skill tool (`skills/hpc-preflight/SKILL.md`) for the workflow: which checks run, how to remediate each failure, when to write the per-cluster cache marker. The skill is the canonical SoT.

This slash command is the human-facing entry point. Reasons to invoke standalone (rather than letting `/submit-hpc` Step 6b auto-invoke):

- **Cluster diagnostics** without a pending submission ("is Hoffman2 up right now?").
- **Force-refresh** the per-cluster cache after fixing your SSH agent — running this command writes the same `~/.claude/hpc/<repo_hash>/preflight-<cluster>.json` marker that `/submit-hpc` reads, so a green standalone run tells `/submit-hpc` to skip its check for the next 24h.
- **First-time-user smoke test** on a new machine before any executor or `tasks.py` exists.

## Args

`--cluster <name>` (optional) — target cluster. When omitted, only local-machine checks run (useful as a `pip install`-time smoke test). When provided, the cluster-specific TCP probe also fires.

## Output

Single-line JSON envelope on stdout. `data.checks[]` carries one entry per check with `name`, `ok`, and `detail`. `data.all_ok` is true iff every check passed. Surface failing checks with their `detail` fields **verbatim** — don't paraphrase; the user needs the raw error to fix it.

Idempotent and read-only. Safe to run as many times as you want.
