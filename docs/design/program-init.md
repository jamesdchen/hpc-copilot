---
status: plan
---
# Program init — materializing the three-tier pack architecture (build spec)

**Status: PLANNED, user-ruled 2026-07-10 (the three-tier distribution ruling
+ the same-session CORRECTIONS in `docs/design/domain-packs.md`'s drift
log — those entries are canon; this spec is the machinery).** Cite
`path::symbol`, never line numbers. Drift log at the foot.

## The ruled architecture (recap, no re-deciding)

* **UPSTREAM**: domain packs ship IN the hpc-agent repo as distributed
  CONTENT (`packs/quant/` migrates here from harxhar-clean). DP1–DP4
  unchanged: bind-as-data, no pack code in core, content-addressed trust.
* **PROGRAM**: at program creation, code CONSUMES the domain skeleton to
  GENERATE the program template (rv_template), pins its sections, stamps
  `derived_from {pack, seam, version, sha}`. The program carries the pinned
  copy; `rv` is a consumed INSTANCE, not a fork.
* **EXPERIMENT**: each experiment's audit source is drafted FROM the program
  template (the existing scaffold/draft path); only the VARIABLE sections
  are modified — the pinned sections must survive verbatim.

## Settled decisions

### P1 — Two seats, not one
* **`program-init`** (new verb): given the domain pack + a program slug,
  generate `packs/<program>/` in the program repo — template instantiated
  from the skeleton, `derived_from` stamped mechanically at generation,
  manifest built (the generic re-seal machinery `state/pack_sweep.py`
  already emits the canonical form), both packs bound, check run (the gate's
  own subprocess seat). One command = a working program layer.
* **experiment scaffold** (existing seat, extended):
  `notebook-scaffold-template` / the draft path source the experiment's
  audit source FROM the program template; the pinned/variable boundary
  travels as markers the domain check verifies (pinned sections verbatim =
  a new `check_quant.py` leg, caller-side as always).

### P2 — The template question dies by construction
With the program layer materialized, the on-ramp NEVER asks for a template
(run-12 finding 1; the interim pack-status/interview composition seat is
superseded) and NEVER asks for experiment_dir when the cwd carries
experiment markers (finding 2 — the invoking repo root composes silently,
disclosed in the record, asked only when no markers exist).

### P3 — Lineage is mechanical at every edge
`derived_from` is stamped BY program-init (code-authored, never asked);
the experiment edge is already sha-bound by the audit. A skeleton upgrade
therefore computes mechanically: which programs are N versions behind,
which contract sections changed (the 12-slug growth is the lifecycle
precedent: skeleton contract-set grows → derivative re-conforms → both
rebuild → receipt re-earned).

### P4 — Resolved sub-rulings (from the architecture correction)
* Program template: COMMITTED in the program repo (generated once, then
  pinned — it is the standards the program's experiments clear under).
* Per-experiment copy: the experiment's audit source (already journaled,
  audited, sha-bound) — no new artifact.
* `.hpc/`: bind state only, gitignored as today.
* Lab-bindings home: DISSOLVED — lab customization IS the program-init act;
  revisit only when one lab runs multiple programs wanting shared defaults.

## Build order
1. Migrate `packs/quant/` into the hpc-agent repo (content move + the
   packaged-data plumbing; harxhar-clean keeps its copy until its programs
   re-init from upstream — no flag-day).
2. `program-init` verb (registry +1; wire model; regen).
3. Pinned-section verbatim leg in the domain check (caller-side).
4. Scaffold-from-program-template extension + retire the interim
   template-composition seat.
5. Findings 1+2 code seats close permanently.

## Test plan (sketch)
program-init generates a bindable, check-passing program layer end-to-end
in a fixture repo; `derived_from` stamped and content-correct; pinned
section edited in an experiment source → check refuses; skeleton bumped →
program reads behind with the changed-contract diff; on-ramp asks neither
template nor experiment_dir in a marked repo.

## Drift log

*(empty — no implementation yet)*
