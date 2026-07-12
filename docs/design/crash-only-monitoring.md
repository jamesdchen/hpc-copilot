# Crash-only monitoring — the cluster announces, ticks replace watchers

Status: **BANKED** (2026-07-11, run-#12 night; user-prompted: "there must be
a more principled way"). The redesign that retires the watch-worker failure
family (findings 3, 16, 17.3, 19 are its SYMPTOMS) instead of hardening it.

## The disease the symptoms share

Long-lived, stateful, client-side pollers over a WAN link. The watcher
rebuilds state the scheduler already knows; its own liveness gets conflated
with the run's; its retry loops amplify into connection storms; its death
loses the "one cold dial" it existed to hold. The JOURNAL — append-only,
stateless between writes — was the only monitoring component that never
failed in run #12. Build the rest like the journal.

## Three inversions

1. **Push, not pull — the cluster announces terminal.** At submit, a
   SENTINEL job rides along: `--dependency=afterany:<array jobs>` (SGE:
   `-hold_jid`) whose sole act is writing `results/<run_id>/.hpc_TERMINAL`
   (a manifest: per-task exit summary, written cluster-side where sacct is
   free and local). Run-end detection becomes "stat one file" — a single
   bounded call, no poll loop that must survive hours. The scheduler's
   epilogue knowledge is captured AT the source.
2. **Stateless ticks, not resident watchers.** The §5 watchdog (an OS
   scheduler entry, already installed) becomes the ONLY monitoring loop:
   each 15-min tick = read journal → if a poll is due AND the breaker
   allows, ONE bounded probe (stat the sentinel; sacct fallback) → record →
   exit. Crash-only: no process owns state between ticks, so nothing can
   die silently, freeze on a lease, or read its own death as the run's.
   `status-watch` survives as an OPT-IN interactive dial (a human actively
   watching), never the load-bearing mechanism.
3. **One ssh budget.** Every consumer (tick, harvest, deploy, doctor)
   behind the ONE throttle-slot gateway with shared breaker state — private
   retry loops are how N well-behaved processes become one connection
   storm. Backpressure becomes structural, not per-caller etiquette.

## What this deletes vs hardens

DELETED once live: the detached watch-worker lifecycle (spawn/lease/
wait-detached re-arm dance) for monitoring; the abnormal-exit sentinel
(nothing long-lived remains to exit abnormally); the re-arm babysitting.
KEPT: the journal as the one truth; harvest as a tick-triggered act on a
POSITIVE terminal (the sentinel manifest IS the positive evidence finding
19 demanded); the breaker (now shared).

## Build sketch (post-Fable, Opus-dispatchable)

- W1: sentinel-job leg in submit (template + dependency wiring, SGE + Slurm
  dialects; manifest schema {run_id, per-task exit codes, written_at}).
- W2: watchdog tick grows the poll leg (journal-driven due-times, sentinel
  stat, sacct fallback, breaker-aware skip); doctor keeps its scans.
- W3: status-watch re-labeled interactive-only; block-drive reads the
  tick-recorded state instead of arming workers.
- W4: ssh gateway unification (throttle slots as the sole door).
Run-#12 evidence file: findings 3/16/17.3/19 in run12-findings.md.

## Drift log — what has landed

- **Phase 1 (task-side announcements), implemented 2026-07-11.** A cheap,
  self-contained slice of inversion #1 that needs no sentinel job: the
  cluster-side dispatcher announces its OWN per-task terminal state, and the
  client settles the run's lifecycle by reading those announcements first —
  the concrete answer to run-12 findings 20/24 (a 20-25 min silent
  status-reporter walk over a NAT'd link, severed mid-flight, left a finished
  run unverifiable). Files:
  - `execution/mapreduce/dispatch.py` — on its terminal bookkeeping (success
    AND failure) the dispatcher writes ONE marker per task,
    `.hpc/announce/<run_id>/task_<id>.complete|.failed` (filename encodes the
    verdict; atomic tmp+rename; best-effort — a write failure never fails the
    task). The state MIRRORS the promote/failure decision (the finding-16
    empty-output guard's verdict, not the raw executor rc).
  - `ops/monitor/announce.py` — `read_announcements` counts per-state markers
    with a pure `ls | wc -l` in ONE bounded ssh exec (positive-evidence ack;
    filename-encoding means no `cat`, no shared-file append).
  - `ops/monitor/reconcile.py` — before the heavy 3-way probe, a FULL
    announcement (`announced == task_count`) settles via the SAME `settle` +
    `mark_run` + transition-gated `harvest_on_terminal` the reporter-backed arm
    uses; a PARTIAL announcement is progress evidence only and never settles;
    zero markers fall through byte-identically (old runs unchanged).
  TRUST BOUNDARY (stated in both module docstrings): markers settle LIFECYCLE
  only — the aggregate integrity gate still independently verifies outputs.
- **Phase 2 (watch consumes announcements end-to-end), implemented
  2026-07-12.** The `monitor_flow` poll loop now prefers the ONE-readdir
  announce census over the per-task reporter WALK for the WHOLE lifecycle —
  not just at terminal (the G1 plan's "steady state when the watch consumes
  announcements end-to-end"). Each tick, `_announce_status` reads the markers
  in one bounded ssh exec; when the announce dir EXISTS (an announce-era run),
  the run's status is resolved from the census with NO walk, the not-yet-
  terminal (`missing`) tasks map to `pending` so the shared classifier
  (`classify.classify_polling`) reads a PARTIAL census as still-in-flight and
  a FULL one settles terminal exactly as the walk would for the same counts.
  A pre-announce run (no announce dir yet) falls back to the reporter walk,
  DISCLOSED (`docs/internals/fallback-inventory.md`: once-per-call INFO +
  in-band `status_source` on every tick) — the fallback need decays as
  pre-announce runs age out. `read_announcements` grew a `present` capability
  flag for this (the ack/no-ack distinction Phase 1 collapsed). File:
  `ops/monitor_flow.py::_announce_status` + the poll-loop announce-first branch;
  pinned by `tests/ops/monitor/test_flow_announce.py`.
  Also this window: **bug-sweep #44** — reconcile's PURE-API branch settled on
  a count-less `{"checked_at": ...}` summary, so a finished pure-API run whose
  jobs aged out of the live queue was flipped `abandoned` (the #351 class,
  pure-API path). Reconcile now pulls `backend.task_statuses` (the same shape
  `record_status` uses) so `settle` sees real completion/failure evidence; a
  liveness-only backend keeps the pinned abandoned via `NotImplementedError`,
  a query failure routes through `unable_to_verify`. Pinned by
  `tests/ops/monitor/test_monitor_pure_api.py`.
  Still banked, NOT yet built: the sentinel job (W1), the stateless watchdog
  poll leg (W2), status-watch re-labeling (W3), ssh-gateway unification (W4).
