---
status: design
---
# Overnight repair — the A/B/C heal taxonomy (design)

**Status: DESIGN. The four-class taxonomy (A / B / C1 / C2) was user-ratified
2026-07-10; the post-run-#12 batch (item 2) folds in run-#12's overnight-failure
census as worked, classified exhibits (§6).** This doc was deliberately written
AFTER run #12 so its evidence feeds two things the taxonomy could not settle in
the abstract: the **retryable-class inventory** (which failures actually occur
overnight, at what rates — decides build order, §8) and the **surprise-tiered
morning briefs** (which disclosures the human reads first, §7.4). Build stays
sequenced behind the 2026-07-11 bug-sweep swarm and the run-#13 defense layers
(§8); the taxonomy and the evidence are now complete, so the classifier
front-end can be built against real cases rather than hypotheticals. Cite
`path::symbol`, never line numbers. Record implementation drift in the drift log
at the foot (the `docs/design/notebook-audit.md` pattern).

This doc is a standalone design surface, not folded into the substrate's home.
The overnight SUBSTRATE (`ops/overnight.py`) has no standalone design doc — it
rode `docs/design/notebook-audit.md` **item 8** and its amendments (the watch
leg, the wake leg, the self-heal ruling). The repair TAXONOMY is a distinct,
sizable, separately-ratified surface (four choreographies, an anchor ledger, a
classifier front-end); it earns its own file and cross-links the substrate
rather than swelling item 8.

## 1. Why now

The overnight substrate shipped (`ops/overnight.py`: a standing consent bound
to `cmd_sha` with hard caps — `assert_consent_hard_caps`; the consumption
ledger — `record_consumption`; the one heal — `self_heal_campaign`; the morning
brief — `overnight_morning_brief`). It heals exactly ONE thing: a dead
reconcile chain, by re-arming the sanctioned watcher. Every other overnight
failure — a login node that can no longer fork (finding 20), a NAT-severed
remote leg (finding 24), a quota trip, an env-drift misroute (finding 24
addendum), a corrupted staging tree — either parks until morning (cluster-hours
the human consented to spend, wasted) or tempts a driving agent to improvise a
"fix" that silently reshapes results. The user ruled the taxonomy on 2026-07-10:
which repairs an unattended agent may make, which need a human anchor first, and
which are never repairs at all.

The scope doctrine still binds: **observe / judge / route, never actuate**. A
repair under this design is a routed, journaled, capped action whose CLASS
determines its authorization — never a license to actuate freely. The
already-shipped `self_heal_campaign` is the reference posture: it re-arms a
watcher and nothing else, reads only local state, and spawns a detached child to
own the one cold SSH dial.

## 2. The ratified taxonomy — four classes (A / B / C1 / C2)

The ratified taxonomy (user, 2026-07-10) is **four classes**: A, B, C1, C2. C1
and C2 are distinct ruled classes, not merely two handlings of one "C" — they
differ in whether an anchor CAN be minted (C1) or the fix is anchorless by
nature (C2). (An earlier compressed statement collapsed them to a single Class C;
the full battle-plan ruling is authoritative — this doc carries the four-class
split.)

| Class | What it restores | Authorization | Re-verify? |
|---|---|---|---|
| **A** invariant-by-construction | an invariant true *by construction* — the check IS the fix | heal freely (under caps + consent) | no (construction is the proof) |
| **B** journaled-anchor | *intent* against a **journaled anchor** (fingerprint / known-answer / generator-spec / manifest) | heal + **MUST re-verify invariance + fresh canary** | yes |
| **C1** head-anchored | a stateable invariant whose anchor is in the human's head | **ELICIT-THEN-HEAL** via the popup — one `y` mints the anchor PERMANENTLY | after mint → B |
| **C2** result-shaping / anchorless | nothing checkable — a *preference* about results | **REPORT-ONLY** — becomes science | n/a |

**Standing rule that overrides the table:** *a crash may only be healed once its
CAUSE is classified.* "Stopped crashing" is REMOVED evidence, not success (§5).
And *result anomalies are ALWAYS C2* — an anomalous number is never evidence of
a mechanical fault to repair; it is a finding.

**The bottom rung — mechanical retry.** Beneath the whole taxonomy sits the floor
that always applies: rerunnable-flag resubmission and the bulk stale-in-flight
reconcile-resubmit-with-dedup (`ops/monitor/reconcile_stale.py` — a record CLOSER,
idempotent, never-actuate). It is cheap and already shipped; the four classes
govern everything ABOVE it and never make plain retry harder (rule 6, §7.2).

## 3. The classification procedure

### 3.1 Path zones are the FIRST filter only — never a verdict

"It is in scratch" ADMITS a candidate to classification; it authorizes nothing
by itself. A scratch-dir action can still be Class C2 (a scratch file whose
CONTENT is a result value). A results-dir action is never A/B. The zone gates
entry to the ladder; the two discriminators decide the class.

### 3.2 The cause-classification gate (runs before everything)

No candidate enters the A/B ladder until its crash CAUSE is classified by the
scheduler+node+task identity classifier (the `cluster_env_init` failure
classification — a run-#12 watch item). An unclassified crash is NEVER healed:
a retry that succeeds without a classified cause destroys the reproduction
(§5). A classified-mechanical cause proceeds; a cause that IS the finding
(a parameter regime that genuinely OOMs) routes to C.

### 3.3 The two discriminators (apply in order)

1. **Pipeline position.** Does the fix touch *mechanics upstream of results*
   (transport, staging, scheduling, watching, environment activation) or does
   it *operate on result values* (what rows exist, what numbers they carry,
   what code produced them)? Upstream mechanics are candidates for A/B; anything
   operating on result values falls through to C.
2. **The stateable-invariant test.** Can the invariant the fix restores be
   STATED as a checkable predicate, and does a *journaled anchor* already exist
   to check it against?
   - stateable **+ anchored** → **B**
   - stateable **but unanchored** (the anchor is in the human's head) → **C1**
   - **not stateable** (the "fix" is a preference about results) → **C2**

## 4. The four (three-plus-one) classes in detail

### 4.1 Class A — invariant-by-construction: heal freely

The healed state is definitionally correct: no result value can change and the
check is the fix itself. Examples:

* re-arming a dead watcher (`ops/overnight.py::self_heal_campaign` — the shipped
  instance; the single-lease guard makes a duplicate spawn a disclosed no-op);
* respawning a dead detached worker against its own lease/spec;
* recreating a missing scratch directory the run spec names;
* **reaping marked, over-age login-node strays** (finding 20 — §6a): the target
  set is invariant-by-construction (a process carrying the `HPC_AGENT_OP` marker
  AND older than the max legitimate deadline is *definitionally* an orphan), so
  killing exactly those PIDs cannot touch a result or an unmarked process.

A dropped *connection* is deliberately NOT an A example: re-dialing is an SSH
action, and the zero-unattended-cold-SSH rule means the healer process never
dials — connection recovery happens inside a spawned detached child that owns
the one cold dial (the enactment rule).

**Obligations:** journaled to the consumption ledger (`HEAL_ATTEMPT_KIND`),
capped per consent, disclosed in the morning brief. No re-verification beyond
the construction itself.

**The enactment rule (binds A and B).** Detection and routing are LOCAL-ONLY
(the doctor scan and the healer process never open SSH — the `self_heal_campaign`
posture). Any heal that must touch the cluster is ENACTED by spawning a detached
submit-block child that owns the one cold dial
(`_kernel/lifecycle/detached.py::launch_submit_block_detached` — the exact
mechanism the watcher re-arm uses), never by the detecting process dialing. Even
`stray-sweep`, which opens its own SSH, is spawned as a detached child under the
overnight seat rather than dialed from the doctor process (§6a).

### 4.2 Class B — journaled-anchor heal + VERIFY

The fix restores *intent* against an anchor the journal already carries, and
correctness is checkable but NOT by construction — so the heal MUST re-verify
invariance against the anchor AND run a fresh canary before the repaired path
carries load. The four recognized anchor kinds (all already journaled):

* **determinism fingerprint** (`docs/design/determinism-fingerprint.md`) —
  resubmit crashed-for-mechanical-reasons tasks; the repaired task's fingerprint
  must match the recorded one;
* **known-answer window** — a repaired path must reproduce the recorded known
  answer before proceeding;
* **generator spec** — re-derive a damaged task list from the journaled
  generator spec (never hand-reconstruct);
* **data-env manifest** (`docs/design/data-manifest.md`) — re-stage a corrupted
  deploy tree; the re-staged tree must re-verify against the manifest.

**Obligations (§7):** everything in A, PLUS the anchor named in the ledger line
(`anchor_ref`), PLUS the verification result journaled (`verify_result`) — a
heal whose verify FAILED flips to fail-loud; it never "tries harder" — PLUS a
fresh canary with **boundary-index sampling** (below).

### 4.3 Class C1 — head-anchored: ELICIT-THEN-HEAL

The invariant is stateable but no journaled anchor exists yet — the anchor is in
the human's head. The agent does NOT heal; it composes the candidate anchor as a
typed elicitation through the popup (the default read-and-sign surface,
`docs/design/mcp-elicitation.md` / `docs/design/unified-render.md`, ruled
2026-07-10), and the human's `y` does two things at once:

1. authorizes THIS heal;
2. **mints the anchor PERMANENTLY** — the signed anchor joins the scope's anchor
   ledger, so the same situation next episode is Class B, healed without waking
   anyone.

Overnight (no human awake), a C1 finding PARKS with the composed anchor ready in
the morning brief — the elicitation fires at wake, not into the void (the
declared-but-dark rule).

**The firing site is `append-decision`.** The popup renders at the
`append-decision` boundary (run-#12 finding 8: the elicitation bubble fires at
`append-decision`, not at audit-view), and the human's typed `y` there IS the
mint. So the choreography reuses the shipped elicitation firing site verbatim —
no new consent surface. The minted anchor is the `append-decision` record itself,
riding the authorship gates (a laundered anchor is the sign-off-echo class — the
Stop hook's echo detection applies). The full render of what would be healed and
what predicate the anchor states rides the popup body
(`docs/design/mcp-elicitation.md` / `unified-render.md`).

**The anchor ledger grows per episode — this is the ratchet.** Each signed C1
anchor joins the scope's permanent anchor ledger, so the identical situation next
episode classifies as B and heals unattended. The unattended-repair range expands
exactly as fast as the human signs anchors, never faster.

### 4.4 Class C2 — result-shaping / anchorless: REPORT-ONLY

The "fix" operates on result values or selects among them, and no predicate
distinguishes fixed from shaped. The canonical members, all ruled: **version
UPGRADES** (an upgraded solver is a different experiment), **winsorize / outlier
drop**, **dtype changes**, **retry-selection** (rerunning until a task "passes"
selects results). A crash that only reproduces under one library version is not
healed by upgrading — it is REPORTED, and the report **becomes science**: a C2
finding is routed into the run story / attention queue
(`docs/design/run-story.md`, `docs/design/attention-queue.md`) as an observation
about the experiment, not an infrastructure event.

**Resource-exhaustion crashes (OOM / walltime kill) are C-adjacent, never
auto-healed.** The discriminators alone would route them to B (upstream
position, generator-spec anchor) — but no predicate distinguishes a transient
neighbor-job node OOM from a parameter regime that GENUINELY exceeds
memory/walltime, and the latter is the finding itself. So an exhaustion crash
routes to ELICIT (C1, with the resubmit proposal as the composed heal) or REPORT
— an auto-resubmit here is exactly the "stopped crashing = removed evidence"
failure §5 forbids.

**Env-drift straddles B and C — the pin decides.** A manifest mismatch the heal
can restore to the PINNED version (re-stage the recorded artifact) is a B
re-stage; a mismatch that cannot be restored to the pin (the pinned version is
gone; only a newer one exists) is the ruled version-upgrade case — report, never
"heal" by accepting the newer version.

**Version upgrades — the reconciled ruling (see drift log 2026-07-11).** An
earlier amendment (2026-07-10) softened version upgrades to
"disclosure-sufficient." The post-run-#12 ratification RESTORES version change to
Class C2 for the AUTONOMOUS overnight healer, and the two are reconciled by WHO
acts: an agent NEVER auto-upgrades a wheel/solver/library overnight (C2 —
report-only); when a HUMAN or the normal release pipeline changes a version, a
prominent disclosure (results marked as produced under the new version) suffices
and is not a tripwire block. Finding 20/24's stale-wheel reinstall (§6c) is the
exhibit: the human ran it; that IS the C choreography — human-enacted, agent-
reported — and it is exactly why an agent must not do it unattended.

## 5. The standing rule — cause before cure (flagship: the three-stacked roots)

*A crash may only be healed once its CAUSE is classified.* "Stopped crashing" is
REMOVED evidence, not success. Run #12's 2026-07-11 saga is the flagship exhibit
for why (finding 24, the "misattributed twice" chain):

The "empty reporter output" failure was attributed, in order, to (1) login-node
fork exhaustion (finding 20 — real, but a SEPARATE fault), then (2) the stale
demo wheel (finding 24 — real, also separate), and only finally to (3) NAT
idle-drop severing the silent remote leg. The tell that finally discriminated
was `ps` on the login node showing the remote python half OUTLIVING its severed
channel — not any "the crash stopped" signal.

Had an unattended agent "healed" by reinstalling the wheel and observed the
symptom shift, it would have banked a false success while TWO real causes
(fork exhaustion, NAT sever) hid behind it. The wheel install fixed nothing
observable on its own — three causes were stacked, and "stopped crashing" would
have masked the two beneath the one that moved. Hence: the cause-classification
gate (§3.2) runs first; a retry that succeeds without a classified cause
destroys the reproduction and is forbidden.

## 6. Worked examples from run #12 (the overnight-failure census)

Findings 20–24 (`docs/design/history/run12-findings.md`) are the first real
census. Each is classified below.

### 6a. Login-node fork exhaustion → "kill orphaned marked processes" = **Class A**

Finding 20: orphaned remote halves of killed ssh commands accumulated until
`jc_905`'s fork quota wedged (`.bashrc: fork: retry: Resource temporarily
unavailable`). The heal "kill the orphaned processes" is **Class A —
invariant-by-construction via the marked-stray rule**: the mechanized healer is
the new `stray-sweep` verb (`ops/recover/stray_sweep.py::stray_sweep`), which
reaps ONLY PIDs that (i) carry the `HPC_AGENT_OP` marker and (ii) exceed the max
legitimate deadline (`--max-age-sec`, default 3900) — a set that is
*definitionally* orphaned framework processes, never an unmarked user process
(`parse_ps_output` enforces both predicates). Killing that set cannot change a
result value; the construction IS the proof, so no re-verification is owed.

Two nuances the enactment rule imposes: (1) `stray-sweep` opens its own SSH (one
fork-minimal `ps`), so under the overnight seat it is SPAWNED as a detached
child that owns the cold dial — not dialed from the doctor process (whose
contract is no-SSH). (2) Detection is free and cheap; only `reap: true` acts. The
observability half (surfacing the stray count — "47 strays would have been
visible days before the quota wedged") is pure disclosure into the morning brief,
Class-A-adjacent and always on.

### 6b. NAT idle-drop → "add keepalives" = **config-class, NOT an overnight heal**

Finding 24: a NAT middlebox dropped the idle TCP flow at ~100s, severing every
long-silent remote leg. "Add keepalives" is NOT a per-episode overnight repair —
it is a **build-time transport property**, already fixed in-repo by splicing
`-o ServerAliveInterval=30 -o ServerAliveCountMax=60` into every framework
ssh/scp (`HPC_SSH_KEEPALIVE_INTERVAL` tunable). It ships in the wheel; it is not
something the overnight healer decides to do to a live run. The overnight-
relevant residue is the SEVERED-CONNECTION recovery, which is **Class A**: re-arm
the watcher inside a fresh detached child whose dial already carries keepalives
by construction. Keepalives PREVENT the fault; they are not a heal OF it. (Where
a human wants to change the interval at runtime, that is a config/env change —
report/human, never an autonomous overnight action.)

### 6c. Stale-wheel reinstall → **Class C2 (report-only)** — even though it fixed things

Finding 24: the demo env carried a stale wheel (missing the `#8` inflight-counter
fix; still exporting a retired `HPC_SSH_ENGINE`), and reinstalling it was part of
the remediation. It is **Class C2 — a version change is result-altering, hence
REPORT-ONLY for the autonomous healer** — *even though the human's reinstall
genuinely helped tonight*. That is precisely the point: the human ran it, with
disclosure; an agent must never auto-reinstall a wheel overnight, because a
version change makes the repaired path a different experiment and no predicate
distinguishes "fixed" from "silently reshaped." This example also anchors §5:
the reinstall "fixed" nothing observable by itself — two more causes hid behind
it — so an agent that healed-by-reinstall and saw the symptom move would have
banked a false success.

### 6d. `HPC_SSH_ENGINE` env drift → detection shipped; heal is **RULING-NEEDED (C1 lean)**

Finding 24 addendum: a run-11-era `HPC_SSH_ENGINE=asyncssh`, recorded as RETIRED,
was still exported in the live env and silently rerouted every ssh through the
engine whose idle reaper severed connections. **Detection is shipped and is pure
disclosure**: the doctor `active_env_overrides` seat
(`ops/recover/doctor.py::_active_env_overrides`) echoes every live `HPC_*` var
verbatim into every brief — "an unexpected entry IS the finding"; it never judges
a value.

The HEAL classification is genuinely underdetermined (**RULING-NEEDED**, §9):

* Discriminator 1 puts an engine choice UPSTREAM of results (transport
  mechanics), admitting it to A/B.
* Discriminator 2 asks for a journaled anchor. There is NONE today — no journaled
  "expected env" manifest — so unsetting the drifted var is **stateable but
  unanchored → C1**: elicit "unset `HPC_SSH_ENGINE`?" and, on `y`, mint an
  **env-pin anchor** so the same drift next episode is Class B (auto-restore env
  to the pinned set, re-verify, canary).
* The open question is whether an env override belongs in the SPEC-IDENTITY
  fingerprint (`cmd_sha`). If it does, a drift is a *spec change* that kills the
  consent (`standing_consent_status` → `spec-changed`) and there is nothing to
  "heal" — the consent simply dies and the human is consulted. If it does not, the
  C1 env-pin-anchor path above is the disciplined route. This is the RULING-NEEDED
  item: **are transport env overrides part of spec identity, or infra-mechanics
  healable via a minted env-pin anchor?**

Interim (build-safe regardless of the ruling): REPORT-ONLY — surface the drift in
every doctor brief and the morning brief; do not auto-unset.

## 7. Obligations that bind repairs

### 7.1 The re-verification obligation for Class B

A B heal is not done when the repair action returns — it is done when the anchor
re-verifies AND a fresh canary passes:

* **invariance re-check** against the named anchor (fingerprint match / known
  answer reproduced / manifest re-verify / generator-spec re-derivation), the
  result journaled as `verify_result`. A FAILED verify flips the heal to
  fail-loud (`HEAL_FAILED_KIND` posture) — it never retries into a different
  outcome.
* **a fresh canary** through the existing S2 canary machinery, gated by the same
  announce markers a normal canary uses — the repaired path re-earns its
  greenlight; it is not grandfathered by the fact that it "used to work."
* **boundary-index sampling** rides along: sample canary tasks at the BOUNDARIES
  of the repaired range (first/last affected index), not just index 0. Run-#10's
  harvest-gap class showed edge indices are where repairs go wrong; a heal that
  re-canaries only index 0 re-earns a greenlight the boundary would have denied.

### 7.2 Standing rules (bind across all classes)

1. **Crash healed only after cause classified** (§5). The classifier runs first;
   only a classified-mechanical cause enters the A/B ladder.
2. **Result anomalies always Class C2** (§2).
3. **Path zones = first filter only** (§3.1).
4. **Two-zone consent identity.** One standing consent, two zones of consumption:
   *infra heals* (A, and B under caps) consume the consent mechanically;
   *semantic boundaries* (anything C, plus every non-`OVERNIGHT_CONSUMABLE_BLOCKS`
   boundary) PARK regardless of any live consent. The consent record's `resolved`
   gains a declared heal-class cap (`heal_classes: ["A"]` or `["A","B"]`) — a
   consent that names no classes heals nothing beyond the shipped watcher re-arm.
   (Verified 2026-07-10: buildable — `resolved` is an open dict on
   `AppendDecisionInput`, already carrying analogous fields like
   `heal_attempts_cap`, and `heal_classes` survives the code-derived-field and
   brief-provenance gates.)
5. **Every fix committed + through the pipeline.** A repair that edits any tracked
   file (env spec, task list, executor) is a COMMIT that rides the normal submit
   pipeline (fingerprint, canary, gates) — never an in-place mutation on the
   cluster. Cluster-side state repairs (re-staging) verify against the manifest
   instead.
6. **Mechanical retry = bottom rung.** Rerunnable-flag resubmission and the bulk
   stale-in-flight reconcile (`ops/monitor/reconcile_stale.py` — a record CLOSER,
   idempotent, never-actuate) are the FLOOR of the ladder — cheap, already
   shipped. The taxonomy governs everything ABOVE that rung; it never makes plain
   retry harder.

### 7.3 Safety boundaries (the hard "never"s)

* **Never heal a Class C.** No autonomous version change, winsorize, drop, dtype,
  or retry-selection — ever, under any consent. C1 elicits; C2 reports.
* **Never heal an unclassified crash** (§5).
* **Caps are load-bearing.** Every heal is bounded by the consent's caps —
  `assert_consent_hard_caps` (an `expires_at` morning boundary + at least one of
  `budget_cap`/`walltime_cap`) plus the per-heal `heal_attempts_cap`. A
  deterministically-failing heal (a spawn that keeps failing) counts against the
  cap (`_heal_respawn_count` counts `respawned` AND `spawn-failed`), so it cannot
  retry forever.
* **Fail loud, flip DEAD.** When the cap is exhausted or the heal is structurally
  impossible, the consent flips DEAD (`_mark_consent_dead` writes
  `HEAL_FAILED_KIND`), refuses every further auto-advance
  (`standing_consent_status` → `heal-exhausted`), fires the push where a channel
  exists (`notification_plan`), and the morning brief LEADS with the failure
  (`overnight_morning_brief`). The DEAD flip lives in the ledger, so it OUTLIVES
  the consent's own expiry — the disclosure never evaporates.

### 7.4 The morning brief reports each class differently

`overnight_morning_brief` gains per-class sections layered on the shipped
`failed_at` vs `surfaced_at` latency disclosure:

* **Class A / B heals** — the consumed-boundary list, each carrying `heal_class`,
  and for B the `anchor_ref` + `verify_result` + canary outcome.
* **Class C1 parked elicitations** — the composed anchors waiting for a `y`,
  rendered ready-to-sign at wake (declared-but-dark: the elicitation fires at
  wake, not into the void).
* **Class C2 findings** — routed OUT of the infra brief and INTO the run story /
  attention queue as observations about the experiment.
* **Fail-loud heal failures** — lead the brief, as today.

## 8. Implementation sequencing sketch

Build order is gated behind the 2026-07-11 bug-sweep swarm and the run-#13
defense layers (finding 20 layer 1/2, finding 24 keepalives) — those own the
neighboring files. Then, ordered by dependency:

1. **Spend meter first** (open question, §9): `standing_consent_status` already
   takes `spent_budget`/`spent_walltime` but callers pass 0.0; a B heal that
   resubmits tasks makes a real meter load-bearing. Meter before the first B heal.
2. **`heal_classes` cap** on the consent record (rule 4) + the consumption-ledger
   detail fields `heal_class` / `anchor_ref` / `verify_result`.
3. **Classifier front-end**: crash-cause → class routing (`cluster_env_init`
   failure classification), wired into the doctor seat as detection+routing ONLY
   (no SSH, never actuate); enactment stays spawned detached children.
4. **Class A widening**: fold `stray-sweep --reap` (finding 20) into the overnight
   seat as a spawned detached child; it is the second Class-A healer after the
   watcher re-arm.
5. **Class B machinery**: the verify-then-canary obligation (§7.1) + boundary-index
   sampling parameter on the canary path.
6. **Class C1 anchor ledger** + mint-on-`y` elicitation (rides the authorship
   gates); the env-pin anchor lands here IF the finding-24d ruling routes env
   drift to C1.
7. **Per-class morning-brief sections** (§7.4) + C2 routing into the run story.

## 9. Open questions / RULING-NEEDED

* **RULING-NEEDED — env overrides and spec identity (finding 24d, §6d).** Are
  transport env overrides (`HPC_SSH_ENGINE`, keepalive tunables) part of the
  spec-identity `cmd_sha`? If yes, a drift kills the consent (spec-changed) and
  there is no heal. If no, env drift is a C1-minted-env-pin-anchor heal. The
  taxonomy underdetermines this; interim posture is REPORT-ONLY.
* **RULING-NEEDED — steady-state engine lifecycle vs the taxonomy (finding 24
  library-boundary lesson).** The post-run-13 plan shrinks hand-rolled connection
  lifecycle to what no library can know (ban-risk breaker, connection-rate
  courtesy) and outsources idle/keepalive/multiplex management to asyncssh. Does
  the overnight healer treat a connection sever as A (re-arm) unconditionally once
  the library owns keepalives, or does a sever that RECURS under correct
  keepalives escalate to a C-style finding (a cluster-social signal, not a
  mechanical fault)? Deferred to the engine-first steady-state work.
* **Spend meters** (sequencing item 1). Order relative to first B heal: meter
  first.
* **C1 overnight wake.** Should a C1 park fire a push (via `notification_plan`) or
  strictly wait for the morning brief? Proposed: push only when the parked heal
  blocks a consented boundary.

## 10. Substrate map (exists → build)

| Seam | Exists | Build |
|---|---|---|
| Consent + caps + wake | `ops/overnight.py::assert_consent_hard_caps` / `assert_wake_armed` | `heal_classes` cap on the consent record |
| Heal audit trail | consumption ledger, `HEAL_ATTEMPT_KIND`/`HEAL_FAILED_KIND` | per-class detail: `heal_class`, `anchor_ref`, `verify_result` |
| The one shipped heal | `self_heal_campaign` (watcher re-arm, Class A) | the classifier front-end: crash-cause → class routing |
| Class-A stray reaper | `ops/recover/stray_sweep.py::stray_sweep` (finding 20) | fold `--reap` into the overnight seat as a spawned detached child |
| Env-drift detection | `ops/recover/doctor.py::_active_env_overrides` (finding 24d) | the finding-24d ruling → report-only vs C1 env-pin anchor |
| Doctor seat | `ops/recover/doctor.py` → `self_heal_scan` | detection + class ROUTING only (no-SSH/never-actuate); enactment = spawned detached children |
| Anchors | fingerprint, manifest, generator spec, known-answer (all journaled) | the C1 anchor ledger + mint-on-`y` elicitation |
| Morning brief | `overnight_morning_brief` (leads with heal failure) | per-class sections; C1 parked elicitations; C2 → run story |
| Fresh canary | S2 canary machinery + announce markers | boundary-index sampling parameter |

## Drift log

* **2026-07-11 — run-#12 census folded in (this revision).** Added §6 (findings
  20–24 as classified worked examples), §5 flagship (three-stacked-root-causes),
  §8 sequencing, §7.3 safety boundaries tied to `overnight.py` symbols. Status
  moved PARKED → DESIGN: the taxonomy + evidence are complete; build stays
  sequenced behind the bug-sweep swarm + run-#13 defense layers.
* **2026-07-11 — version-upgrade ruling reconciled.** The 2026-07-10
  "disclosure-sufficient" amendment is SUPERSEDED for the autonomous healer by the
  post-run-#12 ratification: an AGENT never auto-changes a version overnight (C2 —
  report-only); a HUMAN/pipeline version change with prominent disclosure is not a
  tripwire. Reconciled by WHO acts (§4.4, §6c). The stale-wheel reinstall
  (finding 24) is the exhibit.
* **2026-07-11 — two RULING-NEEDED items opened** (§9): env overrides vs spec
  identity (finding 24d); steady-state engine lifecycle vs the sever→A mapping
  (finding 24 library-boundary lesson).
* **2026-07-11 — four-class correction.** A compressed statement in the writing
  brief said "three-class (A/B/C)"; the authoritative battle-plan ruling is FOUR
  classes A/B/C1/C2 (C1 and C2 are distinct ruled classes, not two handlings of
  one C). This revision carries the four-class split throughout; the collapsed-C
  wording is flagged where it appeared (§2).
* **2026-07-10 — taxonomy ratified** (user). A/B/C1/C2; the standing rules; the
  mechanical-retry bottom rung; `heal_classes` consent cap verified buildable.
  (Prior PARKED note retired by the run-#12 fold-in.)
