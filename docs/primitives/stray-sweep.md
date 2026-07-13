---
name: stray-sweep
verb: query
side_effects:
- ssh: <login-node> (ps -u $USER; kill only marked strays when reap=true)
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
backed_by:
  cli: hpc-agent stray-sweep --spec <path> --ssh-target <ssh_target> [--reap] [--max-age-sec
    <max_age_sec>] [--warn-threshold <warn_threshold>]
  python: hpc_agent.ops.recover.stray_sweep.stray_sweep
---
## Purpose

Login-node **process-hygiene probe** (run-12 finding 20, LAYER 2). The
connection storm's REMOTE cost: orphaned remote halves of killed ssh commands
(a bash+python pair per poll) accumulate on the login node until the user's
per-process fork quota is exhausted and sshd can no longer fork a login shell —
an UNRECOVERABLE state (the `kill` builtin needs a shell to run IN). LAYER 1
(`build_remote_command`, applied at the `ssh_run` seam) already bounds each
remote command's lifetime with a server-side `timeout` so orphans self-destruct;
this verb is the observability + bounded-cleanup half. It runs ONE fork-minimal
`ps -u $USER` over ssh, counts the user's total processes (fork-quota pressure),
identifies the MARKED framework processes (their argv carries the `HPC_AGENT_OP`
marker), flags marked processes older than the max legitimate deadline as
strays, and — only under an explicit `reap` flag — kills exactly those stray
PIDs. It surfaces the stray count the quota-wedge incident lacked (47 strays
would have been visible days before the login node wedged).

## Inputs

A `StraySweepSpec` JSON spec:

- `ssh_target` (str, required) — the login node to sweep (`user@host` or an
  OpenSSH alias, exactly as the run record's `ssh_target`). Mirrored by the
  required `--ssh-target` CLI flag.
- `reap` (bool, default `false`) — when true, kill the strays found — and ONLY
  those exact PIDs (marked AND over-age), never an unmarked user process. Default
  false makes the verb pure detection (the never-act-without-the-flag posture).
- `max_age_sec` (int, default `3900`) — age beyond which a MARKED process is a
  stray. The default sits just above the remote self-destruct default (3600s
  deadline + 60s margin + slack): a marked process older than this outlived even
  the generous no-client-timeout bound.
- `warn_threshold` (int, default `40`) — total process count above which
  `needs_attention` flips (login-node fork limits are typically a few hundred;
  40 leaves ample headroom to act).

## Outputs

A `StraySweepResult`: `ssh_target`, `total_process_count`, `marked_count`,
`strays` (each `{pid, etimes, op, args}`), `reaped_pids`, `reaped` (whether a
reap was requested AND executed), `needs_attention` (total over
`warn_threshold` OR any stray), and a one-line `summary`.

## Errors

- `spec_invalid` — malformed spec (missing `ssh_target`, out-of-range knob);
  enforced at the wire boundary.
- `ssh_unreachable` — the `ps` probe itself could not run (ssh failure, open
  circuit, or a non-zero `ps` exit); retry-safe.

## Idempotency

Idempotent. Detection re-runs freely; a reap is idempotent by PID (a dead PID's
`kill` is a harmless no-op), so re-running never double-acts on a live process.

## Notes

- **Never touches an unmarked process.** Reap targets exactly the parsed stray
  PIDs — marked (`HPC_AGENT_OP` in argv) AND older than `max_age_sec`. A young
  marked process and any unmarked user process (a login shell, an editor) are
  never candidates.
- **Home rationale.** This verb does SSH, so it deliberately does NOT live on
  `doctor` (whose contract is "pure local filesystem read — no SSH, no
  scheduler") nor on `net-triage` (which opens NO ssh by design — it must not
  burn a host's single half-open breaker probe). A small dedicated primitive
  keeps both contracts intact.
- **Fork-minimal.** The probe is one `ps` exec with no conda/module preamble —
  the pattern that survived on a starved login node where heavy multi-fork polls
  died (run-12 finding 20). The sweep's own wrapped ssh command is itself marked
  and young, so it is counted but never reaped.
