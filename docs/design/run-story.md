---
status: shipped
---
# The run story — design + implementation plan

**Status: IMPLEMENTED (2026-07-07).** Waves A/B/C landed
(`state/run_story.py`, `ops/story_render.py`, `_wire/queries/run_story.py`,
`ops/run_story.py`, the boundary contract, the enforcement rows, the primitive
doc). The implementation drift log is at the foot of this doc; the settled
decisions with recorded rationale and the boundary-drift watch list below remain
the durable reference. Cite `path::symbol`, never line numbers. Origin: the
"Related, planned separately"
paragraph of `docs/design/notebook-audit.md` — the run story is the decision
journal's INTERFACE sibling, promised there as a pure ordering/identity
projection and a natural sibling of the audit-view renderer posture.

## Product intent

The journal is now a trustworthy chain of attestations — greenlights, nudges,
sign-offs, auto-clears, receipts, scope locks/unlocks, briefs, block
terminals, kill/supersession stamps, verdict history — but it is an **archive
without an interface**. Reading "why did this run take the shape it did"
today means hand-merging five JSONL files and a journal record. The run story
is one deterministic render of a run's complete journal trail, ordered and
attributed: for the human reviewing their own run, and as the demo artifact
against Claude Science's message-history provenance (their provenance is a
chat scroll; ours is a code-rendered timeline of typed, gated records —
"every event has an author and a hash, and none of it was narrated by a
model").

It is a PURE projection. No LLM prose in the render path (the
`ops/relay_render.py` posture: code renders, the agent relays verbatim). No
new trust machinery (nothing attests a story; `story_sha` is a fingerprint,
not an attestation — deliberately NOT routed through
`state/attestation.py::bind`, which exists to lock *claims about content*,
not to fingerprint projections). No new stores (the story journals nothing;
it is derived state recomputed on every call, the
`ops/notebook_status.py::notebook_status` posture).

The boundary formulation (`docs/design/rigor-primitives.md`, the
four-operations test) fits exactly: the story is ORDERING (merge by
timestamp), IDENTITY (which run/scope/section/record), COMPARISON (same
lineage? same block?), and COUNTING (N events, M omitted). It never names
what any record MEANS.

## Settled decisions

### D1 — sources: the dossier's store inventory, minus the opaque stores

The streams that merge into one timeline are exactly the record stores the
dossier seals (`ops/export_dossier.py::DOSSIER_SOURCES`), with two principled
exclusions. The story's source set:

| Stream | Store | Reader | Event classes |
|---|---|---|---|
| run decision journal | `.hpc/runs/<run_id>.decisions.jsonl` | `state/decision_journal.py::read_decisions` (`scope_kind="run"`) | human `y` greenlights, nudges, reproduction receipts — every `append-decision` record |
| emitted briefs | `.hpc/runs/<run_id>.briefs.jsonl` | `state/decision_briefs.py::read_briefs` | code emitted a decision brief at a block boundary |
| block terminals | `.hpc/runs/<run_id>.<block>.terminal.json` | `state/block_terminal.py::read_terminal` per block (glob the runs tree the way `export_dossier._gather_run` does) | a detached block reached its terminal (stage_reached, cmd_sha) |
| journal record | `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `state/journal.py::load_run` → `RunRecord` | `verdict_history` entries; the timestamped lifecycle stamps (`submitted_at`, `kill_requested_at`/`kill_confirmed_at`, `superseded_at`) synthesized as events |
| scope journals | `.hpc/scopes/<tag>.decisions.jsonl` for each tag on the run's sidecar | `state/decision_journal.py::read_decisions` (`scope_kind="scope"`) | lock/unlock (`resolved.scope_action`) |
| look ledgers | `.hpc/scopes/<tag>.looks.jsonl` | a read alongside `state/scopes.py::looks_path` (the tolerant-read idiom) | a look — this run OR a sibling run reduced against the scope (identity only, per the ledger's own no-metric rule) |
| notebook journal | `.hpc/notebooks/<audit_id>.decisions.jsonl`, only when the sidecar carries an `audited_source` echo | `state/decision_journal.py::read_decisions` (`scope_kind="notebook"`) | human sign-offs, code auto-clears, render receipts (`state/notebook_audit.py` block literals) |

Excluded, with reasons:

- **`aggregated`** — opaque bytes by the dossier's own no-parse pin
  (`tests/contracts/test_dossier_boundary.py`); parsing a harvested aggregate
  to extract events would name the caller's metrics. The S4 terminal + the
  harvest brief already carry the row COUNT, which is the countable fact the
  story may render.
- **`sidecar`** — identity, not events. It feeds the story HEADER (run_id,
  cluster, scopes, `audited_source`) but contributes no timeline entries.
- **the monitor tick log** (`ops/monitor/tick_log.py`'s `.monitor.jsonl`) —
  high-volume liveness telemetry, not decision trail; the dossier excludes it
  too. A thousand ticks would drown twenty decisions. Recorded as a deliberate
  v1 exclusion; the revisit trigger is a user asking "when exactly did the
  driver stall" and the answer not being derivable from the watchdog stamps
  already on the `RunRecord`.

Scope-journal and look-ledger streams are keyed off the sidecar's `scopes`
tags exactly as `export_dossier._gather_run` unions them, so the story's
sources and the dossier's sealed stores can never disagree about what a run's
trail IS. `--include-lineage` widens the run set via the ONE supersession walk
(`state/scopes.py::lineage_chain` — the dossier precedent; no second lineage
definition).

### D2 — deterministic merge: ts-major, stream-rank, intra-file order

The merge key is the triple **(ts, stream_rank, intra_stream_index)**:

- **ts** — every store stamps `infra/time.py::utcnow_iso` (ISO-8601 UTC with
  a fixed `+00:00` offset, second precision), so lexicographic string
  comparison IS chronological comparison within this system. No datetime
  parsing in the merge; a malformed/absent ts sorts to the epoch-front with a
  recorded `ts_missing` flag on the event, never a crash (the tolerant-read
  doctrine: one bad record never strands the trail).
- **stream_rank** — records from DIFFERENT writers land within the same
  second routinely (a block appends its brief, its terminal, and the human's
  decision inside one second). Ties break by a fixed, documented stream order
  chosen to match causal reality at a block boundary: brief → terminal →
  decision → scope → look → notebook → journal-record stamps → verdict
  history. This is a REPRESENTATION choice, pinned by test, not a truth
  claim.
- **intra_stream_index** — the JSONL line number. Append order within one
  file is causal by construction (`decision_journal._append_jsonl_line`'s
  flock); the merge NEVER reorders two records from the same file.

Monotonicity caveat, recorded honestly: writers on different machines can
skew, and second precision loses sub-second order across files. The guarantee
the story makes is **determinism, not oracle truth** — the same stores render
the same timeline byte-for-byte, and the doc/header says "ordered by recorded
timestamp" rather than claiming a total causal order. Rejected alternative: a
vector-clock or sequence-number scheme threaded through every writer — new
trust machinery touching every store for a projection that only needs stable
presentation order.

### D3 — the event model: small, typed, opaque toward domain vocabulary

One frozen dataclass, `StoryEvent`, in the new `state/run_story.py`:

```
StoryEvent:
    ts: str                  # verbatim from the record ("" when absent)
    stream: str              # the SOURCE-STORE noun (D1 table) — the dossier's typing rule
    actor: str               # "human" | "code" — the attestation kernel's attestor vocabulary
    kind: str                # the record-class literal (block name, "scope-lock", "look",
                             #   "verdict", "kill-requested", ... — closed per-stream sets)
    subject_id: str          # run_id / scope tag / audit section / block — opaque identity
    evidence: dict           # sha pointers ONLY: cmd_sha, section_sha, view_sha,
                             #   brief_digest (sha256 of the brief's canonical JSON),
                             #   lineage_root, job/row COUNTS — identity + counting, never values
    text: str                # the HUMAN's verbatim words, when the record carries any
                             #   (a nudge's response, an unlock reason) — else ""
```

Attribution rules:

- `actor="human"` exactly when the record class is a human act under the
  existing gates: a decision-journal `response` (greenlight or nudge), a
  scope unlock, a notebook sign-off. Everything else — briefs, terminals,
  auto-clears, receipts, looks, locks (`state/scopes.py::record_lock` is the
  safe direction, code-reachable), watchdog/kill/supersession stamps, code
  verdicts — is `actor="code"`. `verdict_history` entries carry their own
  `decided_by`; map `judgement`→human-adjacent honestly by passing the
  recorded `decided_by` through in `evidence` and setting `actor` from it
  (`code`→code, anything else→human).
- **Human text renders verbatim; agent text renders as a pointer.** A nudge's
  `response` is archived human authorship — the story quotes it in full (no
  truncation: truncating human words is a silent cap). The `proposal` and
  `evidence_digest` fields are agent/code-drafted prose; the story carries
  only their sha256 digest in `evidence`, never the prose. Rationale: the
  render path must not re-launder LLM-drafted text as timeline narrative;
  the journal itself remains one `read-decisions` away for anyone who wants
  the full record. This is the Q1 line applied to text: human words are
  evidence of authorship (identity), agent words are content (semantics).
- The story never interprets a metric, a tag role, or a brief's
  recommendation. Counts may render (`"harvest complete: 20 row(s)"` — the
  `relay_render._counts_phrase` precedent); values never.

### D4 — the render: canonical JSON + `story_sha`, then code-rendered markdown

Two artifacts from one projection, mirroring the audit view
(`ops/notebook/audit_view.py::_canonical_json` / its `view_sha`):

1. **canonical JSON** — the header + the ordered event list, serialized with
   sorted keys; `story_sha = sha256(canonical_json)`.
2. **markdown** — a deterministic pure-string rendering OF that JSON (one
   line per event: `ts · actor · kind · subject · evidence pointers · quoted
   human text`), grouped under block-phase headings. Same module, no I/O, no
   `_wire` import — the `relay_render.py` posture verbatim.

WHY the story is sha'd like the audit view: the archive-vs-interface
precedent. The audit trail records what the human SAW (`view_sha`); a relayed
story should be verifiable the same way — an agent claiming "the story shows
X" can be checked against `story_sha` by the rule-10 machinery later
(`ops/decision/verify_relay.py` grows a story sibling in v1.5, the T11
pattern: contradiction kinds reused, no new wire enum). v1 ships the sha and
STOPS — no journaled render receipts for stories (a read-only query that
journals every invocation would turn the interface into write traffic on the
archive it renders; the notebook's render receipts exist because execution
evidence feeds a GATE, and no gate consumes a story).

Header: `{run_id, cluster, submitted_at, status, scopes, audit_id?,
supersedes?}` read directly off `RunRecord` fields + the sidecar — direct
field reads, deliberately NOT a copy of the dossier's
`_project_run_identity` allowlist (one-definition note: if the header ever
grows toward that projection, promote the dossier's private function to a
shared symbol rather than fork it; flagged in the drift watch list).

### D5 — surface: a `run-story` query primitive; NOT in the dossier

- **`run-story`** — a read-only `verb="query"` primitive at the ops role
  root (`ops/run_story.py`, the `ops/notebook_status.py` pattern: role-root
  because it reads across subjects; the subject-imports lint short-circuits
  there by construction). Spec: `{run_id, include_lineage?, since_ts?,
  limit?, markdown?}`. Result: `{run_id(s), events, story_sha, markdown,
  total_events, omitted_count}`. Idempotent, derived state, no side effects.
- **NOT MCP-curated in v1** — the `export-dossier` precedent applies
  verbatim (`docs/design/dossier-export.md`): the curated catalog is the
  block loop + recovery verbs; a timeline render is an operator/reviewer
  action, not a decision point, and exposing it invites in-session
  bundle-and-interpret. Same recorded revisit trigger: curate it only on
  evidence of an agent hand-rolling the merge (cat-ing `.hpc` JSONL through
  raw shell because no verb was reachable).
- **Skill/status seats:** no chain wiring, no `next_block` — the story is
  never a block. One line of skill prose in `hpc-status`'s human-facing
  wrapper may MENTION the verb after a terminal snapshot ("`hpc-agent
  run-story <run_id>` renders the full timeline"); prose proposes, never
  sequences (the `/sync` proposal idiom).
- **Dossier inclusion: NO.** The dossier seals RECORDS — concrete on-disk
  store bytes typed by store noun, with `DOSSIER_SOURCES` pinned closed by
  `tests/contracts/test_dossier_boundary.py`. A story is a PROJECTION: it has
  no store, no disk life, and adding it would make the bundle carry an
  artifact derivable from (and therefore able to disagree with) the records
  beside it — the exact two-sources-of-truth shape the one-definition rule
  exists to kill. The right relationship is the inverse, and it falls out for
  free: every story source is a sealed dossier store, so a consumer can
  recompute the story FROM a dossier and check `story_sha`. A
  `--from-dossier <zip>` reading mode is the natural v1.5 portability step
  (the notebook-export precedent: projections over sealed records live in
  the renderer/plugin lane), deferred.

### D6 — scale and windowing: no silent caps

Campaign-adjacent runs accumulate long trails. The honest contract:

- Default = the FULL timeline. No implicit cap, ever.
- Caller windowing: `limit` (newest-last window — the most recent N events)
  and/or `since_ts` (lexicographic ts floor, valid because D2 pins the
  format). When a window applies, the result carries `total_events` and
  `omitted_count`, and the markdown header renders "showing N of M events
  (M−N older events omitted)" — the omission is itself a rendered,
  countable fact. A window can therefore never masquerade as the whole
  story, and `story_sha` is computed over the WINDOWED canonical JSON
  (which includes the omission counts), so a sha can never be passed off as
  covering events it does not contain.
- Campaign-scope stories (the whole-campaign timeline, the attention-queue
  sibling) are OUT of v1 — the run is the unit. The event model and merge
  are scope-kind-agnostic by construction, so the campaign story is a later
  caller of the same kernel, not a redesign.

## Task waves (file-disjoint, parallel Opus dispatch)

Wave A (parallel — no shared files):

- **T1 — `state/run_story.py` (new):** `StoryEvent` + the per-stream
  projections (one small function per D1 stream, closed `kind` sets) + the
  D2 merge (`merge_events`, the one ordering definition). Reads route through
  the existing readers (`read_decisions`, `read_briefs`, `read_terminal`,
  `load_run`, the looks reader); tolerant of absent/corrupt stores. Pure
  I/O-thin state module — no `_wire`, no SSH (the `state/scopes.py` posture).
  Tests `tests/state/test_run_story.py`: determinism (two calls
  byte-identical), tie-break triple (same-second cross-stream order pinned;
  intra-file order never reordered), `ts_missing` tolerance, actor
  attribution per record class (fires: a code record projected as human
  FAILS), verbatim-nudge / digest-only-proposal (fires: proposal prose in an
  event FAILS), look/lock/notebook/verdict/kill-stamp projections each with
  a fires+passes pair.
- **T2 — `ops/story_render.py` (new):** canonical JSON + `story_sha` +
  markdown, taking the merged header+events as input. Pure string work — no
  journal reads, no I/O, no `_wire` (the `relay_render.py` posture; do NOT
  fold into `relay_render.py`, whose contract is the one-liner relay).
  Tests `tests/ops/test_story_render.py`: golden markdown, sha stability
  across dict insertion orders, window header honesty (fires: an
  `omitted_count > 0` render without the "omitted" line FAILS), counts-only
  rule (fires: a metric VALUE from evidence rendered FAILS via a crafted
  event).
- **T3 — `_wire/queries/run_story.py` (new):** `RunStorySpec` /
  `RunStoryResult` Pydantic models (mirror `_wire/queries/notebook_status.py`
  — flat, no domain vocabulary in field names; the dossier's
  forbidden-vocabulary posture). Tests ride the schema-roundtrip contract
  suite.

Wave B (after A — the assembly seam):

- **T4 — `ops/run_story.py` (new):** the `@primitive(name="run-story",
  verb="query", agent_facing=True, side_effects=[])` op at the ops role
  root: sidecar/record header assembly, D1 source gathering (scope tags off
  the sidecar; notebook journal only when `audited_source` echoes;
  `include_lineage` via `lineage_chain`), T1 merge, D6 windowing, T2 render.
  Tests `tests/ops/test_run_story_op.py`: missing-run refusal
  (`errors.SpecInvalid`, the `export_dossier` no-sidecar-no-record guard),
  absent stores are data not errors, lineage union, windowed result carries
  `total_events`/`omitted_count`.
- **T5 — `tests/contracts/test_run_story_boundary.py` (new):** the boundary
  pins — (a) event shape: every event-dict construction carries exactly the
  D3 key set (the dossier entry-shape AST-scan precedent); (b) no
  interpretation: `ops/run_story.py` + `ops/story_render.py` +
  `state/run_story.py` contain no `json.load`/`loads` of the `aggregated`
  tree and no domain-semantics field names on the wire models; (c) the
  render modules import nothing LLM-adjacent and take no free-prose input
  parameter.

Wave C (sequential — hot/shared files, one at a time):

- **T6 — regen + inventory tails:** run all SIX regen scripts
  (`scripts/bake_operations_json.py --write`, `build_operations_index.py`,
  `build_primitive_frontmatter.py`, `build_primitive_index.py`,
  `build_schemas.py`, `build_verb_module_map.py`) — the registry grows
  138→139 and `run-story` takes `--spec`, so expect and update the inventory
  tails: `_SPEC_VERBS` in BOTH `tests/contracts/test_schema_roundtrip.py` and
  `tests/contracts/test_primitive_remediation.py`, the prose primitive count,
  `operations.json`, `cli/_verb_module_map.py`, the generated schemas +
  primitive doc.
- **T7 — `docs/internals/engineering-principles.md` enforcement rows** (hot
  file): one row per T5 pin ("story events are identity/ordering/counting
  over opaque records — no metric, no role vocabulary, no LLM in the render
  path", "one merge definition — a second ordering re-derivation fires the
  contract test", "windowing is never silent — omitted counts are load-
  bearing"), citing the T1/T2/T5 tests as the normative copy.
- **T8 — this doc → final** (status flip + implementation drift log, the
  notebook-audit pattern).

v1.5 (designed-for, deferred): the verify-relay story sibling (T11 pattern —
`story_sha` / event-claim contradictions reuse kinds `number`/`state`); the
`--from-dossier` reading mode (D5); the campaign-scope story + attention
queue (separate design, same kernel); MCP curation on the recorded trigger.

## Boundary-drift flags (the story must never grow)

- **No LLM anywhere in the render path** — not summarization, not "narrative
  smoothing", not a `--prose` flag. Anything an LLM says ABOUT a story goes
  through relay + verify-relay as a claim against `story_sha`, outside this
  module. The pressure to make the timeline "read better" is the feature
  working; the answer is better deterministic grouping, never generation.
- **No domain/metric interpretation** — counts render, values never; scope
  tags stay opaque slugs; a brief's recommendation renders as a digest
  pointer, not advice. The moment an event grows a `role`/`metric`/`verdict-
  quality` field, the Q1 line is crossed (the dossier's forbidden-vocabulary
  test is the model).
- **No mutable state, no store** — the story is recomputed on every call and
  journals nothing. If a future gate wants to consume "the human saw the
  story", that is a new attestation instance routed through
  `state/attestation.py` (the T0 kernel), a deliberate separate design — not
  a side effect quietly added to this query.
- **No second ordering or lineage definition** — the merge lives once in
  `state/run_story.py`; lineage stays `state/scopes.py::lineage_chain`; a
  consumer re-sorting events has forked the timeline.
- **No silent caps** — any future default window, sampling, or "smart
  collapse" must surface its omission count in both the JSON and the
  markdown, or it does not ship.
- **Identity-projection duplication watch** — if the story header converges
  on the dossier's `_project_run_identity` allowlist, promote that one
  function to a shared symbol; never fork the field list.

## Implementation drift log (2026-07-07)

Deviations from the plan above, recorded so the next reader trusts the code
over the prose. Nothing changed a settled decision; these are realization
details the frozen models forced.

- **Wave A (recorded by that agent, respected here):** the journal-record
  lifecycle STAMPS and the `verdict_history` entries share the one
  `journal-record` stream noun and therefore one `STREAM_RANK`; D2's separate
  stamps→verdict position is realized by EMISSION ORDER inside
  `project_journal_record` + the stable merge (a stable sort keeps insertion
  order for equal keys), not by a second rank. `ts_missing` rides `evidence`
  (the frozen 7-field `StoryEvent` has no dedicated flag field). The
  counts-only rule is enforced at the RENDER surface (`story_render`'s evidence
  whitelist drops a non-pointer/non-count key), not in the projections — the
  producers already emit only safe keys, so the whitelist is a defense-in-depth
  guard on the markdown line.
- **T4 header shape:** the header key is `run_ids` (a list), not the plan's
  singular `run_id` — the result already carries the lineage set and the render
  golden expects `run_ids`, so the header mirrors it. `audit_id` / `supersedes`
  are emitted only when present (the dossier's reproduces-if-present idiom); the
  header is direct `RunRecord`/sidecar field reads, NOT a call into the
  dossier's private `_project_run_identity` (the one-definition watch stands —
  promote that function if the two ever converge).
- **T4 windowing order:** `since_ts` (lexicographic floor) applies BEFORE
  `limit` (newest-last slice); `total_events` is the full pre-window count and
  `omitted_count` is what BOTH controls dropped, so the honesty counts cover the
  combined window. The op windows by slicing and never re-sorts (pinned by
  `test_assembly_op_never_re_sorts_the_timeline`).
- **T5 pins realized:** the "no interpretation" pin is expressed as a
  `STREAMS`-equals-dossier-sources-minus-opaque-stores equality + a
  no-`_aggregated`-reference scan (a blanket `json.load/loads` ban would be
  wrong — `state/run_story._read_looks` legitimately parses the look ledger's
  JSONL). The one-ordering pin is TWO assertions: exactly one `sorted()` in
  `state/run_story.py` (the merge) and zero `sorted`/`.sort` in
  `ops/run_story.py`.
- **T6 deferred to the orchestrator:** the six regen scripts + the schema-file
  inventory tails (`operations.json`, `_verb_module_map.py`, the generated
  schemas, the prose primitive count, `_SPEC_VERBS` in
  `test_schema_roundtrip.py`) are baked by the orchestrator, NOT in this wave.
  `run-story` was added to `_SPEC_VERBS` in `test_primitive_remediation.py`
  (the inventory-vs-CLI drift check) with no `XFAIL_NO_FAILURE_FEATURES` entry —
  the dispatch seam attaches `failure_features`. Until the input schema
  (`run_story.input.json`) is baked, `run-story` appears only in that drift
  check, not the schema-file-parametrized remediation probes (the
  notebook-lint precedent).
