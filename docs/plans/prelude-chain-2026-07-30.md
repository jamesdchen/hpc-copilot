# Prelude mechanization — code drives every decision-free transition (2026-07-30)

USER RULING (2026-07-30 chat): "mechanize all parts of the chain that don't
need a decision." Supersedes item 5 of `attended-latency-2026-07-30.md`
(same program, now recon-grounded). Recon: two sweeps (kernel anatomy +
prelude inventory), findings cited inline.

## Ground truth from recon

- The prelude (idea → S1) has exactly FOUR genuine judgment points:
  (a) draft authoring (source body / executor `compute()`) — LLM;
  (b) the typed human sign-off (T8);
  (c) the axis LLM-tree, only behind `classify-axis-auto`'s
      `needs_llm_tree` escalation (code already computes the gate);
  (d) human-owned intent fields (`goal`, `task_generator`, entry-point
      tie-break). Everything else is deterministic.
- Agents hand-author spec fields code could compose:
  `produced_by.operator` (SKILL says "shell out to git config"),
  `entry_point.run_name` (detect-entry-point already names the candidate),
  wrapper `argv`/`signature` (argv_kind is code-classified; param names are
  not extracted by any verb), the fixed-params partition, and
  `homogeneous_axes` (a SKILL prose heuristic table).
- The repo's own precedent for the fix is `classify-axis-auto`
  (`incorporation/classify_axis_auto.py:1-45`): collapse a strict
  producer→consumer verb chain into ONE call with a discriminated
  `{recorded} | {needs_llm_tree}` return, built to stop hand-mis-sequencing.
- block-drive today: parks are HUMAN-only (greenlight/consent); no
  agent-actor park exists. New-chain checklist is well-supported
  (ORDER/SUCCESSORS + wire models + regen + curation auto-derived off
  `next_block` + WORKFLOW_ENTRIES + 6 named tests that go RED).
- **Recorded prior decision being REVERSED**: `mcp_server.py:145-150` — the
  notebook-audit loop is "HUMAN-sequenced (a block-drive-style driver was
  REJECTED there)". Tonight's ruling overturns the sequencing half ONLY:
  code may sequence the deterministic verbs; every human boundary (sign-off,
  greenlight, consent) is untouched. The builder must read
  `docs/design/notebook-audit.md`'s rationale and record the reversal in its
  drift log + a principles enforcement row.

## Wave P1 — "-auto" collapses + composers (no kernel novelty; SAFE, additive)

- **P1.a `wrap-entry-point-auto`** (new `incorporation/wrap_entry_point_auto.py`):
  collapse detect-entry-point → pathway table → decorate/wrapper →
  frozen-YAML scan → fixed-params partition into one verb, discriminated
  return `{onboarded} | {needs_pick: [entry candidates]} | {needs_intent:
  [missing fields]} | {needs_wrapper_argv: argv_kind}`. Includes promoting
  the SKILL's pathway table (`hpc-wrap-entry-point/SKILL.md:93-104`) and
  fixed-params partition (`:180-193`) to code. Mirror classify_axis_auto's
  shape exactly.
- **P1.b `suggest-prelude-action`** (new; the `suggest-setup-action` ladder
  pattern, `cli/setup_actions.py:118-195`): one deterministic "what's next"
  verb over the five prelude substrates (notebook decision journal,
  audit-config seat, pack journal, axes.yaml, interview.json presence) +
  pack opt-in integrity (the 2026-07-30 pack-bind-without-opt-in fumble
  becomes a named remedy here). Extend `scaffold-spec.supported_verbs` to
  `interview` / `classify-axis`.
- **P1.c interview composers** (`ops/memory/interview.py`): stamp
  `produced_by.operator` from `git config user.name` server-side (caller
  override wins); default `entry_point.run_name` from the detect
  candidate; disclose both as composed defaults (the
  `_compose_audit_template_default` posture).
- **P1.d argv extraction**: extend detect-entry-point (or a sibling) to
  extract argparse/click param names+types by AST for the wrapper path;
  unsupported frameworks return `unsupported` honestly (hydra stays LLM).

## Wave P2 — the audit/onboard chain on block-drive (kernel; MORNING, not overnight)

- **P2.a agent-actor park**: `park()` grows an `actor` field
  (`human` default | `agent`); `awaiting_draft` parks route to the LLM with
  a draft brief and carry NO consent semantics (a draft is authorship, not
  authorization). New vocabulary needs its own bare-y-coverage entries.
- **P2.b "audit" chain family**: ORDER
  `[audit-preflight, notebook-lint, notebook-auto-clear,
  notebook-audit-view, notebook-status]` with stage-keyed SUCCESSORS
  expressing the nudge cycle (status:sections_pending → view park;
  lint re-entry on redraft). Parks: `awaiting_draft` (agent),
  sign-off (human). Reversal note per above.
- **P2.c "onboard" family + S1 meld**: `[audit-handoff,
  wrap-entry-point-auto, interview]` chaining into `submit-s1`; when
  intent placeholders are resolvable, handoff+interview run BEFORE the
  sign-off sitting so the sign-off relay carries the S1 brief and fires the
  speculative canary (user ruling: "canary fires as the human reads the
  proposed experiment" — extends R2; canary needs `cmd_sha`, hence
  interview-first ordering). Unresolvable intent joins the ONE sitting
  (authorship-horizon, `attended-latency-2026-07-30.md` item 2).
- **P2.d** every park relays the code-composed asks verbatim: grant_line,
  sign-off scaffold, S1 approve_hint — the contract-taught-by-refusal
  class fix applied chain-wide.

## Integration checklist (repo idiom)

Per wave: merge (worktree branches) → `python scripts/regen_all.py --write`
ONCE → ruff/format/mypy → targeted batteries (`tests/ops/test_block_chain*`,
`tests/contracts/test_spec_hint_completeness.py`,
`tests/contracts/test_bare_y_coverage.py`,
`tests/integration/test_spec_contract.py`, new units' tests) → push → CI →
principles enforcement rows for the reversal + new park actor.

## Status

- 2026-07-30: plan banked from the two-recon sweep. Wave P1 builders
  dispatched (worktrees; regen deferred to integration). Wave P2 held for
  the morning sitting (kernel + doctrine reversal should not land while the
  maintainer sleeps).
- 2026-07-30 (later): Wave P1 INTEGRATED on main — three branches built,
  independently verified, all findings fixed (P1.a 072423c0, P1.b 2f8cb843,
  P1.c+d 1369f7ec), merged + ONE `regen_all --write` (9/9 PASS; both ledger
  rows paid). Loose ends carried: hpc-wrap-entry-point SKILL.md:79/141/222/240
  prose now redundant (composers/extraction own those steps — retire the
  prose when the SKILL is rewired to call `wrap-entry-point-auto`);
  `argv_params` has no consumer yet (P2's chain composes argv from it);
  `conf/*.yml` glob gap preserved verbatim (contract change, needs ruling).

- 2026-07-30 (P2.c, the capstone): the **`onboard` chain** landed —
  `ORDER["onboard"] = [audit-handoff, wrap-entry-point-auto, interview]`,
  chaining off the audit family's `audit_passed` hint and EXITING into
  `submit-s1`. `audit-handoff` was re-homed out of its P2.b single-member
  family (its `block_index` is unchanged at 0, so no §4 routing comparison
  moved). Stage-keyed edges: handoff `placeholders_resolvable` →
  wrap (its `needs_intent` is the ONE field nothing downstream can derive, a
  missing recorded `goal`); wrap `onboarded` → interview, with its three
  escalations split FOUR ways — `needs_wrapper_argv` is the chain's second
  AGENT park (params were mechanically extracted, so the argv template is
  transcription = authorship), `needs_wrapper_argv_unsupported` /
  `needs_pick` / `needs_intent` are human parks; interview `interviewed` →
  `submit-s1`. All three wire models gained the block surface, and
  `compose_successor_spec` gained three IDEMPOTENT composers (the carrier keys
  they read are not fields of the specs they produce, so each recognises its
  own output by a positive shape test).

  Three rulings landed with it. **R-a**: the standing-consent offer at a
  run-scope park now carries the D1 BAR (`overnight.STANDING_CONSENT_BAR` —
  bare `y` = this boundary only; the typed line = standing consent) and the
  FULL spend envelope (`overnight.compose_spend_envelope`, the same arithmetic
  submit-s2's brief renders; ABSENT, never fabricated, when no sidecar exists).
  **R-c**: the audit sign-off park carries an `s1_preview` — S1's own walk run
  READ-ONLY over the persisted interview intent — and is absent-and-honest when
  no interview exists. **R-b**: NO gate was removed; the shrink is consent-mode
  behaviour, pinned by `tests/ops/test_onboard_chain.py::
  test_one_typed_grant_covers_the_whole_submit_chain` (all four `GATED_BLOCKS`
  still gated, all four cleared by ONE typed grant).

  **Reversal recorded**: Row 22's `HPC_S1_SPECULATE=1` opt-in is REVERSED to a
  kill switch (`=0` disables). The opt-in was never once exported, so the
  canary that was meant to be green before the human answered never fired —
  a shipped feature that was really a documented intention. The fire is now
  chain-driven and ON by default, with ONE definition
  (`ops/s1_meld.fire_speculative_canary`) shared by the S1 park and the meld.

  Two CLI/seam changes worth naming: `interview`'s `--campaign-dir` became
  OPTIONAL with `--experiment-dir` as the chain form (a chained span is invoked
  with the standard argv, so a bespoke-flag-only verb could never be one) —
  which also DROPPED `interview` from the `NEEDS_EXTRA_CLI_ARGS` xfail lists in
  two contract batteries, since the spec-validate path is now reachable; and
  `WrapEntryPointAutoInput` gained an OPAQUE `audited_source` carry (the
  `notebook-status.review` precedent) so the audit provenance survives the hop
  into the interview.

  Regen for the changed wire models + `interview`'s CLI shape is DEFERRED to the
  wave's serial rebake and ledgered in `docs/internals/regen-debt-ledger.md`.
  Deliberately NOT done: the onboard TERMINAL does not call the queue-intake
  producer (no run_id exists there — a call that could never fire is a dead
  guard), so the single producer seat is `submit-s1`'s fresh entry; and the
  melded park DISCLOSES where the canary fires rather than firing it there (a
  standing consent and a canary both need a run identity the sign-off boundary
  does not have).
