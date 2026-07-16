---
slug: determinism-fingerprint
order: 4
title: "The determinism fingerprint: measure, don't ask"
scope: "A measured, confidence-labeled run-to-run spread — core measures and compares, never names a metric or invents a tolerance."
---

# The determinism fingerprint: measure, don't ask

The fingerprint is a MEASURED, accumulating, confidence-labeled record of an
experiment's observed run-to-run spread — core measures, classifies by STRUCTURE,
and compares; it never names a metric, never privileges one, and never INVENTS a
tolerance. Every number in an envelope is an OBSERVATION; the envelope is the
observed range plus its labeled n, never a fitted distribution. The full design
center is `docs/design/determinism-fingerprint.md` (settled 2026-07-07); these
rows hold the boundary its enforcement-row table names.

## Enforcement map

| Rule | Enforced by | Fires when |
|---|---|---|
| No invented tolerance: absent a measured envelope and a caller override, every comparison is EXACT — core carries no default float tolerance (per-metric or global), and no numeric tolerance literal lives in the classifier | `tests/contracts/test_determinism_boundary.py::test_last_ulp_floats_empty_ledger_not_match` (two floats one ULP apart, empty ledger, no override → not `match`), `::test_no_numeric_tolerance_literal_in_classifier` (AST pin over `state/determinism._classify_key`; only the literal `0` identity-check is tolerated) | a "reasonable default" epsilon lands anywhere in the classifier, or a 1-ulp deviation auto-clears against an empty ledger |
| The envelope is order statistics ONLY — observed min/max + labeled n, never a fitted distribution at any n | `tests/contracts/test_determinism_boundary.py::test_envelope_is_observed_range_never_fitted` (the reduced range equals the exact observed extrema; `rel_spread` is derived from the range), `::test_no_statistics_or_variance_in_envelope_path` (no `statistics`/`numpy`/`stdev`/`variance`/`mean(` in the kernel source) | someone "improves" the envelope with mean±kσ / an interval estimate, or imports a statistics primitive into the reduction |
| A thin envelope (n<3 or scale/cluster novelty) never auto-clears and never auto-mismatches — a deviation in EITHER direction routes to `needs_verdict` | `tests/contracts/test_determinism_boundary.py::test_thin_envelope_inside_routes_to_needs_verdict`, `::test_thin_envelope_outside_routes_to_needs_verdict_not_mismatch` (outside a THIN envelope is a human question, never a machine `mismatch`) | the well-evidenced bar is weakened, or the thin branch collapses into an auto-verdict in either direction |
| ONE admission rule, SCOPED TO THE FINGERPRINT ENVELOPE: a sample joins iff its comparison got a passing verdict (code's `auto_cleared`, or a gated human acceptance); an unadmitted sample moves nothing and satisfies no demand; the double-canary prior is admitted by construction | `tests/contracts/test_determinism_boundary.py::test_unadmitted_sample_does_not_move_envelope`, `::test_admitted_satisfies_demand_unadmitted_never` (an unadmitted sample never satisfies an `evidence_meets` demand), `::test_double_canary_prior_admitted_by_construction` | a second admission predicate appears, an unadmitted sample moves an envelope or satisfies a demand, or the double-canary prior stops admitting |
| A CODE attestation never satisfies a human tier: a `mismatch` sample (code observing nondeterminism) is admitted ONLY via the GATED human `reproduction-verdict` acceptance naming its `content_sha` — code cannot stand in for the human tier | `tests/contracts/test_determinism_boundary.py::test_mismatch_admits_only_via_human_record_code_cannot_substitute` | a mismatch/needs_verdict sample is admitted without the human acceptance record (laundering through accumulation) |
| No-silent-caps on partiality: every partial comparison's sample AND receipt carry `partial: true`, the exact task indices, and the uncompared accounting — a subset that prints like a full one is refused at append | `tests/contracts/test_determinism_boundary.py::test_partial_sample_without_indices_refused` (the sample shape), `::test_partial_receipt_missing_uncompared_accounting_refused` (the receipt shape via `verify_reproduction._validate_receipt_partiality`) | a partial sample or receipt omits its indices / uncompared counts |
| The double canary's rows never contaminate aggregates: the `-canary` suffix-FAMILY exclusion (`-canary2` included) is defined ONCE in `ops/monitor/reconcile._sibling_run_ids`, and the aggregate reduce routes through it | `tests/contracts/test_determinism_boundary.py::test_canary2_exclusion_routes_through_the_one_sibling_definition` (route-through pin + the ONE definition covers the family); the planted-`-canary2`-row fire test lives in T4's suite (`tests/ops/aggregate/test_flow_ssh_default_reducer.py::test_ssh_fallback_excludes_canary2_sibling_results`), not duplicated | the aggregate reduce re-inlines a literal `-canary` filter, or a second suffix definition appears |
| No verdict verb: the needs_verdict resolution is `append-decision` (block `reproduction-verdict`) or nothing — no registry primitive resolves/mutates a reproduction verdict | `tests/contracts/test_determinism_boundary.py::test_no_verdict_verb_in_registry` | a `resolve-reproduction` / reproduction-verdict-writing verb leaks into the registry |
| Precedence per key: caller override (labeled `caller_override`, disclosed) > well-evidenced measured envelope > exact — an owned override outranks the measurement and is never silent, the measured envelope outranks everything unowned | `tests/contracts/test_determinism_boundary.py::test_precedence_caller_over_measured_over_exact` (all three tiers on one 0.05 deviation: exact→mismatch, measured-inside→match, caller override→wins and is labeled) | a caller tolerance wins UNDISCLOSED/unlabeled, or an unowned default outranks a measurement |
