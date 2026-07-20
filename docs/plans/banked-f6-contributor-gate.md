# Ruling memo — BANKED "F6 contributor-status gate"

Status: ruling-ready, read-only investigation 2026-07-20. Cite format `path::symbol` (repo convention). **[CITED]** vs **[RECONSTRUCTED]** marked per claim.

## Landed (current mechanics)

**Authorship / who may utter a binding 'y'.** `append-decision` is the ONLY human-decision write path; `AppendDecisionInput` carries **no actor field** — the session actor is resolved server-side from `HPC_ACTOR` (`ops/decision/journal/_shared.py::_session_actor`; `docs/design/multi-human.md` MH1–MH4, IMPLEMENTED 2026-07-08). The tiered bar refuses bare acks (`_is_bare_ack`), requires slug-naming, and prefers harness-captured utterance evidence over agent-relayed responses, temporally bound to the artifact (`ops/decision/journal/signoff.py::_signoff_fresh_human_texts`); absent a log it falls to the friction tier. Under >1 declared actors (`interview.json` `actors` block): per-actor utterance files, `attestor_id` stamped on decision records, reviewer≠author (`signoff.py::_assert_signoff_reviewer_not_author`), resolver≠challenger, and opaque policy membership (`_assert_actor_policy`, MH8 — lists/mappings core compares, never role vocabulary). Zero/one actor → byte-identical to today. Run-intent authorship rides `produced_by` on `InterviewSpec` (`_wire/actions/interview.py::_Provenance`, `{kind: human, operator: …}`); the LLM never authors run identity.

**Contributor status on cross-run tables.** Reduce-time provenance records which runs fed a table: `ops/aggregate_flow.py::_persist_local_aggregate` writes `contributing_run_ids` into `metrics_aggregate.json` (clean-repro Task 1, 2026-07-17). `ops/extract_recipe.py::_apply_exclusions` classifies every campaign sibling — canary → superseded → **kept (contributing)** → **dead-end** — mechanical when the seed anchors one cited artifact (`--run-id`/`--aggregate-path`), a disclosed harvest-receipt **proxy** for a bare `--campaign-id` (`_DEADEND_PROXY`). Doctrine throughout: **disclose, never gate** (`docs/design/dead-end-disambiguation.md`; `docs/plans/clean-reproduction-extraction-2026-07-17.md` cite-check drift log: "never gate, not MCP-curated").

## What F6 names

**[CITED]** Only two in-repo-adjacent mentions exist, both session memory, no design doc:
- run-15 open docket: "F6 cross-run table contributor-status gate; completeness verb+triple conformance promotion (both deliberately deferred)" (`memory/project_run15_session_20260718.md` §OPEN DOCKET).
- harvest memory: "BANKED needs-user-ruling: … F6 contributor-status gate (**deferred, no design**)" (`memory/project_session_20260719_harvest.md`, 12th delta).

**[CITED]** The repo's other F6's are unrelated (latency-elimination canary double-pull; daemon premortem; `export_dossier.py` renders; fable-sweep F06 `combined_waves`).

**[RECONSTRUCTED]** F6 = the gate question left by `docs/design/dead-end-disambiguation.md` §"The open ruling" plus clean-repro proposal #2 (`settle-aggregate`): contributor status is currently *purely mechanical or proxy-disclosed*; nothing lets a human *declare* table-level finality or contributor membership, and nothing *gates* on it. Two readings, both defensible: (R-a) the bare-campaign union-vs-anchor ruling — campaign seeds get a mechanical answer only once a human-settled "final table" pointer / `settle-aggregate` record exists; (R-b) a human-declared contributor-status override of the mechanical `contributing_run_ids` (mark a harvested run a dead end, or a missing run a contributor), which is then an authorship question: who may declare it, at what evidence tier, and whether it binds `export-bundle`/dossier seals.

## Open

1. What does the gate bind — recipe membership, `export-bundle` seal, attestation export, or nothing (disclose-only forever)?
2. Does table-level finality become first-class (`settle-aggregate`, proposal #2, size M, its own owed ruling), or is per-run status override the unit?
3. Who declares (MH8 policy key, e.g. `"contributor-status"` block, opaque membership), and at what tier (utterance-bound like sign-off, or directed-settle style like `settle-run`)?
4. Conservative default is fixed by doctrine: disclose-never-gate; any gate must be opt-in and byte-identical-when-absent.

## Ruling questions

1. **Existence: build a contributor-status gate, or keep disclose-only?** (a) Gate — human declarations bind recipe/bundle membership. (b) **Keep disclose-only; revisit when a second lab user exists (recommended).** (c) Disclosure-plus: surface a non-binding human annotation in `excluded[]`. Why: the mechanical set already answers anchored seeds; the only live pain is bare-campaign seeds, which (c) treats without a gate's doctrine cost.
2. **If gated, what unit?** (a) **Table-level finality record — `settle-aggregate` (recommended, already designed as clean-repro #2).** (b) Per-run status overrides. (c) Both. Why: (a) makes bare-campaign union semantics sound and dissolves the dead-end ruling in one object; (b) invites per-run opinion wars the journal can't arbitrate.
3. **Authorship seat for the declaration?** (a) **Directed settle-style `append-decision` block, `produced_by`-stamped, MH8 policy-consulted (recommended).** (b) New mutate verb. (c) Sidecar field. Why: reuses the ONE authorship substrate; a new verb or sidecar field forks the "who may bind" question.
4. **Bare-campaign semantics today, pre-gate?** (a) Union (over-reports). (b) **Require-anchor with disclosure — keep status quo (recommended).** Why: (a) silently pollutes the minimal recipe until table-level finality exists; the doc's own recommendation stands.

## Build scope if ruled (Q1=a or Q2 lands)

- NEW `docs/design/contributor-status.md` (this memo → settled design) — S.
- NEW `ops/settle_aggregate.py` + `_wire` action + schema (clean-repro #2 spec exists) — M.
- `ops/extract_recipe.py::_resolve_seed`/`_apply_exclusions`: consult a settled final-table pointer for campaign seeds (proxy → mechanical upgrade) — S.
- `ops/decision/journal/`: new block gate reusing `_assert_actor_policy` + `_session_actor` — S.
- Tests: red-then-green in `tests/ops/test_extract_recipe.py` + a gate fire-test pair (MH10 pattern); byte-identity battery under zero declared actors; regen (new verb ⇒ all six scripts).
- Total: **M** (≈ one worktree lane + independent verifier).
