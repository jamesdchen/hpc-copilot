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
| Every attestation-shaped feature (approval / sign-off / greenlight / unlock / receipt / lock / auto-clear record) routes through the ONE kernel `state/attestation.py` — its un-fakeable recompute lock (`bind`), its drift-revocation reducer (`reduce`, newest-first → `current`/`stale`/`absent`), and its record-shape validator are a single definition, never a fifth divergent copy (the one-definition rule applied to the primitive itself; `docs/design/notebook-audit.md` T0). Human vs code attestations are the SAME record shape — they differ only in the ADDITIONAL per-instance lock (authorship for human, recompute alone for code) | `tests/state/test_attestation.py` pins the kernel's fire paths (`bind` refuses a mismatched sha, `reduce` reads drift as `stale` not `current`, `validate` refuses a non-literal attestor / an invented-empty `subject_id`); each migrating member (T6 sign-off, T8 auto-clear, greenlight/unlock) adds an `inspect.getsource` route-through assertion as it lands (the `test_layers_share_one_drift_predicate` precedent — a mechanized holder accrues per member, since a route-through cannot be pinned before the member exists) | a new sign-off / receipt / lock record re-inlines recompute-and-compare or newest-first drift instead of calling the kernel, or `bind`/`reduce`/`validate` stops refusing a fabricated hash / a drifted attestation / a bad attestor |
