# Fable sweep — devx + pack-seams implementation handoff (2026-07-15)

Status: **READY FOR OPUS SWARM DISPATCH.** Produced by a 15-agent sweep
(7 deep-dive + 7 adversarial premortem + 1 cross-unit critic) over
`docs/plans/devx-and-pack-seams-2026-07-15.md`. Every unit verdicted
SOUND-WITH-CHANGES; the mandated changes are folded into the unit contracts
below. Full per-unit specs (files, design calls, hazards, test plans,
premortem attacks) live in **`sweep-results.json`** in this directory —
each dispatch brief is authored from this memo + that unit's JSON entry.
Precedent: `docs/plans/handoff-packages-2026-07-12/` (file-disjoint units,
no-commit-of-generated protocol, integrator merge order).

## Frozen seams — settled HERE, before any dispatch (do not re-litigate)

1. **Derivation dataclass name: `DerivedFrom`** (frozen dataclass
   `{pack, seam, version, sha}`; `PackManifest.derived_from: DerivedFrom |
   None = None`; `parse_manifest` treats the key as optional, malformed →
   loud `SpecInvalid`; `reseal_manifest` passes it through). The two dives
   contradicted each other (WS5 said `DerivedFrom`, WS6 said
   `PackDerivation`) — `DerivedFrom` wins; WS6 builds against it.
2. **Compose disclosure contract**: keys `{field, value, pack, source}` are
   byte-compatible with today; WS6 only ADDS keys (candidate list + deciding
   rule). WS7 reads `value`/`pack` and must not depend on the added keys'
   shapes beyond what its brief states.
3. **Regen entry-point invocation string**: `python scripts/regen_all.py
   --check|--write` (plain script, NO pyproject console-script — packaging
   stays byte-untouched while run 13 is live). `regen_all.py` MUST export a
   module-level `REGEN_SCRIPTS` tuple (WS4's count-probe reads it; the
   pre-commit hooks may collapse, so the tuple is the canonical list).
4. **Enforcement-map row** (single row covers P1b/c/d; WS6 pastes it
   verbatim, WS7 CITES it and adds test IDs — WS7's five test IDs ride
   WS6's dispatch brief even though WS7 merges two waves later): *"A
   multi-candidate pack seam resolves by a disclosed rule or refuses loudly
   naming every candidate — never a silent heuristic (first-match,
   opt-in-order, or receipt-binding tiebreaks)."*
5. **Regen script order** (WS1 DC1, verified against the scripts):
   build_schemas → bake_operations_json → build_primitive_frontmatter →
   build_primitive_index → build_operations_index → build_verb_module_map →
   check_no_pending_primitive_docs. Subprocess invocation (not import) —
   env-timing/registry-cache isolation; run-all-report-all failure policy;
   bare invocation refused (per-script bare semantics are inconsistent).

## Wave plan (the critic's, adopted)

**Wave 1 — five merges + one branch-dev, max parallel:**
`WS3-ci`, `WS1-regen-story`, `WS2-merge-driver`, `WS4-doc-lints`,
`WS5-program-init` build and merge; `WS6-compose-rewrite` DEVELOPS on a
branch against the frozen `DerivedFrom` seam but does NOT merge (cannot
self-verify green until WS5 is in the tree).
**Integrator merge order: WS3 → WS1 → WS2 → WS4 → WS5.** Rationale: WS3
shifts ci.yml line numbers so WS1's regen-step re-point is re-anchored by
content after it; WS1 before WS4 so the `167 primitives` literal is
rewritten once (WS4 never allowlists it); WS5 last so the wave's single
regen point closes the wave.
**Wave-1 close-out (integrator, explicit checklist — items owned by NO
unit diff):** (a) run `python scripts/regen_all.py --write` ONCE (WS5 is
the sole regen-needed unit; no unit commits generated files); (b) wire
`merge_generated` ensure() into regen_all (the WS1/WS2 cross-wire); (c)
verify the ledger gate green at HEAD; (d) full CI on the merged head.

**Wave 2:** `WS6-compose-rewrite` rebases onto merged WS5, verifies green,
merges. Short if wave-1 dev happened.

**Wave 3:** `WS7-echoes-collisions` — HARD-serialized behind WS6: same
module, same test file, and WS7 generalizes the fixture helpers
(`_build_pack`/`_opted_in`) WS6's new tests call. Interface-first
parallelism is NOT sound for same-module co-tenants; do not start WS7 from
a stub.

## Serialization rules (integrator-enforced)

- `.github/workflows/ci.yml` — WS3 owns; WS1's hunk applied after, by
  content not line number. Check names (`regen-pr`, `test (3.1x)`,
  `plugins`, `conformance`, `test (windows)`) are frozen — though verified
  live: NO branch protection currently exists (api 404), so renames are
  safe-but-pointless; keep names stable anyway.
- `docs/internals/engineering-principles.md` — append-only hot file; four
  units (WS1/WS2/WS4/WS6) each append one self-contained row; integrator
  serializes trivial end-of-map conflicts in merge order. No unit rewrites
  an existing row.
- `src/hpc_agent/state/pack_declarations.py` + its test file — WS6 then
  WS7, strictly sequential INCLUDING development.
- Generated files (`src/hpc_agent/operations.json` — note: NOT
  `config/operations.json`, the plan's path was wrong —
  `src/hpc_agent/schemas/*.json`, `docs/generated/*`,
  `docs/primitives/README.md` + frontmatter, `cli/_verb_module_map.py`) —
  integrator-only, regen once per wave.
- `.pre-commit-config.yaml` and `docs/internals/adding-a-primitive.md` —
  WS1-exclusive.

## Global hazards (every dispatch brief carries these)

1. **RUN-13 LIVE RELAY — merge is safe, INSTALL is not.** No cluster/demo
   env wheel refresh until run-13 fleets harvest. WS5 touches
   `state/pack_sweep.py::_semantic`: a pre-derived_from manifest MUST
   round-trip `_semantic`-equal or every existing pack reads stale on the
   next gate tick and mass-reseals, revoking harxhar-clean's live receipts
   the moment a fix wheel ships. Mandatory regression test:
   no-derived_from manifest compares `_semantic`-equal between recipe-fresh
   and on-disk.
2. **COUPLED-DEPLOY TRAP.** Once a wheel carrying WS6 reaches the lab env,
   multi-candidate compose REFUSES until the human runs `program-init`
   adopt to stamp `derived_from` into the rv manifest (the verb ships in
   the same wheel, so the refusal window is unavoidable). Sequence:
   wheel-install → immediate `program-init` adopt, same human session,
   after run-13 harvest. Adopt mode MUST NOT byte-change the signed
   `rv_audit.py` or all five section sign-offs are invalidated (finding-1
   blast radius). The demo's current explicit-template workaround bypasses
   compose entirely and keeps working throughout.
3. **MAIN-STAYS-SHIPPABLE vs the strict ledger gate.** WS1 reconciles the
   regen-debt ledger against live HEAD in the SAME commit-series the gate
   lands (the ledger's 6 outstanding rows verified stale-green on HEAD;
   three rows have prose-only gate cells that must be normalized or
   'no live gate'-marked for the parser).
4. **`model_dump` null disclosure (WS5).** `cli/_dispatch.py` serializes
   without `exclude_none`, so `"derived_from": null` WILL appear in
   pack-status output for legacy packs. RULING: accepted as an additive
   change (do NOT touch the global dispatch serializer); pin it with a
   test and note it in the pack-status doc. The dive's "legacy output
   byte-identical" claim is retired.

## Per-unit premortem deltas (mandated changes; full text in sweep-results.json)

- **WS1**: add the regen_all smoke-test file to new_files (disjointness);
  the pytest-embedded regen check must respect ci.yml's self-heal doctrine
  (--check gates only on legs that ran a local --write — gate on 3.12 or
  keep it out of the matrix legs); fix the union-trigger regex to include
  `src/hpc_agent/schemas/*.json`; `cli/_verb_module_map.py` currently has
  NO --check gate in ANY workflow and no auto-commit pattern covers it —
  the unit closes that hole explicitly; export `REGEN_SCRIPTS`.
- **WS2**: merge-driver command needs sh-quoting (repo path contains a
  space); the tmp-repo behavioral test must fixture-copy the driver script
  (precedent `tests/infra/test_audit_fixes.py:212`); document
  merge-OR-REBASE semantics (rebase inverts ours/theirs — agent branches
  silently drop their own generated changes until regen); pin root-only
  `.gitattributes` (a second one exists at `packs/quant/.gitattributes`);
  graceful stderr fallback naming the six scripts when `regen_all.py`
  absent; new script carries lint_text_io_encoding + ruff obligations;
  standing gap: CI is ubuntu-only, Windows driver behavior is uncovered.
- **WS3**: use `!cancelled()` not `always()` (cancel-in-progress
  concurrency at ci.yml:27-29 — `always()` leaks runner minutes on
  cancelled runs); the coverage step is ALREADY gating today (no
  continue-on-error) — fold accordingly; branch-protection residual
  verified closed (no protection configured).
- **WS4**: the pin scan is LINE-BASED (full-text finditer lets `\s+` cross
  newlines — false positives); schemas live-count = RECURSIVE glob (249
  incl. skill_returns/, not 244/245); note `test_prose_primitive_count`
  sits at its exact tolerance boundary today (|167-169| = 2) — WS4
  replaces that pin anyway; add a one-line drift-log entry with the
  appended section; include 'regen scripts' in the pattern alternation
  (the plan doc's regex omitted it).
- **WS5**: add the unowned-but-touched test files to files_owned
  (tests/ops/pack/test_status_audit_template.py,
  test_toy_pack_integration.py, tests/_wire/test_pack_wire.py); add the
  new wire model to test_pack_boundary.py's hand-listed `_pack_wire_models`
  (it silently escapes the forbidden-vocab lint otherwise); spec adopt-mode
  sweep.json rewrite + unknown-key preservation (only a reader exists
  today); define no-op rebind semantics for the unchanged domain pack;
  document the old-wheel reseal hazard (an old wheel's auto-remedy emits a
  fixed key set and STRIPS derived_from on the first drift after
  migration — the migration precondition names the minimum wheel);
  self-verification protocol: the unit runs regen locally to test but
  commits NONE of it (test_generated_map_is_in_sync_with_registry pins
  committed state — the integrator regen at merge is what turns it green;
  the unit's own branch CI will be red on that one test, disclosed).
- **WS6**: the best-effort `except SpecInvalid: continue` manifest-skip at
  pack_declarations.py:700-703 silently shrinks the candidate set —
  under the new law that skip becomes part of the disclosure (skipped
  candidates NAMED with their parse error), not a silent drop; a malformed
  derived_from must surface, not be swallowed; add the zero-survivor
  (self-edge / mutual-edge cycle) refusal to the test matrix; note the
  intake side-effect precedent (compose fires at interview.py:445 AFTER
  tasks.py materializes at :380 — the new refusal leaves .hpc/tasks.py
  behind; disclose, don't fix here); add the domain-packs.md drift-log
  entry (the selection law is a design-center change); record_interview is
  the universal submit intake — the refusal's remedy text must make sense
  for non-audit callers too.
- **WS7**: update pack_declarations.py's own module docstring + the
  :534-543 fail-open absolute claims in the same commit (doc-truth);
  define the healthy-claimant predicate explicitly (manifest-sha-only, the
  echo path's existing bar — do NOT silently strengthen to the module's
  wider drift definition); RULE: duplicate same-pack opt-in entries dedupe
  before collision counting (two entries for one pack are NOT a
  collision); disclose that the exhaustive walk widens the live probe
  surface (packs after the first claimant are now read); the
  compose-resolves-but-echo-refuses third outcome must be in the test
  matrix; scope the enforcement row's third fires-clause so it does not
  indict S2 failure_patterns' sanctioned cross-pack union.

## Dispatch protocol

Per handoff-packages precedent: one Opus agent per unit, worktree-isolated
(`isolation: worktree`) since units edit files, no-commit-of-generated
protocol, each brief = this memo's frozen-seams + global-hazards sections +
the unit's sweep-results.json entry + its premortem-delta block above.
Agents report; the integrator (relay session) stages each agent's own files
only, merges in the stated order, runs the wave close-out checklist.
