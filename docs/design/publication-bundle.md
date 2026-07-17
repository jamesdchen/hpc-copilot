---
status: design
audience: maintainer — the reproducibility program's publication-time deliverable
---
# The publication bundle — one offline-verifiable artifact a scientist ships with a paper

**Status: DESIGN (2026-07-17).** The concrete embodiment of the reproducibility
thesis's public claim (`docs/design/reproducibility-thesis.md` §5): a single
artifact a scientist attaches to a manuscript that says *"here is the proof my
table is reproducible — the minimal recipe, the signed provenance, the audit of
every cited number, the sealed evidence,"* checkable by a third party with no
cluster access. This memo scopes it: what it IS, the verb shape, what it PROVES
vs ASSERTS honestly, the offline-verify story, the doctrine, and the build
decomposition. It COMPOSES the shipped verbs (`export-dossier`, `extract-recipe`,
`cite-check`, `provenance-manifest`, `export-attestations`) — it reinvents
nothing. Cite `path::symbol`, never line numbers. Where this memo and the code
disagree, the code and its enforcement-mapped tests win.

The pieces the thesis names already exist as separate verbs:

| Piece | Verb / symbol | What it seals |
|---|---|---|
| the minimal runnable recipe | `ops/extract_recipe.py::extract_recipe` | the messy→clean walk: minimal run-set, exclusions disclosed+counted, fingerprints (incl. wheel + env-lock), a signature over ONLY the minimal set, the re-derivation steps, gaps |
| the sealed evidence + the recipe | `ops/export_dossier.py::export_dossier` | every on-disk store the run left, copied byte-verbatim, integrity-manifested — and, since BR-4, the recipe as a first-class sealed member |
| the signed provenance manifest (v3) | `ops/provenance_manifest.py::provenance_manifest` | one signable `{code, data, env, env-lock, wheel, params}` record per campaign run |
| the number → paper audit | `ops/cite_check.py::cite_check` | every manuscript number bucketed `matched` / `uncitable` against the SEALED table |
| the portable attestations | `ops/export_attestations.py::export_attestations` | the same sealed entries as in-toto Statements in DSSE envelopes, verifiable without hpc-agent |
| the reproduction act | `ops/verify_reproduction.py::verify_reproduction` | the fresh-run comparison (and the anti-laundering claim-check split) |

A scientist finishing a paper wants ONE artifact that packages all of it, with
ONE seed and ONE offline-verify entry point — not four separate verb outputs and
a README explaining how they relate. That artifact is the publication bundle.

---

## 1. What the bundle IS

**One-sentence definition.** The publication bundle is the single,
offline-verifiable artifact a scientist ships with a paper — the sealed dossier
(evidence + the minimal runnable recipe), the signed provenance manifest, and a
cite-check audit of every number the manuscript cites against the sealed table —
under one top-level `VERIFY` manifest that classifies each reproducibility link
as **mechanical-or-disclosed** and can be checked by a stranger with no cluster
access.

Concretely, a bundle is a `.zip` composing:

1. **the sealed dossier evidence** — via the ONE dossier gather
   (`ops/export_dossier.py::compute_dossier_signature`), which ALREADY contains
   the derived clean-reproduction **recipe** member (BR-4) alongside the sidecar,
   journals, briefs, harvested aggregates, and the determinism-fingerprint
   ledger, every byte copied verbatim;
2. **the signed provenance manifest** (`provenance_manifest` v3 —
   `PROVENANCE_MANIFEST_SCHEMA_VERSION = 3`), which the dossier does NOT seal
   today (see §6);
3. **the cite-check report** — `cite_check`'s per-number audit of the manuscript
   against the sealed `metrics_aggregate.json` values (the ONE member that
   requires a new input, the manuscript);
4. **the top-level `VERIFY` manifest** — the per-link
   MECHANICAL/DISCLOSED/ABSENT classification (lifted verbatim from the thesis
   §3), a code-emitted honest verdict, the union of every disclosed gap, member
   pointers, and the offline-verify recipe (the exact canonicalization a stranger
   recomputes);

all sealed under ONE `bundle_sha256` (reusing the ONE signable digest,
`ops/provenance_manifest.py::manifest_signature`), the dossier's own seal
discipline one level up.

### What is genuinely NEW vs already-covered-by-dossier

Be honest: most of the bundle already exists inside a dossier. The dossier is
NOT re-implemented — it is composed.

**Already covered by the dossier (composed, not rebuilt):**

- the **minimal runnable recipe** — sealed as the `recipe` member since BR-4
  (`export_dossier.py::_gather_recipe` invokes `extract_recipe` at seal time);
- **all evidence bytes** — sidecar, decision/brief/terminal journals, journal
  record, scope journals, harvested aggregates, the fingerprint ledger, audited
  source + renders + pack trail (the closed `DOSSIER_SOURCES` set);
- the **self-attesting seal + offline sha-recompute** — `bundle_sha256 =
  manifest_signature(entries)`, each entry `{source, path, sha256, bytes}`
  (already checkable offline; see §4).

**Genuinely new to the bundle:**

1. **the cite-check report over the MANUSCRIPT.** The dossier seals the run's own
   evidence but has never seen the paper. The audit of every cited number against
   the sealed table is the ONE member sourced from a new input, and it is the
   whole point of "publication time" — it closes the last-mile transcription link
   the thesis §3.7 names ABSENT-into-the-manuscript. (`cite_check` exists; the
   bundle is the first thing to SEAL its report as durable evidence rather than
   print it to a reviewer's terminal.)
2. **the signed provenance manifest as a sealed member.** The dossier does NOT
   seal `.hpc/provenance/<campaign>.json` — `DOSSIER_SOURCES` has no
   provenance-manifest noun. The recipe's fingerprints only *prefer* the signed
   values when a manifest happens to be on disk (`extract_recipe::_signed_field`);
   the signed artifact itself never travels with the evidence. The bundle folds
   it in so the wheel-sha + env-lock-sha are signature-attested *inside the
   sealed artifact*, not merely referenced.
3. **the top-level `VERIFY` manifest** — the honest per-link classification, the
   code-emitted verdict, the ONE verify entry point, and the *union* of every
   disclosed gap across the whole chain (dossier absent-stores + recipe gaps +
   cite-check's uncitable bucket + the env/data disclosures). No single existing
   verb produces this cross-cutting honest ledger; each discloses its own slice.
4. **the composition itself** — one seed, one artifact, one verify story. Today a
   scientist runs `export-dossier`, `provenance-manifest`, `cite-check`, and
   `export-attestations` as four disjoint acts producing four outputs; the bundle
   is the single download a reviewer receives.

---

## 2. The verb shape

**`export-bundle` — a SIBLING of `export-dossier`, not an extension.** This is
the exact `export-attestations` precedent (`docs/design/conformance-kit.md`
D-K4): a composition/portability layer that consumes the ONE dossier gather and
projects/adds on top, never a modification of `export-dossier`'s run-scoped
contract. `export-dossier`'s spec is `{run_id, include_lineage?, output_path?}`
— it has no manuscript input and must not grow one (a manuscript is a
publication-scoped concern, not a run-scoped one). The bundle is where the
manuscript belongs.

Mirror `export-dossier`'s decorator: `verb="mutate"`, a single local
`file_write` side effect, **no SSH**, `agent_facing=True`, idempotent on the
seed. ("Read-only" in the reproducibility sense the task means — it observes and
seals, never actuates a cluster, never interprets a metric — realized exactly as
`export-dossier` realizes it: pure local reads + one seal write. The
`mcp_server.py` curated-catalog note loosely calls the exporters "read-only
query" verbs; the authoritative decorator is `mutate`.)

**Spec (`ExportBundleSpec`, flat, no domain vocabulary):**

- **the seed** — exactly one of `run_id` / `campaign_id` / `aggregate_path` (the
  `extract_recipe._resolve_seed` contract, reused verbatim — the same contract
  `cite-check` already reuses);
- **the manuscript** — optionally one of `manuscript_text` / `manuscript_path`
  (the `cite_check` input contract). ABSENT is legal (disclose-not-gate): a
  bundle with no manuscript still seals the dossier + recipe + signed manifest
  and records a `cite-check-skipped` gap (R-B2);
- `include_lineage?` — widen the dossier gather to the supersession chain
  (`export-dossier`'s own flag, threaded through);
- `output_path?` — default `<experiment>/_dossier/<seed>.bundle.zip`.

**Composition (the house rule — extend, don't reinvent):**

```
compute_dossier_signature(seed_run, include_lineage)   # the ONE gather → evidence + recipe member
build_provenance_manifest(exp, campaign) + manifest_signature(...)   # the signed manifest, in-memory
cite_check(exp, spec={seed, manuscript})               # the report member (when a manuscript is supplied)
manifest_signature(bundle_entries)                     # the ONE top-level seal
```

The dossier stores are **never walked twice** — the bundle consumes
`sig.entries` + `sig.write_map` exactly as `export-attestations` does, adds the
two new members and the `VERIFY` manifest, path-sorts, and seals over the union.

**`verify-bundle` — the offline check (see §4 + R-B1).** The load-bearing offline
path needs no hpc-agent at all (the `VERIFY` manifest is self-attesting; stock
DSSE tooling round-trips the attestations member). `verify-bundle` is a thin
convenience `query` verb for a stranger who *does* have hpc-agent installed: it
recomputes every member sha + the `bundle_sha256`, re-runs
`verify_provenance_manifest`, re-derives the per-link classification, and emits
`pass` / `disclosed-gaps` — the one thing stock tooling cannot do (stock tooling
verifies digests; it cannot re-classify a reproducibility link).

---

## 3. What it PROVES vs ASSERTS — honestly

The bundle's whole credibility rests on never overclaiming. It is a
**proof-of-what-is-mechanical + an honest ledger-of-what-is-disclosed**, never a
"reproducibility certificate."

**What the bundle PROVES** (mechanical; a stranger confirms with sha recompute
alone, trusting the scientist for nothing):

- **internal consistency** — every sealed member's bytes hash to its recorded
  `sha256`, and `bundle_sha256` seals the set (the dossier's own seal discipline,
  one level up);
- **the minimal-set attestation** — the recipe's `recipe_signature`
  (`extract_recipe`, `manifest_signature` over ONLY the minimal run-set) covers
  the fingerprints of exactly the runs that produced the table, with canary /
  superseded / dead-end members excluded-and-counted;
- **the signed provenance** — `verify_provenance_manifest` re-hashes the manifest
  body as written; a flipped wheel-sha, env-lock-sha, or a null-marker turned
  into a value breaks the match;
- **the transcription-fidelity floor** — every `matched` cite-check finding is a
  manuscript digit that EQUALS a sealed `aggregated_metrics` value under the
  faithful-render tolerance (`verify_relay.match_number`). Those numbers in the
  paper ARE the sealed table's numbers.

**What the bundle ASSERTS / DISCLOSES** (inherited from the chain, never
laundered into a proof). The bundle inherits **every** disclosure — it can be no
more honest than its weakest link, and it says so:

- **data opt-in** — an undeclared run writes both data fields `null` and is
  invisible to data-drift attribution (thesis §3.1, `state/data_manifest.py`).
  The bundle classifies the data link **DISCLOSED (opt-in)**, never MECHANICAL,
  when the contributing runs did not declare inputs;
- **environment drift** — `env_lock_sha` is captured and now signed (v3) but the
  full-environment identity remains weak and `env_hash` is never gated (thesis
  §3.3). The bundle classifies environment **DISCLOSED**, surfacing
  `env_lock_status` verbatim;
- **cite-check's uncitable bucket** — a manuscript number with no sealed backing
  is `uncitable` with a `nearest_chain_value` CONTEXT hint; it is NOT called a
  typo and NOT called a mismatch (the v1 two-bucket ruling,
  `docs/design/cite-check.md`). The bundle carries the uncitable count as a
  disclosed fact, never as a failure;
- **the recipe's own gaps** — `table-run-set-link-absent` (a pre-Task-1 table),
  `operator-bypass` (`source: operator-settled` — numbers code-computed,
  provenance human-asserted), `pack-csv-opaque` (a pack `*.csv` is an OPAQUE
  citation, never parsed);
- **the dossier's absent stores** — any expected store not on disk rides through
  as a `gap`.

**The refusal to overclaim is mechanical.** The `VERIFY` manifest carries the
thesis §3 per-link classification and the top-level verdict is a **code-emitted**
template filled by that classification (the `CLAIM_CONSISTENT_SENTENCE`
precedent — trusted code, never LLM-composed). It **never** stamps
"REPRODUCIBLE" where a link is DISCLOSED or ABSENT. The strongest sentence the
bundle emits is the thesis §5 claim scoped to THIS bundle:

> Every citable number is reducer-computed and byte-sealed here — never computed,
> never silently altered, by a language model. The minimal run-set is
> signature-verified and gap-disclosing. The chain is **mechanical for code**,
> **mechanical for data and environment where the scientist opted in and
> disclosed where not**, and the number→paper transcription is audited to the
> matched/uncitable split. The one unbound link is a human typing the sealed
> number into the paper.

A `verify-bundle` that passes returns `pass` **and** the disclosed-gap list in
the same breath — never a bare "reproducible."

---

## 4. The offline-verify story

The design center: **a stranger with the bundle, no cluster, ideally no
hpc-agent, gets pass/disclosed-gaps.** Three layers, strongest-portability first.

**Layer 1 — zero dependencies (the load-bearing path).** The `VERIFY` manifest is
self-attesting exactly as the dossier's `manifest.json` is:
`bundle_sha256 = manifest_signature(entries)`, each entry `{path, sha256,
bytes}`. A stranger recomputes it with a ~20-line stdlib script — unzip, sha256
each member, compare; then recompute the canonical digest over the path-sorted
entries and compare to `bundle_sha256`. The exact canonicalization is documented
IN the `VERIFY` manifest so a non-Python reimplementation is possible, quoting
the conformance-kit's pinned rule (`docs/design/conformance-kit.md`
`test_canonicalization.py`): `sort_keys=True` (code-point order, deliberately NOT
RFC 8785), compact separators, `ensure_ascii=False`, UTF-8, SHA-256 lowercase
hex. This is the whole proof-of-internal-consistency, with no hpc-agent and no
network.

**Layer 2 — stock in-toto / DSSE tooling.** The bundle ships the
`export-attestations` output as a member (`<seed>.attestations.jsonl` — one
unsigned DSSE envelope per sealed entry, `ops/export_attestations.py`). Stock
in-toto bindings parse every Statement and verify each subject digest against the
predicate's embedded verbatim bytes. Scope, pinned honestly (conformance-kit
D-K4): "verify" here is parse + subject-digest comparison, NOT DSSE *signature*
verification (v1 is unsigned; `signatures: []`). Ecosystem tooling confirms the
sealed evidence is what it claims to be, again with no hpc-agent.

**Layer 3 — with hpc-agent (`verify-bundle`).** For a reviewer who installs
hpc-agent: recompute Layer 1, then `verify_provenance_manifest` on the signed
manifest member, then re-derive the per-link MECHANICAL/DISCLOSED/ABSENT
classification and emit `pass` + the disclosed-gap ledger. This is the only layer
that re-classifies the reproducibility links (stock tooling checks digests, not
semantics), and it is strictly a convenience — Layers 1–2 already prove
everything a stranger needs about integrity.

The verify OUTPUT is honest at every layer: PASS means "internally consistent +
these links mechanical"; it always co-reports the disclosed-gap list; it never
returns a bare "reproducible."

---

## 5. Doctrine

- **Read-only in the reproducibility sense; one local write.** Mirror
  `export-dossier`'s decorator: `verb="mutate"`, a single `file_write`, no SSH,
  no scheduler, no interpretation. Observe and seal, never actuate — the
  observe/judge/route scope doctrine.
- **Disclose, never gate.** Nothing refuses. A missing manuscript, an absent
  signed manifest, an operator-bypass table, an uncitable number — each is a
  disclosed gap on the `VERIFY` manifest; the bundle still seals. This is the
  amplification posture (thesis §4): the tool makes the next rung of evidence
  cheap to accrue, it does not hold publication hostage. A gate a tired scientist
  routes around at midnight protects nothing; a disclosure that survives into the
  sealed record is worth more.
- **NOT MCP-curated (the projection ruling).** Follow the recorded NON-EXPOSURE
  the `mcp_server.py` comment pins for `export-dossier` / `export-attestations`:
  they are HUMAN-run publish/export steps after a run completes, not agent-loop
  touchpoints, so neither is in `_CURATED_EXTRA_VERBS`. The publication bundle is
  the same shape — a scientist assembles it at publication time. Register in the
  CLI registry; keep OUT of the curated catalog. (Consistent with
  `extract-recipe` / `cite-check` / `trace` / `run-story`, all deliberately not
  curated.)
- **Compose existing verbs; extend, don't reinvent (the house rule).** The ONE
  dossier gather (`compute_dossier_signature`, never a second store walk),
  `extract_recipe` (already inside the dossier), `cite_check`,
  `build_provenance_manifest`, and the ONE signable digest (`manifest_signature`).
  Zero re-implemented walks.
- **The no-parse boundary holds.** The bundle sealer copies member bytes
  verbatim and NEVER `json`-parses a sealed member's content — the dossier's Q1
  posture. The cite-check report and the signed manifest are FRAMEWORK-derived
  members (like the recipe member): serialized once, sorted-keys, sealed as
  opaque-to-the-sealer bytes. `json.dumps` to serialize a framework record is
  allowed; `json.load(s)` to read a sealed member back into structure is not.
- **Role-root op.** `ops/publication_bundle.py` lives at the `ops/` role root
  (sibling to `export_dossier.py` / `extract_recipe.py` / `cite_check.py`)
  because it reads across subjects; the subject-imports lint short-circuits for
  role-root files.

---

## 6. Build decomposition

House rule honored throughout: compose the shipped walks; render is pure code
(`*_render.py`), never LLM; the bundle is IDENTITY + ORDERING + COUNTING +
COMPARISON over opaque + framework-derived records — it never names a metric or
judges a run "best."

**Files.**

- **NEW `ops/publication_bundle.py`** (role-root) — `export-bundle` (`mutate`,
  one local write) and the thin `verify-bundle` (`query`) if R-B1 rules it in.
  Composes `compute_dossier_signature` + `cite_check` + `build_provenance_manifest`
  + `manifest_signature`. The genuinely new code is: (a) the compose-and-seal over
  the union of dossier entries + the two new members + the `VERIFY` manifest; (b)
  the per-link classification table (a code map from the thesis §3 links to
  MECHANICAL/DISCLOSED/ABSENT, filled from the recipe gaps + cite-check buckets +
  the sidecar data/env fields); (c) the union-of-disclosures ledger. This is a
  SMALL surface — the same size `export-attestations` was.
- **NEW `_wire/actions/publication_bundle.py`** — `ExportBundleSpec` /
  `ExportBundleResult` (flat, no domain vocabulary — the `extract_recipe` /
  `cite_check` wire posture). If `verify-bundle` lands: `_wire/queries/verify_bundle.py`.
- **NEW `ops/bundle_render.py`** (optional, SMALL) — a deterministic `VERIFY.md`
  render for human eyeballs (the `relay_render.py` / `recipe_render.py` posture),
  sealed alongside the JSON `VERIFY` manifest.
- **`schemas/*.json`** via `build_schemas`; **`docs/primitives/export-bundle.md`**
  (+ `verify-bundle.md`) from the template, citing the not-MCP-curated ruling.
- **regen** — `python scripts/regen_all.py --write` (covers `build_schemas` +
  `bake_operations_json` + frontmatter + the verb-module-map); `--check` in the
  gauntlet.
- **`tests/contracts/test_publication_bundle_boundary.py`** — modeled on
  `test_extract_recipe_boundary.py` / `test_dossier_boundary.py`: read-only
  side-effect scan (one write, no SSH), no-LLM-in-render, the no-parse pin (never
  `json.load(s)` a sealed member's content), the seal-consistency pins, and the
  **disclosure-inheritance pins** — a bundle over an opted-out run classifies the
  data link DISCLOSED not MECHANICAL; an `uncitable` number rides the gap ledger,
  never a failure; the top-level verdict never says "reproducible" when any link
  is DISCLOSED/ABSENT (verify the guard can actually fire — the honest-verdict
  branch must be exercised both ways).

**The cite-check-report-as-member decision (the key decomposition choice).** The
cite-check report is a **BUNDLE member** in `export-bundle`'s own manifest
vocabulary, NOT a new `DOSSIER_SOURCES` noun. This is deliberate and
minimal-blast-radius:

- adding a noun to `DOSSIER_SOURCES` fires the dossier boundary pin
  (`test_dossier_boundary.py`, equality-pinned) AND forces the
  `export-attestations` `PREDICATE_TYPES` pair-edit — two reviewed edits for a
  member that is a *publication* concern, not a *run* store;
- more fundamentally, the dossier is run-scoped and has no manuscript input; the
  cite-check report is bundle-scoped by nature. Keeping it a bundle member keeps
  `export-dossier`'s contract untouched (the `export-attestations` precedent:
  consume the dossier signature, add your own projection, never modify the
  dossier).

So the bundle carries its OWN small closed member vocabulary
(`dossier-evidence`, `recipe` [already inside the dossier gather],
`provenance-manifest`, `cite-check-report`, `attestations`, `verify`), pinned by
`test_publication_bundle_boundary.py`, disjoint from `DOSSIER_SOURCES`.

**Size:** MEDIUM — it rides the ONE gather + two shipped query verbs + the ONE
signable digest; the new code is compose+seal+classify, comparable to
`export-attestations`.

### Rulings needed

- **R-B1 (the verify surface).** Does offline verify ride ENTIRELY on the
  self-attesting `VERIFY` manifest + the stock-DSSE attestations member (no new
  verb), or also ship a `verify-bundle` convenience `query` verb?
  **Recommend:** ship the self-attesting manifest + the DSSE member as the
  load-bearing zero-dependency path (Layers 1–2, stranger-friendly), AND a thin
  `verify-bundle` for hpc-agent users (Layer 3 — it adds the per-link
  re-classification stock tooling cannot do). Flag for the maintainer: a
  `verify-bundle` verb is registry arithmetic (+1 query) and its own boundary
  pins.
- **R-B2 (manuscript optionality + multiplicity).** A bundle with no manuscript
  still seals (dossier + recipe + signed manifest) with a disclosed
  `cite-check-skipped` gap? **Recommend YES** (disclose-not-gate — a scientist
  may bundle before the manuscript is final, or bundle for an artifact with no
  paper yet). ONE manuscript per bundle in v1; a multi-paper corpus is a future
  additive.
- **R-B3 (cite-check report's vocabulary home).** BUNDLE member vs
  `DOSSIER_SOURCES` noun. **Recommend BUNDLE member** (no dossier-boundary blast
  radius, no `export-attestations` pair-edit — the `export-attestations`
  composition precedent). Flag so the maintainer ratifies the new closed
  bundle-member vocabulary.
- **R-B4 (the top-level verdict text).** The `VERIFY` manifest's honest verdict
  is **code-emitted** (a fixed template filled by the per-link classification, the
  `CLAIM_CONSISTENT_SENTENCE` precedent), never LLM-composed. Minor;
  **recommend code-emitted** and pin it in the boundary test.
- **R-B5 (schema).** The bundle is a NEW artifact with its own
  `BUNDLE_SCHEMA_VERSION = 1`; no existing schema breaks (dossier v2, manifest v3,
  cite-check, extract-recipe all unchanged). No version-bump ruling beyond the
  new-artifact version.

---

## Drift log

- **2026-07-17 — created (design).** Scopes the publication bundle as the
  concrete embodiment of the reproducibility thesis's public claim
  (`docs/design/reproducibility-thesis.md` §5) — "here is the proof my table is
  reproducible." Grounded against the shipped machinery read directly:
  `export-dossier` (`ops/export_dossier.py`, `DOSSIER_SCHEMA_VERSION = 2`, the
  BR-4 `recipe` member + `compute_dossier_signature` ONE-gather seam),
  `extract-recipe` (`ops/extract_recipe.py`, the minimal-set walk + signed-value
  preference), `cite-check` (`ops/cite_check.py`, the v1 two-bucket
  matched/uncitable audit over the SEALED `aggregated_metrics`),
  `provenance-manifest` (`ops/provenance_manifest.py`, v3 signing wheel + env-lock),
  `export-attestations` (`ops/export_attestations.py`, the in-toto/DSSE
  portability layer over the ONE dossier gather), and `verify-reproduction`
  (`ops/verify_reproduction.py`, the reproduction act + claim-check anti-laundering
  split). Cites the clean-reproduction-extraction program
  (`docs/plans/clean-reproduction-extraction-2026-07-17.md`), the cite-check
  design (`docs/design/cite-check.md`), the conformance kit's offline-verify +
  canonicalization story (`docs/design/conformance-kit.md` D-K4), the onboarding
  rungs (`docs/design/onboard-by-reproduction.md`), and the engineering
  principles (`docs/internals/engineering-principles.md` — Q1 substrate/no-parse,
  the compose-don't-reinvent house rule, the verify-a-guard-can-fire rule). Key
  scoping findings: the dossier already seals the recipe (BR-4) but NOT the signed
  provenance manifest (`DOSSIER_SOURCES` has no such noun); the cite-check report
  and the top-level cross-cutting disclosure ledger are the genuinely-new
  members; `export-bundle` is a SIBLING of `export-dossier` (the
  `export-attestations` precedent), not an extension; the cite-check report is a
  BUNDLE member, not a `DOSSIER_SOURCES` noun (R-B3), to avoid the dossier
  boundary + attestations pair-edit blast radius. No `src/**` change; no regen; no
  commit — five rulings (R-B1..R-B5) flagged for the maintainer.
