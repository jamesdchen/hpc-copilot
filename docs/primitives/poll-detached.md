---
name: poll-detached
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent poll-detached --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.monitor.poll_detached.poll_detached
---
## Purpose

**Instant, non-blocking snapshot** of a detached worker's state — the MCP-safe
complement to `wait-detached`. Where `wait-detached` BLOCKS on the worker's
lease pid so the harness can wake the agent exactly once at completion, this
verb takes a single read and returns "where is this worker *right now*?"
without holding a turn open. That matters over MCP: the in-process server
dispatches tools synchronously, so a multi-hour blocking wait would wedge it
(`_kernel/extension/mcp_server.py::_refuse_blocking_over_mcp` refuses
`wait-detached` there); `poll-detached` is the read a caller reaches for on
that transport. Purely local — zero cluster contact, no SSH.

## Inputs

A `PollDetachedInput` JSON spec with:

- `run_id` (str, strict run-id shape) — the run whose worker to snapshot.
- `block` (str, required) — the detached block named by its detach **verb**
  (e.g. `campaign-run`, `submit-s2`, `status-watch`) — the same key the
  launcher stamps the lease under and the terminal store is keyed by. Required:
  the lease path and terminal lookup are both `(run_id, block)`-keyed, so a
  snapshot with no block would have no worker to point at.

`experiment_dir` is supplied through the standard `--experiment-dir` CLI arg
(the optional MCP input property), exactly as every sibling monitor query
resolves it — the journal and terminal reads receive it as a kwarg.

## Outputs

A `PollDetachedResult` fusing three durable signals a detach-by-contract worker
leaves behind, each read locally:

- `lease_present` (bool) — whether the `<block>-<run_id>.lease.json` file exists
  (a present-but-corrupt lease still counts as present: a worker was launched).
- `pid` (int | None) — the pid recorded in the lease, or `None` when
  absent/unreadable.
- `pid_alive` (bool) — whether that pid names a live process right now (the
  single liveness probe, `infra.proc.pid_alive`).
- `journal_status` (str | None) — the run's journal status, or `None` when no
  record exists yet.
- `terminal_recorded` (bool) — whether a block terminal record exists for
  `(run_id, block)`.
- `watch` — the constant `"journal"`: every further observation of a detached
  worker is a journal read, never an SSH dial.

`state` derives the one answer callers act on:

- `running` — lease present and its pid is alive: the worker is driving the
  block. Observe further via the journal; do **not** relaunch (the lease is
  single).
- `exited_recorded` — pid dead AND a terminal is on disk: the worker finished
  and stamped its verdict. A re-invoke replays, it does not re-spawn.
- `exited_unrecorded` — pid dead but NO terminal recorded: the dead-worker gap
  (run-#12). The worker died without stamping a terminal; escalate to the
  doctor or re-arm rather than waiting on a wake that will never come.
- `no_lease` — no lease file: the worker was never launched (or the lease was
  reclaimed). The journal status still reports whatever is on disk.

## Errors

- `spec_invalid` — malformed spec (run-id shape, empty block); enforced at the
  wire boundary.

## Idempotency

Idempotent — a pure read that writes nothing. Safe to call repeatedly; it is
the intended polling surface when an event-driven `wait-detached` is
unavailable (e.g. over MCP).

## Notes

- A corrupt or mid-write lease is treated as **present** with `pid=None` (a
  worker was launched), never as `no_lease` — an unreadable lease must not
  misreport the worker as absent.
- The `block` field is deliberately NOT constrained to the core
  `SUPPORTED_DETACHED_BLOCK_VERBS` set: a plugin may add detachable verbs, and
  coupling the wire model to the core frozenset would break the
  library-knowledge boundary.
- Distinguishing `exited_recorded` from `exited_unrecorded` is the whole point:
  it lets a caller separate a worker that finished-and-recorded from one that
  died silently, and route the latter to recovery instead of an indefinite
  wait.
