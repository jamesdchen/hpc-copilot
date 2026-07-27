# Context-footprint reduction — five levers (plan)

**Status: PLAN (user-ordered 2026-07-27; follows the elicitation retirement,
PR #24).** Five coordinated reductions of what hpc-copilot puts into an agent
session's context window. The governing rule, user-ruled the same day:
**defer only what a BRANCH needs; whatever the mainline always needs stays
inline.** Applied uniformly: schemas an agent needs to CALL a tool stay in
`tools/list`, prose it needs only when confused moves behind `describe`;
refusal text a human must read to act stays inline, forensic detail moves to
disk; skill guidance a conditional path needs moves to a per-branch reference
file, the mainline skeleton stays in `SKILL.md`. Cite `path::symbol`, never
line numbers. Record drift in a log appended here.

## Shared boundary-drift flags (all five tasks)

1. **Never move a read-and-sign surface out of line.** The sign-off/consent
   refusals, coverage briefs, and the inline `full: true` audit relay are the
   human's read surface (elicitation-retirement ruling) — no offload, no
   truncation beyond the already-disclosed elision.
2. **Disclosed, never silent.** Every offload/trim carries an in-band pointer
   (`describe <verb>`, a brief path, a reference-file name, a count). A
   silent drop is the misleading-summary class.
3. **Additive on the wire.** No breaking envelope/schema change: trims are
   presentation-side (`tools/list` projection, markdown) or additive spec
   fields; `extra="forbid"` result models change only by REMOVING a field
   after the F4 consumer audit proves nothing programmatic reads it (the B1
   `sections[].diff` precedent).
4. **Fail-open on the error path.** F2's brief write may never turn a refusal
   into a crash or scaffold a namespace (the `state/utterances.py`
   no-scaffold rule).

---

## F1 — `tools/list` schema trim (mcp_server)

**Decision.** `mcp_server._tool_input_schema` embeds the packaged
`schemas/<basename>.input.json` verbatim under the `spec` property; the
nested `description` strings are documentation prose and dominate the bytes
of every `tools/list` — a fixed per-session cost. The projection served to
`tools/list` strips `description` keys from the embedded spec schema BELOW
its top level (structural validation — `type`, `required`, `enum`,
`properties` names, `additionalProperties` — is untouched); the spec
property's own top-level description becomes a one-line pointer:
`"JSON spec object. Full field documentation: describe <verb> (or the
tiered catalog's describe tool)."` The tool's own `description` (from
`PrimitiveMeta.description`) stays. `describe` and the CLI keep serving the
full contract — the branchpoint rule: calling needs structure; reading docs
is the branch.

**Mechanics.**
- New pure helper `mcp_server._strip_schema_descriptions(schema) -> dict`
  (recursive, returns a copy; never mutates the cached packaged schema).
- Applied in `_tool_input_schema` to the `spec` sub-schema only — the
  argv-level properties' one-line `arg.help` descriptions stay (they are
  already short and are the only doc for non-spec args).
- No flag. The full/tiered/curated catalogs all serve the same trimmed
  projection; `describe` is the documented full-contract surface in all
  three (curated exposes `describe`? It does NOT today — verify; if absent,
  the pointer text for curated names the CLI `hpc-agent describe <verb>`
  instead. Resolve at build time, one conditional string.)

**Tests.** `tests/test_mcp_server.py`: (a) a curated/full `tools/list` entry's
embedded spec schema contains no nested `description` keys but keeps
`required` + `enum`; (b) the pointer line is present; (c) the packaged schema
file on disk / `describe` output still carries the full descriptions
(anti-overreach pin); (d) byte-size regression guard is NOT pinned (sizes
drift with the registry — assert structure, not bytes).

**Regen.** None (the packaged schemas are untouched; the trim is a serve-time
projection).

## F2 — oversized structural-refusal offload (cli/_helpers)

**Decision.** In `cli/_helpers._err`, when the composed `message` +
`remediation` exceeds `_REFUSAL_INLINE_MAX_BYTES` (proposed 2000 — generous;
today's typical refusals are far under it), AND the refusal does NOT carry
the `authorship_evidence` key in `failure_features` (the human's
read-and-sign refusals stay inline whole, flag 1), AND a `.hpc` directory
already exists under the invocation's experiment dir (resolved from the
`--experiment-dir` the dispatch already parsed, falling back to cwd;
no-scaffold: absent `.hpc` ⇒ no offload), THEN: write the FULL
message+remediation to `.hpc/briefs/refusal-<sha12>.txt` (sha12 of the full
text — content-addressed, idempotent) and serve the envelope with the
message truncated at a line boundary plus the disclosure
`… full detail: .hpc/briefs/refusal-<sha12>.txt`. Any write error ⇒ inline
unchanged (fail-open).

**Mechanics.** One helper `cli/_helpers._offload_oversized_refusal(payload,
experiment_dir) -> payload`; called from `_err` (which every error path
funnels through). `_err` needs the experiment dir threaded — `_err_from_hpc`
callers in `cli/_dispatch.py` know it; thread as an optional kwarg defaulting
to `None` (None ⇒ cwd probe). The envelope keys are unchanged (the pointer
rides inside `message`) — no schema/wire change, no new error_code.

**Tests.** New `tests/cli/test_refusal_offload.py`: oversize structural
refusal → truncated + pointer + file content equals full text; oversize
authorship-marked refusal → inline whole; no `.hpc` → inline whole (and no
directory created); write failure (read-only briefs dir) → inline whole;
under-threshold → byte-identical envelope (the regression pin).

**Regen.** None.

## F3 — skill branchpoint references

**Decision.** A skill's mainline (the numbered loop every invocation walks)
stays in `SKILL.md`. A block is split into
`slash_commands/skills/<skill>/references/<branch>.md` ONLY when it is
branch-gated: read at most on a conditional path. `SKILL.md` keeps a
one-line trigger per branch: `"When <condition>: read references/<file>
(this skill's directory) and follow it."` Confirmed branchpoints from the
audit:

- **hpc-notebook-audit** — (a) the step-5b **nudge/re-draft arm** (only when
  the human nudges instead of signing): the re-draft + hash-move + re-enter
  loop guidance; (b) the step-1 **interview handoff on-ramp** (only when the
  audit will hand off to a submit interview): the `notebook-record-config`
  goal/task_axes journaling block. The mainline sign-off rendezvous, the
  relay ladder, and every contract-pinned sentence
  (`tests/contracts/test_notebook_audit_skill_guidance.py`) STAY in
  SKILL.md.
- **hpc-submit** — the **recovery arms** (`revise-resolved` /
  `retarget-run` — read only when a submit fails or the human revises).
- Other skills: audited by the same criterion; a skill with no conditional
  block splits nothing (hpc-claim-check at 64 lines splits nothing).

**Mechanics.**
- `pyproject.toml` `[tool.setuptools.package-data]` gains
  `"slash_commands/skills/*/references/*.md"` (verify the existing entry
  covering `SKILL.md` and extend beside it).
- `scripts/lint_skills.py` + `scripts/lint_skill_command_sync.py`: verify
  they tolerate the references dir (they scan `SKILL.md`); add a lint rule:
  every `references/<file>` named in a SKILL body exists, and every file in
  `references/` is named by its SKILL body (no orphans) — the guard-can-fire
  posture, with a fire-path test in `tests/scripts/`.
- Contract-test pins keep matching because pinned sentences never move.

**Tests.** The new lint's fire-path test (synthetic dangling/orphan
reference); `tests/contracts/test_notebook_audit_skill_guidance.py` unchanged
and green (pins stayed in SKILL.md).

**Regen.** `lint_skill_command_sync` (no regen artifact); wheel build picks
up package-data.

## F4 — result double-carriage audit

**Decision.** For every Result model carrying a verbatim-relay `markdown` /
`render` field (survey: `attention_queue`, `challenge_status`, `cite_check`,
`conformance_status`, `dir_digest`, `evidence.brief`/`evidence.period`,
`extract_recipe`, `notebook_audit_view` (done — B1), `notebook_draft_context`,
`program_verify`, `run_story`, `trace_diff`, `trace_render`,
`worker_log_digest`, `notebook_dry_run`), classify each structured sibling
field as (a) **consumed programmatically** (a gate/test/chained verb reads
it) → KEEP; (b) **pure duplicate of the render bytes with zero non-test
consumers** → DROP the structured side (the B1 precedent: the render is
derivable/relayed; the bytes lived twice); (c) **digest-able** (a long list
whose render already carries the bodies) → keep counts + a capped list. The
audit procedure is mechanical per field: grep non-test consumers of the
attribute name; a field with only serialization-path hits is class (b).
Deliverable: a small table appended to this plan's drift log recording every
field's verdict — INCLUDING the "kept, consumed by X" rows, so the audit is
re-runnable, not a one-shot judgment.

**Mechanics.** Per dropped field: wire model edit + `build_schemas` +
`bake_operations_json` regen + the verb's tests re-pinned. `extra="forbid"`
means a drop is visible to any out-of-tree consumer — each drop lands in
CHANGELOG under a "wire" heading (the SemVer surface,
`docs/integrations/CONTRACT.md` governs whether a major/minor note is owed).

**Tests.** Per-verb: the render still carries the dropped content (the
relay surface is the survivor); the serialized result no longer carries the
duplicate bytes (the `test_default_response_carries_no_diff_or_full_body_bytes`
idiom).

## F5 — `read-decisions` digest mode

**Decision.** `ReadDecisionsInput` gains `digest: bool = False` (additive;
`extra="forbid"` unchanged). `digest: true` serves
`records_digest: [{ts, block, actor?, response_sha12, resolved_keys,
response_chars}]` + `count` + `path` and OMITS `records` (the full bodies) —
the loop's chain-coherence preflight needs ordering + identity + counts,
never the prose. Default (`false`) is byte-identical to today (compat: the
gates and tests that read full records are untouched). The skills that name
`read-decisions` MCP-direct for the preflight scan (hpc-submit / hpc-status /
hpc-aggregate / hpc-campaign / hpc-notebook-audit) switch their preflight
instruction to `digest: true`, with "pass digest: false only when you must
read a record's full text" — the branchpoint rule again.

**Mechanics.** `_wire/queries/decision_journal.py` (spec + a
`DecisionRecordDigest` helper model, added to `build_schemas._HELPER_NAMES`
if nested-only), the verb in `ops/` (locate via
`grep -rn "read-decisions" src/hpc_agent/ops`), schema regen, skill-body
edits (5 files), `lint_skill_mcp_reachability` re-run.

**Tests.** digest omits bodies + carries sha12s/counts; default
byte-identical (serialize both ways over the same journal); bogus digest
value refused by pydantic.

---

## Task waves (file-disjoint where parallel)

- **Wave 1 (parallel):** F1 (`mcp_server.py` + its tests), F2
  (`cli/_helpers.py`, `cli/_dispatch.py` + new test file), F5 wire+verb.
- **Wave 2:** F3 (skills + pyproject + new lint) and F5's skill-body edits
  (same files as F3 for the 5 loop skills — sequential with F3, never
  parallel).
- **Wave 3:** F4 audit table → the individual drops it authorizes (each drop
  its own commit; schema regen after each).
- **Verification (every wave):** `ruff check --fix && ruff format`, `mypy`,
  `python scripts/run_lint_gauntlet.py`, `python scripts/regen_all.py
  --check`, full `pytest`; `scripts/suggest_tests.py --run` as the inner
  loop.

## Drift log

(open)

**2026-07-27 — Waves 1–3 implemented (same day; F4 audit table below).**
F1: `mcp_server._strip_schema_descriptions` + `_SPEC_DOCS_POINTER`, applied to
the `spec` sub-schema in `_tool_input_schema`; packaged schemas untouched
(pinned by `tests/test_mcp_server.py::test_tools_list_spec_schema_is_structure_only_with_docs_pointer`
+ `::test_packaged_schema_on_disk_keeps_full_descriptions`). F2:
`cli/_helpers._offload_oversized_refusal` wired into `_err`, experiment dir
threaded from the three `_dispatch` error sites; battery in
`tests/cli/test_refusal_offload.py`. F3: `references/` dirs shipped
(pyproject package-data), splits landed for hpc-submit
(`nudge-revision.md`, `retarget-anomaly.md`) and hpc-notebook-audit
(`interview-handoff.md`); the `branch-reference-integrity` lint rule
(error-severity, fire-path-tested) guards dangling/orphan references. F5:
`ReadDecisionsInput.digest` + `DecisionRecordDigest`; the default response is
byte-identical (the `records_digest` key is dropped when absent by the
model serializer — pinned); the hpc-submit / hpc-status chain-coherence scans
now pass `digest: true`.

**F4 audit table (the per-field verdicts; re-runnable — the criterion is
"does a non-test consumer read the structured side, and does the render carry
the same bytes"):** every markdown/render-bearing Result class is consumed
in-src only by its own producing op (verified by class-name grep), so the
verdicts turn on duplication size and out-of-tree utility.

| Result | Verdict |
|---|---|
| `NotebookDraftContextResult.template_sections[].source` | **DROPPED** (this wave): the whole template's cell prose rode verbatim in the rows AND the markdown. Rows now carry `source_sha12` (the audit's normalized sha) — identity, verifiable against the render. Renderer takes the bodies separately; the content-keyed cache self-heals (validation-guarded load recomputes an old-shape payload). |
| `NotebookDryRunResult.sections[]` traceback bodies | **RECORDED FOLLOW-ON**: the per-section traceback tails ride in the rows and the markdown. Same surgery as draft-context (rows keep outcome + counts, bodies stay in markdown). Not done this wave — the rows also carry assert verdicts consumers may want typed; needs its own pass. |
| `RunStoryResult.events` | **KEPT**: the D3 closed-key event set is the machine-readable story product (identity+ordering+counting atoms); the markdown is its projection. Dropping it would make the render the only projection of a computed (non-persisted) result. |
| `AttentionQueueResult.items` | **KEPT**: compact per-item metadata (ids, states), not body bytes; the render is a digest of it, not a duplicate. |
| `EvidenceBriefResult` conclusions/envelopes/activity | **KEPT**: structured evidence rows are the citable product (sha-bearing); render is a projection. |
| `TraceRenderResult` / `TraceDiffResult` structured fields | **KEPT**: waterfall/lineage/divergence atoms are typed data the render summarizes; not verbatim duplication. |
| `CiteCheckResult.findings`, `ExtractRecipeResult` rows, `ProgramVerifyResult` rows | **KEPT**: sha-bearing verification rows (the citable half); renders are projections. |
| `WorkerLogDigestResult.tail` | **RECORDED FOLLOW-ON**: the tail lines ride in `tail` AND inside `render`; small in practice (bounded tail), so low value — fold into the dry-run pass if taken. |
| `DirDigestResult`, `ChallengeStatusResult`, `ConformanceStatusResult` | **KEPT**: counts/states/histograms — metadata beside the render, no body duplication. |
