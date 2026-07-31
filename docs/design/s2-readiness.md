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
- Pillar 3: **BUILT** (2026-07-30) — the four seamed invariants
  (`auth` / `scratch` / `scheduler` / `env`) became sensors + harvest sites,
  and the storage-side vocabulary extension collapsed into a plain mirror of
  `SensorKind` (see "Pillar 3 as built" below). The reactive package's L1/L2
  discriminated-cause work is the transport invariant's own slice of it.
- Pillar 4 (subset): reactive package L3 flap-riding retry (in build).
- Pillar 5 (partial): recoveries registry exists; S2 wiring absent.
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
/ `preamble` plus `auth` / `scratch` / `scheduler` / `env`. Those last four
were a storage-side extension while no sensor covered them; pillar 3 built
their sensors and they moved UP into `SensorKind`, so
`state/readiness.SENSOR_KINDS` is now a plain MIRROR rather than an extension —
one definition, which is what "extended, never forked" was always pointing at.
`SensorVerdict` (`ok` /
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
says so), plus the three submit-flow harvest sites pillar 3 added (below).
`auth` remains a seam BY CONSTRUCTION for the BREAKER — its SUCCESS verdict
folds an auth rejection into "reached the host" — which is exactly why the
sensor layer derives `auth` from the CONNECT SENSOR instead, where a zero exit
means a command really ran on the far end.

## Pillar 3 as built: four invariants, four owners

| invariant | sensor (probes when invoked) | harvest site (zero network) |
|---|---|---|
| `auth` | `auth_atom` — DERIVED from the connect reading's exit/stderr signature; no probe at all | rides every `sense_preamble` rung |
| `scratch` | `sense_scratch` — `test -d` + `df -P` (a bare `test -d` passes against a hung mount) | the submit flow's staging step: a landed rsync push + `deploy_runtime` is `ok`; a failure is `down` ONLY when its own stderr names a storage cause |
| `scheduler` | `sense_scheduler` — the family's cheap CLI banner (`squeue --version` / `qstat -help` class), family resolved from `clusters.yaml` | the main array dispatch: job ids returned is `ok`, the typed dispatch failure is `down` |
| `env` | `sense_env` — `hpc-agent --version` under the cluster activation, the release flow's own command class | the activation-class preflight (`command -v uv` after `module load` / `conda activate`) — **`runtime: uv` batches only today**, see the scope note below |

Two rules hold the shape:

- **A sensor may probe; a feed site may not.** The three probing rungs are
  OPT-IN on `read_path_readiness` (each is one more connection, and the S2 path
  gate must not pay for storage to answer a path question). Every feed site
  passes on a verdict already in hand, pinned by the no-network tripwire.
- **An invariant atom is never a PATH verdict.** They stay out of `_classify`:
  a full scratch disk is not a reason to call the path dead, and letting one in
  would make the S2 gate refuse naming the wrong cause. They degrade the
  cluster through `overall_verdict` instead, which is where "reachable ≠ usable"
  belongs.

A feed site that cannot attribute a failure records NOTHING. This is the rule
that costs the most to hold and matters the most, so it is stated once and
applied at all three sites:

- **staging** — a failure with no storage marker in its stderr is a transport
  fact, so no `scratch` atom is written.
- **dispatch** and **the env preflight** — a failure that
  `_stage_failure_is_flap` (this module's one transport-class definition, the
  staging retry's own) calls a flap writes NOTHING either. Both handlers would
  otherwise file `scheduler: down` / `env: down` for a severed tunnel, and the
  dispatch handler in particular would then be classifying ONE exception two
  ways — a flap for the job-id recovery twelve lines below, a scheduler verdict
  for the ledger.

Recording a guess here sends the next human to the queue system or the conda env
while the VPN is what is broken: the 2026-07-30 misdiagnosis class, one layer
down and pointed at a different wrong subsystem.

**Scope note — `env` harvests on `runtime: uv` batches only today.** The
activation-class preflight is the one place the submit flow runs the cluster's
own activation and holds the verdict; it is gated on `HPC_RUNTIME=uv`, so a
conda-runtime batch contributes no `env` atom. Widening it would mean ADDING a
remote call to a path that does not make one, which the harvest-never-probe rule
forbids — so the honest fix is either a new opt-in sensor rung (`net-triage
--probe-invariants` already offers exactly that) or a future activation-class
check that non-uv batches genuinely need for their own reasons. Until then a
conda-only cluster reads `env: unknown`, which is true.

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
- 2026-07-30 (pillar 3): the four seamed invariants got sensors + harvest
  sites (table above), and seam (b) above is CLOSED — `RunRecord.accepted_at`
  is stamped by `promote_submitting_record` in the SAME locked write as
  `job_ids` (so a crash cannot separate the stamp from the evidence it dates)
  and read through the one `s2_slo.accept_stamp` definition, which falls back
  to `submitted_at`. The fallback is EXACT rather than degraded wherever it
  fires: on the `submit_and_record` path `submitted_at` is itself taken with
  the ids parsed, so it already means "accepted" — which is why closing this
  needed no backfill. Seam (a) was already closed by the pillar-1 wave.
  Two shape decisions worth recording because they were not obvious:
  * The `scheduler` touch accepts a NON-ZERO exit that printed a banner.
    `qstat -help` exits non-zero on several Grid Engine builds while printing
    its usage — the CLI answered, which is the fact the sensor reads, and the
    strict reading would report a broken scheduler at every such site. The exit
    code stays disclosed in `detail`; the leniency is scoped to this one class.
  * `REQUIRED_SENSORS` stayed at `connect` alone. `scratch` / `scheduler` /
    `env` are fed by the SUBMIT flow, so requiring them for `ready` would pin
    every configured-but-not-yet-submitted-to cluster at `stale` forever —
    less truthful, not more.
  Regen: `net_triage.output.json` re-emitted in-branch (the `ReadinessAtom`
  sensor enum grew the four kinds) — one file, `build_schemas.py --write`, no
  deferred debt.
