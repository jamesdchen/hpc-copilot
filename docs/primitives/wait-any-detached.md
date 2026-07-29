---
name: wait-any-detached
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent wait-any-detached --spec <path>
  python: hpc_agent.ops.monitor.wait_any_detached.wait_any_detached
---
## Purpose

**One held seat for a whole fleet.** `wait-any-detached` blocks until ANY
detached worker in a target SET exits (or the budget elapses) and reports every
target's state on the way out. It is the fleet form of
[`wait-detached`](wait-detached.md), and it exists for exactly one reason: a
drain pass over N running items today holds **N** chunked `wait-detached`
relays — one subagent seat each, 480s per chunk — because a single waiter
watches exactly one `(run_id, block)` lease. Holding the fleet therefore costs
held-hours × runs, paid in small agent calls. One `wait-any-detached` collapses
that to **one seat for all in-flight runs**: cost per night goes flat in the
number of runs, and the woken caller already knows which run resolved
(`docs/plans/run-queue-placement-2026-07-28.md` §7, "`wait-any-detached`
(Phase 2 efficiency)").

Like `wait-detached` it is a **pure read**: it stats and reads lease files,
probes pids, and reads terminals that were already recorded. It writes
nothing — no journal append, no lease write, no marker, `side_effects: []`.
That read-only-ness is a contract, not an implementation detail: it is what
makes concurrent waiters (several passes, overlapping sets, a waiter alongside
the worker it watches) safe by construction, and it is pinned by a test that
asserts the experiment tree and the lease dir are byte-and-mtime identical
across a call.

Identity, vocabulary, and budget discipline are `wait-detached`'s, unchanged: a
target is the same `(run_id[, block])` pair, the outcomes are the same three
literals, and `timeout_sec` / `poll_interval_sec` carry the same defaults and
clamps — so a caller branches on ONE vocabulary whether it waited on one run or
forty. Launch it through the harness's native backgrounding (Claude Code
`run_in_background`), never over the synchronous MCP server.

## Inputs

A `WaitAnyDetachedInput` JSON spec (**required** — it has a required field) with:

- `targets` (list of `{run_id, block?}`, **required**, 1–128 entries) — the
  watch set. Each entry is the identity `wait-detached` takes singly: `run_id`
  (strict run-id shape) plus an optional `block` (e.g. `submit-s2`); omitting
  `block` watches **any** live lease for that run. An empty list is refused at
  the wire boundary (there is no wait to perform, and burning the whole budget
  on nothing is not a tolerance), as is a duplicated `(run_id, block)` pair
  (it would report one worker twice under two rows).
- `timeout_sec` (float, default `7200`, max `86400`) — wall-clock budget, same
  default/clamp as `wait-detached`.
- `poll_interval_sec` (float, default `2`, max `60`) — local pid-probe cadence,
  applied across the whole set each tick.

## Outputs

A `WaitAnyDetachedResult` — `{outcome, targets[], triggered_count, waited_sec}`.

`outcome` is `wait-detached`'s vocabulary verbatim:

- `worker_exited` — at least one watched lease pid died within the budget. The
  triggering rows carry the wake payload; read those runs' journals next.
- `no_live_worker` — at least one target had no live worker at the **first**
  poll (already exited, or never launched). The wait returns at once: there is
  already something actionable, so holding a seat would be dead air. The
  degenerate all-dead set lands here too.
- `timeout` — the budget elapsed with every watched worker still alive. Not an
  anomaly by itself (long queue waits are normal); re-arm with the same set, or
  consult `queue-status` / `status-snapshot`.

`targets[]` is the FULL snapshot in the input's order — one
`DetachedTargetState` per target — so a single return says both what to act on
and what is still in flight:

- `state` ∈ `worker_exited` / `no_live_worker` / `still_running`. The first two
  are `wait-detached`'s literals for a resolved target; `still_running` is the
  one state a single waiter never has to name (this target is alive while a
  SIBLING is what returned, or the budget elapsed).
- `triggered` (bool) — true for the target(s) whose resolution ended the wait.
  Several can trigger together (two pids dying between the same pair of polls),
  which is why it is a per-row flag rather than one top-level pointer; it is
  always false on `timeout`, where `triggered_count` is `0`.
- `block` / `pid` / `log_path` — taken from the found lease (source of truth)
  when one exists, else the target's passthrough.
- `brief` / `relay` / `next_verb` — the wake payload, on the same terms
  `wait-detached` returns it: read from the exited worker's recorded terminal,
  falling back to the §5 pending-decision marker. Populated only for a resolved
  target; a `still_running` row has no terminal to read, so it stays null.

## Errors

- `spec_invalid` — malformed spec (empty or duplicated `targets`, over the
  128-target cap, bad run-id shape, out-of-range budget/interval); enforced at
  the wire boundary.

## Idempotency

Idempotent — a pure wait over a set that writes nothing. Re-arming after a
`timeout`, running several waiters over overlapping sets, or waiting on an
already-exited fleet are all safe; the last returns `no_live_worker`
immediately.

## Notes

- **Drop resolved targets before re-arming.** A target that returned
  `no_live_worker` will return it again instantly on the next call (it is
  already resolved) — exactly `wait-detached`'s behaviour for a dead worker. A
  caller that re-arms the SAME set unchanged after a resolution spins; the
  contract is that the caller acts on the triggering rows and re-arms with the
  remaining ones.
- Corrupt or mid-write lease files are skipped, never fatal (the shared
  `wait-detached` lease readers) — an unreadable lease must not strand the
  fleet waiter; that target simply reads as `no_live_worker`.
- The lease-identity helpers are imported from `wait-detached`, not
  re-implemented: one definition of "which lease is this target's, and is its
  pid alive".
- Blocking by construction, so it belongs OUTSIDE the synchronous MCP server —
  route it through backgrounded Bash like `wait-detached`. Its result carries
  no `next_block`, so the curated MCP catalog excludes it automatically; the
  `full`/`tiered` seam refusal (`_kernel/extension/mcp_server.py`'s
  `_BLOCKING_WAIT_VERBS`) should name it alongside `wait-detached` — that one
  line is owned by the MCP module and is the follow-up this build did not
  touch.
- The §5 watchdog remains the untouched backstop: if the fleet waiter dies with
  its session, only the notification is lost — doctor / the watchdog still
  catch every run.
