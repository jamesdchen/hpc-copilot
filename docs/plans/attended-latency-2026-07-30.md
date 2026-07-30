# Attended latency — the human-waiting-on-machinery program (2026-07-30)

User rulings from the 2026-07-30 night session (har_base_sweep sign-off
exhibit). The target is ATTENDED time: minutes a human sits watching
machinery, and round-trips a gate could have prevented. Hours-scale async
tail items (repair ladder, queue wait) are tracked separately and do NOT
outrank these.

## The exhibit

One typed utterance ("sign target-construction feature-construction
baseline. also, I want to go to sleep") produced: 1 refusal (engagement bar
never disclosed), 1 pass, 1 refusal (freshness law never disclosed), a
re-render, and a re-ask — at bedtime. Every refused requirement was
computable at render time. The declared leave-intent was journaled and
ignored.

## Landed this session (shared-definition fix)

- `ops/notebook/audit_view.py` now owns the ONE sign-off contract
  vocabulary: `signoff_token_names` / `section_engagement_tokens` /
  `engagement_hint_tokens` (facade-exported via `ops/notebook_view.py`);
  the T8 gate (`ops/decision/journal/signoff.py`) consumes them — surface
  and refusal can no longer drift.
- The next-actions footer states the FULL bar (slug floor, engagement with
  per-section hint tokens, freshness-after-render, one-utterance-batch,
  contest path) and no longer composes the refusable roster example; the
  backwards auto_cleared/"higher bar" line is corrected (raised bar is
  human_required's; redundant sign-offs WAIVE it).
- Refusal hint ranking fixed (was `sorted(pool)[:8]` → 'a', 'and',
  'could'); underscore-carriers/longer tokens first, display-only.
- Tests: `test_footer_states_the_gates_full_bar`,
  `test_footer_hints_derive_from_the_gate_pool` (drift guard),
  `test_engagement_hints_rank_identifier_shaped_tokens_first`.

## Ruled, to build

1. **Contract-taught-by-refusal is a CLASS — sweep the repo.** For every
   human-boundary gate (sign-off, scope-unlock, consent minting,
   greenlight briefs, contest/nudge): pair each refusal leg with the
   upfront surface the human reads, derive both from one definition, add a
   drift fire-test per pair + principles enforcement rows. Inventory pass
   over `_refuse_missing_authorship` / SpecInvalid sites in
   `ops/decision/journal/*` vs their briefs/renders/menus.
2. **Authorship horizon (leave-intent).** A journaled leave-intent
   utterance ("I want to go to sleep") must trigger ONE composed sitting:
   every outstanding typed-authorship demand (sign-offs w/ current
   renders + contracts) + every offerable grant line (overnight consent,
   spend envelopes). Machine composes the ASK, never the attestation
   wording (laundering bar unchanged). Generalizes the 2026-07-29
   speculative-y offer from consent grants to authorship demands.
3. **S1 melds into the audit sitting; canary fires at view-relay.** The
   audit view relay IS the proposed-experiment review: render the
   materialized S1 successor spec brief beside the sign-off ask; fire the
   speculative canary when the view is relayed (extension of R2 — same
   bounded 1-task spend, cmd_sha TTL cache, nudge-orphaning), so S2 opens
   with canary evidence in hand. Needs a ruling on the earlier fire point
   + spend disclosure inside the audit sitting.
4. **Wire `HPC_S1_SPECULATE`.** R2's code-fired canary
   (`submit_blocks.py:608`) is set NOWHERE in src — SKILL.md claims
   "DEFAULT at the S1 relay" but nothing exports the env var. Verify/wire
   the default, or item 3 obsoletes it.
5. **Prelude mechanization — the 15-min handoff→submit gap.** Every
   prelude step is ALREADY a verb (detect-entry-point, decorate-entry-point,
   classify-axis-easy, interview, audit-preflight, notebook-draft /
   lint / auto-clear / audit-view / status) but the SEQUENCING is skill
   prose: each transition costs an agent round-trip (read output → decide
   → author spec → call), ~10–20 of them ≈ the observed 15 min. Post-
   greenlight the same shape is one block-drive tick. Build: extend the
   kernel chain upstream — prelude blocks where code runs the
   deterministic transitions in ONE tick (detect→decorate→classify→
   interview; lint→auto-clear→view→status), parking ONLY at the genuine
   judgment points: (a) draft authoring (LLM), (b) classification
   ambiguity, (c) the human sitting. With ruling 3 the whole chain
   becomes: handoff → prelude tick → draft → audit tick (view + S1 brief
   + speculative canary) → ONE human sitting → submit. The 2026-07-30
   pack-bind/opt-in fumble is the same class (unchained prelude step
   discovered by refusal).

## Deferred (hours-scale async tail, separate thread)

07-27 canary `reporter_unreachable` rc=255 regression (blocks results
outright); repair-ladder serial walltime escalation → priors +
jump-to-cap; canary express-partition placement; terminal-sidecar ts
collisions (unmeasurable final aggregate).
