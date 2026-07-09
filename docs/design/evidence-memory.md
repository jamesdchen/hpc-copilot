---
status: plan
---
# Evidence memory — the lab notebook that writes itself, with provenance

**Status: IMPLEMENTED (2026-07-08). Waves A/B/C landed on branch `br-ev-c`
(the substrate T1–T6, then T7 the conclusion scope kind, T8 the authorship
gate, T9 the fail-open greenlight/S1 embed, T-NB the never-blocking pin, T10
the campaign-unconcluded queue collector, T11 the enforcement rows, T12 the
skill prose + this flip).** Implementation drift is recorded in the drift log
at the foot of this document. This document is the durable hand-off
(the `docs/design/notebook-audit.md` pattern): settled decisions with
recorded rationale, file-disjoint task waves for parallel Opus dispatch,
enforcement rows, and boundary-drift flags. Cite `path::symbol`, never line
numbers. Record implementation drift in the drift log at the foot of this
document.

## Product intent

The archive is already trustworthy — every trusted thing in the system is an
attestation (`state/attestation.py` module docstring). What it cannot yet do
is REMEMBER: "what have we tested under tag X, when, with what envelopes and
what verdicts?" is answered today by a human scrolling journals or — worse —
writing a summary from memory. Evidence memory makes the research program
CUMULATIVE: the cross-experiment question is answered as a **projection over
sealed records** — dated, sha-linked, drift-aware — never a narrative anyone
authored after the fact.

Strategic frame (memory-recorded 2026-07-07): per-artifact provenance is
table stakes — Claude Science ships it. A **queryable, attested research
history across years of a non-stationary domain** is what a solo researcher
compounds into an edge. The domain is non-stationary, so the memory must be
time-indexed evidence ("no alpha in 2025H1, envelope ±2%, n=4"), never
permanent verdicts — the no-kill-ledger posture
(`docs/design/registration-kernel.md` R7) extended from single registrations
to the whole program's history.

What this is NOT: a dashboard, a knowledge base the LLM curates, a dedupe
gate that refuses "already-tested" work, or a second source of truth. The
index is derived and disposable; the records it projects are the existing
journals, sidecars, ledgers, and one new record type.

## The settled design center (user-ruled 2026-07-07 — DECIDED)

Every item below is settled; this document plans the consequences. Departures
during implementation are drift to be logged, not re-litigated.

### E1 — THE CONCLUSION ATTESTATION: the one new record type

A **conclusion** is a human-authored finding — "edge X showed no alpha vs RV
data in 2025H1 — see dossier a3f2…" — recorded as an ordinary attestation
riding `append-decision` (no new store, no migration; the R1 posture):

- **Evidence-bound.** The record MUST cite at least one sha of the evidence
  it rests on, and the human's `response` MUST name at least one cited sha by
  an 8+-hex prefix — the registration-kernel R6 sha-prefix authorship bar
  reused verbatim. You cannot conclude about evidence you didn't name; a sha
  prefix exists nowhere in prior vocabulary and can only derive from the
  presented evidence.
- **Dated.** The record's `ts` is the finding's date; every projection
  renders it. A conclusion is dated evidence about a period, never a
  permanent truth.
- **Journaled through `append-decision`**, under a gated block (E-shape
  below) — no conclusion verb, no chain, no next_block, no skill affordance
  (the no-unlock-verb doctrine, `docs/design/rigor-primitives.md`).
- **NEVER blocking anything, and required NOWHERE at creation.** Three
  pressures replace a mandate: (a) the attention queue carries "campaign
  concluded, no conclusion recorded" as an AGING standing item (a new
  collector, the D5 route-through posture of
  `docs/design/attention-queue.md`); (b) consumers demand declaratively — a
  registration template may name a conclusion prerequisite (the
  `evidence_meets` pattern; kind reserved, E6); (c) the authorship bar keeps
  writing effortful, so a conclusion is a deliberate act, not a form field.
- **Superseded, never deleted.** Append-only; newest-wins per subject via
  `state/attestation.py::reduce`. A superseded conclusion remains a truthful
  dated record of what was believed when.

### E2 — VOCABULARY: scope tags + lineage fallback

- **Tags are human-authored free text**, elicited in the interview /
  audit-prelude flows and utterance-derived per the existing authorship
  machinery (`ops/decision/journal.py::_assert_human_authorship`, harness
  capability 1). The tag shape is the existing scope-tag slug
  (`state/scopes.py::validate_tag` — shape only, never vocabulary). **An
  agent-invented tag is index poisoning — the fabrication class.** Core never
  interprets what a tag means.
- **cmd_sha LINEAGE is the always-present fallback key.**
  `state/scopes.py::lineage_chain` (the supersession walk) +
  `state/run_sha.py` (param identity) mean untagged work stays findable by
  CODE IDENTITY: "everything ever run with this command shape" needs no
  human to have tagged anything.
- **Absence is DISCLOSED, never refused.** An untagged run or an
  unconcluded campaign shows up in the greenlight brief and the queue as a
  disclosed gap ("no tags declared; lineage-keyed priors only", "0
  conclusions under these tags") — the no-silent-caps posture.
- **Conclusions carry tags = retro-indexing.** A conclusion's own `tags`
  list makes old, untagged evidence findable under today's vocabulary
  without rewriting a single sealed record — the newest conclusion is the
  retroactive index entry.

### E3 — AUTOMATIC ADVISORY SURFACING AT GREENLIGHT

The greenlight / audit-prelude brief embeds the point-query digest for the
new work's tags (and its lineage): *"3 prior campaigns under this tag;
newest conclusion 2025-11, negative, envelope ±2% (n=4, main-scale)"* —
every line dated and sha-cited, rendered by code.

**ENFORCEMENT-PINNED NEVER-BLOCKING.** A contract test mechanically asserts
the surfacing path contains no raise/gate/refusal branch: a run with ten
negative priors greenlights IDENTICALLY to one with none. This is the
anti-stage-0-dedupe pin — the kill-ledger decision (evidence is
time-indexed, the domain is non-stationary, "already tested" is never a
mechanical refusal) held against future contributors. **This pin is
LOAD-BEARING and gets its own task (T-NB) and its own enforcement row** —
it is the single most likely line for a well-meaning contributor to cross.

### E4 — INDEX = derived, never authoritative

Recompute-on-read plus a content-keyed cache (the
`state/describe_cache.py` posture: opportunistic, any I/O error falls
through to the live path, an env var opts out). Fleet discovery reuses the
existing `repo.json` glob
(`ops/attention_queue.py::discover_fleet_experiments` — non-creating,
`skipped` accounting). **Deleting the cache loses nothing**; there is no
digest file, no served page, no watermark (reconcile-is-truth).

### E5 — UX: the POINT QUERY is primary

- **`evidence-brief`** — a read-only `verb="query"` primitive (naming
  settled against the registry idiom below): spec
  `{tags, lineage?, as_of?, fleet?}` → a deterministic, code-rendered digest
  SIZED FOR BRIEF EMBEDDING — newest conclusion first, envelopes with their
  evidence weights, dated, every line sha-cited. Cheap enough (journal-first,
  no SSH) to run inside every greenlight.
- **`evidence-period`** — the PERIOD DIGEST: a time-window projection over
  the SAME collector (a `run-story` sibling — `ops/run_story.py`'s pure
  ordering/identity posture), ending with the **unconcluded-campaigns
  list** — the place the conclusion loop closes.
- **Exploratory browse is a RECORDED NON-GOAL as a verb.** The agent is the
  browser (the attention-queue precedent against dashboard-thinking); see
  the boundary-drift flags.

## Decisions settled in THIS document

### E-shape — the conclusion record, exactly

**Scope kind: a NEW kind `"conclusion"`** in
`state/decision_journal.py::SCOPE_KINDS`, path branch →
`.hpc/conclusions/<conclusion_id>.decisions.jsonl` (+ the
`_wire/actions/decision_journal.py::ScopeKind` literal, schema regen).
Weighed against riding an existing kind, per the R9 collision note:

- **Not `"scope"` (the tag's journal):** a conclusion may carry SEVERAL
  tags and may carry none (lineage-only). It has no single tag home;
  scattering copies or electing a "primary tag" both corrupt the one-record
  rule. Tag journals also already carry lock/unlock records — cross-family
  noise in every reduction.
- **Not `"run"` / `"campaign"`:** a conclusion typically spans runs and
  campaigns and outlives any one of them (the R9 "outlives any single run's
  journal" rationale, verbatim).
- **Sequencing:** `"pack"` (domain-packs T8) and `"registration"`
  (registration T6) land during the slate; `SCOPE_KINDS` is a frozenset with
  no real ordinal (slate standing rule), so `"conclusion"` is simply the
  next kind to land AFTER the slate — our T7 serializes behind both, and
  behind any other in-flight edit to `state/decision_journal.py`.

**Block name:** `"conclusion"` — refused for any `scope_kind` other than
`"conclusion"` and vice versa (the `scope-unlock` / R6 block-convention
mirror). Supersession = a newer record under the same `conclusion_id`;
explicit withdrawal = a `"conclusion-revoke"` record with a mandatory
free-text reason (the R7 form; no sha recompute — it binds nothing new).

**`resolved` fields (all validated server-side at append):**

```json
{"conclusion_id": "<caller-authored slug — RunIdStrict class, path segment>",
 "tags": ["<scope-tag slug>", "..."],
 "concludes": [{"scope_kind": "campaign|run|scope", "scope_id": "..."}, "..."],
 "citations": [{"kind": "<CITATION_KINDS member>",
                "ref": "<opaque id — run_id / cmd_sha key / dossier path>",
                "sha": "<full sha the evidence carries>"}, "..."],
 "finding": "<the human's free-text finding — opaque, echoed, never parsed>"}
```

- `tags` may be empty (disclosed, not refused); each member faces
  `state/scopes.py::validate_tag`.
- `concludes` is OPTIONAL identity linkage — which subjects this concludes.
  It exists so the unconcluded-campaign predicate is pure identity matching
  (a terminal campaign with no conclusion naming it), never text matching.
- `citations` MUST be non-empty — the evidence-bound rule. Kinds below.
- `finding` is opaque caller prose: stored, rendered verbatim, never
  interpreted (the identity-only discipline of
  `state/scopes.py::record_look`).

**The attestation projection:** `attestor="human"`,
`subject_kind="conclusion"`, `subject_id=<conclusion_id>`,
`content_sha=<canonical-JSON sha over the VERIFIED citations list>` (the
harness sha canonicalization, `docs/internals/harness-contract.md` —
`json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)`,
SHA-256 lowercase hex), `view_sha` OPTIONAL (present when the human signed
after reading a rendered `evidence-brief` — binds what-they-saw; a
conclusion may honestly be written without one). Bound via
`state/attestation.py::bind` with `recompute` = the server-side
re-canonicalization of the citations the gate just resolved — so the
content_sha cannot be asserted into existence and the conclusion is
hash-locked to its evidence set. Newest-wins reduction routes through
`state/attestation.py::reduce` (the one kernel — enforcement row).

**`CITATION_KINDS` — a CLOSED set of core mechanism nouns**
(`state/evidence.py::CITATION_KINDS`, equality-pinned — the
`PREREQUISITE_KINDS` / `DOSSIER_SOURCES` pattern). Each kind names the ONE
existing resolver the gate dispatches to:

| Kind | Resolves against (the one definition) | The sha compared |
|---|---|---|
| `dossier` | `ops/export_dossier.py` T3 seam `compute_dossier_signature` — a DRY re-gather from the LIVE stores (the R2 posture; registration T3 lands in slate Phase 2, before us) | `bundle_sha256` |
| `run` | the run sidecar + `state/run_sha.py` identity | `cmd_sha` |
| `fingerprint` | `state/fingerprint_store.py` ledger read (determinism-fingerprint T3; slate Phase 3, before us) | a sample's `content_sha` |
| `attestation` | `state/attestation.py::reduce` over a named journal `{scope_kind, scope_id}` — the generic escape hatch (any receipt, sign-off, registration) | the record's `content_sha` |

**Dispatch placement (corrected — pre-implementation verification
2026-07-07).** `state/evidence.py` owns `CITATION_KINDS`, the shape
validation, and the resolvers that are themselves state-level (`run`,
`fingerprint`, `attestation`). The `dossier` resolver
(`compute_dossier_signature`) lives in `ops/export_dossier.py`, and the
state substrate NEVER imports `ops` (zero such imports exist tree-wide;
the registration precedent split exactly this way:
`state/registration.py::check_chain` stays pure while
`ops/registration/prereqs.py` owns the ops-touching checker dispatch). So
the citation dispatch is composed AT THE OPS CALLERS: the append gate (T8)
and the read-side verbs pass the dossier resolver in (a callable in the
dispatch table's `dossier` slot — the `attestation.bind` recompute-callable
form), or route through a thin ops-root composer; `state/evidence.py`
itself must not gain an `ops` import. Note the import spelling at the T8
seat: `ops/decision/journal.py` is inside the `decision` subject, so it
reaches ops-root modules only via the established alias form
(`from hpc_agent.ops import export_dossier`) that
`scripts/lint_subject_imports.py` permits for non-subject root files.

**How citations resolve/verify — the honest choice, recorded.** Two moments,
two postures:

- **At APPEND, verification is against LIVE stores and a failure REFUSES.**
  The gate resolves every citation through its kind's resolver and compares
  the asserted `sha` against the resolved answer. An unresolvable or
  mismatched citation refuses the append with the recorded-vs-recomputed
  pair — you cannot conclude about evidence the machine cannot find on this
  namespace at write time. Rationale: this is the only moment the bind lock
  can be un-fakeable; trusting a dossier MANIFEST handed in by the caller
  would be the receipt-laundering hole (the registration R2 rejection,
  verbatim — the archive embeds what the caller says; the live stores say
  what is true).
- **At READ (`evidence-brief` / `evidence-period`), re-resolution
  DISCLOSES, never refuses.** Evidence legitimately moves after a
  conclusion is recorded — archived to S3, a store re-exported, a repo
  wiped. The digest re-resolves each citation and renders
  `cited (verified)` / `cited (unresolvable here)` per line. The conclusion
  stays a truthful dated record (the R5 template-drift divergence,
  same rationale: a record made under the evidence in force at its
  timestamp does not rot into a refusal); the drift is disclosed, the
  reader decides.

**The authorship gate** —
`ops/decision/journal.py::_assert_conclusion_authorship`, the
`_assert_registration_authorship` sibling (R6's three-lock structure):

1. **Lock 1 — no affordance:** append-decision under block `"conclusion"`
   is the only write path; no primitive named conclude/conclusion in the
   mutate registry (contract-test pinned).
2. **Lock 2 — recompute:** citations resolved server-side per the table;
   `content_sha` bound via `attestation.bind` against the re-canonicalized
   verified set; tags shape-validated; `conclusion_id` slug-validated.
3. **Lock 3 — authorship, the R6 bar reused:** bare acks refused
   (`ops/decision/journal.py::_is_bare_ack`); the response must name the
   `conclusion_id` token-exact AND at least one cited sha by an 8+-hex
   prefix matched against the chain the gate just verified. Under harness
   capability 1 the tokens derive from the out-of-band utterance log
   (full-strength tier); absent it, the journal-response friction tier
   applies, honestly named weaker (`docs/internals/harness-contract.md`).
   There is NO auto-cleared tier: the attestor is always human — a machine
   has no findings, only measurements, and the measurements are already
   attested elsewhere.

### E-collector — the tag index: what it walks, in what order

One collection function, `state/evidence.py::collect_evidence(experiment_dir,
*, as_of)` — **the ONE definition every surface calls** (both verbs, the
greenlight embed, the queue collector; the attention-queue "one ordering
definition" enforcement pattern). Per namespace (experiment dir) it walks,
via non-creating globs (the D3 discipline), tolerant-read throughout:

1. **Conclusion journals** — `.hpc/conclusions/*.decisions.jsonl`: reduce
   per `conclusion_id` via `attestation.reduce` + revoke/supersession
   winner-selection (the `state/registration.py` reduction form). The
   newest current conclusion per subject is the digest's lead.
2. **Scope journals + look ledgers** — `.hpc/scopes/*.decisions.jsonl` and
   `*.looks.jsonl` (`state/scopes.py::count_prior_looks` shape): per tag,
   look counts, distinct lineages, lock state, dates.
3. **Campaign journals** — `.hpc/campaigns/*/decisions.jsonl`
   (`state/decision_journal.py::latest_decision` — the D5 predicate): which
   campaigns exist, their dates, and — joined against (1)'s `concludes`
   sets — which terminal campaigns have NO conclusion (the loop-closing
   list).
4. **Run sidecars** — `.hpc/runs/*.json`, globbed DIRECTLY
   (`experiment_dir / ".hpc" / "runs"` — never via
   `RepoLayout(experiment_dir).runs`, whose property MKDIRS lazily; the
   `_all_run_records` cite is the glob-STYLE precedent only — that helper
   walks the journal-home namespace `<journal_home>/<repo_hash>/runs/*.json`,
   a different store; precision recorded in pre-implementation verification
   2026-07-07): tags a run declared (the sidecar's slug-validated `tags`
   field — already on the wire model, verified), `cmd_sha`, dates; lineage
   keys via `state/scopes.py::lineage_chain` + `state/run_sha.py`.
5. **Fingerprint ledgers** — `_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl`
   (determinism-fingerprint D-store): the envelope + evidence labels
   `{n, n_full, n_partial, scales, clusters}` QUOTED VERBATIM per lineage —
   the digest prints "envelope ±2% (n=4, main-scale)" from the ledger's own
   reduction (`state/determinism.py` envelope math), never recomputing or
   reinterpreting a number.

**Fleet mode** (`fleet: true`): the identical per-namespace walk over
`ops/attention_queue.py::discover_fleet_experiments` — glob `*/repo.json`
under the journal home, never `journal_dir()` (which mkdirs), skipped
namespaces counted `{repo_hash, reason}`.

**Query keys.** `tags` select by tag membership (a record matches when it
carries any queried tag OR a current conclusion retro-indexes it under
one); `lineage` is a `run_id` whose `lineage_chain` + `cmd_sha` select by
code identity — the fallback that needs no tags. Both may be given; the
union is disclosed per-source.

**`as_of` semantics — everything time-indexed.** `as_of` is an ISO
timestamp; the collector includes only records with `ts <= as_of`
(conclusions, looks, samples, decisions — every store already carries
`ts`). The digest is then "what was known as of that date" — reconstructible
history, the append-only dividend. **Regime vocabulary stays caller-side:**
`as_of` is a timestamp, never a named period ("2025H1", "post-vol-spike" are
caller words; the caller translates them to timestamps).

**Ordering:** conclusions newest-first (the point query's lead), then
per-tag activity newest-first, deterministic `(ts desc, kind, subject_id)`
total order — byte-reproducible render for a given store state (the D2
total-order discipline).

### E-render — the digest shape (code-rendered, sized for embedding)

`ops/evidence_render.py` — pure string work, no I/O, no `_wire` import (the
`ops/relay_render.py` / `ops/story_render.py` posture). The point brief:

```
evidence · tags: edge-x, rv-data · computed 2026-07-07T06:12Z · as_of=<...>
CONCLUSION 2025-11-14 · edge-x-2025h1 · cited a3f2c9d1 (verified) — <finding, verbatim>
  supersedes 1 earlier · tags: edge-x, rv-data
PRIOR WORK · 3 campaigns, 14 runs, 2 lineages · newest 2025-11-02 · 9 looks on rv-holdout
ENVELOPE · lineage 7be4… · ±2.1% rel (n=4: 3 full + 1 partial, scales: main, clusters: hoffman2)
UNTAGGED · 2 lineages matched by cmd_sha only (no tags declared — disclosed)
```

- Every line dated; every claim sha-cited; envelope lines quote the
  fingerprint ledger's evidence block verbatim, never a paraphrase.
- **Sizing:** newest conclusion per subject + per-tag counts + per-lineage
  envelope one-liners; older conclusions collapse to a disclosed count
  ("+2 superseded, +1 earlier conclusion — run evidence-brief for the
  full list"). Truncation is deterministic and DISCLOSED (no-silent-caps).
- The period digest renders the window's timeline (conclusions, campaign
  completions, look activity, fingerprint samples — dated one-liners) and
  ENDS with the unconcluded-campaigns list, each item dated by its
  completion ts — the standing invitation to close the loop.
- **No urgency, no recommendation, no interpretation prose** — the queue's
  D6 rule: wording composed from the records' own fields. "3 negative
  priors" is a count; "you probably shouldn't run this" is a sentence core
  never says.

### E-verbs — naming and wire shapes

Naming settled against the registry idiom (hyphenated mechanism nouns:
`run-story`, `attention-queue`, `notebook-status`, `verify-registration`):
**`evidence-brief`** (point, primary) and **`evidence-period`** (window).
`memory-brief` rejected — "memory" is a metaphor; "evidence" is the noun the
tree already uses (`evidence_digest`, `evidence_meets`). Two verbs rather
than one spec-dispatched verb: the two projections have different result
shapes and different leads (the run-story-vs-attention-queue precedent —
one verb per projection). Both `verb="query"`, `side_effects=[]`,
`idempotent=True`, `requires_ssh=False`, agent-facing, MCP-exposed.
Registry +2 (146 expected after the slate per
`docs/design/slate-sequencing.md` → 148; verify against
`hpc-agent capabilities` at implementation).

`_wire/queries/evidence.py`:

- `EvidenceBriefSpec {tags: list[str] = [], lineage: str | None = None,
  as_of: str | None = None, fleet: bool = False}` — at least one of
  `tags`/`lineage` required (an unkeyed point query is the browse non-goal).
- `EvidencePeriodSpec {since: str, until: str | None = None,
  tags: list[str] = [], fleet: bool = False}`.
- Results: `{computed_at, as_of?, conclusions: [...], activity: [...],
  envelopes: [...], unconcluded: [...] (period only), citations_status:
  [...], skipped: [...], cache: "hit" | "miss" | "disabled",
  render: <markdown>}` — `render` rides the result for verbatim relay (the
  `AttentionQueueResult` posture). No domain vocabulary in field names (the
  `_FORBIDDEN_FIELD_NAMES` walk, mirrored).

### E-cache — content-keyed over the walked stores' fingerprints

`state/evidence_cache.py`, the `state/describe_cache.py` posture
(opportunistic, fall-through on any I/O error, `HPC_NO_EVIDENCE_CACHE=1`
opt-out) with CONTENT keying replacing version keying:

- **Key = sha256 over the canonical JSON of
  `{pkg_version, spec fields, per-namespace store fingerprint}`** where the
  store fingerprint is the sorted list of
  `(relpath, mtime_ns, size)` for every file the collector would walk
  (globbed cheaply, no reads). Any append to any journal/ledger/sidecar
  moves an mtime → new key → recompute. `pkg_version` in the key means a
  render-logic upgrade invalidates for free (the describe-cache lesson —
  the earlier version-string trap is avoided by keying on content AND
  version, not version alone).
- Stored under `<journal home>/evidence_cache/<key[:16]>.json`; old keys
  are harmless kilobyte debris; **deleting the directory loses nothing**
  (enforcement row: cache-deleted output byte-equals cached output).
- mtime granularity is accepted honestly: a same-mtime same-size rewrite
  could serve stale ONCE — tolerable for an advisory digest whose remedy is
  re-run, and the reason the cache is never consulted by the APPEND gate
  (verification always live).

### E-embed — the greenlight seam

The briefs gain an additive `evidence` field = the point-query result for
the new work's declared scope tags + its lineage key (the snapshot
`attention`-embed posture: readers tolerate additive fields; no
`next_block`/chain change; `render_relay` untouched):

- **`campaign-greenlight`** (`meta/campaign/blocks.py` — verify the exact
  brief-construction symbol at implementation) — the primary seat: the
  once-at-start spec greenlight is where priors matter most.
- **`submit-s1`** (`ops/resolve_submit_inputs.py`, the S1 human boundary) —
  the per-run seat, same additive field.
- **The audit prelude** — no code seam: the `hpc-notebook-audit` skill
  prose gains one step (run `evidence-brief` for the elicited tags; relay
  `render` verbatim), because the prelude is a skill-driven flow, not a
  brief-emitting block.

Both code seats call `collect_evidence` + the render — never a private
re-collection (the one-collector row).

**FAIL-OPEN AT THE SEAT (specified explicitly — pre-implementation
verification 2026-07-07).** The embed must never mint a new failure mode in
the submit/greenlight path: the collector's tolerant-read covers expected
I/O noise, but a BUG (an unexpected exception anywhere in
`collect_evidence`, the cache, or the render) raised mid-greenlight would
turn an advisory digest into a submit refusal — the never-blocking pin
violated by accident rather than intent. Therefore each code seat wraps the
entire embed call in a broad guard: ANY exception degrades to
`evidence: {"unavailable": true, "reason": "<one line, exception class +
message>"}` — disclosed in the brief, logged, never propagated. The
greenlight/S1 decision surface (gates, `needs_decision`, `next_block`) is
byte-identical whether evidence collected, collected empty, or failed.
Collector failure is never a submit error; the remedy is running
`evidence-brief` directly, where the same failure IS loud (a dedicated
query verb may raise honestly — only the embedded advisory seats fail
open). The APPEND gate (E-shape) is the deliberate exception: citation
verification there is load-bearing and refuses loudly, and it never runs
inside a greenlight. Tag elicitation: the interview /
audit-prelude flows ASK the human for tags (free text, utterance-derived);
an empty answer is recorded as no tags, disclosed downstream — never
defaulted, never invented (the fabrication class).

### E-queue — the unconcluded-campaign collector

New `ops/attention_queue.py` kind `campaign-unconcluded`, class
`informational` (no verdict is PENDING — nothing is blocked; it is the
aging standing item E1(a) names). D5 route-through: the predicate is
`collect_evidence`'s unconcluded reduction (itself composing
`state/decision_journal.py::latest_decision` over campaign journals + the
conclusion journals' `concludes` sets) — the queue collector calls it,
never re-implements it (`inspect.getsource` pin, the module's standing
rule). `since` = the campaign's completion-brief ts, so the item AGES
honestly. Fan-out 0 (no encoded edge — a missing conclusion blocks
nothing, by E3). The item's `action` carries no prose beyond the
identity line.

### E6 — the registration seam (reserved, not built here)

A registration template can demand a conclusion TODAY via the generic
`attestation` prerequisite kind (`{scope_kind: "conclusion", scope_id:
<conclusion_id>}`, R3 table) — works day one with zero registration-kernel
changes. **Reserved for a future reviewed vocabulary change:** a first-class
`conclusion` member of `PREREQUISITE_KINDS` with `requires` like
`{tags: [...], newer_than: <ts>}` ("a current conclusion under tag X dated
after T"), checkable by identity + counting over the conclusion reduction —
the `evidence_meets` pattern. It is NOT added now: `PREREQUISITE_KINDS` is
closed and adding a member is a reviewed change (R3); the generic kind
covers the concrete near-term demand.

## Task waves (file-disjoint, Opus-sized — sequenced AFTER the slate)

This plan implements **after slate Phases 1–5 and proving run #10**
(`docs/design/slate-sequencing.md`). Hard sequencing dependencies inherited
from the slate: registration T3 (`compute_dossier_signature`) and
fingerprint T3 (`state/fingerprint_store.py`) are citation-resolver inputs;
registration T6/packs T8 land their scope kinds before our T7; registration
T8 and fingerprint T7 both edit `ops/attention_queue.py` before our T10;
E2/registration T7 edit `ops/decision/journal.py` before our T8. Standing
rules apply: regen commits strictly serial; enforcement-map edits
append-only and serialized; every wave ends regen → full suite → commit →
push → CI green. Every task lands with a fires+passes test pair.

**Wave A (parallel — new files only):**

- **T1** `state/evidence.py` (new) — `CITATION_KINDS` (closed,
  equality-pinned) + the citation-resolver dispatch table (each kind routes
  through its one existing definition; pure dispatch, the `check_chain`
  form — with the `dossier` resolver INJECTED by ops callers per the
  dispatch-placement note above; no `ops` import in this module, ever) +
  the conclusion record blocks/validation + the conclusion
  reduction (`current | superseded | revoked | absent`, routing through
  `state/attestation.py::reduce`; route-through `inspect.getsource`
  assertion) + `collect_evidence` (the E-collector walk, `as_of` filter,
  unconcluded reduction) + the canonical citations-sha helper
  (harness-contract canonical form). Tests: crafted journals/ledgers; every
  refusal fires; `as_of` excludes newer records; retro-indexing (a
  conclusion's tags surface untagged lineage work).
- **T2** `_wire/queries/evidence.py` (new) — the two specs + results
  (E-verbs). Tests: spec validation (empty point key refused; unknown
  fields; the `_FORBIDDEN_FIELD_NAMES` walk over the schemas).
- **T3** `state/evidence_cache.py` (new) — the content-keyed cache
  (E-cache). Tests: key moves on any walked-file mtime change; deleted
  cache → byte-identical result; `HPC_NO_EVIDENCE_CACHE=1` bypass.

**Wave B (after A, parallel — one new file each):**

- **T4** `ops/evidence_render.py` (new) — both digest renders (E-render).
  Tests: golden renders; byte-stability under dict-order shuffling;
  disclosed truncation; the unconcluded list terminates the period render;
  no interpretation vocabulary (AST/token pin: no "urgent", "recommend",
  "should" in the render source's literals).
- **T5** `ops/evidence_brief_op.py` (new) — the `evidence-brief`
  `@primitive` (point; fleet via `discover_fleet_experiments`, skipped
  accounting, non-creating — test asserts no directory created under a
  fresh journal home).
- **T6** `ops/evidence_period_op.py` (new) — the `evidence-period`
  `@primitive` (window). Both T5/T6: registry +1 each; ALL SIX regen
  scripts after the wave (the dev_regen_list lesson); primitive doc pages;
  `_SPEC_VERBS` inventory tails.

**Wave C (sequential — hot files, one at a time):**

- **T7** `state/decision_journal.py` — the `"conclusion"` scope kind + path
  branch (`.hpc/conclusions/`) + the `ScopeKind` wire literal + schema
  regen + contract tests in lockstep (the notebook-T7 / registration-T6 /
  pack-T8 precedent, serialized behind all of them).
- **T8** `ops/decision/journal.py` — `_assert_conclusion_authorship`
  (E-shape's three locks) + the revoke floor, wired beside
  `_assert_registration_authorship`. Fire tests per lock: fabricated
  citation sha, unresolvable citation, empty citations, bare ack, response
  missing the sha prefix, response missing the `conclusion_id`, an
  agent-tier response under a present utterance log.
- **T9** the greenlight embeds — `meta/campaign/blocks.py`
  (campaign-greenlight brief) and `ops/resolve_submit_inputs.py` (S1),
  additive `evidence` field via `collect_evidence` + the render (E-embed).
  Serialized behind whatever the slate left owning those briefs; two
  commits if the files are concurrently hot.
- **T-NB — the NEVER-BLOCKING pin (its own task, deliberately):**
  `tests/contracts/test_evidence_boundary.py::test_surfacing_never_blocks` —
  (a) mechanical: source scan of the embed call path (T9's seams + the
  render + `collect_evidence`) asserting no `raise` on any
  evidence-content-dependent branch (collection I/O tolerance excepted —
  those fall through, never up), and asserting the seat-level broad guard
  exists (the E-embed fail-open wrapper); (b) behavioral: a fixture
  namespace with ten negative-conclusion priors under the submitted tags
  greenlights with a byte-identical decision surface (same
  `needs_decision`, same `next_block`, same gate outcomes) as an empty
  namespace; (c) fault-injection (added 2026-07-07): with
  `collect_evidence` monkeypatched to raise (and, separately, a corrupted
  journal fixture), the greenlight still completes with the disclosed
  `evidence.unavailable` stub and an otherwise byte-identical decision
  surface — a collector bug can never become a submit error. This is the
  kill-ledger decision mechanized against future contributors.
- **T10** `ops/attention_queue.py` — the `campaign-unconcluded` kind
  (E-queue): `KIND_CLASS` entry, collector routing through
  `collect_evidence`'s reduction, D5-table row, route-through pin.
  Serialized behind registration T8 + fingerprint T7 (the slate's named
  collision).
- **T11** `tests/contracts/test_evidence_boundary.py` — the remaining
  enforcement rows (below) + TOY fixtures only (widget-lineage
  conclusions; never harxhar/quant vocabulary — the domain-packs
  toy-fixture rule).
- **T12** skill prose — the interview / audit-prelude tag-elicitation step
  + the evidence-brief relay step (`hpc-notebook-audit`, the interview
  skill, `hpc-campaign`); skill lints (`lint_skills.py`,
  `lint_no_blocklisted_commands.py`, `lint_no_raw_ssh.py`).
- **T13** this doc — status flip + drift log.

## Enforcement rows (accrue to `docs/internals/engineering-principles.md`)

| Rule | Enforced by | Fires when |
|---|---|---|
| **NEVER-BLOCKING surfacing (the anti-stage-0-dedupe pin, load-bearing):** no evidence content ever gates, refuses, or reshapes a greenlight; ten negative priors greenlight byte-identically to none | T-NB (source scan + behavioral byte-equality test) | any raise/gate/branch keyed on prior-evidence CONTENT lands in the embed path — the kill-ledger decision being re-litigated in code |
| ONE collector: both verbs, both embeds, and the queue kind route through `state/evidence.py::collect_evidence`; no surface re-walks or re-reduces | route-through `inspect.getsource` pins in the T-suite (the attention-queue D5 form) | a surface re-implements the walk, the `as_of` filter, or the unconcluded reduction |
| No LLM in the render: digests are code-composed from record fields; deterministic total order; byte-reproducible | T4 golden + byte-stability tests + the no-interpretation-vocabulary pin | free prose, urgency words, or nondeterministic ordering enters the render path |
| Citations must verify at append: every citation resolved server-side through its kind's one resolver; `content_sha` bound via `attestation.bind`; a caller-asserted sha is never trusted-then-recorded | T8 fire tests (fabricated sha refused; unresolvable citation refused) | the gate trusts a resolved-field sha (the receipt-laundering hole, at the memory boundary) |
| Conclusion attestations route through the ONE kernel — bind, reduce, winner-selection never re-inlined | T1 route-through assertions (the accruing-member rule on the attestation row) | a conclusion path bypasses `state/attestation.py::bind`/`reduce` |
| No agent-authored tags or conclusions: tags are shape-validated caller data elicited from human utterances; the conclusion attestor is always human; no code path appends block `"conclusion"` | T8 authorship fire tests + the no-affordance registry pin (no conclude/conclusion mutate verb) + `"conclusion"` absent from every code-writer block set | a mechanical writer gains the block, a default tag lands in core, or a verb-shaped affordance appears |
| The index is disposable: deleting the cache changes no output; no digest file, no watermark, no persisted queue | T3 cache-deletion byte-equality test + a write-probe over the `ops/evidence_*` modules | a persisted projection becomes load-bearing (a second source of truth) |
| `CITATION_KINDS` is closed and mechanism-only; core never interprets a tag, a finding, or a metric | equality pin + no-literal-vocab AST pin over `state/evidence.py` + `ops/evidence_*` + the wire-schema `_FORBIDDEN_FIELD_NAMES` walk | a domain word becomes a kind, or a core branch reads tag/finding content for meaning |
| Conclusions are required nowhere at creation: no gate in core demands one except a caller's declarative registration prerequisite | registry/gate scan in T11 (no core seat conditions on conclusion presence) | a submit/campaign/audit gate grows a "must have a conclusion" branch |

## Boundary-drift flags (the Q1 watch list — written before implementation)

- **No browse verb — a RECORDED NON-GOAL.** No `evidence-search`, no
  `evidence-list-all`, no faceted explorer, no served page. THE AGENT IS
  THE BROWSER: exploratory questions are answered by the agent composing
  point queries and reading journals — the attention-queue precedent
  against dashboard-thinking. Rationale, recorded: a browse surface's value
  is ranking and summarization, which is exactly the prose this design bans
  from core; the moment core ranks "interesting" evidence it is fabricating
  relevance. Pressure for browse is routed to: more point queries, the
  period digest, or caller-side tooling over the wire results.
- **No urgency or recommendation prose in any digest.** Counts, dates,
  shas, verbatim envelope evidence — never "stale", "promising",
  "don't re-run". The D6/D2 no-fabricated-urgency rule, verbatim.
- **No agent-authored tags or conclusions, ever.** An agent-invented tag is
  index poisoning — the fabrication class. Softening the conclusion
  authorship bar (waiving the sha prefix, admitting a bare ack) is
  rubber-stamp fatigue announcing itself; soften only via richer
  harness-captured utterances (the D-attention flag, verbatim).
- **Core never interprets tag meaning.** A tag is identity; "holdout",
  "edge-x", "rv-data" are caller words. The moment a core branch keys on a
  tag's TEXT, the line is crossed.
- **The index stays disposable.** If a consumer starts requiring the cache,
  or the cache grows fields the live recompute lacks, it has become a
  second source of truth — delete-and-recompute must stay a no-op forever.
- **Conclusions never block.** The never-blocking pin covers the greenlight
  path; watch the same pressure at every OTHER seat: the queue item stays
  `informational`, the period digest's unconcluded list stays a list, and
  no chain ever parks on "conclusion missing". Demanding a conclusion is
  exclusively a caller's declarative registration prerequisite (E6).
- **`as_of` stays a timestamp.** Named regimes, market periods, and "since
  the last conclusion" sugar are caller vocabulary — core compares
  timestamps, never names periods.
- **Fleet stays a projection.** No cross-repo authoritative evidence store,
  no merged global index file — fleet mode is the same walk over discovered
  namespaces, recomputed, skipped-accounted.

## Related docs

- `docs/design/notebook-audit.md` — the attestation formulation this
  instantiates; the hand-off pattern this document follows.
- `docs/design/registration-kernel.md` — the R6 sha-prefix bar (reused),
  R2 live-store recompute (reused), R7 dated-evidence posture, the E6 seam.
- `docs/design/determinism-fingerprint.md` — the envelope evidence shapes
  the digest quotes verbatim; `evidence_meets` (the declarative-demand
  pattern E6 mirrors).
- `docs/design/attention-queue.md` — the D5 route-through collector rule,
  fleet discovery, the no-urgency render discipline.
- `docs/design/run-story.md` — the period digest's projection sibling.
- `docs/design/slate-sequencing.md` — this plan slots AFTER Phase 5 and
  proving run #10; the hot-file serialization it inherits.
- `docs/internals/harness-contract.md` — the sha canonicalization and the
  capability-1 authorship tiers.
- `docs/internals/engineering-principles.md` — the Q1 boundary the flags
  patrol; the enforcement maps the rows accrue to.

## Implementation drift log

- **2026-07-07 (pre-implementation verification, adversarial review — three
  corrections applied in place, all against the live tree):**
  1. **The embed fail-open posture made explicit (the latent defect).** The
     plan pinned never-blocking against evidence CONTENT but did not say
     what a collector/render EXCEPTION does mid-greenlight — a bug in
     `collect_evidence` would have raised straight into the submit path, a
     brand-new failure mode at the S1/greenlight seats. E-embed now
     specifies the seat-level broad guard (`evidence.unavailable` stub,
     disclosed, never propagated) and T-NB gains the fault-injection leg
     (c).
  2. **Citation-dispatch layering corrected.** T1 as written put the
     `dossier` resolver dispatch (→ `ops/export_dossier.py::
     compute_dossier_signature`) inside `state/evidence.py`; the state
     substrate imports no `ops` module anywhere in the tree, and the
     registration precedent this doc cites actually splits state-pure
     composer / ops-side checker dispatch. The dossier resolver is now
     injected by ops callers; the T8 seat's alias-import spelling noted
     (subject files reach ops-root modules only via
     `from hpc_agent.ops import X` under `scripts/lint_subject_imports.py`).
  3. **Run-sidecar glob precision.** `_all_run_records` walks the
     JOURNAL-HOME namespace, not `.hpc/runs/`; and `RepoLayout.runs` mkdirs
     on access, so the collector globs `.hpc/runs/*.json` directly.
     Verified the sidecar wire model already carries slug-validated `tags`
     and `cmd_sha` — E2's vocabulary needs no new sidecar field.
  - Cite-integrity re-verified same pass: `state/scopes.py::validate_tag`/
    `lineage_chain`/`count_prior_looks`/`record_look`,
    `state/decision_journal.py::latest_decision` + the `SCOPE_KINDS`/
    `decisions_path` branch shape, `.hpc/campaigns/<id>/decisions.jsonl`
    (`meta/campaign/dirs.py::campaign_dir`), `<tag>.looks.jsonl`,
    `ops/attention_queue.py::discover_fleet_experiments` (returns
    experiment dirs — fleet impedance OK), `state/describe_cache.py`
    (`HPC_NO_DESCRIBE_CACHE` precedent), `RunIdStrict`,
    `_wire/actions/decision_journal.py::ScopeKind`, the three skill lints,
    and registry 141 (`operations.json`; 141→146 slate→148 here is
    arithmetic-consistent with `docs/design/slate-sequencing.md`). The
    fingerprint-ledger path and evidence-label vocabulary quoted here match
    `docs/design/determinism-fingerprint.md` verbatim. `st_mtime_ns` is
    available on win32 (`os.stat`), so the E-cache key is cross-platform.

- **2026-07-08 (Wave A/B/C implementation — deviations recorded as they landed):**
  1. **Run tags ride the sidecar's existing `scopes` key (E2), not a new
     `tags` field.** The collector reads a run's declared tags from the run
     sidecar's `scopes` list (`state/runs.py` write model; `state/evidence.py::
     collect_evidence` step 4) — the E2 vocabulary needed NO new sidecar field,
     confirming the pre-implementation verification. The two greenlight/S1 embed
     seats likewise source tags from `scopes` (the campaign manifest's
     `scopes`/`tags`, defensively; the sidecar's `scopes` at S1).
  2. **The collector RE-DERIVES three predicates inline (non-creating), rather
     than calling the state helpers.** `collect_evidence` re-implements the
     scope-lock scan, the fingerprint admission rule (`_sample_admitted`), and
     the decision-journal path build — because the canonical helpers
     (`is_scope_locked`, `fingerprint_store.compute_admitted_flags`,
     `decision_journal.decisions_path`) reach through `RepoLayout`, whose `.hpc`
     / `.runs` properties `mkdir` on access, incompatible with the collector's
     non-creating pin. `lineage_chain`/`read_samples` (which touch the
     journal-home store, not this namespace's `.hpc`) are still routed through.
     **Unification debt:** the re-derived admission rule and the store's
     `_is_admitted` are two copies of one predicate — a future refactor should
     give `fingerprint_store` a non-creating admission reader both call.
  3. **The canonical citations sha reuses `determinism.canonical_sha`.**
     `citations_content_sha` routes through the ONE harness-contract
     canonicalization already in `state/determinism.py` rather than a fourth
     local copy. **Canonical-sha unification debt — RESOLVED (2026-07-09, P-S1):**
     the sibling copies are gone. `data_manifest.manifest_doc_sha` now routes
     through `determinism.canonical_sha` and
     `fingerprint_store.content_sha_over_payloads` through
     `determinism.compute_content_sha` (their `_canonical_json` copies deleted),
     pinned byte-for-byte by unit tests. See the `data-manifest.md` drift log for
     the left-out lanes (`run_sha`, `provenance_manifest`, the `conformance*`
     copy — a later pass).
  4. **T5/T6 verbs re-render rather than routing through `render_brief` (T4).**
     `ops/evidence_brief_op.py` / `ops/evidence_period_op.py` carry their OWN
     `_render` / line-builder helpers instead of calling
     `ops/evidence_render.py::render_brief`/`render_period`. Both verbs DO route
     through the ONE `collect_evidence` (the one-collector row holds, T11-pinned);
     the duplication is only in the pure string RENDER. **T5/T6
     shared-projection-helper debt — PARTIALLY RESOLVED (2026-07-09):** the
     genuinely byte-identical collection→WIRE projection subset now lives in
     `ops/evidence_project.py` — `project_envelope_lines` (the envelope-line loop,
     formatter injected) and `apply_evidence_order` (the fleet total-order sort).
     Both verbs route through it; behavior byte-identical (full test files pass
     unchanged). The genuinely-different pieces stay local (`_conclusion_lines`
     sha truncation, `_activity_lines` roll-up, `_citation_lines` verified
     predicate). STILL OPEN: collapsing the verbs' markdown RENDER helpers with
     `ops/evidence_render.py` (a separate `list[str]` renderer) — a later pass.
  5. **T9 embed centralized into `ops/evidence_embed.py` (a new module).** The
     plan named two seats each wrapping the embed; both now call ONE shared
     helper `build_evidence_embed` that owns the broad fail-open guard (the
     never-blocking pin lives in one auditable place — T-NB scans it). The two
     seats (`meta/campaign/blocks.py::_digest_spec`,
     `ops/resolve_submit_inputs.py`) call it; no dossier resolver is injected at
     the read seats (a dossier citation DISCLOSES at read, never raises).
  6. **Regen deferred (Wave rule: note debts, no regen).** The `ScopeKind` +1
     kind (`conclusion`) and the new `ResolveSubmitInputsResult.evidence` field
     both want a schema regen: `schemas/append_decision.input.json` (its
     `scope_kind` enum already omitted `pack` — a PRE-EXISTING deferred debt —
     and now also `conclusion`) and `schemas/resolve_submit_inputs.output.json`.
     Run the six regen scripts (`[[dev-regen-list]]`) before release.

(Populate per further deviation, each with its recorded reason, when
implementation lands. The `docs/design/notebook-audit.md` drift log is the
form to follow.)
