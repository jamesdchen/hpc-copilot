---
slug: determinism-boundary
order: 1
title: "The determinism boundary: judgment in the LLM, mechanism in verbs"
scope: "Judgment stays in the LLM; every rule-fixed step is a composed verb, enforced by removing the affordance."
---

# The determinism boundary: judgment in the LLM, mechanism in verbs

An autonomous worker should perform only *genuine judgment* — the free-text
intent it relays (a campaign `goal`), long-tail classification a matcher can't
resolve, choosing among real candidate ambiguities. Every step whose outcome is
fixed by a rule belongs in a **composed verb**, not in skill prose the model
executes: authoring source or spec files, sequencing a deterministic verb chain,
resolving a field that has a known default, deriving a path. And every
agent-facing capability and contract must be reachable through a verb or a doc
the worker prompt points at — the worker must never read framework source (or
`inspect.getsource`) to learn a contract, nor hand-roll a capability the
framework already provides.

The enforcement is **removing the affordance**, not adding prose. Prose ("apply
a two-line edit", "do not invent a task_generator") is honor-system: the model
rationalizes around it under pressure. Observed failures that prose did not hold:
an `Edit`-tool decoration step that rewrote a scaffold's whole function body; a
fabricated `task_generator` justified by "autonomous mode applies safe_defaults";
a hand-sequenced classify pipeline mislabelled "in parallel" across a strict
producer→consumer dependency; a hand-rolled SLURM campaign controller and a
strategy contract reverse-engineered from site-packages source. Each is the same
root cause in a different face — **authoring / sequencing / discovery** — and
each fix takes the same shape: a bounded verb does the deterministic step, and
the tool or surface that allowed freelancing is removed (no `Edit` in onboarding
skills; the strategy is materialized by `scaffold-strategy`, not copied from
source; the preflight→classify chain is one `classify-axis-auto` call, not
hand-sequenced; the submit resolution applies safe-defaults via a deterministic
verb whose field partition refuses to fabricate a `task_generator`).

A guard the LLM itself satisfies is not a guard. A provenance marker claiming
"this task_generator was caller-supplied" was rejected for exactly this reason
(see "Verify a guard can actually fire") — the same model that fabricates the
value sets the marker. The lock is the missing affordance plus a deterministic
field partition (`ops/submit/field_partition.py`) whose `Ambiguity` refuses a
safe-default on a required-caller field — a guard that *can* fire.

## Enforcement map

Rows accrue per surface as the verbs land; the first two ship with the
`decorate-entry-point` surface.

| Rule | Enforced by | Fires when |
|---|---|---|
| Onboarding skills carry no `Edit` (decoration is a verb, not free-form source editing) | `tests/contracts/test_onboarding_skill_no_edit.py` | the `hpc-wrap-entry-point` skill's `allowed-tools` lists `Edit` |
| `decorate-entry-point` leaves the function body byte-identical | `tests/incorporation/test_decorate_entry_point.py::test_decorates_and_leaves_body_byte_identical` | the AST splice changes any line other than the inserted import + decorator |
| A `@register_run` swept flag naming no run() parameter is refused at interview time (no `**kwargs`), warned when `**kwargs` can absorb it — never deferred to the cluster canary (run #8: samples/n_samples swept-flag mismatch) | `tests/ops/memory/test_interview.py::TestSweptFlagValidation` | `_validate_swept_flags_against_run` stops refusing a swept `resolve(i)` key that maps to no signature flag (and is neither a framework-injected/`fixed_params` exempt nor absorbed by `**kwargs`), or starts refusing a matching/exempt/`**kwargs` case |
| No raw `ssh`/`scp`/`rsync` affordance in agent-facing prose (remove the side channel that bypasses the connection-storm guards) — the affordance removed is the `inspect-deployment` companion: cluster reads go through a throttled verb, not raw ssh | `scripts/lint_no_raw_ssh.py` (CI + pre-commit), fire path pinned by `tests/scripts/test_lint_no_raw_ssh.py` | a bare `ssh`/`scp`/`rsync` invocation appears in a code span of a SKILL body or `worker_prompts/*.md` (a cited `ALLOWLIST` exempts a genuine human-debug doc) |
| No harness-block-listed command in agent-facing prose (`python -c`/`bash -c`, `$(...)`, a pipe, background `&`, a deny-listed verb, or a chain to a non-allow-listed command) — an autonomous worker that emits one stalls on a non-bypassable permission prompt, which mid-run is unrecoverable | `scripts/lint_no_blocklisted_commands.py` (pre-commit), clean-tree + fire path pinned by `tests/scripts/test_lint_no_blocklisted_commands.py` | a runnable blocked command appears in a code span of a SKILL / `worker_prompts/*.md` (an all-`hpc-agent`/`git` `&&` chain is exempt on a SKILL — the classifier splits + allows each segment; the invoke-only worker fires on ANY chain; a cited `(path, category)` `ALLOWLIST` exempts a human-debug doc) |
| No unlock/relax verb AFFORDANCE exists (the no-unlock-verb doctrine's registry leg, B8; philosophy audit 2026-07-12): no primitive is named like an unlock/relax verb and no chain-table step carries one — a scope unlock is an append-decision record under the gated block or nothing. Scope caveat, recorded honestly: the pin is substring-based (`unlock`/`relax`), so a synonym-named relaxing verb (`reopen`, `release`) would evade it — same posture as the sign-off sibling pin | `tests/ops/test_decision_journal_primitives.py::test_no_unlock_affordance_in_registry_or_chains` (landed `d9c6632`, mirrors `test_no_signoff_affordance_in_registry`) | a primitive named with `unlock`/`relax` lands in the core registry, or a chain-table step in `infra/block_chain.ORDER` carries one |
| `agent_facing` is bounded by REACHABILITY, and the verb implications cannot outrank it: a primitive with no `CliShape` declares `agent_facing=False`, because both agent surfaces invoke by rendering that shape into argv — CLI dispatch directly, and the MCP server through `_cli_primitives()` → `_invoke_cli()`, which the tiered catalog's generic `run-primitive` tool RECURSES INTO rather than bypasses (it re-enters `call_tool`, resolves through the CLI-gated `_invocable()`, then asserts `isinstance(shape, CliShape)`). The three verb-implication rows above it (workflow / scaffold / validate ⇒ agent-facing) are therefore scoped to CLI-bearing primitives. The flag's only effect is whether `render_llms_full` expands a primitive's body + both schemas into the context dump, so flagging an uninvokable primitive is pure cost — the agent reads a contract it cannot call. Corollary, deliberately unmechanized: `agent_facing=False` is NOT a claim that nothing calls the primitive, only that no *agent* can; composed sub-checks surface through their composer's envelope, and that composer is the agent-facing verb | `tests/contracts/test_agent_facing_partition.py::test_cli_less_primitives_are_not_agent_facing`, with `::test_workflows_are_agent_facing` / `::test_scaffolds_are_agent_facing` / `::test_validators_are_agent_facing` scoped by `meta.cli`; the downstream gate `tests/cli/test_cli_completeness.py::test_every_agent_facing_primitive_has_a_cli_subcommand` now runs with NO exemption set, so the two rules can no longer disagree silently | a primitive with `cli=None` declares `agent_facing=True`, a verb-implication row stops being scoped by `meta.cli` (re-creating the contradiction), or a blanket no-CLI exemption set reappears in the CLI-completeness gate |
| Every attestation-shaped feature (approval / sign-off / greenlight / unlock / receipt / lock / auto-clear record) routes through the ONE kernel `state/attestation.py` — its un-fakeable recompute lock (`bind`), its drift-revocation reducer (`reduce`, newest-first → `current`/`stale`/`absent`), and its record-shape validator are a single definition, never a fifth divergent copy (the one-definition rule applied to the primitive itself; `docs/design/notebook-audit.md` T0). Human vs code attestations are the SAME record shape — they differ only in the ADDITIONAL per-instance lock (authorship for human, recompute alone for code) | `tests/state/test_attestation.py` pins the kernel's fire paths (`bind` refuses a mismatched sha, `reduce` reads drift as `stale` not `current`, `validate` refuses a non-literal attestor / an invented-empty `subject_id`); each migrating member (T6 sign-off, T8 auto-clear — both LANDED with route-through assertions) adds an `inspect.getsource` route-through assertion as it lands (the `test_layers_share_one_drift_predicate` precedent — a mechanized holder accrues per member, since a route-through cannot be pinned before the member exists); greenlight/unlock are FORMALLY DEFERRED (RULED A, 2026-07-17): their records carry no content sha for `bind` to lock (the greenlight gate enforces by name-membership against the persisted brief) and `reduce`'s newest-valid-wins would regress the gate's timestamp-anchored nudge-supersession selection — they are sha-less precedence records, not attestations, and stay on their own gate (BR-14 stop-report, `docs/plans/backlog-2026-07-17.md`) | a new sign-off / receipt / lock record re-inlines recompute-and-compare or newest-first drift instead of calling the kernel, or `bind`/`reduce`/`validate` stops refusing a fabricated hash / a drifted attestation / a bad attestor |
| **An AGENT park never consumes nor mints consent.** Code sequencing a deterministic chain is the boundary's own doctrine, but a chain that stops for the LLM ("write the draft") stops at a park — and every affordance the park machinery composes (`greenlight_target`, `consent_hint.compose_approve_hint`, the overnight standing-consent grant offer, `answer_menu`'s bare-`y` line) was built for a HUMAN answering a CONSENT question. Reusing them for an authorship request would mint consent out of a drafting ask, which is the fabricated-approval class in a new face. So an `actor="agent"` park (`block_chain.AGENT_PARKS`, keyed `(verb, stage)`) composes NONE of them — `greenlight_target` returns `None` there by construction, the park writes a code-authored `draft_ask` instead of an answer menu, and its RESUME leg never reads the decision journal at all (it re-runs the parked block so the block re-reads the world; the evidence is the file on disk, never a journaled `y`). Structural, not prose: the resume branch sits ABOVE the greenlight read in `run_tick`, so no committed greenlight — for this boundary or any other — is even in scope | `tests/_kernel/lifecycle/test_agent_park.py::test_agent_park_resolves_no_greenlight_target`, `::test_agent_park_composes_no_consent_affordances`, `::test_agent_park_never_satisfies_assert_greenlit_or_consented`, `::test_agent_park_resume_consumes_no_committed_greenlight`, `::test_agent_park_resume_ignores_a_greenlight_committed_for_the_boundary`, `::test_human_park_marker_is_byte_identical`; the census leg `tests/contracts/test_bare_y_coverage.py::test_every_park_boundary_takes_a_bare_y_or_states_why_not` (an agent park has no target, so it must sit on the ALLOWLIST with its reason stated) | an agent park resolves a greenlight target, composes an `approve_hint` / standing-consent offer / bare-`y` menu, advances on a journaled `y`, or a human park's marker bytes move |

- 2026-07-29 — the `agent_facing` reachability bound landed, closing a
  contradiction that had been resolved the wrong way for as long as both rules
  existed. `test_agent_facing_partition` asserted `verb=validate ⇒
  agent_facing=True` unconditionally; `test_cli_completeness` asserted
  `agent_facing ⇒ has a CLI subcommand`. Nine primitives satisfied the first and
  failed the second, and the resolution had been an `_INTENTIONALLY_NO_CLI`
  exemption set in the CLI gate rather than a fix to the flag — so seven
  uninvokable sub-validators of `validate-campaign` / `submit-pipeline` kept
  expanding their bodies and both schemas into every `capabilities --full`
  render (~75 KB, ~3.6% of the dump the flag exists to keep honest). Nothing
  pinned the exemption set against rot, and two of its eleven entries
  (`recommend-partition`, `update-run-constraints`) had since grown real CLI
  verbs and were exempting a check they already passed. Fixed at the source:
  reachability now bounds the flag, the verb implications are scoped to
  CLI-bearing primitives, and the exemption set is deleted rather than re-pinned
  (an allowlist with no liveness test was the mechanism that hid the bug).
  `validate-self-qos-limit` is recorded as inert in its own doc rather than
  quietly wired in — it has no composer at all, and giving it one is a behavior
  change (campaigns at or above the QoS cap would start being refused), not a
  cleanup.
- 2026-07-29 (same day, follow-up ruling) — both inert primitives DELETED
  after verification, not preserved. `decide-resubmit`: superseded by design,
  not merely unwired — its only non-trivial branch needs
  `resubmit_failed_threshold > 0`, a knob that exists nowhere else in the
  tree (no spec field, no config, no caller), and the shipped posture is
  explicit that silent auto-resubmit is NOT a code path (`hpc-status`
  SKILL; `ops/monitor/classify.py`) — the live recommendation is the
  categorical anomaly table in `ops/status_blocks.py`, so wiring the
  threshold verb in would have introduced the exact affordance the design
  removed. `validate-self-qos-limit`: its designed feeder (the slash
  command running `squeue`/`sacctmgr` raw and passing the numbers in)
  became impossible when the no-raw-ssh lint removed that affordance
  class, and no throttled verb exposes the QoS cap — the guard could not
  legally fire from any current surface without new Slurm-dialect probe
  work. The bug class it covered is recorded as knowingly unguarded in
  `docs/plans/backlog-2026-07-17.md` §4 rather than silently dropped.
- 2026-07-17 — greenlight/unlock kernel-migration RULED A (formally deferred): the BR-14 dispatch investigation proved the fit is absent (no content sha to bind; `reduce` selection semantics would regress the greenlight gate's supersession invariants — a consent hole, not a refactor) and T8 was already migrated. The row's member list now reflects T6+T8 landed, greenlight/unlock deferred-by-ruling. Option B (a non-sha precedence reducer) rejected as a kernel API change without a safety payoff.
- 2026-07-30 (P2.c) — the AGENT-park row gained its SECOND member:
  `("wrap-entry-point-auto", "needs_wrapper_argv")`. The doctrine is unchanged
  (a draft is authorship, not authorization); what this member adds is the
  demonstration that "authorship" is not a synonym for "drafting prose": what is
  owed at that boundary is the TRANSCRIPTION of CLI parameters `detect-entry-point`
  already read mechanically off the AST into an argv template. The split is a
  registry edit, not a runtime sniff — the non-extractable case emits a DISTINCT
  stage (`needs_wrapper_argv_unsupported`) that resolves to a human park with a
  chain-forward override target, precisely so `AGENT_PARKS` stays keyed by
  `(verb, stage)` and "who answers this" remains a one-line auditable decision.
  Pinned by `tests/ops/test_onboard_chain.py::test_wrapper_argv_park_is_the_chain_s_second_agent_park`
  plus the bare-`y` census allowlist entry (an agent park has no greenlight
  target, so the census DEMANDS the stated reason).
