# S2 readiness — the proactive design (2026-07-30)

USER DIRECTION (2026-07-30): "we cannot just be reactionary, we must also be
proactive — what is the proper way to do S2 devx." This doc is the answer;
the reactive S2 hardening package (attended-latency plan item 7, in build)
is the incident-response subset of it.

## The principle

**S2 never discovers anything at fire time.** S2 is where local intent
becomes remote reality — the first block where transport, remote env,
storage, scheduler, and harness permissions must ALL work. Today it learns
whether they do by attempting the full operation, serially, reporting
failure via detached-worker-log archaeology (the 2026-07-30 night: two
16-second worker corpses, four failure classes, every diagnosis human).
The proper shape: S2 is the REVEAL of a readiness state computed before the
human sat down, plus the one irreversible act (the array submit) gated on
the journaled y.

## Pillars

1. **Standing readiness ledger** (new substrate). Per cluster: route verdict
   PER CHAIN ELEMENT (hop, target, direct-alternative — `ssh -G`-resolved),
   auth, preamble-class verdict, scratch reachability, scheduler touch, env
   fingerprint vs expected wheel. Refreshed OPPORTUNISTICALLY: every SSH the
   system already makes feeds it (the breaker's per-host records and the
   capabilities cache are existing fragments — one substrate, one freshness
   vocabulary). Rendered with age in the S1 brief and `suggest-*` surfaces.
   Fire-time ASSERTS freshness; cold probing at fire time is the failure
   mode, not the design.
2. **Speculate everything reversible** (ruled: R2 canary, R5 eligibility
   law; P2 wires canary-at-view). Staging push speculates on S1 park
   (delta-push is idempotent + content-keyed = eligible); canary verdict is
   in before the y. The y flips a switch; it never starts machinery.
3. **Named invariants, one owner each.** transport / env / storage /
   scheduler / permissions each carry their own verdict vocabulary,
   freshness, and composed remediation. Every S2 refusal names the broken
   invariant — discriminated cause as the PRIMARY structure (the reactive
   package's L1/L2 generalized).
4. **Interruption is normal.** S2 = checkpointed resumable state machine;
   one fire converges across flaps/kills; completed sub-steps never repeat
   (delta-push and the canary TTL cache are the existing precedents —
   extend the posture to every sub-step). "Attempt count" disappears as a
   concept; only progress exists.
5. **Failures are product surface.** Terminal causes land structured in the
   journal + attention-queue with composed remediation from the recoveries
   registry. A human reading a worker log to learn WHY is a design defect
   (tonight's exhibit).
6. **A measured SLO.** Instrument: (a) seconds from journaled y to
   array-accepted, (b) human interventions per submit, (c) readiness age at
   fire. Telemetry labels exist; the S2 scorecard rides run telemetry and
   the morning brief. Unmeasured devx regresses.

## Status vs in-flight work

- Pillar 2: P2 kernel unit (canary-at-view) + R5 S2-push wiring (not yet
  built).
- Pillar 3 (subset): reactive package L1/L2 (in build 2026-07-30).
- Pillar 4 (subset): reactive package L3 flap-riding retry (in build).
- Pillar 5: **BUILT** (2026-07-30) — see "Pillar 5 as built" below.
- Pillars 1 and 6: BUILT ahead of sign-off. The substrate shape (one JSON
  doc per host under the journal home, breaker-adjacent, storing
  `VerdictAtom` verbatim) and the three SLO fields are USER SIGN-OFF OWED —
  the code landed first so the design could be reviewed against something
  real, not instead of review.
- Pillar 1: **BUILT** (2026-07-30) across two tiers —
  `infra/readiness_sensors.py` (the sensor layer: `ssh -G` chain resolution,
  per-leg sensors, the `VerdictAtom` unit of record, an in-process
  record/consult ledger) and `state/readiness.py` (the DURABLE tier: a
  per-host JSON doc under the journal home, sibling of `_ssh_circuit/`,
  storing that same atom). Read surface: the `cluster-readiness` query verb.
  Opportunistic feed: the ssh circuit breaker's existing record sites.
- Pillar 6: **BUILT** (2026-07-30) — `state/s2_slo.py`, a pure reducer over
  records that already exist, surfaced as one `slo:` line in
  `monitor-summary`'s body.

## Pillar 1 as built: two tiers, one vocabulary

The ledger is ONE ledger with two storage tiers, not two ledgers:

| tier | module | lifetime |
|---|---|---|
| cache | `infra/readiness_sensors.py`'s in-process dict | one invocation |
| durable | `state/readiness.py` → `<journal home>/_readiness/<host>.json` | every process on the box, across restarts |

Read path is **consult-process-then-durable**; write path is
**write-through**. The durable tier stores `VerdictAtom` verbatim
(`sensor`/`target`/`verdict`/`detail`/`latency_ms`/`at`/`at_epoch`/`route`)
plus one additive durable-only `source`, so a stored reading and a live one
can never disagree about what was seen.

`SensorKind` is EXTENDED, never forked: `hop` / `direct` / `path` / `connect`
/ `preamble` (the sensor layer's) plus `auth` / `scratch` / `scheduler` /
`env` (the pillar-3 invariants no sensor covers yet). `SensorVerdict` (`ok` /
`down` / `timeout` / `unknown` / `skipped`) and the `route` axis are adopted
verbatim. Atom identity is `(sensor, route, target)` — a chain has several
hops, and the same sensor over the effective vs the direct route is the
dead-`ProxyJump` discriminator.

Overall verdict vocabulary: `{ready, stale, degraded, unknown}`. A STALE
failure reads `stale`, not `degraded` — the host may have healed and nothing
has looked since.

**Standing rule for every feed site: harvest, never probe.** Sensing belongs
to the sensor layer; the ledger only stores what the system already learned.
Wired today: the ssh circuit breaker's `record_connection_success` /
`record_connection_failure` emit a `connect`/`effective` atom (and a
`preamble`/`effective` `timeout` when its own degradation classifier already
says so). `auth` is a seam BY CONSTRUCTION — that seam's SUCCESS verdict folds
an auth rejection into "reached the host".

## Pillar 5 as built: one classifier, two surfaces, three timestamps

The defect was never that the worker log was missing something. It was that the
log was the ONLY structured place the death existed: `emit_fatal_block` takes no
`error_code`, and on the `rc != 0` arm the typed exception has already been
collapsed to an int by `_err_from_hpc` one frame in, so the block terminal
hardcodes `"error_code": "detached_worker_exit"`. Everything the S2 hardening
built — `PathCause`, the `mark_transport_flap` identity, the breaker state,
`dispatch_evidence` — reached the human as prose, or not at all.

**The classifier runs at the point of death.** `ops/recover/terminal_cause.py`
is called from the exit path in `cli/dispatch.py`, where the exception object
still exists. Evidence is trusted in a fixed order, STRUCTURE before prose:

1. the typed exception (`error_code` / `category` / `retry_safe`, and the flap
   IDENTITY — a stamped attribute, never a message match);
2. the run record's own `dispatch_evidence` (rung 0's class: provable offline,
   so it outranks anything sensed over a wire that may itself be the break);
3. the bounded worker-log tail, scanned for the discriminated cause VOCABULARY —
   not a heuristic: those tokens are emitted verbatim precisely so one word
   crosses the fire-time gate, the worker log, and triage;
4. the breaker's durable state file (a file read).

It never dials — the standing "harvest, never probe" rule. A reading that is not
already available is `None`, and `recovery_kind=None` is an HONEST outcome: no
guessed remediation, because the 2026-07-30 failure was a confident message
pointing at the wrong host, not a missing probe.

**Storage** is an append-only journal in the run sidecar tree,
`<experiment_dir>/.hpc/runs/<run_id>.terminal-causes.jsonl` — its own file, the
`overnight.jsonl` precedent, so a code-authored failure record never pollutes the
y/nudge journal the block gate and Stop guard scan.

**Two read surfaces, one record.** `attention-queue` gains the `worker-terminal`
kind (class `blocked`, fan-out 0) whose `action` IS the registry's composed
remediation, byte-identical to `hpc-agent recoveries show --kind <kind>`; the
morning brief gains `class_sections.worker_terminal_failures` (via
`heal_taxonomy.worker_terminal_sections`, threaded the brief's own `surfaced_at`
so there are not two disclosure clocks in one brief), and a terminal death now
earns a brief on its own — gating it on a standing consent would reproduce the
defect for every attended run. `worker-terminal` stays DISTINCT from
`dead-worker`: the latter is the liveness scan's finding (a dead pid, no
terminal — the shape a hard kill leaves when nothing was flushed), the former is
the worker's own structured disclosure.

**Three timestamps, because two would lie.** `failed_at` (the death),
`recorded_at` (when the machine wrote the disclosure), `surfaced_at` (when a
human's read computed the projection). The queue line carries `· disclosed +Nh`
and the brief carries `latency_seconds` — a duration, never a judgment. A noon
read of a 3am death says five hours in numbers.

Registry kinds added for the four live classes: `dead_hop_route` (net-triage
rung 0 + the `ssh -G` config discriminator, and a rank-0 option that is
deliberately NOT `host-retarget`), `flap_exhausted_staging` (re-fire converges
because the delta push is content-keyed; the breaker state is the honest wait),
`canary_reporter_unreachable` (the rc=255 route-class pointer that decides which
side of the wire to look at), `zombie_submitting_record` (documents the rung-0
auto-heal, and carries the residual menu for the UNKNOWN evidence case a pre-fix
record leaves).

The `[fatal]` block STAYS. Logs remain the forensic tier — the traceback, the
child stderr, the heartbeat trail — and the attention item POINTS at the log
rather than replacing it. What changed is that nothing a human needs in order to
DECIDE lives only in there
(`tests/ops/recover/test_terminal_cause.py::test_item_carries_everything_the_log_does`).

## Pillar 6 as built: the three fields

- `y_to_array_accepted_seconds` — LAST-ATTEMPT scoped (the last
  `submit-s2`-targeting `y` at/before accept, to `RunRecord.submitted_at`);
  `first_y_to_array_accepted_seconds` carries the day-scale
  across-all-re-drives view and renders only when it differs (see the
  verification drift entry below).
- `interventions_count` — every submit-chain decision record, nudges and
  `host-retarget` recovery y's included.
- `readiness_age_at_fire_seconds` — the durable ledger's age reconstructed AT
  the fire instant (atoms stamped later are excluded, never back-dated);
  `None` when there is no ledger.

All three are declared `cumulative` in `ops/monitor/summary.FIELD_KIND` and
render through `_render_scalar`, so `scripts/lint_telemetry_labels.py` covers
them: a total elapsed time can never acquire the `+` delta marker.

## Drift log

- 2026-07-30: created from the user direction; nothing built against
  pillars 1/6 yet.
- 2026-07-30 (verification): the "earliest y targeting submit-s2+" boundary
  measured 11.1 h on a real re-driven run (`har_base_sweep-53c27e42`, 9
  qualifying greenlights) because it spanned every re-drive of the day. The
  primary field is now LAST-ATTEMPT scoped (52.7 min on the same data) with
  `first_y_to_array_accepted_seconds` carrying the day-scale view. Open
  question for sign-off: should the first-y field subtract idle time between
  attempts (currently it cannot — an unattended gap is indistinguishable
  from an attended one in the journal).
- 2026-07-30 (later): pillars 1 and 6 built. The pillar-1 seam originally
  designed as "one file per cluster holding dotted atom kinds
  (`route.target`, `preamble_class`, …)" was RECONCILED against the sensor
  layer landing in the same wave: the dotted vocabulary was dropped and
  `VerdictAtom` + `SensorKind`/`SensorVerdict` adopted as the stored shape, so
  there is one definition rather than a storage vocabulary drifting from a
  sensing one. Regen for the new `cluster-readiness` verb is DEFERRED to the
  wave's serial rebake and ledgered in `docs/internals/regen-debt-ledger.md`.
  Two integration seams remain, both one-liners in files this wave did not
  own: (a) `readiness_sensors.record_readiness` / `consult_readiness` write
  through to / fall through to `state/readiness.record_atoms` /
  `consult_atoms`; (b) a true scheduler-accept stamp at
  `submit/runner.promote_submitting_record` feeding
  `s2_slo.compute_slo(accepted_at=…)`, which today reads `submitted_at` (an
  under-estimate by the dispatch duration on the submit-once path).
- 2026-07-30 (pillar 5): built. Two decisions worth recording because the
  obvious alternative was taken and rejected. (a) The terminal-cause record does
  NOT ride the overnight consumption ledger, even though that ledger already
  computes `failed_at` vs `surfaced_at`: a worker death is fallout, not a
  consented auto-advance, and folding it into `consumed` would misreport what
  the standing consent spent. It rides `class_sections` instead. (b) No wire
  model changed, so this unit carries **no regen debt**: the new evidence rides
  the existing free-form `AttentionItemModel.evidence` dict, and
  `AttentionItemModel.kind`'s description was left alone — it has been a
  non-exhaustive sample since the eleven kinds that landed after it, and making
  it exhaustive now would create a description that every future kind must
  re-bake. Two residues: the non-zero-`SystemExit` arm still records no BLOCK
  terminal (pre-existing, and the block layer's contract, not the disclosure
  layer's — the cause record now fires there regardless); and a HARD kill that
  flushes nothing still has no exit path to run, so it surfaces as `dead-worker`
  (the liveness scan) with no discriminated cause. Minting a cause record from
  the scan was rejected here: `doctor` is a `query` primitive with
  `side_effects=[]` and the attention collectors are source-scanned for write
  calls, so the mint would have to land on a mutating verb (`wait-detached` is
  the natural seat) — deliberately left for that unit rather than smuggled into
  a read path.
- 2026-07-30 (pillar 5, adversarial review): one BLOCKING defect and five
  findings fixed. **The blocker: `worker-terminal` items never cleared.** The
  journal is append-only and the collector projected one item per record, so a
  run that died three times and then succeeded rendered three standing BLOCKED
  items forever — the attention queue's own "an item persists until the human
  clears its SUBJECT" rule, broken by the kind that most needed to obey it. The
  fix gives the item a clearable subject identity (`(run_id, block)`, latest
  record only) plus a resolution predicate over substrate the other kinds already
  read (`ANOMALY_STATUSES`/`TERMINAL_STATUSES` and the block-terminal store);
  both legs fail SAFE (unreadable → the item stands, because wrongly hiding a
  failure is the defect and a stale item is the cheaper error). Ten mutations
  against those predicates die. **The other material finding:** the
  staging-exhaustion arm was DEAD CODE with a false provenance claim — its three
  markers appear nowhere in the tree, and its docstring asserted the
  shared-vocabulary rationale that the `PathCause` arms legitimately have but a
  composed-message match does not. It now matches what `_stage_exhausted_error`
  actually writes, is pinned by a test that reads that composer's source, and
  carries a negative test so a later loosening is caught; the docstring separates
  the two provenances instead of borrowing the stronger one. The
  `flap_exhausted_staging` summary was likewise staging-specific while its own
  battery demonstrated a probe-raised flap — it now leads with the stamped
  identity. Remaining fixes: `[fatal]`-still-written assertions on all three exit
  arms; the "carries everything the log does" test now DERIVES its fact set from
  `emit_fatal_block`'s real output, so a new emitter fact fails it;
  `record_latency_seconds` documented as structurally 0.0 until the scanner-side
  mint exists; `:` added to the path guard (a Windows drive-relative `run_id`
  escapes with no separator at all); and when the zombie class wins precedence
  over a dead hop, the composed message now LEADS with the route fact, so that
  menu's `/submit-hpc` option is not read as "resubmit through the hop that just
  killed the worker".
