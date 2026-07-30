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
- Pillars 1 and 6: NEW — need design sign-off on the readiness substrate
  shape (one file per cluster under the journal home, breaker-adjacent) and
  the SLO fields before building.

## Drift log

- 2026-07-30: created from the user direction; nothing built against
  pillars 1/6 yet.
