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

6. **Double-consent boundary (harness classifier × hpc-agent gates).** Live
   2026-07-30: the human's journaled greenlight authorized submit-s2, then
   Claude Code's auto-mode classifier blocked the MCP call anyway — in-band
   consent is invisible to the harness permission layer, so the human pays
   the same boundary twice (and the classifier also blocks the agent from
   fixing the allowlist, correctly). Fix candidates: `doctor-install` /
   agent-assets ships a RECOMMENDED permissions fragment for the experiment
   repo's `.claude/settings.local.json` covering exactly the verbs whose
   spend hpc-agent's own typed-consent machinery gates (human pastes or
   approves it once — the fewer-permission-prompts pattern); the fragment's
   rationale line names the gate that makes each verb safe to standing-allow.
   Keep the third-party-launcher fence (kimi/qwen deny-list) separate and
   unchanged. HARD REQUIREMENTS (live 2026-07-30, an agent composed a
   settings snippet that violated all three): (a) the fragment MERGES into
   existing settings — never an overwrite; (b) `append-decision` and `kill`
   are NEVER in the allow set — append-decision is the consent-commit verb,
   and standing-allowing it converts "no prompts" into "consent journaled
   without harness visibility" (the eligibility criterion is mechanical:
   allow only verbs that REFUSE without a prior journaled greenlight/consent
   — the gate census, not a hand list); (c) the fragment carries ONLY
   hpc-agent verbs — no unrelated Bash rules may ride along.

7. **Route-blind triage (live 2026-07-30, the S2 night).** Two compounding
   blind spots: (a) `net-triage` probes bare hostnames, not the
   config-resolved SSH path — it reported "hoffman2: reachable" while the
   configured `ProxyJump usc-discovery` hop was dead, blessing a failover
   INTO the dead hop; (b) the breaker's degradation classifier read
   "probe-OK + preamble-timeout" as node-local degradation ("NOT a
   transport fault") when a flapping VPN tunnel dropping mid-command
   produces the identical signature, and its remediation steered toward
   `host-retarget` to a sibling reached through the same dead hop. The
   discriminating test that settled it live: run the SAME command class
   that failed (the conda preamble) over an ALTERNATE route (direct,
   no-jump) — PREAMBLE_OK ⇒ transport, hang ⇒ node. Mechanize: net-triage
   resolves each host's effective chain (`ssh -G`), probes hop and target
   separately, and when a jump exists also probes the direct alternative;
   the breaker's degradation disclosure names the tunnel-drop alternative
   whenever the path has a ProxyJump and offers the alternate-route
   preamble probe as the discriminator before recommending host-retarget.

## Status (2026-07-30, post Wave P1 + Wave P2 integration)

The docket above is now HISTORY for items 5-7 and partial for item 1. Landing
shas below are on `main`; each build sha is followed by its verifier-findings
fix and the integration merge, because the wave discipline was
build → independent verifier → fix → merge and citing only the feature commit
would misrepresent what shipped.

| # | Item | State | Landing shas |
|---|---|---|---|
| 1 | Contract-taught-by-refusal is a CLASS | **PARTIAL** — the exemplar pair landed and the class is now named at two new sites, but the repo-wide inventory sweep (`_refuse_missing_authorship` / `SpecInvalid` sites in `ops/decision/journal/*` vs their briefs/renders/menus) is NOT done | `86abf3a3` (sign-off contract = ONE shared definition, the "Landed this session" fix above); class applied at `incorporation/wrap_entry_point_auto.py` (`c9c1ba2e`, seam-closure `4f835bba`) and at the hpc-submit skill's paste-ready grant-line ruling (`75101964`) |
| 2 | Authorship horizon (leave-intent) | **NOT BUILT** — no leave-intent seam exists in `src/`; the composed-sitting design is unwritten | — |
| 3 | S1 melds into the audit sitting; canary at view-relay | **NOT BUILT** — still needs the ruling it names (earlier fire point + spend disclosure inside the audit sitting). The audit loop it would meld INTO is now on the driver (item 5), so the seam is ready | — |
| 4 | Wire `HPC_S1_SPECULATE` | **NOT BUILT — and re-confirmed open.** `submit_blocks.py` still reads it and nothing in `src/` sets it, so the R2 canary remains opt-in-by-env with no default. Item 3 may obsolete it; until item 3 is ruled, the SKILL.md "DEFAULT at the S1 relay" claim stays false | — |
| 5 | Prelude mechanization — the 15-min handoff→submit gap | **LANDED** (Waves P1 + P2.a/b) — the deterministic prelude transitions now run in code; the audit loop is a block-drive family; the judgment points park | P1.a `c9c1ba2e` (fix `072423c0`, merge `5666a343`); P1.b `f01e67aa` (fix `2f8cb843`, merge `71faca8c`); P1.c+d `5964fb0b` (fix `1369f7ec`, merge `cfcde1ea`); serial rebake `402208b4`; skill rewire `75101964` (merge `f664b735`); seam closure `4f835bba` (merge `84cdf2c1`); P2.a+b `73420576` (fix `2292910b`, merge `0a6b8dde`); wave integration `98d60905` |
| 6 | Double-consent boundary (harness classifier × hpc-agent gates) | **LANDED — but NOT by the mechanism this plan proposed.** The settings-fragment candidate was REJECTED in the build ("trades one round-trip for a permanently open boundary"). What shipped is a `PreToolUse` hook that reads the SAME journal the gate reads and forwards a greenlight already on file. The plan's three HARD REQUIREMENTS survive as hook invariants: it fails **closed to ask** (never deny), `append-decision` and `kill` are ALWAYS ask (authorizing a consent-commit from consent on file is laundering), the gated set derives from `block_chain.GATED_BLOCKS` at call time rather than a hand list, and only `mcp__hpc-agent__.*` is matched (no unrelated Bash rules ride along) | `6973cf1c` (fix `b0de19f6`, merge `d9f3554d`) |
| 7 | Route-blind triage | **LANDED** — both blind spots closed. (a) `infra/readiness_sensors.py` reads the effective chain from ssh's OWN resolution (`ssh -G`, never a second ssh_config parser), senses each leg separately, and derives the path verdict dead at the first dead hop regardless of what the target answered; net-triage became a thin composer. (b) `ssh_circuit.degradation_advice` under a ProxyJump now names BOTH causes and offers the alternate-route preamble probe as the discriminator instead of steering to a sibling behind the same dead hop; the un-jumped text is byte-identical. Also shipped in the same unit: the pre-detach path gate (`ops/path_gate.py`, wired into `submit-s2`) and the flap-riding bounded stage retry | `1e89053a` (fix `5f82bab1`, merge `bff6ca32`) |

Adjacent work that rode the same waves (not docket items, recorded so the
history is not misread as gaps): the S2 readiness ledger + SLO pillars
`40d80fc4` (fix `cb4875aa`, merge `4acf2e67`), the reconcile rung-0 zombie fix
`637f57dc` (merge `8db21792`), the Windows no-console ssh capture `1407526e`,
and the items 6+7 doc landing `abd1acf7`.

**What is still owed from this plan:** items 2, 3, 4 in full, and item 1's
inventory sweep. Items 3 and 4 are coupled — rule 3 first, because a ruling
that fires the canary at view-relay may delete item 4 rather than complete it.

## Deferred (hours-scale async tail, separate thread)

07-27 canary `reporter_unreachable` rc=255 regression (blocks results
outright); repair-ladder serial walltime escalation → priors +
jump-to-cap; canary express-partition placement; terminal-sidecar ts
collisions (unmeasurable final aggregate).
