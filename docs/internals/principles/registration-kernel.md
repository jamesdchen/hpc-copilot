---
slug: registration-kernel
order: 3
title: "The registration kernel: the deployment-boundary attestation is mechanism-only"
scope: "The deploy-boundary promotion is one more attestation over the sealed dossier — mechanism-only, agnostic by five mechanisms."
---

# The registration kernel: the deployment-boundary attestation is mechanism-only

The registration kernel (`docs/design/registration-kernel.md`) makes the most
consequential promotion in the pipeline — a strategy going live — into ONE more
attestation (R1) over the strongest subject the system seals (the dossier, R2),
at the strongest human tier (R6). It sits on the same Q1 boundary every rigor
primitive holds: core knows which STORE / MECHANISM a prerequisite routes
through and NOTHING about what a field slug, a `subject_id`, or "ready to deploy"
means in any domain. The five agnosticism mechanisms (the plan's "Agnosticism by
FIVE mechanisms") are each an enforcement row; T9
(`tests/contracts/test_registration_boundary.py`) mechanizes them, and the T7
gate fire tests (`tests/ops/decision/test_registration_authorship.py`) hold the
recompute-and-authorship legs the contract suite cannot see.

## Enforcement map

| Rule | Enforced by | Fires when |
|---|---|---|
| No registration WRITE affordance: no mutate/submit/workflow verb named register/registration; append-decision under the gated block is the ONLY write path; `verify-registration` is `verb="query"` with no side effects (R1/R6 lock 1, the no-unlock-verb doctrine) | `tests/contracts/test_registration_boundary.py::test_no_registration_write_affordance_in_the_registry` | a mutating verb named register/registration lands in the primitive registry, or `verify-registration` grows a non-query verb or a side effect |
| `PREREQUISITE_KINDS` is CLOSED and mechanism-only: equality-pinned to the store-noun set (the `DOSSIER_SOURCES` pattern) and equal to the wire `PrerequisiteKind` literal; every kind dispatches to ONE named checker that routes through its ONE existing currency definition; `requires` keys are a closed set per kind and the generic `attestation` kind accepts NONE | `tests/contracts/test_registration_boundary.py::test_prerequisite_kinds_is_the_closed_mechanism_noun_set`, `::test_every_kind_dispatches_to_a_named_checker_that_routes_through`, `::test_requires_keys_are_closed_per_kind_and_attestation_accepts_none` | a kind is added ad hoc (state set, wire literal, or `_DISPATCH` drift out of lockstep), a checker stops naming its route-through symbol (re-inlines a member's currency logic), or a `requires` key set opens for a kind that core cannot check |
| Core ships NO default template and NO registration vocabulary: no template file (a `{fields, prerequisites}` JSON) in package data; no domain word / field-slug / toy-slug string literal in `ops/registration/` or `state/registration.py` (R5, the fabrication + no-invented-defaults classes) | `tests/contracts/test_registration_boundary.py::test_core_ships_no_default_registration_template`, `::test_core_source_inlines_no_registration_field_vocabulary` | a registration template lands under `src/hpc_agent/`, or a core registration module hardcodes a field slug / domain word / harxhar-quant token |
| No domain vocabulary on the wire: every verify-registration schema exposes no `_FORBIDDEN_FIELD_NAMES` member (the `_schema_property_names` recursive walk, mirrored from the dossier suite) | `tests/contracts/test_registration_boundary.py::test_wire_models_expose_no_domain_vocabulary` | a verify-registration wire model grows a meaning-bearing field name (a caller role on the promotion wire) |
| The registration attestor is ALWAYS human: no CODE-writer block→attestor set names a registration block, and the gate binds the literal `'human'` attestor with NO auto-clear / redundant / waived tier (R6 — the one seat where D-attention's answer is "always human-required by construction") | `tests/contracts/test_registration_boundary.py::test_registration_attestor_is_always_human_never_a_code_writer` + the T7 fire tests (`tests/ops/decision/test_registration_authorship.py`) | a mechanical writer's attestor map names a `registration`/`registration-revoke` block, or the gate binds `'code'` / grows a waiver-vocabulary path |
| Toy-domain fixtures only: no harxhar/quant vocabulary in the registration tests/fixtures/examples (R4 mechanism #4, the toy-domain fixture rule mechanized — AST-scanned so a docstring stating the rule is not a violation) | `tests/contracts/test_registration_boundary.py::test_registration_fixtures_carry_no_real_domain_vocabulary` | a real domain word (the harxhar quant model, its vocabulary) lands as fixture data or an identifier in a registration test/fixture/example |
| The registration `reproduction` `requires` floor is the REAL R4 address chain (no longer a not-yet-available stub): newest receipt → `repro.cmd_sha` → `state/fingerprint_store.load_evidence` (admitted, current-identity) → ONE `state/determinism.evidence_meets` call; a short ledger (missing=n=0, too few, wrong scale/cluster) reads STALE with the demand NAMED, and unknown `requires` keys stay loud | `tests/ops/registration/test_prereqs.py::test_reproduction_requires_floor_met`, `::test_reproduction_requires_floor_unmet_n`, `::test_reproduction_requires_floor_unmet_scale`, `::test_reproduction_requires_floor_missing_ledger_is_shortfall`, `::test_reproduction_unknown_requires_key_is_spec_invalid` | the floor fabricates a pass on a missing/short ledger, the checker re-implements the envelope reduction instead of calling `evidence_meets`, or an unknown `requires` key stops being refused |
