# Handoff-package template (`_TEMPLATE-handoff`)

The shape three successful Fable swarm programs converged on, distilled so the
fourth doesn't re-derive it. A handoff package is two artifacts that let an
architect settle every design call ONCE and then hand file-disjoint units to a
swarm of implementation agents who never coordinate at runtime:

- **`ARCHITECT-MEMO.template.md`** — the human-readable pre-settling design: the
  settled-calls table (every premortem finding → binding resolution), the wave
  plan with dependency edges, the ruling-needed docket the maintainer answers,
  the per-unit pre-push battery, the enforcement rows owed, and the residual-risk
  register. If the memo contradicts a unit's brief, THE MEMO WINS.
- **`unit-specs.template.json`** — the machine-readable twin: an array of
  file-disjoint `units`, each with an exclusive `files` claim, `forbidden_files`,
  `design_constraints`, `acceptance_tests`, `test_batteries_to_run`,
  `regen_required`, and `merge_risk`. This is what `scripts/check_handoff_disjointness.py`
  validates.

## Why file-disjointness is the load-bearing invariant

Parallel agents are safe only if no two touch the same seam. Three failure modes
this model exists to prevent (all observed on real runs):

1. **Unchecked convergence** — two agents' `files` claims overlap and neither
   knew (the calibration/SGE collision). Caught by the same-wave overlap check.
2. **Claim drift** — a unit's `files` list stops matching what it touched; a
   typo'd path silently claims nothing. Caught by the path-reality check.
3. **In-flight overlap at dispatch** — a dirty working tree at dispatch time
   already owns a file a unit claims (the wave-0 partial-work reset). Caught by
   `--against-worktree`.

Run the checker before dispatching a wave:

```
.venv/Scripts/python.exe scripts/check_handoff_disjointness.py docs/plans/<program>/unit-specs.json --against-worktree
```

## Precedents (read these — the template is distilled from them)

- `docs/plans/handoff-packages-2026-07-12/` — origin of the ARCHITECT-MEMO form
  (memo + HANDOFF.md; predates the JSON twin).
- `docs/plans/latency-elimination-2026-07-16/` — memo + first `unit-specs.json`
  (integer waves; adds `claims` + `enforcement_map_rows`).
- `docs/plans/daemon-engineering-2026-07-16/` — memo + `unit-specs.json` with
  string waves (`DW0`…`DW3-rungN`) and `merge_risk`; the schema this template pins.
