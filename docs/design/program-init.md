---
status: landed
---
# Program init — materializing the three-tier pack architecture (build spec)

**Status: P1a LANDED (2026-07-15, fable-sweep devx+pack-seams wave 1). The
`program-init` verb + the `derived_from` lineage field + the pack-status lineage
echo ship; the pinned-section verbatim check leg (build-order item 3) and the
scaffold-from-program-template on-ramp integration (item 4) remain PLANNED.**
Original ruling: user-ruled 2026-07-10 (the three-tier distribution ruling + the
same-session CORRECTIONS in `docs/design/domain-packs.md`'s drift log — those
entries are canon; this spec is the machinery). Cite `path::symbol`, never line
numbers. Drift log at the foot.

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

## Migration — existing packs acquire the stamp without hand-editing

The lineage stamp is applied **by re-running the init/reseal path over the real
lineage, never by hand-editing** a manifest (a hand-edit self-revokes: the recipe
is truth, `state/pack_sweep.py::_semantic` includes `derived_from`, so a reseal
restores the recipe's value).

* **Domain packs (`quant`, upstream + lab copies): NO stamp.** A domain pack is a
  lineage ROOT; `derived_from` is absent by design — nothing to migrate.
* **Program packs (`rv` in harxhar-clean): `mode: adopt`.** In the lab repo, once a
  stamped-aware wheel is installed, run:

  ```
  hpc-agent program-init --experiment-dir . \
    --spec <file with {"program":"rv","domain_manifest":"packs/quant/manifest.json","mode":"adopt"}>
  ```

  Adopt computes `derived_from` from the on-disk co-bound domain seam file (the
  REAL lineage — the lab quant skeleton, whose sha differs from the
  portability-neutralized upstream copy; the edge is identified by `{pack, seam}`
  name-match, so the sha divergence reads "lineage behind" but NEVER severs the
  edge), rewrites `packs/rv/sweep.json` with the `derived_from` block (unknown lab
  keys preserved), reseals `manifest.json` canonically, and rebinds ONLY `rv` (the
  domain root is untouched). The signed `rv_audit.py` stays **byte-identical** — no
  sign-off is invalidated; the rebind stales the `quant-audit` receipt, re-earned
  by one check re-run. `harxhar-clean/packs/rv/build_rv_pack.py` is superseded by
  recipe + reseal (the lab repo is not touched by this unit).

### PRECONDITION — every gate-running env must be stamp-aware FIRST

**Before running `program-init adopt` over a lab repo, refresh EVERY environment
that runs the pack gate over that repo (local shell + demo + hook envs) to a
stamped-aware wheel.** An OLD wheel's gate auto-remedy (`pack-refresh`) reseals a
manifest via the old `fresh_manifest_dict`, which emits a FIXED key set and does
NOT carry `derived_from` — so on the first genuine content drift after migration it
STRIPS the stamp from the manifest (the recipe keeps it), and the next new-wheel
tick then reads stale (the new `_semantic` includes `derived_from`), forcing an
extra reseal+rebind and one more round of receipt revocation mid-run. Refreshing
all gate-running envs before adopt closes that window. (The coupled-deploy note:
once a wheel carrying the P1b compose rewrite reaches the lab env, multi-candidate
compose refuses until adopt has stamped `rv` — so wheel-install → `program-init
adopt` in the SAME human session, after run-13 harvest.)

## Drift log

* **2026-07-15 — P1a landed (fable-sweep wave 1).** `state/pack.py` grew
  `DerivedFrom {pack, seam, version, sha}` (frozen) + `PackManifest.derived_from:
  DerivedFrom | None` (optional, back-compat: a legacy manifest parses to `None`);
  the shared `parse_derived_from` validates the shape (slug pack, seam ∈
  `SEAM_NAMES`, non-empty version, 64-hex sha) loudly. `state/pack_sweep.py` grew
  `SweepRecipe.derived_from`, `fresh_manifest_dict` emits the manifest block iff
  the recipe carries it, `_semantic` includes it (absent-in-both projects
  identically — the live-pack no-spurious-staleness guard, regression-tested), and
  `stamp_recipe_derived_from` does the adopt raw read-modify-write. `program-init`
  (`ops/pack/init_op.py` + `_wire/actions/program_init.py`) ships create + adopt
  modes modeled on `pack-refresh`. `pack-status` grew an optional `derived_from`
  lineage/freshness echo (`current`/`behind`/`source-not-bound`, DC10). DC1: the
  recorded sha is the **template-granularity seam FILE sha**, not the source
  manifest sha (a manifest sha would churn on swept-doc edits). DC2: the derivation
  EDGE is `{pack name, seam}`, never sha equality — the lab-vs-upstream skeleton sha
  divergence reports "behind" but keeps the edge. **`"derived_from": null` appears
  in pack-status output for legacy packs** (`cli/_dispatch.py` serializes without
  `exclude_none`) — ACCEPTED as an additive change (the global dispatch serializer
  is untouched; pinned by `tests/ops/pack/test_status_op.py::
  test_legacy_pack_derived_from_serializes_as_null`). Build-order items 3 (pinned
  verbatim check) + 4 (scaffold-from-program-template + findings 1/2 on-ramp) stay
  PLANNED.
