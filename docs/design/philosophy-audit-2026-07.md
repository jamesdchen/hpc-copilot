# Philosophy audit — 2026-07 (repo-wide doctrine-alignment sweep)

Status: **SKELETON + B4 swept** (inventory 2026-07-10; the full sweep was
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

- The 9 packaged skills + 5 packaged commands + worker prompts: sweep each
  against B1/B2/B4/B11 (the run-#12 precedent class).
- `SESSION_HANDOFF.md` durable-reference section vs. current reality.

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
