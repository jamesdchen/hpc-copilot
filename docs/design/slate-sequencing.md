# The slate sequencing plan — implementation order for the six planned features

**Status: ACTIVE (2026-07-07).** The master order for implementing the planning
slate (`domain-packs`, `registration-kernel`, `determinism-fingerprint`,
`conformance-kit`, `mcp-elicitation`; `connection-broker`'s Option 1 is already
IMPLEMENTED). Derived from the cross-doc coherence review's wave-collision
table: the six plans are individually file-disjoint per wave, but ACROSS plans
they share hot files, and this document is the one place that serializes them.
Cite `path::symbol`, never line numbers.

## Standing rules (apply to every phase)

- **Regen commits are strictly serial** — one plan's registry/regen artifacts
  (`operations.json`, schemas, `_verb_module_map`, `docs/generated/*`) land and
  bake before the next plan's verbs enter the tree (the 0.8.0 lesson at 5×;
  cross-slate registry sum = 146 from the 141 @ `e1e9ab27` baseline).
- **Enforcement-map + `test_primitive_remediation.py` edits are append-only
  and serialized** across concurrent agents (re-read + retry on conflict).
- **Every phase ends with**: the six regen scripts → full suite → commit →
  push → CI green, before the next phase dispatches.
- Scope kinds have NO real ordinal (`SCOPE_KINDS` is a frozenset): `"pack"`
  and `"registration"` land in whichever phase order executes; the docs'
  ordinal language is nominal.

## The order

**Phase 0 — tonight's fix waves (in flight).** Adversarial findings F1–F8
(the ingest utterance-write laundering close, auto-clear recorded-config-only,
the snapshot attention/watermark reorder, the leverage terminal filter, the
resolver fallback, the `renders` dossier noun, posix relpaths, receipt-prose
honesty) + the terminal-key fix + the coherence doc fixes. Bake, full suite,
push, CI.

**Phase R10 — proving run #10.** Envs re-refreshed first (the wheel moved
repeatedly). Opens with the audit prelude (`hpc-notebook-audit` on a harxhar
template — the substrate's first live exercise), then the quant campaign.
First live test of: the detached status watch (zero unattended SSH), the
trusted-display renders, journaled receipts, scope-lock look counts, the
doctor scan, campaign blocks over MCP. Fixes from the run land before Phase 1.

**Phase 1 — MCP elicitation (E1–E7).** Self-contained and small; goes first
because **E2** (the machine-readable authorship-evidence marker in
`ops/decision/journal.py`) must precede registration T7 (which adds its own
gate to the same file and inherits the marker path in its fire tests), and
**E5** (contract re-pins) must precede conformance-kit K10.

**Phase 2 — the registration kernel (T1–T10).** The product-critical plan.
Internal ordering: **T3 first** (the `compute_dossier_signature` pure
refactor of `ops/export_dossier.py` — it unblocks fingerprint T8 and packs
T10, which add store nouns on top); then Wave A/B; T6 (the `"registration"`
scope kind) freely — packs T8 serializes behind it; T7 (the authorship gate)
after E2 per Phase 1.

**Phase 3 — the determinism fingerprint (T1–T11).** After registration
because: T8 (the disclosure dossier noun) builds on registration T3's
refactor; T7 (the `reproduction-needs-verdict` attention collector)
serializes behind registration T8's fan-out edges in
`ops/attention_queue.py`; and T4 (the double canary in `ops/submit_flow.py`)
must land before packs T9 touches the same file. The fingerprint's evidence
tiers then have real data accruing before packs/kit consume them.

**Phase 4 — domain packs (T1–T13 + the F10 interview task).** T8 (the
`"pack"` scope kind) behind registration T6; the InterviewSpec `packs` block
task behind v1.6 (landed); T10 (the two store nouns) behind registration T3
and fingerprint T8 (three `_EXPECTED_SOURCES` pair-edits, serial); T9 (the
`submit_flow` gate seat) behind fingerprint T4. Packs land before the kit so
harxhar's first pack can be a kit-era fixture.

**Phase 5 — the conformance kit (K1–K10).** Last, deliberately: K3's
predicateType table derives from the FINAL `DOSSIER_SOURCES` (`renders`
already landed in Phase 0; phases 2–4 add the disclosure noun and the two
pack nouns on top); K10's
version stamp lands after E5's re-pins; and the kit then certifies the two
reference adapters against the finished surface. Publishing mechanics
(contract SemVer, the version constant K10 owns) close the slate.

## The post-slate phases (added pre-implementation verification 2026-07-07
## — the four later plans share three hot files and needed their own order)

All four are PLANNED docs verified tonight; they serialize on
`ops/decision/journal.py`, `ops/attention_queue.py`, and
`state/decision_journal.py::SCOPE_KINDS` (which gains up to four more
kinds across them — pack and registration land in the slate phases; the
rest here, ordinals nominal throughout):

**Phase 1a — the data manifest verb** (`docs/design/data-manifest.md`,
user-ruled 2026-07-07). The `data-manifest` verb + greenlight-brief
disclosure only — self-contained, registry +1. Its OTHER two seams land
elsewhere by hot-file serialization: the sidecar data-identity echo +
the fingerprint's third drift dimension land INSIDE Phase 3 (they touch
`submit_flow` / the sample-admission model); the `manifest-current`
prerequisite kind is one Phase-2 vocabulary row.

**Phase 6 — evidence memory** (`docs/design/evidence-memory.md`). First
among the four: challenge-attestation reuses its `CITATION_KINDS`
resolver table and its T-NB never-blocking form; its `"conclusion"` scope
kind lands here.

**Phase 7 — live-conformance** (`docs/design/live-conformance.md`).
Directly after the registration/fingerprint machinery it amends is warm:
the `review_horizon` additive amendment (T6/T7), the registration-journal
block-family additions (`registration-review`, `conformance-verdict` —
coordinated with registration R6's family set), the fingerprint-envelope
helper reuse. No new scope kind.

**Phase 8 — challenge-attestation** (`docs/design/challenge-attestation.md`).
After evidence memory (resolvers) and after fingerprint T12 (the
`reproduction-verdict` authorship gate its promotion seam leans on); the
`"challenge"` scope kind lands here.

**Phase 6.5 — onboard-by-reproduction** (`docs/design/onboard-by-reproduction.md`,
user-ruled 2026-07-07). After Phase 3 (fingerprint receives samples) AND
Phase 6 (optional conclusion composition). Thin: verify-reproduction's
external-baseline mode, the `claim-check` receipt kind + its
anti-laundering enforcement row, the orchestrating skill. No new verb
expected; a verb would be a registry-arithmetic drift note in its doc.

**Phase 9 — multi-human** (`docs/design/multi-human.md`). Deliberately
LAST of the four: it edits the authorship gates in
`ops/decision/journal.py` after every other plan's gate additions are in
(registration T7, elicitation E2, challenge's gate), and — per the
whichever-lands-second rule both docs record — Phase 9 EXECUTES the
resolver≠challenger comparison (challenge T5's reserved follow-up, ruled
in multi-human MH7).

## The cross-plan reuse ledger (2026-07-07 — one definition each; implementers
## check here before building)

- **JSONL append**: `infra/io.py::append_jsonl_line` (exists) — fingerprint
  T3 and the conformance ledger RE-POINT onto it; never a third copy.
- **The order-statistics envelope helper** (fingerprint T1a) — shared by
  live-conformance's comparator; one definition.
- **`CITATION_KINDS` resolvers** (evidence memory T1) — challenge-attestation
  target resolution reuses the table; one resolver set.
- **The fleet-discovery glob**: `ops/attention_queue.py::
  discover_fleet_experiments` — evidence memory + any fleet reader reuse it.
- **The fake-client duplex rig**: built ONCE (elicitation E1's test harness)
  and consumed by the conformance kit's reference adapter — two plans, one
  rig.
- **Env-flag reads**: `infra/env_flags.py` — multi-human's `HPC_ACTOR`
  routes there (and cures its stale docstring).
- **Digest formatting**: the run-story/attention render helpers — the
  evidence-brief/period renderers reuse them; land the punch-list
  three-way count-summary fold BEFORE Phase 6.
- **Block-reader idiom**: packs' manifest/opt-in reading mirrors
  `ops/notebook_gate.py::_read_audited_source` — the one interview-block
  reader shape.
- **Canonical-JSON sha**: pending punch-list P-S1's helper — every NEW sha
  site (conclusion content_sha, conformance payloads, challenge targets,
  the data-manifest DOC sha) uses it from day one; only legacy sites carry
  shims. (Manifest FILE-CONTENT shas are raw-byte hashes — a different
  discipline, allowlisted separately.)
- **The never-blocking contract-test shape**: whichever lands FIRST of
  evidence-memory's surfacing pin (Phase 6) and the data-manifest
  disclosure pin (Phase 1a) writes the shared test helper (assert a code
  path contains no raise/gate branch); the second REUSES it — one
  assertion idiom, not two hand-rolled AST walks.
- **Registry arithmetic (updated 2026-07-07 late)**: baseline is now
  **142** @ `326a9124` (`notebook-scaffold-template` landed same night);
  the slate's +5 stands; Phase 1a adds +1 (`data-manifest`), and
  `docs/design/draft-context.md` adds +1 (`notebook-draft-context`,
  freestanding post-run-#10) → expected post-slate sum **149**. Phase 6.5
  adds none. Draft-context's V-LINK view embed serializes with the next
  view-touching phase (canon bump).
- **Outsource presumptions recorded**: federation rides GIT (append-only
  content-addressed journals are git's native case — no sync daemon, no
  CRDT libs); DSSE envelope CONSTRUCTION rides securesystemslib/in-toto
  when signing arrives (kit lane); verified identity, if ever demanded,
  rides SSH-key record signing — never core PKI. Statistics above the
  order-statistics floor: scipy/statsmodels PACK-SIDE with owned alphas.

**Next planning target beyond all phases: none in core.** The
jurisdiction is mapped (the scope doctrine: trust mechanisms, never
operations; observe/judge/route, never actuate). Post-Phase-9 growth =
consumers: packs, harnesses, domains, teams — plus the standing punch
list in the machine-local memory.
