# Design: submit dedup vs. resubmit, by journal status

> **Status:** implemented. Tracks
> [#276](https://github.com/jamesdchen/hpc-agent/issues/276).
> Shipped: `state.journal.is_resubmittable_terminal`, consulted by
> `submit_flow._dedup_existing`, the canary-reuse path in
> `submit_flow._submit_one_spec`, and `runner.submit_and_record`.

`submit-flow` is idempotent on `run_id` (a deterministic
`<run_name>-<cmd_sha[:8]>`). When a journal record already exists for the
run_id, the submit path must decide between two outcomes:

- **block (dedup)** — return the prior record, touch nothing (no rsync / qsub);
- **proceed** — run a fresh submission, overwriting the record.

The decision keys on the record's **`status`** (the verdict), *not* on whether
`job_ids` is populated. `job_ids` is forensic data that a terminal record keeps;
treating its presence as "this run is live" is exactly the bug #276 fixed.

The single predicate is `is_resubmittable_terminal(record)`:

| `status` | submit decision | why |
|---|---|---|
| `in_flight` | **block** | a live run — proceeding would double-submit. |
| `complete` | **block** | finished experiment; a same-`run_id` resubmit is a replay, not new work (idempotency). |
| `failed` | **proceed** | terminal, nothing left running; a re-run is a fresh attempt. |
| `abandoned` | **proceed** | the monitor gave up tracking (often a transient status-probe flake); not a live run. |
| *held* (`pending_verdict`) | **block** | parked on a #231/#234 escalation; that flow owns resubmission — a plain submit must not clobber the hold. |

i.e. resubmittable = `TERMINAL_STATUSES − {complete}`, minus held runs.

## Why `timeout` is deliberately NOT resubmittable

`timeout` is the one terminal-ish outcome where "fall through to a fresh submit"
would be unsafe — and it is handled correctly by *not being a journal status at
all*:

- `timeout` is a **`LifecycleState`** (the monitor-flow envelope field), never a
  **`JournalStatus`**. `mark_run` validates against
  `JournalStatus = {in_flight, complete, failed, abandoned}`, and
  `update_run_status` has no `status` in its whitelist — so a record's `status`
  is never `timeout`.
- A wall-clock-exceeded run therefore **stays `in_flight`** in the journal. That
  is deliberate and safe: timeout means the cluster jobs *may still be running*,
  so the run must keep **blocking** a fresh submit to avoid a double-submit.

Adding `timeout` to the resubmittable set would be both inert (it can never be a
record status) and, in the hypothetical where it could fire, hazardous (a
double-submit while the old jobs run). So it is excluded by design; the
`in_flight` row already covers a timed-out run with the safe behavior.

## History

Before #276, `_dedup_existing` deduped against *any* existing record, so a single
transient status-poll failure that minted an `abandoned` corpse (with `job_ids`
populated) wedged every future submit for that run_id until the user manually
deleted `~/.claude/hpc/<repo_hash>/`. #276 narrowed dedup to "live, or
successfully complete," letting `failed`/`abandoned` fall through while
preserving `complete`'s idempotency, `in_flight`'s (and timeout's) double-submit
guard, and held runs' escalation hold.

This is the dedup layer only. Operators can independently skip the submit-flow
**preflight probes** (not the dedup) with `HPC_AGENT_SKIP_PREFLIGHT=1` — see
[env-vars.md](../reference/env-vars.md) (#275).
