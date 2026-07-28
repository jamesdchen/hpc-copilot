export const meta = {
  name: 'land-async-refill',
  description: 'Land campaign async-refill (#362) Phase 1: the campaign-refill actor with campaign-run as its spine, trial-tokens fix, scaffold verification, docs/ruling closure',
  phases: [
    { title: 'Design', detail: 'one architect validates the refill-actor design against the tree', model: 'opus' },
    { title: 'Build', detail: '5 parallel file-disjoint units', model: 'opus' },
    { title: 'Integrate', detail: 'regen, lint, typecheck, full tests, serial commits, push', model: 'opus' },
    { title: 'Review', detail: '3 adversarial lenses, then fix + re-verify', model: 'opus' },
  ],
}

const REPO = '/home/user/hpc-copilot'
const BRANCH = 'claude/tool-latency-reduction-iqpgcr'

const CONTEXT = `
REPO: ${REPO} (Python package hpc-agent, src/ layout, installed editable).
GOVERNING SPEC: docs/design/campaign-async-refill.md (RFC #362) — read it in full first. Also read docs/internals/campaign-lifecycle.md (TL;DR + invariants) and docs/internals/engineering-principles.md.
NON-NEGOTIABLE INVARIANTS:
- One step per tick; NO driver memory across ticks; every byte of resume state on disk in .hpc/ or the journal. The refill computation is a pure function over journal state, recomputed each tick.
- Opt-in only: with manifest async_refill unset/false, behavior is byte-identical to today (property-tested).
- "A guard the LLM itself satisfies is not a guard" — enforcement is affordances/verbs, never prose.
- Never orphan cluster jobs; the advance ladder's _drain_before_stop already handles terminal-stop draining.
- Cluster-social envelope: batch-status is one scheduler query per login node per poll regardless of K; new submits ride existing throttle/slots/breaker. Do not add per-run polling.

WHAT IS ALREADY LANDED (verified in tree 2026-07-12 — do NOT rebuild):
- meta/campaign/atoms/advance.py: full async ladder (--async-refill/--max-in-flight, _refill rule with refill_count = min(K - in_flight, remaining_max_jobs), _drain_before_stop, manifest defaulting from top-level async_refill/max_in_flight).
- schemas/campaign_manifest.json: async_refill + max_in_flight fields.
- meta/campaign/atoms/load_context.py: _campaign_async_config + _async_should_refill (calls campaign-advance authoritatively; routes a decide/refill step only on decision=="refill").
- meta/campaign/blocks.py: campaign-watch treats "refill" as watching_healthy (this will change — see U-A).
- execution/mapreduce/templates/scaffolds/optuna_async_strategy.py: tell-by-trial_token + constant_liar; incorporation/scaffold_strategy.py has --async-refill emit path.
- ops/campaign_run.py: campaign-run composite (submit-pipeline → status-pipeline → aggregate-flow), detach-capable via _kernel/lifecycle/detached.py (SUPPORTED_DETACHED_BLOCK_VERBS includes campaign-run), terminal replay keyed on cmd_sha via state/block_terminal.py.

THE GAP THIS SWARM CLOSES:
1. No refill ACTOR exists: grep "refill" in ops/campaign_run.py, block_drive.py, drive.py = zero hits. load_context.py's comment cites "the refill arm" of a resolver (meta/campaign/deterministic_resolver.py) that was DELETED in the worker-removal wave. The RFC's §3 resolver bullet must be re-homed onto the block-drive architecture.
2. RFC §5 trial-tokens fix: _wire/workflows/resolve_submit_inputs.py computes trial_tokens via compute-run-id but its sidecar_spec model_copy(update={...}) injects only run_id/cmd_sha — tokens dropped, breaking out-of-order tell.
3. Docs/RFC are stale (RFC header says "Nothing here is implemented yet"; campaign-lifecycle.md and meta/campaign/README.md reference the deleted hpc-campaign-driver console script / driver.py).

SETTLED DESIGN PRIORS (validate against the tree; escalate in your report if the tree contradicts them, but do not re-litigate philosophy):
- New primitive \`campaign-refill\` (verb="workflow", agent_facing, side-effecting submit, idempotent per tick) in ops/campaign_refill.py + wire model _wire/workflows/campaign_refill.py (CampaignRefillSpec/CampaignRefillResult; Result DECLARES next_block so it derives into the MCP curated catalog).
- Per tick: call campaign_advance; if decision != "refill" → typed no-op stage (e.g. "no_refill_needed", carrying the advance decision). If "refill": for each of refill_count slots, build the next iteration's campaign-run spec (reuse ops/scaffold_spec._scaffold_campaign_run / the existing campaign-run spec-building seam — compute-run-id runs the strategy's ask control-plane-side, so each slot gets fresh distinct trial_params via the constant_liar async scaffold) and invoke campaign_run with detach=True. Collect handles. Return stage "refilled" with submitted run_ids/pids.
- Crash-mid-tick safety: no new state files. Each submitted iteration immediately has a sidecar → in_flight rises → next tick's refill_count shrinks. Partial ticks self-correct. Do NOT add a cursor.
- Refuse un-greenlit campaigns (manifest.greenlit check mirroring the sibling blocks) — campaign greenlight is the standing consent; iterations carry no per-iteration human boundary (human-amplification design §4).
- Wiring: campaign-watch gains a fourth terminator stage "watching_refill" (needs_decision=False) emitted when the advance decision is "refill" (replacing its current lumping into watching_healthy); infra/block_chain.py SUCCESSORS gains ("campaign-watch","watching_refill") → "campaign-refill"; campaign-refill's stages all map to None (chain ends; the next cron//loop tick re-enters via campaign-watch — preserves one-step-per-tick). campaign-refill is NOT in GATED_BLOCKS.
- load_context._build_delegate: when async_refill is on and the campaign manifest is greenlit, the refill delegate step becomes kind="cli" mapping to campaign-refill (deterministic — no judgment), keeping kind="agent" for the sync decide step. Update the stale "refill arm" comment.
- MCP: campaign-refill is fast (spawns detached children, returns) → sync-capable over mcp-serve; do NOT add it to _DETACH_REQUIRED_VERBS.
- Every @primitive addition requires the regen scripts (integrator runs them; builders do NOT run regen).
HOUSE STYLE: dense module docstrings carrying design rationale with path::symbol cites; guards must demonstrably fire (each new branch gets a test with the flag ON); comments state constraints, not narration; Pydantic wire models extra="forbid" on inputs; follow existing sibling files closely (campaign_run.py, aggregate_blocks.py are the best templates).
`

phase('Design')
const memo = await agent(
  CONTEXT +
  `\nYOUR TASK (architect): Read the governing spec + the files named above IN FULL (also: infra/block_chain.py, _kernel/lifecycle/block_drive.py::_chain and detached.py, meta/campaign/blocks.py, ops/scaffold_spec.py::_scaffold_campaign_run, _wire/workflows/campaign_run.py + campaign_blocks.py, state/block_terminal.py, and search docs/ + issues text for any '#362' plan superseding the RFC). Validate every settled prior against the tree. Produce the implementation memo the build units will follow: exact file list per unit, exact symbol names, the campaign-refill spec/result field lists, the block_chain rows, the load_context delegate change, how the per-slot campaign-run spec is concretely built (name the exact seam and required spec fields — this is the one genuinely open question; trace how a campaign iteration's submit spec is produced today end to end and pick the minimal reuse path), idempotency/replay interaction (block_terminal keying for campaign-refill if any — note campaign-run children handle their own terminal replay), the test list per unit, and any tree-contradiction escalations. Be exact: builders will not re-derive.`,
  { label: 'design:refill-actor', phase: 'Design', model: 'opus', effort: 'high' }
)

phase('Build')
const UNITS = [
  {
    key: 'actor',
    prompt: `UNIT A — the campaign-refill actor. Files you own (exclusive): ops/campaign_refill.py (new), _wire/workflows/campaign_refill.py (new), meta/campaign/blocks.py (add watching_refill terminator to campaign-watch), meta/campaign/atoms/load_context.py (delegate kind flip + stale-comment fix), infra/block_chain.py (SUCCESSORS/ORDER rows for campaign-refill), plus NEW test files tests/meta/test_campaign_refill.py and tests/meta/test_watch_refill_stage.py (do not edit existing test files). Implement per the memo. Register the primitive with a CliShape mirroring campaign-run's (spec_arg, experiment_dir_arg, schema_ref input="campaign_refill" output="campaign_refill"). Write the Pydantic wire models; do NOT hand-write schemas/*.json (integrator regens). Unit tests: refill tick over a synthetic journal with a mocked campaign_run/submit seam (assert N detached submissions, correct spec threading, distinct run handling); no-op path when advance says wait/stop/continue; un-greenlit refusal; crash-mid-tick self-correction (simulate 2-of-3 submitted, re-tick, assert refill_count shrank); watching_refill stage mapping; block_chain successor row. Run ONLY your own test files (pytest <paths> -q). If an unrelated import breaks transiently (parallel sibling edits), retry once after 30s.`,
  },
  {
    key: 'tokens',
    prompt: `UNIT B — RFC §5 trial-tokens fix. Files you own: _wire/workflows/resolve_submit_inputs.py, plus NEW test file tests/_wire/test_resolve_submit_inputs_trial_tokens.py. Read the RFC §5 and execution/mapreduce/reduce/history.py::prior_records. Thread the compute-run-id result's trial_tokens (and trial_params if the sidecar schema carries them — check state/runs.py sidecar fields and _wire/actions/write_run_sidecar.py) into the sidecar_spec model_copy(update={...}) that currently injects only run_id/cmd_sha. Test: end-to-end round-trip — resolve inputs with a tokened compute-run-id result, write the sidecar, assert prior_records surfaces trial_tokens. Run only your own tests.`,
  },
  {
    key: 'scaffold',
    prompt: `UNIT C — async strategy scaffold verification per RFC §4. Files you own: execution/mapreduce/templates/scaffolds/optuna_async_strategy.py, incorporation/scaffold_strategy.py (only if gaps found), plus NEW test file tests/execution/test_optuna_async_scaffold.py. Verify the shipped async scaffold satisfies: (a) tell by trial_token out-of-order; (b) constant_liar sampler; (c) proposal distinctness under K in-flight — note the architecture is one-trial-per-run (K runs each ask once), so B-distinct-asks is satisfied via constant_liar across K separate asks: verify the proposal-index derivation (submitted count, not completed) is correct under concurrent refill and that a re-ask after crash replays the SAME persisted proposal file (idempotent). Verify scaffold-strategy --async-refill emits this variant and that plain optuna_strategy is refused/warned for async campaigns if the RFC requires it. Add unit tests with a mocked optuna study (out-of-order tell; re-tell no-op; distinct concurrent proposals; crash-replay idempotency). Fix only demonstrated gaps. Run only your own tests.`,
  },
  {
    key: 'docs',
    prompt: `UNIT D — docs + ruling closure (you are the ONLY agent touching docs; never touch .py files). Files you own: docs/design/campaign-async-refill.md, docs/internals/campaign-lifecycle.md, src/hpc_agent/meta/campaign/README.md, docs/design/history/rulings-ledger-2026-07.md, CHANGELOG.md, and the campaign skill prose src/hpc_agent/slash_commands/skills/hpc-campaign/SKILL.md (+ its installed twin if the repo pattern requires; check how skills are single-sourced first). Changes: (1) RFC: flip status header to reflect reality, append a drift log (notebook-audit.md pattern) recording — what landed earlier (advance ladder, manifest fields, load-context routing, async scaffold), the §3 resolver bullet re-homed onto the campaign-refill block (deterministic_resolver.py + worker_prompts/campaign.md were deleted in the worker removal), and this build; state Phase 2 live-verify (scripts/campaign_async_live_verify.py) still gates non-experimental status. (2) rulings ledger: append a RULED 2026-07-12 entry — user-ordered Phase-1 build of #362 with campaign-run as the iteration spine, live-verify gate unchanged. (3) campaign-lifecycle.md + meta/campaign/README.md: correct the deleted hpc-campaign-driver/driver.py references to the current reality (_kernel/lifecycle/drive.py substrate + block-drive skills; verify against pyproject [project.scripts] and the tree — shrink stale present-tense facts to pointers per the drift-log doctrine). (4) SKILL.md: add the watching_refill relay line per existing brief-line style. (5) CHANGELOG entry. Cite path::symbol, never line numbers.`,
  },
  {
    key: 'props',
    prompt: `UNIT E — property/integration tests (tests only; no src edits). Files you own (all NEW): tests/meta/test_async_refill_default_unchanged.py, tests/meta/test_async_refill_drain.py. (1) Property test: with async_refill absent/false, campaign-advance's decision ladder output is byte-identical to the pre-async ladder across a generated grid of synthetic evidence states (in_flight 0..N, budget exhausted/not, breaker tripped/not, converged/not, ack fresh/stale) — assert the async-only branches are unreachable flag-off. (2) Drain-before-stop: flag ON, terminal stop pending (converged/breaker) with in_flight>0 → wait_in_flight until in_flight==0, then the stop fires; budget halt does NOT wait (matches sync). (3) Livelock guard: _async_should_refill routes a refill step ONLY when advance decides refill (pool full → no refill step). Read meta/campaign/atoms/advance.py + load_context.py first; use synthetic journals (tmp_path + state/journal.py writers) not mocks where feasible — mirror existing tests/meta style. Run only your own test files.`,
  },
]
const built = await parallel(UNITS.map(u => () =>
  agent(CONTEXT + `\nARCHITECT MEMO (follow it; escalate contradictions in your report rather than improvising):\n${memo}\n\n${u.prompt}\n\nReturn: files changed, tests added + their pass/fail output, any memo contradictions found, anything you deliberately left for the integrator.`,
    { label: `build:${u.key}`, phase: 'Build', model: 'opus', effort: 'high' })
))

phase('Integrate')
const integration = await agent(
  CONTEXT +
  `\nARCHITECT MEMO:\n${memo}\n\nBUILD REPORTS:\n${built.filter(Boolean).map((r, i) => `--- ${UNITS[i] ? UNITS[i].key : i} ---\n${r}`).join('\n')}\n\n` +
  `UNIT F — integrator (serial; you own the whole tree now). In ${REPO}: (1) git status/diff to survey all unit changes. (2) Discover the canonical regen protocol (grep docs + pyproject for regen; the scripts are in scripts/: build_schemas.py, bake_operations_json.py, build_operations_index.py, build_primitive_index.py, build_primitive_frontmatter.py, build_verb_module_map.py — run them in the documented order; check_no_pending_primitive_docs.py too). (3) ruff check . && ruff format . && mypy src/hpc_agent. (4) pytest -q -m 'not slow' (full; fix fallout — including registry/contract pins that enumerate verbs, operations.json baking, schema conformance tests, lint_* scripts run by CI). (5) git checkout -B ${BRANCH}, then commit the work as a small series of logical commits (actor+wiring; trial-tokens; scaffold tests; property tests; docs+ruling), messages in-house style (feat(campaign): ... (#362)), each ending with exactly these two trailer lines:\nCo-Authored-By: Claude Fable 5 <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/session_01So7J6oM5V5gtDvifrm9JCT\n(6) git push -u origin ${BRANCH} (on network failure retry 4x with 2/4/8/16s backoff). Report: regen results, test summary (counts), commit shas, push status, anything unresolved.`,
  { label: 'integrate', phase: 'Integrate', model: 'opus', effort: 'high' }
)

phase('Review')
const LENSES = [
  ['crash-safety', 'Adversarially attack crash/kill safety and idempotency: kill the driver between any two statements of the refill tick — can a trial be double-submitted (same or different run_id)? Can a cluster job be orphaned? Can the optuna proposal index skew under crash-replay? Does partial-tick self-correction actually hold given sidecar write timing inside submit-pipeline (when exactly does in_flight rise relative to qsub)? Trace, do not assume.'],
  ['doctrine', 'Audit against the repo doctrine: default byte-identical (would any flag-off path behave differently?), one-step-per-tick with zero driver memory, guards that can fire (is any new branch untested with the flag ON?), determinism boundary (did any judgment leak into code or any mechanism leak into prose?), manifest field-mirror discipline, no new unlock/sign-off-shaped affordances, house docstring/citation style, engineering-principles.md enforcement-map obligations.'],
  ['correctness', 'Hunt concrete bugs in the new code: refill_count arithmetic under budget caps, spec threading into campaign-run (run_id/cmd_sha/trial fields), block_chain successor rows and stage names EXACTLY matching emitted stage_reached strings, load_context delegate kind conditions, MCP catalog derivation (does the Result truly declare next_block), Pydantic model strictness, schema regen consistency, test assertions that would pass vacuously.'],
]
const findings = await parallel(LENSES.map(([lens, brief]) => () =>
  agent(
    `Repo ${REPO}, branch ${BRANCH} (committed + pushed; review the landed diff: git log --oneline -8 and git show/diff against the pre-series base). Context:\n${CONTEXT}\nINTEGRATION REPORT:\n${integration}\n\nLENS: ${lens}. ${brief}\n\nVerify each candidate finding against the actual tree before reporting (path::symbol + concrete failure scenario). Report only findings that survive your own attempt to refute them; severity-ranked. If none survive, say so.`,
    { label: `review:${lens}`, phase: 'Review', model: 'opus', effort: 'high' })
))
const fixed = await agent(
  CONTEXT + `\nREVIEW FINDINGS (three lenses):\n${findings.filter(Boolean).join('\n\n===\n\n')}\n\nUNIT G — fixer. In ${REPO} on ${BRANCH}: adjudicate each finding against the tree (refute or fix; the reviewers were told to pre-verify but re-check). Apply fixes, re-run the affected tests plus pytest -q -m 'not slow', ruff/mypy, re-run regen if a primitive/schema changed, commit (same trailer lines as before:\nCo-Authored-By: Claude Fable 5 <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/session_01So7J6oM5V5gtDvifrm9JCT\n) and push with retry. Report: per-finding verdict (fixed/refuted + why), final test summary, final shas.`,
  { label: 'fix', phase: 'Review', model: 'opus', effort: 'high' }
)

return { memo_head: String(memo).slice(0, 1500), integration: String(integration).slice(0, 3000), findings_head: findings.filter(Boolean).map(f => String(f).slice(0, 800)), fix_report: String(fixed).slice(0, 3000) }