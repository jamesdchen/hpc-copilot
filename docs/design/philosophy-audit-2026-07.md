# Philosophy audit — 2026-07 (repo-wide doctrine-alignment sweep)

Status: **EXECUTED 2026-07-12 — both sweeps + synthesis complete.** Sweep 1
minted and verification-corrected `docs/plans/upstream-fixes-2026-07.md`
(15 generators, 12-entry residue); sweep 2 delivered all 26 axis verdicts,
each twice-verified (Opus adversarial first pass, Fable second pass; every
verdict CONFIRMED). Verdict table in the Sweep log below; exit criteria
met (enforcement map updated, plan ranked and merged, one consolidated
RULING-NEEDED list at the top of the plan). Prior status for the record:
**SKELETON + B4 swept** (inventory 2026-07-10; the full sweep was
scheduled Day 2 = 2026-07-11 and was DISPLACED by the ship-day firefight —
49 fixes, findings 20-24; see the ship-day memory). **RESCHEDULED, user-
ratified 2026-07-11 late: the sweep is the FIRST OPUS WAVE of the run-13
relay session** — dispatch it into the CI/wheel/env idle windows, one agent
per axis group, AFTER reading the C-2026-07-11 additions below (the ship
day grew the axis list). User-named mandate: *"double check that everything
in the repo is aligned"* — distinct from the adversarial seam sweep
(correctness between the week's trains). This audit asks, per doctrine
axis: does the code everywhere still obey the ruling, and is the ruling
**enforced** or merely **remembered**?

## Method

1. Each axis below gets a **testable predicate** and a sweep scope.
2. Per axis, one verdict:
   - **ENFORCED** — name the lint/test holding the line (and confirm its
     fire path is pinned, per the "guard can actually fire" rule).
   - **ALIGNED-UNENFORCED** — no violation found, but nothing would catch
     one; propose the enforcement row (the never-fires-assertion
     conversion).
   - **DRIFTED** — cite file:line; land the fix if cheap, else write the
     dispatch-ready spec.
3. Cheap fixes land during the sweep; everything else becomes an Opus spec
   (the handoff-bank pattern). The output of this audit is an updated
   enforcement map, not prose.
4. **Output 2 — the upstream-fix plan (user-mandated 2026-07-11: "feed a
   follow-up plan that fixes all problems upstream stemming from design
   choices").** Every DRIFTED/EXPOSED finding must additionally name its
   GENERATOR: the design choice that produces this class of defect — or
   state "local defect, no generator" explicitly. Findings clustering on
   one generator become a generator-level proposal in
   `docs/plans/upstream-fixes-2026-07.md`: the upstream alternative, the
   symptoms it retires (cite each finding), migration cost, and the
   scope-by-constraint flip trigger (what makes the steady-state design
   shippable). The 2026-07-11 exemplar of the pattern: NAT keepalives,
   client timeouts, the idle reaper, orphaned remote processes, and the
   40-minute reporter walk were FIVE symptoms of ONE choice (pull status
   over long-lived silent channels); the generator fix is crash-only /
   cluster-announces, and Phase 1 shipped the same day the cluster
   diagnosed it. Per-symptom hardening is the belt; the generator fix is
   the plan. A sweep that produces enforcement rows but no generator
   analysis has done half its job.
   **Input corpus (user-directed 2026-07-11): confirmed BUGS join the
   audit findings as generator evidence — and they RANK the plan.**
   Retro-classify every confirmed bug in
   docs/internals/bug-sweep-2026-07-11.md (73) and the run-findings
   dockets (runs 5/10/11/12) with the same generator tag; observed
   symptom count + diagnosis cost order the proposals (a generator with
   four fired bugs outranks a predicate with zero incidents). Starter
   clusters from the 2026-07-11 corpus — hypotheses for the sweep to
   VERIFY, not conclusions: (1) hand-rolled connection lifecycle vs
   library-native (#8, #35, #16, #15, #50, finding 24 — overlaps B16);
   (2) pull-status-over-silent-channels (the recorded exemplar);
   (3) win32-as-retrofit in path/process handling (#10, #11, #64, #65,
   finding 17, the MSYS arg-conversion trap); (4) bare writes vs ONE
   atomic-write discipline (#42, #52, #57, #61 — four independent
   torn-write bugs, one missing helper); (5) text/substring forensics vs
   structured evidence (#12, #39, #54, #55 — the bound-capture
   generator); (6) hand-maintained vocabulary Literals vs
   subset-contract-tests against their catalog (#2, #34; the repo's own
   ⊆-test precedent is the fix pattern). Bugs prove a generator is LIVE;
   predicates bound its blast radius — the plan needs both.
   **Structure (user-ruled 2026-07-11, refined same night): TWO SWEEPS +
   ONE SYNTHESIS, bug sweep FIRST.**

   *Sweep 1 — the upstream bug sweep* (one agent, dockets only, no code;
   fits the relay's first idle window). Retro-classifies the bug corpus
   and MINTS `docs/plans/upstream-fixes-2026-07.md`. Output shape is a
   TABLE (bug → generator → confidence), with an explicit
   **unclassified-residue bucket** — a bug that fits no generator stays
   local-defect; shoehorning is worse than residue. **Entry threshold: a
   generator needs ≥2 independent symptoms** to enter the plan (one
   symptom = a local defect until a second fires). **Rank = fired-symptom
   count weighted by diagnosis cost** — the dockets narrate the hours
   each bug burned (the 2026-07-11 saga: one generator, a full day);
   use them.

   *Sweep 2 — the per-axis philosophy sweep* (agents by axis GROUP, not
   per axis; group by shared scope so one agent reads one code region:
   authorship/attestation gates [B4/B14/B8], transport/lifecycle
   [B3/B3'/B15/B16/A-subprocess/B13], journal/state truth [B12/A4/B7],
   altitude/packs [A3/A5/A6/A7/B6], human-attention surfaces
   [B1/B2/B5/B10], prose/skills [C + B11]). Standing instructions for
   every agent: (a) **check HEAD first** — git log the suspect surface
   before writing a verdict; the 2026-07-11 swarm twice re-dispatched
   already-fixed findings; (b) read-only plus CHEAP fixes only (≈≤20
   lines + a test); anything larger is a spec, not an edit; (c) findings
   carry a generator tag (sweep 1's clusters ride in the prompt as
   membership hypotheses) but agents do NOT edit the plan.

   *Synthesis — one editor, not an append.* Sweep-2 agents EMIT tagged
   findings in their reports; a single synthesis pass (the relay's
   judgment) merges them into the plan: resolves generator-naming
   collisions, re-ranks, and labels bug-unfired generators
   predicted-risk. Parallel agents never write one ledger (the
   2026-07-11 swarm's shared-file collisions are the precedent).

   *Verification discipline* (the bug-sweep's own lesson: 11 of 84
   candidates were refuted — ~13% of unverified findings are wrong):
   an **ENFORCED** verdict requires a DEMONSTRATED fire (run the
   synthetic violation, cite the failing output), never a claimed one;
   a **DRIFTED** verdict enters the plan only after the synthesis editor
   re-reads the cited file:line. Verdicts failing verification go back
   as PLAUSIBLE, not in.

   *Exit criteria — the audit is CLOSED when:* (1) the enforcement map
   in engineering-principles is updated with every new/verified row;
   (2) the plan ranks every ≥2-symptom generator with its upstream
   alternative, retired-symptom list, migration cost, and
   scope-by-constraint flip trigger; (3) ONE consolidated RULING-NEEDED
   list stands at the top of the plan for the user — scattered inline
   rulings do not count as surfaced.

## The drift class this hunts (run-#12 precedents, all found live 2026-07-10)

- The `hpc-notebook-audit` skill step 5 still encoded the pre-popup
  chat-first flow after the popup was ruled THE default read-and-sign
  surface (fixed `101cd111`).
- A stale `interview.json` `audited_source` outranked the bound pack seam
  (finding 5, open).
- The T8 notebook sign-off gate lacked the utterance-log evidence tier its
  sibling gates (scope-unlock, registration) already had — E-render-primary
  was structurally dead at its flagship site (fixed `101cd111`).

Common shape: a surface built under an older ruling survives a later ruling
that superseded it. Prose and skills rot faster than code; gates rot by
missing their siblings' upgrades.

## Axis inventory

### A. From `docs/internals/engineering-principles.md` (re-verify each
### enforcement row's fire path, then sweep for new members)

| # | Axis | Predicate sketch |
|---|---|---|
| A1 | Guard-can-fire | every lint rule / defensive default demonstrably fires (synthetic-violation test exists) |
| A2 | Determinism boundary | no skill/worker prose executes a step a composed verb owns; no new affordance lets the model author/sequence/discover what a verb should |
| A3 | Library-knowledge boundary (Q1–Q4) | no core vocabulary names experiment semantics; assembly points declared; import budgets per surface hold |
| A4 | Lifecycle verdicts / run identity one-definition | no re-inlined terminal-verdict or dedup logic |
| A5 | Registration kernel mechanism-only | deployment-boundary attestation stays mechanism, no semantic creep |
| A6 | Determinism fingerprint measure-don't-ask | no surface asks the model/human what code measures |
| A7 | Domain packs bind-as-data | core compares/hashes pack content, never interprets it; gate fires on drift |
| A8 | Live conformance chart-judges | operator adjusts, chart judges; no LLM verdict on conformance |
| A9 | Multi-human attributed-never-verified | identity comparisons only under >1 declared actors; byte-identical otherwise |

### B. Standing rulings (2026-07 ledger + design-doc drift logs) — the axes
### most likely to have pre-ruling surfaces still live

| # | Axis | Predicate sketch | Known-suspect surfaces |
|---|---|---|---|
| B1 | Popup = THE default read-and-sign surface; one render mechanism | every human-attention seat over MCP attempts the elicitation path first; no skill parks for chat where a popup can fire | other `append-decision` seats (greenlight, unlock — D6 says hook-tier: verify that ruling still stands vs. E-render-primary), campaign anomaly briefs |
| B2 | Poka-yoke: compose/default/auto-remedy what code can; refuse only trust boundaries | every surviving interactive question is a trust boundary; converted refusals have never-fires assertions | findings 1/2/5 (template, experiment_dir, compose-at-every-verb) are the open members |
| B3 | Positive-evidence verdicts; timeouts = UNKNOWN never terminal | no silence-as-success or silence-as-terminal path left | sentinel-ack landed for scheduler-query; sweep other transports (harvest, probe, doctor) |
| B4 | Authorship tiers: harness-captured > journal-response friction; response never self-satisfies a gate | every authorship gate reads the utterance-log tier when present | T8 fixed today — sweep remaining gates for the same missing tier |
| B5 | Trusted display: code-authored renders relayed verbatim; model-carried copies never load-bearing | every human-facing verdict has a code-rendered artifact + sha lock | audit-view done; check status/digest/brief surfaces |
| B6 | Altitude: LISTS never NOMINATES; core never learns what a config/metric is | no core surface ranks, names a baseline, or interprets a root | draft-context clean; sweep newer projections |
| B7 | One-definition rule | shared predicates (resolution, drift, attestation, canonical view) have exactly one home with route-through pins | linked_sources shared today — verify no second copy crept in elsewhere |
| B8 | No-unlock-verb / no sign-off affordance | relaxing verbs don't exist; relax actions ride journaled blocks with gates | pinned by contract test — re-verify fire path |
| B9 | Observe/judge/route, never actuate (scope doctrine, post-Phase-9 CLOSED) | no verb added since the ruling actuates outside the sanctioned set | the mechanize-now wave is the sweep scope |
| B10 | Attention is scarce: tiered everything; no-silent-caps; honest gaps disclosed | every cap disclosed; every unverifiable path/receipt named honestly | `unverifiable_paths` pattern — sweep for silent truncations |
| B11 | CLI verbs over Python internals; MCP-first; no version strings (verify by import/source) | agent-facing surfaces are verbs; docs never instruct source-reading; env checks inspect source | worker prompts + skills sweep |
| B12 | Journal as truth: state claims must match a journal record at utterance time | no surface invites unjournaled state claims | item-5 hook landed; sweep prose |
| B13 | No bare `subprocess.run` reachable from `mcp-serve` (finding 4's enforcement candidate — promote to a lint) | stdin isolation + tree-kill bounded everywhere under the server | NEW ROW to write |
| B14 | Trust the channel, not the inference: attestation is CAPTURED BOUND at a scope-aware surface, never reconstructed forensically from a general stream (findings 9/10 — the treadmill class) | every evidence tier is honestly ranked; bound capture ahead of forensic ahead of friction; no new gate starts at the forensic tier when a binding surface exists | `docs/design/bound-capture.md` is the banked plan; sweep other gates for the same retrofit smell |

### B-additions from the 2026-07-11 ship day (write these rows into the
### sweep alongside B1-B14; each has fresh live evidence)

| # | Axis | Predicate sketch | Known-suspect surfaces |
|---|---|---|---|
| B15 | Env-vs-record drift: framework-relevant env is DISCLOSED at every judgment surface | any HPC_* override active at decision time appears in the brief (doctor's `active_env_overrides` landed; is any OTHER surface that judges transport/state blind to env?) | HPC_SSH_ENGINE sat exported for days contradicting the session record — hours of misattribution (finding 24). Sweep: status-snapshot, net-triage, campaign briefs |
| B16 | Connection-lifecycle library boundary: hand-rolled lifecycle shrinks to what NO library can know (ban-risk breaker, connection-RATE courtesy); idle mgmt / keepalives / multiplexing are the library's | no framework code re-implements a transport-lifecycle mechanism its library exposes | the engine's idle reaper was the ONLY failing piece of an otherwise library-native stack (finding 24 library-boundary lesson); ssh_slots is the next suspect; ruled steady-state item, post-run-13 |
| B3′ | (B3 hardening) every REMOTE READ routes through `wrap_with_ack`/`split_ack` — silence-as-success is now one-definition | a parse_remote_json / `.stdout`-reading ssh consumer without an ack is a lint hit | sentinel-ack landed for status-reporter + scheduler (d028ac24) with a swept inventory in connection-broker.md; the ALIGNED-UNENFORCED conversion = a lint over new consumers |

### C. Skill/prose surfaces (rot fastest — full re-read against current rulings)

- The 9 packaged skills + 5 packaged commands: sweep each against
  B1/B2/B4/B11 (the run-#12 precedent class). *(Scope correction,
  swept 2026-07-12: the original line also named "worker prompts", but
  `hpc_agent/_kernel/extension/worker_prompts/` no longer exists — the
  §6 worker deletion removed it; the prose lints' glob for it matches
  nothing, an A1 guard-can-fire gap recorded under the B11 verdict. The
  sweep also found a TENTH skill, `hpc-claim-check`, orphaned in the
  pre-R2 tree `src/slash_commands/` — unpackaged and lint-invisible;
  moved into the packaged tree, commit `44536fd`.)*
- ~~`SESSION_HANDOFF.md` durable-reference section vs. current reality.~~
  *(Scope correction, swept 2026-07-12: no such file exists anywhere in
  the repo — the release skill Step 8 shows it deliberately lives OUTSIDE
  the repo (user homedir). Not sweepable in-repo; struck rather than
  reported DRIFTED.)*

## Sweep log

### B4 — authorship-gate tier parity (swept 2026-07-11, ahead of Day 2;
### triggered by findings 9/10)

Every utterance-log consumer in `ops/decision/journal.py` audited for the
standing-prompt class (evidence text that pre-dates what it supposedly
attests). The whole-log read is `_registration_authored_text` /
`_actor_scoped_human_texts` — NO consumer except the fixed T8 gate bounds
it temporally.

| Gate | Evidence legs | Verdict |
|---|---|---|
| T8 notebook sign-off | naming + engagement + TEMPORAL (render mtime) | **FIXED** (`3d8b40e0`) |
| scope-unlock (`_assert_unlock_authorship`) | word-OVERLAP of rationale vs the WHOLE log — the weakest leg in the file; no anchor | **DRIFTED/EXPOSED** — any composed rationale sharing one `_ha_word_tokens` word with any prior prompt passes. Anchor exists: the scope's newest LOCK record ts (an unlock rationale must post-date the lock it re-opens) |
| registration sign-off / review / conformance-verdict / reproduction-verdict / conclusion-file / challenge-file | naming over the whole log (exposed) **+ sha-prefix leg** — an 8-hex prefix cannot pre-exist the artifact it fingerprints | **ALIGNED** — the sha leg is temporal binding by vocabulary impossibility (the strongest form; note for bound-capture: this is the third design lever). Naming-leg exposure is moot behind it |
| registration-REVOKE / conclusion-REVOKE / challenge-verdict + withdraw | **naming ONLY** over the whole log; no sha leg | **DRIFTED/EXPOSED — the destructive direction**: the human named every id when CREATING it, so the naming leg is permanently satisfied and an agent-composed revoke/verdict rides through. Anchor exists: the TARGET record's ts (a revoke must post-date what it revokes — the gates already load the target record to verify existence, so its ts is at hand) |
| overnight-consent (leg 1) | word-overlap vs the whole log; hard-caps + wake legs are structural | **EXPOSED, anchor OPEN** — no natural artifact to anchor to (a standing consent precedes the boundary it covers). Candidate: recency window or bound capture only. USER RULING needed |
| field-ownership (`_assert_human_authorship`) | value/number derivation from the whole log | **ALIGNED as-is** — derivation semantics, not attestation: the kickoff prompt stating the goal IS the intended evidence; standing text is the point |

Fix-wave shape (dispatch-ready): one shared helper — filter utterance
records to `ts >= anchor` (the finding-10 pattern generalized, anchor
caller-supplied) — applied at scope-unlock (anchor = newest lock record)
and the four naming-only revoke/verdict gates (anchor = target record ts).
Registration-family filing gates unchanged (sha leg suffices).
Overnight-consent parks on the ruling. Bound capture
(`docs/design/bound-capture.md`) supersedes the forensic tier at popup-
capable seats when it lands; these anchors harden the fallback tier that
remains.

### Sweep 2 — all 26 axes (swept + twice-verified 2026-07-12)

Protocol: six Fable finder agents by axis group; every axis verdict then
adversarially verified by an Opus first pass and independently adjudicated
by a Fable second pass. Every verdict below is **CONFIRMED** (nothing came
back PLAUSIBLE); every ENFORCED verdict carries a fire DEMONSTRATED by at
least two of the three passes (synthetic violations planted, guard output
quoted, tree restored). Cheap fixes landed in commits `d9c6632`,
`65e9b14`, `44536fd`, `081d32e`; specs and enforcement-row proposals are
banked in `docs/plans/upstream-fixes-2026-07.md` §Sweep-2 merge.

| Axis | Verdict | The load-bearing fact |
|---|---|---|
| B12 journal-as-truth | **ENFORCED** | item-5 hook live (runs verify_relay in-process, `relay_audit_stop.py:841`); false-block on truthful supersession relays (G11 twin-store drift) fixed `65e9b14`, both directions reproduced |
| A4 lifecycle one-definition | **ENFORCED** | both fire demos re-run by mutation (drift-predicate route-through pin; settled-failure-outranks-absence, 9 parametrizations) |
| B7 one-definition | **DRIFTED** | sixth `read_terminal` consumer at `state/run_story.py:301-306` skips the currency compare; enforcement row proposed |
| B4 authorship tiers | **DRIFTED** | ts>=anchor fix-wave NOT landed; all five exposed gates re-confirmed; faithful exploit demonstrated (one shared word "holdout" from a standing pre-lock utterance lands an unlock; no-overlap control refused) — the finder's own demo was corrected by the first pass (empty-log no-op) |
| B14 bound-capture | **DRIFTED** | forensic tier still primary at every gate; `docs/design/bound-capture.md` remains the banked dispatch-ready spec |
| B8 no-unlock-verb | **ENFORCED** | NEW pin `test_no_unlock_affordance_in_registry_or_chains` (`d9c6632`) sweeps registry + chain tables; substring-scope caveat (synonyms evade) recorded in the map |
| A9 multi-human | **DRIFTED (narrow)** | the sweep's ONE overturned verdict: finder+first-pass said ENFORCED; second pass found the notebook-draft attestor stamp under a single declared actor — byte-identity row re-scoped, RULING NEEDED (census-null vs keep) |
| B3 positive-evidence | **DRIFTED→fixed** | aggregate `verify_per_task_outputs` false-green (rc-0 severed channel = "all outputs present") reproduced against pre-fix commit; one-ack-definition fix landed `65e9b14` |
| B3′ ack one-definition | **DRIFTED** | remaining un-acked `.stdout` consumers inventoried; `lint_remote_read_ack` spec banked (allowlist seeded from connection-broker.md + the B3 low-severity observations) |
| B13 no-bare-subprocess under mcp-serve | **DRIFTED** | bounded_subprocess hardening landed `65e9b14`; AST-walker lint spec banked (inventory re-derived mechanically, not from prose) |
| B15 env-vs-record | **DRIFTED** | doctor-local only; shared env-disclosure helper spec banked (five transport-affecting HPC_* vars incl. `HPC_SSH_CIRCUIT_OVERRIDE`) |
| B16 lifecycle library-boundary | **ALIGNED-UNENFORCED** | no new hand-rolled mechanism since the ruling; `test_transport_lifecycle_homes` contract-test proposal banked (post-run-13 schedule stands, ruling 5) |
| B9 observe/judge/route | **ALIGNED-UNENFORCED** | no verb added since the ruling actuates outside the sanctioned set (`update-run-constraints` is the one sanctioned cluster-write); operations.json side-effects contract-test proposal banked |
| A3 library-knowledge Q1-Q4 | **ENFORCED** | all four rows' fire paths re-demonstrated (planted heavy import + template-imports-core probe both caught verbatim) |
| A5 registration mechanism-only | **ENFORCED** | vocabulary pin fires on planted "holdout" literal; the cross-kind `uncontested` key verified as mechanized amendment, not creep |
| A6 measure-don't-ask | **ENFORCED** | AST pin fires on planted `1e-9` classifier literal; caller_override disclosure verified |
| A7 packs bind-as-data | **ENFORCED** | pack_gate drift fires green; pack-boundary pins' hand-listed file tuple covered 3 of 5 ops modules (G10 instance) — widened to a derived glob `081d32e`; aliased-from-import evasion recorded predicted-risk |
| B6 LISTS-never-NOMINATES | **ENFORCED** | newer projections clean; sibling-pin vocabulary divergence noted for the map; relay_render/status_blocks follow-on row proposed |
| B1 popup-primary | **DRIFTED** | mechanism ENFORCED (block-agnostic elicitation, real-gate popup round-trip green) but run-12 finding 12 NOT fixed: audit-view still ships the diff twice (~11k tokens model-carried); omit-at-the-source spec banked into the plan so it cannot be lost; D6 prose corrected in-place `081d32e` |
| B2 poka-yoke | **ENFORCED** | run-12 findings 1/2/5 all fixed-at-HEAD (compose-and-disclose at on-ramp/interview/preflight); preflight compose branch gained its missing fire tests in-sweep; experiment_dir compose is prose-tier only — annotated in the map |
| B5 trusted display | **ENFORCED** | render+sha locks verified on status/digest/brief surfaces; one retired-comment doc nit noted |
| B10 no-silent-caps | **DRIFTED** | #71 live at HEAD (whole challenge silently dropped on one unvalidatable filing — B10 member confirmed; severity upgraded from the bug-sweep's LOW, disclosed); #68 fixed-at-HEAD with pin; never-fires test spec = the exact valid-then-invalid repro |
| A8 chart-judges | **ENFORCED** | no LLM verdict on conformance; NEEDS_VERDICT routes to the human attention queue (`attention_queue.py:226-227`) |
| C skill/prose | **DRIFTED** | orphaned 10th skill (above); opt-in/DEFAULT canary contradiction; fenced `qmod -cj` prescription; stale hpc-worker references — all fixed `44536fd`; popup-primary prose now pinned by a failing-on-regression test |
| B11 verbs-over-internals | **DRIFTED** | worker_prompts lint glob points at a deleted directory; scan-root duplication in `.pre-commit-config.yaml` `files:` filters; CI reaches these lints only via pre-commit — consolidation spec banked |
| A2 determinism boundary (prose) | **ENFORCED** | no-Edit onboarding pin + decorate-byte-identical fire demos re-run |

Verification-discipline yield, for the record: the two-pass protocol
corrected one full verdict (A9), invalidated and faithfully rebuilt one
exploit demo (B4), caught stale line-cites/counts in over a dozen evidence
fields before they entered the map, and confirmed 100% of the finder
verdicts it did not correct — consistent with the ~13% unverified-error
base rate the discipline was built against.
