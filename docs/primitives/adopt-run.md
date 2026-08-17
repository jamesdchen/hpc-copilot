---
name: adopt-run
verb: mutate
side_effects:
- file_write: <experiment>/.hpc/runs/<run_id>.json + the journal record (+ the directed-settle
    decision on the terminal branch)
idempotent: false
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent adopt-run --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.adopt_run.adopt_run
---
# adopt-run

First-class ingest of a run submitted **outside** hpc-agent (freestyle
`sbatch`/`qsub`, hand-rolled scripts). Agents explore freestyle; hpc-agent
verifies ex-post — `adopt-run` is the verb that brings the foreign run under
the fidelity machinery so the standard chain (`status-watch` /
`aggregate-check` → `aggregate-run` → `verify-reproduction`
external-baseline) engages. It **composes existing machinery** — the sidecar
goes through `write-run-sidecar` (all of its guards apply unchanged), the
journal record through the existing record path with **no scheduler call**,
and a terminal adoption settles through the settle-run mechanism on directed
evidence.

## Inputs

`schemas/adopt_run.input.json` (from `AdoptRunInput`). Key fields:

- `run_id` (required) — the identity to adopt under; must be fresh (see
  Errors).
- `command` (required) — what the foreign run executed. `cmd_sha` is
  **always derived** in code as `sha256(command.strip())` (full 64 hex);
  it can never be caller-supplied (`extra="forbid"` refuses it at the wire).
- `cluster`, `ssh_target` (`user@host`), `remote_path` (all required) — the
  cluster contract recorded on the journal record + sidecar.
- `job_ids` (optional, scheduler-id-shaped) — **present ⇒ in-flight
  adoption** (record lands `in_flight`, hand-off to `status-watch`);
  **absent ⇒ already-terminal adoption**, which then requires
  `terminal_evidence`.
- `terminal_evidence` (required when `job_ids` absent) — WHAT proves the
  terminal state; journaled as a directed-settle sign-off (scope `run`,
  response `y`, block `adopt-run`). Empty ⇒ refused (settle-run doctrine: an
  evidence-free settle is a bare status flip).
- `executor` (optional, defaults to `command`) — must satisfy
  `write-run-sidecar`'s real-per-task-command guard.
- `result_dir_template` / `task_count` (optional) — supplied, or inferred
  from `results_sample`.
- `results_sample` (optional) — a **local** path/glob anchored on the run's
  task dirs. Inference globs the task dirs, detects the trailing-integer
  task pattern (`task_count = max(index) + 1`, gap-safe), and verifies
  `summary_artifact` presence. `adopt-run` declares `file_write` only and
  never probes over ssh: a remote or non-resolving anchor yields a
  `needs_elicitation` envelope naming exactly what to supply — never a
  guess.
- `summary_artifact` (optional, default `metrics.json`), `profile`,
  `job_name`, `resources` — recorded verbatim.

## Outputs

`schemas/adopt_run.output.json` (from `AdoptRunResult`). `stage_reached`:

- `adopted_in_flight` — sidecar + journal record written (`status:
  in_flight`); `next_block = {verb: status-watch, ...}`.
- `adopted_terminal` — sidecar + journal record written, then settled
  `complete` through the settle-run mechanism (decision sign-off →
  `mark_run` → receipt-gated `harvest_on_terminal`); `next_block = {verb:
  aggregate-check, ...}`.
- `needs_elicitation` — layout could not be inferred; **nothing was
  written**; `needs_decision: true` and `reason` names the missing fields
  (`result_dir_template` + `task_count`); `next_block` null.

Both adopted envelopes carry the derived `cmd_sha`, the recorded
`task_count` / `result_dir_template`, the `sidecar_path`, and a hint that
`verify-reproduction` in external-baseline mode is the claim-comparison
path.

## Errors

`spec_invalid` (user, not retry-safe):

- the `run_id` already has a sidecar (`<exp>/.hpc/runs/<run_id>.json`) OR a
  journal record — adopt-run **never clobbers**; the message points at the
  existing record.
- `job_ids` absent and `terminal_evidence` empty/missing.
- any refusal from the composed `write-run-sidecar` guards: dispatcher-shaped
  or bare-script `executor`, `{placeholder}` leakage, a multi-task
  `result_dir_template` with no per-task placeholder, or a
  `.hpc/tasks.py` identity mismatch (the cross-check applies unchanged).
- malformed wire fields (`job_ids` must be scheduler-id-shaped —
  digit-leading; fabricated prose ids are refused at the wire).

## Idempotency

**Not idempotent** (`idempotent: false`): the second adoption of the same
`run_id` is refused by the duplicate guard rather than replayed — pointing at
the existing record is the correct behavior for an ingest verb (a silent
replay could mask two different foreign runs claiming one identity). The
`needs_elicitation` branch writes nothing, so re-invoking with completed
fields is safe.

## Notes

- **No scheduler calls, ever.** In-flight adoption records via the existing
  record-only path (`submit_and_record`); terminal adoption mirrors its
  fresh-record construction with `job_ids: []` (a real submit spec refuses an
  empty job list by design). Nothing is submitted, killed, or probed.
- **The settle is the same machinery as `settle-run`** — `append_decision`
  sign-off with directed provenance, typed `last_status`, `mark_run`, and the
  receipt-gated `harvest_on_terminal` — keyed `block: adopt-run` so the
  journal names the verb that took the evidence.
- The sidecar's `extra.adopted = {by: adopt-run, at: <iso>}` marks the run as
  adopted (recorded verbatim through the existing `extra` pass-through).
- Compose with: `status-watch` (in-flight successor), `aggregate-check` /
  `aggregate-run` (terminal successor), `verify-reproduction`
  (external-baseline claim check), `evidence-brief` (digest).
