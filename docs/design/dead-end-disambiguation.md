---
status: v1-built
# Mechanical dead-end disambiguation for `extract-recipe` (G2)

**Status: v1 BUILT (2026-07-17).** The mechanical dead-end signal is anchored on
the cited artifact's `contributing_run_ids` for the two seeds where a single
cited artifact is well-defined (`--run-id`, `--aggregate-path`): a campaign
sibling absent from that set is a mechanically-reasoned DEAD-END, a supersession
ancestor is SUPERSEDED, a canary-family member is CANARY, and a proven
contributor is KEPT even without a harvest receipt (the run-13 graft class). The
bare `--campaign-id` seed has no single cited artifact and stays on the
harvest-receipt PROXY, with the proxy-ness disclosed in the exclusion reason and
the ambiguity recorded below as a **ruling recommendation** (the
superseded-intermediate-table case). Built in `ops/extract_recipe.py`
(`_resolve_seed` / `_candidates_from_provenance` / `_apply_exclusions`);
`tests/ops/test_extract_recipe.py` carries the red-then-green pins.

Cite `path::symbol`, never line numbers. Where this doc and the code disagree,
the code and its enforcement-mapped tests win.

Origin: the clean-reproduction-extraction program
(`docs/plans/clean-reproduction-extraction-2026-07-17.md`), gap **G2** —
"abandoned-but-not-superseded is indistinguishable from contributed." That
program shipped `extract-recipe` (the artifact → minimal-run-set → recipe walk)
and Task 1 (the reduce-time `contributing_run_ids` provenance,
`ops/aggregate_flow::_persist_local_aggregate`). This memo closes the last leg
G2 named: making the DEAD-END exclusion *mechanical* rather than a proxy.

## The gap, stated as the product move

A scientist finishes a messy multi-run campaign — dead ends, retargets,
parameter drift — and asks `extract-recipe` for the clean minimal recipe of a
citable table. The recipe must exclude the runs that did NOT feed that table.
Two exclusion classes were already first-class:

* **CANARY** — a `-canary` / `-canary2` family sibling (`canary_parent_of`).
* **SUPERSEDED** — an older member of another run's supersession chain
  (`lineage_chain`; run-level `RunRecord.supersedes`).

The third — **DEAD-END** — was a *proxy*: `harvest_receipt_exists`. A run with no
harvest receipt was called a dead end. That proxy has a hole: a run that ran to
completion and WAS harvested (has a receipt) but was a **dead end** — the human
tried a parameter setting, saw it was wrong, and moved on without ever feeding it
into the citable table — reads as a contributor and **pollutes the minimal
recipe**. "Which runs are THE result" still leaned on a human knowing which runs
were dead ends. That is exactly the reproducibility-crisis gap the program exists
to close.

## The mechanical signal that closes it

Task 1 (BR-9 / `f6d9959e`) landed reduce-time provenance:
`ops/aggregate_flow::_reduce_input_provenance` records, at reduce time, the
`contributing_run_ids` that ACTUALLY fed a table — read from the reduce's own
on-disk inputs (the `_combiner/wave_*.json` partial `run_id` membership + the
`_per_task_results/.hpc_cmd_sha` set it consumed). Persisted into
`_aggregated/<run_id>/metrics_aggregate.json`'s `provenance` block.

The key insight, confirmed against the code:

> **"Which runs actually fed the citable table" is now MECHANICAL.** A run absent
> from a table's `contributing_run_ids` did NOT feed that table, regardless of its
> lifecycle state (complete / failed / abandoned) or whether it has a harvest
> receipt. So a DEAD-END is simply **"a campaign run NOT in the cited table's
> `contributing_run_ids`, and not a supersession ancestor of a contributor"** —
> not a harvest-receipt guess.

This also fixes a *false-exclusion* the proxy caused: the run-13 graft class. A
repair that re-ran arms under a NEW run id into another run's tree appears in the
table's `contributing_run_ids` (its wave partial's `run_id`) but was never
independently harvested, so it has no harvest receipt of its own. The old proxy
excluded it as "dead-end" even though it PROVABLY fed the table. The mechanical
rule keeps it: membership in `contributing_run_ids` outranks the receipt proxy.

## The anchor: a minimal recipe is relative to ONE cited artifact

The resolution turns on a single observation. **The minimal recipe is only
well-defined relative to a specific cited artifact.** `contributing_run_ids` is a
property of ONE `metrics_aggregate.json`. Given a specific cited table T:

* its `contributing_run_ids` IS the mechanical minimal set (the runs whose pieces
  are in T);
* every OTHER run in T's campaign either (a) is a supersession ancestor of a
  contributor — SUPERSEDED, collapse to the newest; or (b) is not in
  `contributing_run_ids` and not such an ancestor — a mechanical DEAD-END *with
  respect to T*.

There is no third category. A run that "fed a superseded *intermediate* table but
not T" is, relative to T, simply **not in T's `contributing_run_ids`** → a
dead-end w.r.t. T. That is the correct honest answer: the recipe to re-derive T
from scratch does not need it. The apparent ambiguity the program flagged
("dead-end or provenance ancestor?") dissolves once the question is anchored to a
specific cited table — *because the recipe reproduces T, not the history of
superseded intermediates that led to it*.

### Seeds where the anchor holds → BUILT (mechanical)

`--run-id` (with a persisted table) and `--aggregate-path` (a
`metrics_aggregate.json`) each name ONE cited artifact. For these:

* **contributing set** = the table's `contributing_run_ids`.
* **candidate universe** = the contributing set ∪ the owner run's CAMPAIGN
  siblings ∪ the owner run. Broadening from "just the contributing set" to the
  whole campaign is what makes dead-end disambiguation *visible*: the human sees
  every campaign sibling classified, with the mechanical reason each was excluded.
  The kept set is unchanged (= contributing heads), so the `recipe_signature` is
  unaffected — broadening only adds `excluded` disclosures.

Exclusion order (a run gets exactly ONE reason, first match wins):

1. `canary_parent_of(r) is not None` → **canary**.
2. `r ∈ ancestors_of_contributors` (r is an older member of SOME contributor's
   `lineage_chain`) → **superseded**. This collapses supersession lineages toward
   the contributing head and also catches an ancestor that did not itself feed T
   but whose descendant did.
3. `r ∈ contributing` → **kept** (a proven contributor — receipt-independent).
4. else → **dead-end** `(not in the cited table's contributing_run_ids)`.

The minimal set = contributors that are not superseded by another contributor =
the contributing heads. Provably the contributing set; the human sees WHY each
excluded run was excluded.

The opaque pack `*.csv` seed (R2 — content never parsed) keeps its owner run as
the sole contributor (`contributing = {owner}`, universe `= {owner}`); its content
is never read, so it grows no campaign universe and classifies no siblings.

### The seed where the anchor does NOT hold → PROXY + ruling recommendation

The bare `--campaign-id` seed names a campaign, not a single cited artifact. A
campaign produces many per-run tables over its iterations, and **nothing
mechanically designates one as THE citable/final table**: run-level supersession
(`RunRecord.supersedes`) is first-class, but *table-level* supersession — "table
T2 supersedes intermediate T1" — is NOT recorded anywhere, and run lifecycle has
no "this result was abandoned" verdict (the terminal states are
complete / failed / abandoned, where "abandoned" means *the reporter could not
verify it finished*, not *the human decided this was a dead end* — see
`docs/internals/principles/lifecycle-verdicts.md`).

So for a bare campaign seed there is no mechanical `contributing_run_ids` to
anchor on. Two honest resolutions, each with a doctrine cost:

* **(a) union** — contributing set = the union of every campaign table's
  `contributing_run_ids`. Mechanical, but **over-reports**: a run that fed an
  intermediate table later abandoned in favour of a re-run IS in that
  intermediate's `contributing_run_ids`, so the union keeps it — the very
  "superseded-intermediate" pollution G2 names, which no mechanical signal can
  strip because table-level abandonment is not first-class.
* **(b) require the anchor** — a bare campaign seed cannot answer the
  minimal-recipe question; the human must point at the specific cited table
  (`--aggregate-path` / `--run-id`), whose `contributing_run_ids` IS the answer.

**Recommendation (v1): (b), realised as disclosure not refusal.** The bare
campaign seed keeps the harvest-receipt PROXY it has today (behaviour unchanged),
but the proxy-ness is now stated IN the exclusion reason —
`dead-end (harvest-receipt proxy — seed --aggregate-path/--run-id for the
mechanical contributing set)` — so the human is never misled into reading a
campaign-seed dead-end as the mechanical answer, and is pointed at the seed that
gives it. This honours the amplification doctrine (disclose, never gate) and the
cite-check precedent (ship the mechanical core; gate the ambiguous leg behind a
maintainer ruling).

**The open ruling (the "superseded-intermediate-ancestor case"):** should a bare
`--campaign-id` seed adopt option (a) union semantics (mechanical but
over-reporting) as an additive future mode, or stay on (b)? (a) is only sound once
table-level supersession/abandonment becomes first-class — e.g. a
`settle-aggregate` record (clean-repro proposal #2) or a "final table" pointer per
campaign. Until then, (b) is the honest posture and (a) would silently pollute.
This is a maintainer doctrine call, not an agent guess — recorded here per the
BR-14 precedent.

## What is BUILT

`ops/extract_recipe.py`:

* `_candidates_from_provenance` returns the contributing set as
  `set[str] | None` — `None` (not an empty set) when the table predates Task 1
  (no `contributing_run_ids`), so an old-shape table falls to the proxy with the
  `table-run-set-link-absent` gap already disclosed, never mislabelling siblings.
* `_resolve_seed` returns the contributing set alongside the (now campaign-broad,
  for anchored seeds) candidate universe.
* `_apply_exclusions` takes `contributing: set[str] | None` and runs the
  mechanical branch when it is a set, the proxy branch (with the disclosed reason)
  when it is `None`.

Boundary posture is unchanged: the recipe is still IDENTITY + ORDERING + COUNTING
over opaque records — the new reason strings name no metric, and the mechanical
signal is set membership, not a value.

## Drift log

- **2026-07-17 — created + v1 BUILT.** Anchored the DEAD-END exclusion on the
  cited artifact's `contributing_run_ids` for the `--run-id` / `--aggregate-path`
  seeds (mechanical), fixed the graft false-exclusion (membership outranks the
  harvest-receipt proxy), broadened the anchored-seed candidate universe to the
  owner run's campaign so dead-end disambiguation is visible, and kept the bare
  `--campaign-id` seed on the harvest proxy with the proxy-ness disclosed in the
  reason string. Recorded the superseded-intermediate-table ruling recommendation
  (require the anchor, option (b)) for a bare campaign seed. No new lint / no
  MCP-curation change / no schema break (the `excluded[].reason` free string
  already carries the disclosure; the wire shape is untouched).
</content>
