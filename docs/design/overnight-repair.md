---
status: planned
---
# Overnight repair — the A/B/C1/C2 heal taxonomy (design)

**Status: DESIGN (2026-07-10, rulings user-ratified 2026-07-10). Build is
post-run-#12 batch item 2** — run-#12 evidence feeds the retryable-class
inventory and the surprise-tiered briefs before any code lands. Cite
`path::symbol`, never line numbers. Record implementation drift in the drift
log at the foot (the `docs/design/notebook-audit.md` pattern).

## Why now

The overnight substrate shipped (`ops/overnight.py`: standing consent bound to
`cmd_sha` with hard caps, the consumption ledger, `self_heal_campaign`, the
morning brief) — but it heals exactly ONE thing: a dead reconcile chain, by
re-arming the sanctioned watcher. Every other overnight failure — a node
death, a quota trip, a transient filesystem error, an env-drift crash, a
corrupted staging tree — either parks until morning (wasted cluster-hours the
human consented to spend) or tempts a driving agent to improvise a "fix" that
silently reshapes results. Runs #7–#11 produced both failure modes. The user
ruled the taxonomy on 2026-07-10: which repairs an unattended agent may make,
which need a human anchor first, and which are never repairs at all.

The scope doctrine still binds: **observe / judge / route, never actuate**
applies to the *watcher* substrate; a repair under this design is a routed,
journaled, capped action whose class determines its authorization — it is not
a license to actuate freely.

## The two discriminators (apply in order)

Every candidate repair is classified by two questions, and the FIRST question
is pipeline position — path zones (scratch vs results dirs) are a first
filter only, never the verdict:

1. **Pipeline position.** Does the fix touch *mechanics upstream of results*
   (transport, staging, scheduling, watching, environment activation) or does
   it *operate on result values* (what rows exist, what numbers they carry,
   what code produced them)? Upstream mechanics are candidates for A/B.
   Anything operating on result values falls through to C.
2. **The stateable-invariant test.** Can the invariant the fix restores be
   STATED as a checkable predicate, and does a *journaled anchor* already
   exist to check it against? Stateable + anchored → B. Stateable but
   unanchored → C1. Not stateable (the "fix" is a preference about results)
   → C2.

## The four classes

### Class A — invariant-by-construction: heal freely

The fix restores an invariant the system can verify *by construction* — the
healed state is definitionally correct, no result value can change, and the
check is the fix itself. Examples:

* re-arming a dead watcher (`ops/overnight.py::self_heal_campaign` — the
  already-shipped instance; the single-lease guard makes a duplicate spawn a
  disclosed no-op);
* respawning a dead detached worker against its own lease/spec;
* recreating a missing scratch directory the run spec names.

(A dropped *connection* is deliberately NOT an A example: re-dialing is an
SSH action, and the zero-unattended-cold-SSH rule means the healer process
never dials — connection recovery happens inside a spawned detached child
that owns the one cold dial, per the enactment rule below.)

**Obligations:** journaled to the consumption ledger (the
`HEAL_ATTEMPT_KIND` pattern), capped per consent, disclosed in the morning
brief. No re-verification beyond the construction itself.

**The enactment rule (binds A and B).** Detection and routing are LOCAL-ONLY
(the doctor scan / the healer process never opens SSH — the shipped
`self_heal_campaign` posture). Any heal that must touch the cluster is
ENACTED by spawning a detached submit-block child that owns the one cold
dial (`_kernel/lifecycle/detached.py::launch_submit_block_detached` — the
exact mechanism the watcher re-arm already uses), never by the detecting
process dialing. The doctor seat routes and journals; it never enacts.

### Class B — journaled-anchor heal + VERIFY

The fix restores *intent* against an anchor the journal already carries, and
correctness is checkable but not by construction — so the heal MUST re-verify
invariance against the anchor AND run a fresh canary before the repaired path
carries load. The four recognized anchor kinds:

* **determinism fingerprint** (`docs/design/determinism-fingerprint.md`) —
  resubmit crashed-for-mechanical-reasons tasks; the repaired task's
  fingerprint must match;
* **known-answer window** — a repaired path must reproduce the recorded
  known answer before proceeding;
* **generator spec** — re-derive a damaged task list from the journaled
  generator spec (never hand-reconstruct);
* **data-env manifest** (`docs/design/data-manifest.md`) — re-stage a
  corrupted deploy tree; the re-staged tree must re-verify against the
  manifest.

**Obligations:** everything in A, plus the anchor named in the ledger line,
plus the verification result journaled (a heal whose verify FAILED flips to
fail-loud — it never "tries harder"), plus a fresh canary with
**boundary-index sampling**: sample canary tasks at the *boundaries* of the
repaired range (first/last affected index), not just index 0 — run-#10's
harvest-gap class showed edge indices are where repairs go wrong.

### Class C1 — head-anchored: ELICIT-THEN-HEAL

The invariant is stateable but no journaled anchor exists yet — the anchor is
in the human's head. The agent does NOT heal; it composes the candidate
anchor as a typed elicitation through the popup (the default read-and-sign
surface, `docs/design/mcp-elicitation.md` unified-render ruling 2026-07-10),
and the human's `y` does two things at once:

1. authorizes THIS heal;
2. **mints the anchor PERMANENTLY** — the signed anchor joins the scope's
   anchor ledger, so the same situation next episode is Class B, healed
   without waking anyone.

The anchor ledger grows per episode. This is the ratchet: the system's
unattended-repair range expands exactly as fast as the human signs anchors,
never faster. Overnight (no human awake), a C1 finding parks with the
composed anchor ready in the morning brief — the elicitation fires at wake,
not into the void (the declared-but-dark rule — a proposed run-#12 watch
item, pending that run).

**Obligations:** the elicitation carries the full render of what would be
healed and what predicate the anchor states; the minted anchor is an
`append-decision` record riding the authorship gates (a laundered anchor is
the sign-off-echo class — the Stop hook's echo detection applies).

### Class C2 — result-shaping / anchorless: REPORT-ONLY

The "fix" operates on result values or selects among them, and no predicate
distinguishes fixed from shaped. The canonical members, all ruled: **version
UPGRADES** (an upgraded solver is a different experiment), **winsorize /
outlier drop**, **dtype changes**, **retry-selection** (rerunning until a
task "passes" selects results). A crash that only reproduces under one
library version is not healed by upgrading — it is REPORTED, and the report
**becomes science**: a C2 finding is routed into the run story / attention
queue as an observation about the experiment, not an infrastructure event.

**Resource-exhaustion crashes (OOM / walltime kill) are C2-adjacent, never
auto-healed.** The discriminators alone would route them to B (upstream
position, generator-spec anchor) — but no predicate distinguishes a
transient neighbor-job node OOM from a parameter regime that GENUINELY
exceeds memory/walltime, and the latter is the finding itself. The
scheduler+node+task classifier resolves *where/what*, not *whether the crash
is meaningful*, so an exhaustion crash routes to ELICIT (C1-style, with the
resubmit proposal as the composed heal) or REPORT — an auto-resubmit here is
exactly the "stopped crashing = removed evidence" failure rule 1 forbids.

**Env-drift straddles B and C2 — the pin decides.** A manifest mismatch the
heal can restore to the PINNED version (re-stage the recorded artifact) is a
B re-stage; a mismatch that cannot be restored to the pin (the pinned
version is gone; only a newer one exists) is the ruled C2 version-upgrade
case — report, never "heal" by accepting the newer version.

**Standing rule:** result anomalies are ALWAYS C2 — an anomalous number is
never evidence of a mechanical fault to repair; it is a finding.

## Standing rules (bind across all classes)

1. **Crash healed only after cause classified.** "Stopped crashing" is
   *removed* evidence, not success — a retry that succeeds without a
   classified cause destroys the reproduction. The classifier
   (scheduler+node+task identity — `cluster_env_init` failure classification,
   a proposed run-#12 watch item) runs first; only a
   classified-mechanical cause enters the A/B ladder.
2. **Result anomalies always C2** (above).
3. **Path zones = first filter only.** "It's in scratch" admits a candidate
   to classification; it never authorizes anything by itself.
4. **Two-zone consent identity.** One standing consent, two zones of
   consumption: *infra heals* (A, and B under caps) consume the consent
   mechanically; *semantic boundaries* (anything C, plus every
   non-`OVERNIGHT_CONSUMABLE_BLOCKS` boundary) PARK regardless of any live
   consent. The consent record's `resolved` gains a declared heal-class cap
   (`heal_classes: ["A"]` or `["A","B"]`) — a consent that names no classes
   heals nothing beyond the shipped watcher re-arm.
5. **Every fix committed + through the pipeline.** A repair that edits any
   tracked file (env spec, task list, executor) is a COMMIT that rides the
   normal submit pipeline (fingerprint, canary, gates) — never an in-place
   mutation on the cluster. Cluster-side state repairs (re-staging) verify
   against the manifest instead.
6. **Mechanical retry = bottom rung.** Rerunnable-flag resubmission and the
   bulk stale-in-flight reconcile (`ops/monitor/reconcile_stale.py` — a
   record CLOSER, idempotent, never-actuate) are the floor of the ladder —
   cheap, already shipped. The taxonomy governs everything ABOVE that rung;
   it never makes plain retry harder.

(Verified 2026-07-10: the rule-4 `heal_classes` cap is buildable as stated —
`resolved` is an open dict on `AppendDecisionInput`, the consent record
already carries analogous fields (`heal_attempts_cap`), and `heal_classes`
survives the code-derived-field and brief-provenance gates.)

## Substrate map (exists → build)

| Seam | Exists | Build |
|---|---|---|
| Consent + caps + wake | `ops/overnight.py::assert_consent_hard_caps` / `assert_wake_armed` | `heal_classes` cap on the consent record |
| Heal audit trail | consumption ledger, `HEAL_ATTEMPT_KIND`/`HEAL_FAILED_KIND` | per-class ledger detail: `heal_class`, `anchor_ref`, `verify_result` |
| The one shipped heal | `self_heal_campaign` (watcher re-arm, Class A) | the classifier front-end: crash-cause → class routing |
| Doctor seat | `ops/recover/doctor.py` `spec.self_heal` → `self_heal_scan` | detection + class ROUTING only (the seat stays no-SSH/never-actuate); enactment = spawned detached submit-block children per the enactment rule |
| Anchors | fingerprint, manifest, generator spec, known-answer (all journaled) | the C1 anchor ledger + mint-on-y elicitation |
| Morning brief | `overnight_morning_brief` (leads with heal failure) | per-class sections; C1 parked elicitations; C2 findings routed to run story |
| Fresh canary | S2 canary machinery | boundary-index sampling parameter |

## Evidence wanted from run #12 (before build)

* the actual overnight failure census: which classes occur, at what rates —
  the retryable-class inventory that decides build order;
* whether B-class verify-then-canary latency is acceptable at real scale;
* surprise tiers for the morning brief (which disclosures the human actually
  reads first — feeds the tiered-briefs design).

## Open questions

* **Spend meters.** `standing_consent_status` takes `spent_budget` /
  `spent_walltime` but callers pass 0.0 — B-class heals that resubmit tasks
  make a real meter load-bearing. Order relative to first B heal: meter
  first.
* **C1 overnight wake.** Should a C1 park be allowed to fire a push (via
  `notification_plan`) or strictly wait for the morning brief? Proposed:
  push only when the parked heal blocks a consented boundary.

## Drift log

*(empty — no implementation yet)*
