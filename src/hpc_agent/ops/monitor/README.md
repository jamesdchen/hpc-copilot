# ops/monitor/

## What + why

`ops/monitor/` owns the read-only side of a live campaign: poll the
cluster for job status, fetch per-task stderr, reconcile journal state
against cluster ground truth, render the user-facing tick summaries,
mutate scheduler `Features` constraints without losing age priority,
and enumerate runs still in flight. The subject is the bridge between
"jobs are running on the cluster" and "the agent / human knows what's
happening" — every observation primitive plus the workflow atom that
composes them into a poll-to-terminal loop lives here.

## Invariant

`ops/monitor/` promises: typed monitor spec or run identifier in →
fresh cluster snapshot + reconciled journal + lifecycle decision out;
each poll is idempotent and never advances the run's terminal state on
its own (only `mark-run-terminal` does that, and only when the cluster
already says every task is done).

## Public vs internal

All eight modules in this directory are agent-facing primitive modules
(the `monitor-flow` workflow itself lives at `ops/monitor_flow.py`,
role-root sibling per P5a):

- `status.py` — registers `poll-run-status` (`record_status`); also
  exports `ssh_status_report` / `_ssh_status_report` as the canonical
  SSH-driven status reporter used cross-subject by `aggregate` and
  `recover`.
- `reconcile.py` — registers `reconcile-journal` (self-healing resume
  from cluster ground truth) and `mark-run-terminal` (the journal
  hand-off used by `monitor-flow` and `aggregate-flow`).
- `logs.py` — `fetch_task_logs`: the SSH-driven stderr tail used by
  the `logs` atom and by recover's `failures` atom.
- `logs_atom.py` — registers `logs`: the user-facing atom that selects
  task ids (explicit list or `--all-failed`) and tails their stderr.
  Distinct file name from `logs.py` because the atom is the
  agent-facing primitive while `logs.py` is the transport-side fetch
  the atom (and `recover/failures_atom`) consume.
- `update_constraints.py` — registers `update-run-constraints`: runs
  `scontrol update jobid=N Features=X` over SSH so SLURM constraints
  can shift without losing accumulated age priority.
- `arm.py` — registers `decide-monitor-arm`: picks cron / loop / none
  arm + cadence + cron schedule string from the run's current summary.
- `summary.py` — registers `monitor-summary`: renders the canonical
  user-facing tick summary read by the `/monitor-hpc` slash command.
- `list_in_flight.py` — registers `list-in-flight`: enumerates runs
  with `status=in_flight` in the local journal (recovery path).

No internal-only files in this subject.
