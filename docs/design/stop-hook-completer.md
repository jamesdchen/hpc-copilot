---
status: implemented (dark — capability-gated; see drift log 2026-07-10)
---
# Stop-hook completer — rejector → completer (design)

**Status: DESIGN (2026-07-10, ruling user-ratified 2026-07-10). Build is
post-run-#12 batch item 1** — the flagship of the refusal + hook-bounce
inventory (Day-2 audit feeds the class mapping below before code lands).
Cite `path::symbol`, never line numbers. Drift log at the foot.

## Why now

`_kernel/hooks/relay_audit_stop.py` is a REJECTOR: every finding — a
contradicted number, an undischarged relay-due marker, an unjournaled
decision-state claim — blocks the stop once and forces a full extra model
turn to re-say what deterministic code already knows verbatim. That bounce
is the single most common source of extra-model-turn latency in proving
runs, and it is also a *trust downgrade*: the forced re-relay is model-typed
text, which is exactly the surface the paraphrase pass (G1) exists to police.
Code already holds the artifact (the render file, the journal value, the
marker's key tokens). The ruling (2026-07-10): **when code can complete the
relay itself, it appends the artifact directly — a code-appended render is
model-untouched and therefore MORE trusted than an LLM relay — and the
bounce survives only where judgment is genuinely required.**

This is the poka-yoke doctrine applied to the hook itself: compose/default/
auto-remedy what code can; refuse only at trust boundaries; a converted
refusal becomes a never-fires assertion.

## Tonight's evidence (run #12, 2026-07-10/11)

Three live episodes ground the design — one for each side of the boundary.

* **The rendezvous livelock (finding 21).** In the aggregate chain for
  `causal_tune_linear-de448128`, `decision_rendezvous_stop_guard` REJECTED
  *every* turn-end for hours — "invoke `block-drive` … (do not end the turn)"
  — while the login node was fork-exhausted (finding 20) and could not
  advance the driver. Each forced continuation re-ran `aggregate-check`, i.e.
  fired another SSH volley at the starved node: the rejector became an SSH
  amplifier. `stop_hook_active` capped it at one forced tick per turn, so it
  was a per-turn tax, not an in-turn spin — but ~15 block-drive ticks were
  spent bouncing the turn back to the LLM to do what code was already trying
  to do. The obligation here — "advance the driver" — is *mechanical*: code
  can run the tick (it is `block-drive`), so the bounce buys nothing. This is
  the omission class hiding inside a Stop guard the completer inventory had
  not yet reached. (The NARROW half of finding 21 — a *consumed* greenlight
  re-arming the guard forever — is fixed independently via the boundary-scoped
  `greenlight_targets_boundary` predicate; see the migration table. The
  completer question is the DEEPER half: even a legitimately-unconsumed park
  should not bounce a mechanical advance.)

* **The rule-10 false positives (findings 19, tonight).** The relay-audit
  rejector forced the agent to *reword true statements* three ways this run:
  it flagged `27` as an unsupported numeric claim when it was exactly
  `len(job_ids)` (finding 19); it read the prose token `5-minute-enumeration`
  as a run-id-shaped mention; and it flagged the words `timeout` / `failure`
  as state contradictions against a journal whose run was legitimately
  `in_flight` (a transport timeout and a task failure are both true and both
  compatible with an in-flight array). Every one was a TRUE statement the
  hook made the model launder into different words — the opposite of the
  distortion it exists to catch. These are the evidence that a detector's
  precision is a PRECONDITION for completion (see "The false-positive tax").

* **The precedents that already complete (skill-return, brief, alert).**
  `skill_return_autofetch`, `decision_rendezvous_autofetch`, and
  `alert_count` already discharge their obligation IN CODE — they inject the
  envelope / brief / alert count and never bounce. The completer is not a new
  shape; it is the generalization of these three to the Stop rejectors.

## The three classes (the ruling)

### 1. Omission class → COMPLETE (no bounce)

The finding is that something owed was not said, and code holds the exact
content owed. Members (today's passes):

* **relay-due markers** — `notebook-status` terminal verdicts and
  `notebook-audit-view` per-section `view_sha12` markers
  (`state/notebook_audit.py::read_undischarged_relay_markers`), plus the
  campaign-scope markers the same discharge pass scans;
* **brief surfacing** — a computed brief (morning brief, park brief) that
  reached disk but not the human.

Completer behavior: code APPENDS the owed artifact to the turn's visible
output (channel: D1), records the discharge
(`state/notebook_audit.py::record_relay_discharge`) with completer
provenance (D3), journals the append — and the stop PROCEEDS. Zero extra
model turns. The appended render for an audit-view marker is the render
file's own content via the ONE composer (the unified-render ruling,
`docs/design/mcp-elicitation.md` drift log 2026-07-10) — verbatim by
construction, so the G1 paraphrase class cannot exist for appended content.

### 2. Violation class → APPEND CORRECTION; bounce only on a poisoned decision

The finding is that something said contradicts the durable record. Members:
the rule-10 contradiction pass (`number`/`state`/`run_id` mismatches from
`ops/decision/verify_relay.py::verify_relay` / `verify_notebook_relay`), the
decision-state-claims pass (`_decision_state_findings`), and the paraphrase
pass (`_paraphrase_findings` — the correction is the verbatim render lines).

Completer behavior: code appends a correction UNDER the claim — a labeled,
code-authored block quoting the claim and the journal's actual value
(`journal: <value>` — the same `nearest_source_value` the reason string
carries today). The human reads the correct value in the same turn; the
model's error is visible but neutralized. The stop proceeds, EXCEPT:

**The poisoned-decision test.** Bounce (today's block-once) survives when
the contradicted claim feeds a PENDING decision — an unresolved decision
brief for the same scope whose proposal the human is about to `y` on the
strength of the wrong number/state. Test, mechanically: the scope's LATEST
persisted brief (`state/decision_briefs.py::read_briefs` /
`latest_brief_for_block` — the brief store persists in BOTH driving modes)
has no subsequent committed `y` in the decision journal, and the finding's
claim tokens intersect that brief's content. Deliberately NOT keyed on the
run journal's `pending_decision` marker — that marker exists only under
block-drive (`state/decision_briefs.py` records this verbatim), so a test
built on it would silently never fire for MCP-direct-driven runs, which is
the default skill path. And NOT keyed on
`is_latest_committed_greenlight` being false — that is true of essentially
every mid-flight run and discriminates nothing. A correction footnote under
a poisoned proposal is not enough — the proposal itself must be re-relayed,
which is model work, hence the bounce stays.

Audit-scope violations (`verify_notebook_relay` findings) ALWAYS take the
append path: there is no per-audit brief store, so no poisoned-decision
test applies to them (the sign-off boundary has its own gates).

**Sign-off echo** (`_sign_off_echo_findings`) — RE-RULED (2026-07-10 night,
user; supersedes the same-day append-only ruling): **journal-only
provenance, in BOTH modes.** "The LLM suggesting stuff is helpful for human
amplification — the human should be given a helping hand; the only thing
that makes it too easy is to y-ack." Model-drafted wording is sanctioned;
the y-ack-ease hazard is guarded by the digest-read / tiered sign-off
gates, not wording originality. The detection survives as archive honesty:
each finding becomes one deduped `notebook-echo-provenance` record
(`state/notebook_audit.py::record_echo_provenance`) — never surfaced, never
appended, never blocking, absent from the attestation reduction like the
marker/discharge blocks.

### 3. Judgment class → BOUNCE (unchanged)

The finding requires the model to produce something code cannot: an
unanswered direct question from the human, or a driver continuation the agent
abandoned mid-chain whose next step ONLY THE MODEL can resume (it must render
a proposal, choose a nudge, or answer a human). These keep today's block-once
seam verbatim (`stop_hook_active`, never loops, never hard-blocks).

**The finding-21 boundary inside "driver continuation."** Not every abandoned
continuation is judgment. When the parked driver's next step is a *mechanical
tick* — `block-drive` advancing a deterministic span — code can RUN it; that
is the omission class ("advance the driver" is content code holds), not
judgment. Tonight's rendezvous livelock is exactly this: the guard bounced
"invoke `block-drive` … (do not end the turn)" ~15 times when the tick was
code's to run. The narrow half of finding 21 (a *consumed* greenlight
re-arming the guard) is already fixed by the boundary-scoped
`greenlight_targets_boundary` predicate, which prevents the FALSE bounce. The
open half: when a bounce is TRUE (a genuinely-unconsumed park whose successor
is mechanical), should the guard COMPLETE by running the tick itself rather
than bouncing to the LLM? **RULING-NEEDED** — running a tick opens an SSH
volley, so a completer here must gate on readiness (never re-fire against a
tripped breaker / a not-ready block); until ruled, the guard stays a
bounce and only its false-bounce fix ships. See "The decision-rendezvous
guard" below for the full analysis.

## Per-hook migration table

Every harness hook `hpc-agent` ships (`_kernel/hooks/`), with its class and
completer target. "Completer-shaped already" = the hook discharges its
obligation in code today and never bounces — the pattern this design
generalizes. Cite `module::symbol`, never line numbers.

| Hook | Event | Today | Class | Target |
|---|---|---|---|---|
| `skill_return_autofetch` | PostToolUse(Bash `emit-skill-return`) | injects the envelope as `additionalContext`, never blocks | omission | **Completer-shaped already — THE precedent.** No change. |
| `decision_rendezvous_autofetch` | PostToolUse(Bash `block-drive`) | injects the parked `brief`, never blocks | omission | Completer-shaped already (sibling precedent). No change. |
| `alert_count` | SessionStart | prints the unacked watchdog count into context, never blocks | omission | Completer-shaped already (delivery-in-code). No change. |
| `utterance_capture` | UserPromptSubmit | writes the human prompt to the utterance log, silent | observer | Not a gate — never blocks, never completes an obligation. It is the AUTHORSHIP trust anchor (§ laundering). No change. |
| `answer_capture` | PostToolUse(AskUserQuestion) | writes TYPED answers to the utterance log, silent | observer | Same as above; a CLICK on an agent-authored label is deliberately never captured — the laundering line a completer must respect. No change. |
| `scheduler_write_fence` | PreToolUse(Bash) | exit-2 blocks `qsub`/`sbatch`/`qdel`/… | **stays rejector** | Trust/actuation boundary (conduct rule 7). A completer that "completes" a scheduler mutation would ACTUATE without a journaled greenlight — forbidden. Rejection is the whole point. |
| `skill_return_stop_guard` | Stop | blocks over an unfetched sub-skill envelope | omission (fetch) + judgment (continue) | **Hybrid completer candidate.** The fetch is code's (the autofetch sibling already reads the same envelope); the "continue the parent skill's next step" is judgment. Target: inject the envelope (complete the fetch); keep the bounce only for the continuation the model must author. **RULING-NEEDED.** |
| `decision_rendezvous_stop_guard` | Stop | blocks "invoke `block-drive` to advance" | omission (mechanical tick) vs judgment (model-only resume) | **Completer candidate (finding 21).** Narrow false-bounce fix LANDED (boundary-scoped `greenlight_targets_boundary`); the mechanical-advance completer is **RULING-NEEDED** (SSH-readiness gate). See below. |
| `relay_audit_stop` | Stop | rejector → completer, capability-gated dark | omission / violation / judgment | **BUILT (the flagship).** §1–§2 + D1–D4. |

## The decision-rendezvous guard — the second completer candidate

`decision_rendezvous_stop_guard::build_hook_output` is a pure REJECTOR: on a
committed-but-unadvanced decision it returns
`{"decision": "block", "reason": "… invoke block-drive … (do not end the
turn)."}`. Finding 21 split this into two distinct defects:

1. **The false bounce (FIXED, not a completer change).** A *consumed*
   greenlight is byte-indistinguishable from a fresh one to a marker-present +
   latest-record-is-`y` test, so a re-park after a `not_ready` tick left the
   stale `y` re-arming the guard forever. The fix is the ONE shared predicate
   `block_drive::greenlight_targets_boundary` — a greenlight fires only when
   its `resolved["next_block"]` names the parked `next_verb` AND its `ts` is
   at/after the marker's `awaiting_since`. A prior boundary's consumed `y` or a
   same-boundary re-park's stale `y` no longer targets the boundary, so the
   guard stays silent (still "waiting for the human"). This is a rejector
   PRECISION fix, and it belongs regardless of the completer ruling — it is
   the same class as the relay-audit false positives below: a bad detector
   must be tightened before completion is even on the table.

2. **The true bounce (RULING-NEEDED — the completer question).** When the
   park is genuinely unconsumed and the next verb is a *mechanical* tick, the
   bounce spends a full model turn to make the LLM type a command code can run
   itself. The completer target: the guard runs `block_drive.run_tick` in
   code and either advances (marker clears — self-heals, nothing to block) or
   re-parks with a fresh brief that `decision_rendezvous_autofetch` already
   injects. That collapses the ~15-bounce livelock to zero model turns.

   The safety gate that keeps this RULING-NEEDED: a tick opens an SSH volley.
   Finding 20/21 is the counter-example — completing blindly would have
   *amplified* the fork-exhaustion storm, not relieved it. A rendezvous
   completer must therefore refuse to fire when the run's transport breaker is
   open or the block reports `not_ready`, degrading to today's bounce (or, at a
   fork-exhausted node, to silence + a scheduled-`doctor` backstop). This is
   strictly harder than the relay-audit completer, whose append touches only
   local files. Ship order: the false-bounce fix now (done); the mechanical
   completer only after the crash-only-monitoring plan removes the SSH-storm
   hazard the completer would otherwise feed.

## The false-positive tax — completion raises the stakes of a bad detector

Tonight's rule-10 false positives (findings 19 + the `27` / `5-minute-
enumeration` / `timeout`/`failure` cases) are not completer bugs — the
completer was dark. They are REJECTOR precision bugs. But they carry a
load-bearing lesson for the completer: **a false positive is cheap under a
rejector and expensive under a completer.**

* Under the rejector, a false flag bounces the turn; the model reads the
  reason, sees the flag is wrong, and re-relays (annoying, a wasted turn, but
  the model gets to PUSH BACK, and the human still reads the model's true
  statement).
* Under the completer, a false "violation" appends a code-authored CORRECTION
  under a TRUE human-facing claim — `journal: <value>` asserted as
  authoritative beneath a statement that was already correct. The model never
  mediates it, so nothing pushes back, and the human reads code confidently
  contradicting a true statement. That is misinformation with the completer's
  own trust stamp on it — the model-untouched property (§ Why now) working
  in reverse.

Therefore: **a violation-class finding is only eligible for the append path
once its detector is precise enough that a code-authored correction is
trustworthy.** The three tonight failures each name a precision debt the
detector must clear BEFORE its findings may be completed rather than bounced:

* **Derivable counts.** `27 = len(job_ids)` must be in the number pool
  (finding 19) — the verify-relay number set should admit counts of journaled
  lists, not just stored scalars.
* **Token shape.** `5-minute-enumeration` is prose, not a run id — the mention
  scan's substring match (`mentioned_run_ids`) needs a token-boundary / id-
  shape guard so a hyphenated English phrase cannot masquerade as a run
  mention.
* **State compatibility.** `timeout` and `failure` are compatible with an
  `in_flight` run (a transport timeout, a single task's failure) — the
  decision-state / state passes must not read a lifecycle-neutral English word
  as a terminal-state contradiction.

Until a given detector clears its precision debt, its findings stay on the
BOUNCE path even when the completer is active — a rejector's false positive is
recoverable, a completer's is not. This is why §2 already routes paraphrase /
audit-scope findings (which carry no per-claim value token) to append-only and
reserves the bounce for the poisoned-decision case: the append path is earned
by attribution precision, never assumed.

## Settled decisions

### D1 — The append channel: hook `systemMessage`, capability-gated

The Claude Code hook output contract carries a `systemMessage` field —
code-authored text the harness displays to the user — alongside (or instead
of) a `decision`. The completer emits
`{"systemMessage": "<owed artifact / correction>"}` with NO block decision:
the stop proceeds and the human sees the code-appended content in the same
turn. This is the model-untouched property the ruling values: the text never
passes through the model at all.

Two hard caveats, both build-time obligations:

* **Verify display, don't assume it.** A `systemMessage` the harness
  swallows is the declared-but-dark class. `systemMessage` has ZERO evidence
  in this repo today — `docs/internals/harness-contract.md`'s
  relay-enforcement section knows only `decision`/`reason` — so it is an
  unverified external dependency until probed. Build step 1 is a
  conformance probe (the `conformance/` kit): a NEW capability key
  (`stop-hook-append`) with its own detection seam added to
  `ops/harness_capabilities.py`'s result model, plus the harness-contract
  version bump — the existing four-capability model has no append seam
  (`trusted_display` sits at `"unknown"` for exactly this no-seam reason).
  The probe MUST cover both output shapes: `systemMessage` alone
  (stop proceeds) AND `systemMessage` combined with `decision: "block"`
  (the D2 mixed-class output) — display behavior may differ between them.
  Where the capability is absent, the completer DEGRADES TO THE REJECTOR —
  today's block-once bounce — never to silence. This mirrors the MCP
  elicitation display-receipt gap (`docs/design/anti-vendor-lockout.md`):
  the harness contract gains the append channel as a declared capability so
  a second harness can conform.
* **The append is display, not transcript.** A `systemMessage` does not
  join the assistant transcript, so a LATER stop's mention scan will not see
  it. Discharge must therefore be keyed to the append event itself (D3),
  never re-derived from transcript text.

### D2 — Completion never blocks; classes compose in one output

One stop can carry findings from all three classes. Composition rule:
completions and corrections are gathered into one `systemMessage`;
if ANY judgment-class finding (or poisoned-decision violation) exists, the
output ALSO carries `{"decision": "block", "reason": ...}` for those
findings only — the completed/corrected findings are already discharged and
MUST NOT be re-stated in the reason (or the model re-relays what code just
appended, resurrecting the latency this design kills). On a
`stop_hook_active` forced continuation, completions still run (they never
block, so they are loop-safe by construction — a strict widening of today's
"discharges still land on forced stops" behavior); blocks never re-fire.

**Discharge is gated on confirmed display.** A completion whose
`systemMessage` rides a BLOCKED output is completer-discharged ONLY where
the conformance probe has confirmed the harness displays `systemMessage` on
a blocked stop for that harness (D1). Where unconfirmed, mixed-class stops
DEFER completions to the post-continuation stop (which is never blocked —
the block-once seam — so the plain append path applies). Otherwise a
swallowed systemMessage plus an already-recorded discharge would silently
and permanently lose the owed verdict — the exact failure the marker
exists to prevent.

### D3 — Discharge provenance: `discharged_by`

`record_relay_discharge` records gain `discharged_by: "completer" |
"relay"` in `resolved` (append-only; existing records read as `"relay"`).
This is the automatability metric's data: the journal-derived count of
completer discharges vs model-relayed discharges IS the measure of latency
killed, and it keeps the audit trail honest about which artifacts the human
saw as code-appended text vs model prose.

### D4 — The completer composes from files, never from model text

Appended content sources, in order of preference: the trusted render file
for view markers — selected BY the marker's `view_sha12`, which is embedded
in the trusted render's filename (`state/notebook_audit.py::`
`RENDER_RELAY_DUE_RECORD_KIND` records this), so the mapping is
`view_sha12 → the .hpc/renders/<audit_id>/*.md file whose name carries it`,
never a glob-all (the `_paraphrase_findings` corpus glob is NOT the model
here); a marker whose sha matches no on-disk render degrades to the
token-level floor below; the journal
record's own fields for status/decision-state corrections; the marker's
`key_tokens` verbatim as the floor. The completer NEVER quotes the model's
final text back except to label the claim being corrected. Caps ride along
(the `_MAX_*` posture): total appended bytes bounded; over-cap content
degrades to the token-level floor plus a file reference.

## Loop-safety invariants

Every completer must hold these five, byte-checkable at build time. They are
the generalization of the block-once seam every existing Stop guard already
obeys.

1. **`stop_hook_active` never bounces.** A hook-forced continuation that
   re-enters the same Stop must never emit a `block` decision — that is the
   loop. Completions (appends) MAY still run on a forced continuation (they do
   not block, so they cannot loop); the poisoned-decision bounce and every
   sibling-guard bounce are suppressed under `forced`
   (`relay_audit_stop::_completer_output` gates the poison on `not forced`).
2. **At-most-once bounce per stop.** A given Stop is blocked at most once
   across its whole forced-continuation chain — the second entry carries
   `stop_hook_active` and passes through. Unchanged from today.
3. **Idempotent discharge.** A completion records its discharge keyed to the
   APPEND EVENT (D3, `discharged_by="completer"`), never re-derived from
   transcript text (a `systemMessage` is display, not transcript — D1). A
   marker discharged once is absent from the next stop's undischarged scan, so
   a completed obligation can never re-fire. The discharge record is
   append-only and NOT part of `_marker_key`, so it never changes which marker
   it closes.
4. **Completion failure degrades to the rejector, then to silence.** If a
   completer cannot record its discharge, it must NOT append the artifact and
   claim it (the owed obligation stays owed —
   `_completer_output` `continue`s past a failed `record_relay_discharge`). If
   the capability is absent/unknown, or reading it raises, the whole hook
   degrades to `_rejector_output` byte-for-byte. If the rejector itself
   raises, `main` swallows it and exits 0 (the stop proceeds; the scheduled
   `doctor` tick is the out-of-session backstop). Never a wedge.
5. **Display-gated discharge on a mixed output (D2).** A completion whose
   `systemMessage` rides a BLOCKED output discharges ONLY where the harness
   has confirmed it displays `systemMessage` on a blocked stop
   (`detect_stop_hook_append_on_block`); otherwise the completion DEFERS to
   the never-blocked post-continuation stop. A swallowed message plus a
   recorded discharge would silently and permanently lose the owed verdict —
   the exact failure the marker exists to prevent.

## Failure modes & the laundering hazard

* **The laundering hazard (the hard invariant).** A completer appends
  CODE-AUTHORED text sourced from FILES (D4) — a render selected by
  `view_sha12`, a journal value, a marker's `key_tokens`. It MUST NEVER author
  content attributed to a human, and MUST NEVER quote the model's own text
  back except to label the claim being corrected. The trust boundary the
  completer must not cross is the same one the authorship gate polices:
  `ops/decision/journal.py::_assert_human_authorship` /
  `_assert_signoff_authorship` require attestations to derive from the
  out-of-band utterance log that `utterance_capture` (UserPromptSubmit) and
  `answer_capture` (typed AskUserQuestion answers, never a click) write. A
  completer that composed a sign-off, or appended text a human then pastes as
  their attestation, would reopen exactly the laundering channel those hooks
  close. This is why the sign-off echo detection is JOURNAL-ONLY provenance
  (§2, re-ruled 2026-07-10): it records that model wording was echoed, but it
  NEVER appends, never completes, never blocks — a completer must not touch
  the authorship surface at all. Authorship / trust boundaries are the one
  place rejection is not a latency cost to be optimized away; it is the
  invariant.
* **Swallowed `systemMessage`.** `systemMessage` has zero evidence in this
  repo (D1): where the capability probe cannot confirm display, the completer
  degrades to the rejector, never to silent loss (invariant 4/5).
* **Detector false positives.** A completer inherits its detector's precision
  debt (§ false-positive tax): a false append is unrecoverable, so an
  imprecise detector's findings stay on the bounce path until the debt clears.
* **SSH-amplifying completion (the rendezvous case).** A completer that runs a
  tick can feed a connection storm (finding 20/21). Such a completer must gate
  on transport readiness and degrade to the bounce, never fire blindly — the
  reason its ruling is deferred behind crash-only monitoring.

## What this kills / what it keeps

* Kills: the extra model turn per omission (the most common bounce), the
  paraphrase risk on re-relays, the "corrected relay forgets a second
  marker" re-bounce chain.
* Keeps: block-once loop safety, fail-open-everywhere (every completer pass
  wraps like today's passes — a completer crash degrades to the rejector,
  then to silence, never a wedge), the no-scaffold discovery probe, and the
  verb-level `unverifiable` policy (still not a hook concern).

## Implementation sequencing

Ordered by trust cost — local-file appends first, SSH-touching completers
last. Each step ships its own tests and is independently revertible.

1. **`relay_audit_stop` completer — DONE (dark).** Built capability-gated
   (`_completer_output`, D1–D4); degrades to `_rejector_output` byte-for-byte
   with no capability. Tests: class routing per finding kind, poisoned-decision
   (pending brief + token intersection), D2 composition, D3 provenance, D4
   render-sourced append + cap degradation, the forced-continuation discharge.
2. **The `stop-hook-append` conformance probe.** Replace the two env markers
   (`HPC_STOP_HOOK_APPEND`, `HPC_STOP_HOOK_APPEND_ON_BLOCK`) with a real
   `conformance/` probe that verifies the primary CLI surface actually
   DISPLAYS `systemMessage` in both output shapes (proceeding, and combined
   with `decision:"block"`). Until this passes on the primary surface, the
   completer stays dark. Tests: the relay-triples suite gains the completer
   leg; probe present/absent → completer/rejector.
3. **Detector precision debts (§ false-positive tax).** Derivable counts in
   the number pool, id-shape guard on `mentioned_run_ids`, state-word
   compatibility for `in_flight`. These are rejector fixes that ALSO unlock
   the corresponding violation findings for the append path. Tests: each
   tonight false positive becomes a regression that must NOT flag.
4. **`skill_return_stop_guard` hybrid.** Inject the envelope (complete the
   fetch) via the same read the autofetch sibling uses; keep the bounce for
   the parent-continuation the model must author. Gated by the same
   `stop-hook-append` capability. Tests: fetch-completed vs continuation-
   bounced; forced-continuation no-loop.
5. **`decision_rendezvous_stop_guard` mechanical completer — DEFERRED
   (RULING-NEEDED).** Only after crash-only monitoring removes the SSH-storm
   hazard. Must gate on transport readiness (breaker-closed, block ready) and
   degrade to today's bounce otherwise. Ship the false-bounce precision fix
   (`greenlight_targets_boundary`) independently and FIRST — it is already
   landed.

## Test plan (sketch)

* Unit: class routing (each existing finding kind → its class), the
  poisoned-decision test (pending proposal + token intersection), D2
  composition (mixed-class stop), D3 provenance on discharge records,
  D4 render-sourced append with cap degradation.
* Conformance: `stop-hook-append` probe present/absent → completer/rejector;
  the relay-triples suite (`tests/conformance_kit/test_relay_triples.py`
  pattern) gains the completer leg.
* Regression: forced-stop completions still discharge; a completed marker
  never re-blocks a later stop.

## Open questions

* Whether `systemMessage` renders in ALL Claude Code surfaces this project
  drives (CLI, VS Code, web) — the conformance probe answers per-harness,
  but the v1 gate should be verified on the primary CLI surface first.
* ~~Echo-class placement~~ — RULED 2026-07-10 (violation class, append-only,
  never bounces; see §2).
* **RULED 2026-07-12 — the rendezvous mechanical completer: ON, gated on
  transport readiness.** User: "the proper thing for code to do if nothing
  ambiguous is in the way of the workflow." `decision_rendezvous_stop_guard`
  runs the parked `block-drive` tick in code when the greenlight genuinely
  targets the boundary AND the next verb is mechanical AND the run's SSH
  breaker/transport is healthy; anything ambiguous (breaker open, degraded
  transport, judgment verb) still bounces to the model. The fork-exhaustion
  night stays the canonical counter-example the gate exists for.
* **RULED 2026-07-12 — the `skill_return_stop_guard` split: YES.** Code
  completes the fetch (inject the envelope — the autofetch sibling is the
  precedent); the parent-skill continuation stays a judgment bounce.

## Drift log

* **2026-07-10 — BUILT (rejector → completer, capability-gated dark).** The
  completer path is fully implemented and tested but DARK by default (D1's safe
  landing): with no harness declaring the capability, `build_hook_output`
  degrades to the REJECTOR byte-for-byte (`_rejector_output`), so every
  pre-existing test still passes unchanged.
  * **Capability 5 `stop-hook-append`** — added to `ops/harness_capabilities.py`
    (`detect_stop_hook_append` / `detect_stop_hook_append_on_block`, the ONE
    detection home the hook imports) and reported by the `harness-capabilities`
    verb as a fifth `CapabilityEntry` + tier consequence. Tri-state like
    `trusted_display`: no passive install seam exists, so it reads `"unknown"`
    until a conforming harness activates it. **Deviation from D1 as written:** the
    activation seam is TWO env markers (`HPC_STOP_HOOK_APPEND`,
    `HPC_STOP_HOOK_APPEND_ON_BLOCK`), NOT the `conformance/` probe. Reason: the
    doc names the conformance probe as "build step 1 … the systemMessage-display
    conformance probe is follow-up" — the mechanize-now order is the completer
    machinery FIRST behind an explicit opt-in, the passive probe seam SECOND. The
    two markers cover D1's two required output shapes (proceeding vs blocked). The
    harness contract bumped 1.0.0 → **1.1.0** (additive minor — a new capability;
    doc line + `HARNESS_CONTRACT_VERSION` + kit `CONTRACT_VERSION` stay three-way
    equal).
  * **D3** — `state/notebook_audit.py::record_relay_discharge` gained
    `discharged_by` (`DISCHARGED_BY_RELAY` default / `DISCHARGED_BY_COMPLETER`);
    the field is additive (pre-D3 records read `"relay"`) and NOT part of
    `_marker_key`, so it never changes which marker a discharge closes.
  * **D4** — `_compose_owed_artifact` sources a render view-marker's owed content
    from the trusted render file selected BY `view_sha12` in its filename
    (`_render_by_view_sha`, the ONE composer — verbatim by construction), degrades
    to the token floor + a file reference over the append cap
    (`_MAX_APPEND_ARTIFACT_BYTES`), and falls to the token floor for a
    `notebook-status` terminal (no render file). Never quotes model text.
  * **Poisoned-decision test** — `_is_poisoned_decision` keys on
    `state/decision_briefs.py::read_briefs` (latest brief with no subsequent
    committed `y` + claim-token intersection with the brief content); explicitly
    NOT on `pending_decision` and NOT on `is_latest_committed_greenlight`. Bias to
    the append path everywhere (any error → not poisoned). It is itself
    block-once (never fires on a `stop_hook_active` forced continuation).
  * **Judgment class — no members here (scope note, not a deviation).** §3's
    judgment bounces (unanswered question, abandoned continuation) live in the
    SIBLING Stop guards (`decision_rendezvous_stop_guard`,
    `skill_return_stop_guard`), not this hook, so the only surviving bounce in the
    completer is the poisoned-decision one.
  * **Decision-state / paraphrase violations** carry an empty/verb-category
    `claim` and take the APPEND path in practice (a paraphrase/audit-scope finding
    never poisons — no per-claim value token to intersect a brief; the sign-off
    boundary has its own gates), matching §2's "audit-scope violations ALWAYS take
    the append path."

* **2026-07-11 — SCOPE WIDENED to the full hook inventory (run-#12 findings
  19/21).** The doc was relay-audit-only; tonight's evidence pulled two more
  Stop rejectors into the class map and named the observers/precedents that
  already complete.
  * **Per-hook migration table added** — all ten `_kernel/hooks/` modules
    classified. `skill_return_autofetch`, `decision_rendezvous_autofetch`,
    `alert_count` named as the completer-shaped PRECEDENTS (inject-in-code,
    never bounce); `scheduler_write_fence` named as the permanent rejector
    (actuation boundary — completing a scheduler mutation would submit without
    a greenlight); `utterance_capture` / `answer_capture` named as the
    authorship trust anchor a completer must never launder into.
  * **Finding 21 (rendezvous livelock) docketed as the second completer
    candidate.** Split into (a) the false bounce — a CONSUMED greenlight
    re-arming the guard — FIXED independently via the boundary-scoped
    `block_drive::greenlight_targets_boundary` predicate (a rejector-precision
    fix, already landed; the guard + planner now treat a stale/prior-boundary
    `y` as spent), and (b) the true bounce — a mechanical advance code could
    run — left **RULING-NEEDED** behind an SSH-readiness gate, because
    completing blindly would amplify the finding-20 fork-exhaustion storm.
    §3's "driver continuation keeps the bounce" sharpened: only a MODEL-ONLY
    resume is judgment; a mechanical tick is omission.
  * **The false-positive tax section added (findings 19 + tonight).** The
    rule-10 rejector forced the model to reword TRUE statements — `27`
    (`len(job_ids)`) read as unsupported, `5-minute-enumeration` read as a
    run-id mention, `timeout`/`failure` read as state contradictions against an
    `in_flight` run. Load-bearing consequence: a false positive is cheap under
    a rejector (the model pushes back) and UNRECOVERABLE under a completer (a
    code-authored correction under a true claim is misinformation with the
    completer's trust stamp). Ruling: a violation finding earns the append path
    only once its detector clears its precision debt (derivable counts, id-shape
    guard, state-word compatibility); until then it stays a bounce even with the
    capability active.
  * **Loop-safety invariants + Failure-modes/laundering + Implementation
    sequencing** promoted to standalone sections (were implicit in D2/§2). The
    laundering invariant now cross-references the authorship gate
    (`ops/decision/journal.py::_assert_human_authorship` /
    `_assert_signoff_authorship`) and the two capture hooks explicitly: a
    completer NEVER touches the authorship surface.
