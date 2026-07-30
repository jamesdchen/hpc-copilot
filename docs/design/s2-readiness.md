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
- Pillar 5 (partial): recoveries registry exists; S2 wiring absent.
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

## Pillar 6 as built: the three fields

- `y_to_array_accepted_seconds` — the EARLIEST submit-chain `y` targeting
  `submit-s2` or later, to `RunRecord.submitted_at`. Later y's in the chain
  fall INSIDE the window on purpose: they are latency the human paid.
- `interventions_count` — every submit-chain decision record, nudges included.
- `readiness_age_at_fire_seconds` — the durable ledger's age reconstructed AT
  the fire instant (atoms stamped later are excluded, never back-dated);
  `None` when there is no ledger.

All three are declared `cumulative` in `ops/monitor/summary.FIELD_KIND` and
render through `_render_scalar`, so `scripts/lint_telemetry_labels.py` covers
them: a total elapsed time can never acquire the `+` delta marker.

## Drift log

- 2026-07-30: created from the user direction; nothing built against
  pillars 1/6 yet.
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
