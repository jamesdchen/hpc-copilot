---
status: v1-built
# cite-check — the number → paper transcription audit (v1 BUILT; v2 ruling-gated)

**Status: v1 BUILT (2026-07-17).** The two-bucket shape (Option B — `matched` /
`uncitable` with `nearest_chain_value` context, zero alignment, zero false-match)
is SHIPPED: `ops/cite_check.py` + `ops/cite_render.py`, `_wire/queries/cite_check.py`
(`CiteCheckInput` / `CiteCheckResult` / `CiteFinding`), `schemas/cite_check.{input,output}.json`,
`docs/primitives/cite-check.md`, and the boundary contract
`tests/contracts/test_cite_check_boundary.py`. The label-anchored **`mismatch`**
bucket (Option A) remains **ruling-gated** as the additive v2 (it only reclassifies
some `uncitable` into `mismatch`; it never changes a `matched`, so v2 lands without
a schema break). The original DESIGN-STOP record is preserved below.

**Status: DESIGN-STOP (2026-07-17).** The contract, the composed machinery, and
the false-positive discipline are settled below. The build is **gated on one
ruling** — how to distinguish a *mismatch* ("cited X, chain says Y") from an
*uncitable* number ("no chain value for this digit") without importing the
verify-relay number-pool false-positive class into a manuscript, which is a far
more adversarial surface than an LLM relay. That distinction is not a mechanical
build: both honest resolutions embed a doctrine tradeoff (false-match noise vs.
completeness of the mismatch bucket) that belongs to the maintainer, not to a
guess. This follows the BR-14 precedent (`docs/plans/backlog-2026-07-17.md`):
when the assumed-simple build hides a judgment that is not the agent's to make,
write the memo with the open question + recommendation rather than guessing.
Cite `path::symbol`, never line numbers.

Origin: the clean-reproduction-extraction program
(`docs/plans/clean-reproduction-extraction-2026-07-17.md`). That program sealed
the citable **table** — `extract-recipe` walks a citable artifact back to its
minimal run-set and signs it; the dossier seals the record trail; the reduce-time
`contributing_run_ids` provenance makes the table→run-set link first-class. But
the chain **stops at the dossier**. The last link — the citable digit as it is
**typed into the manuscript** — is hand-transcribed and unaudited. `extract-recipe`
proves *these runs, at these shas, reduced by this command, produced this table*;
nothing yet proves *the number in the paper is that table's number*. cite-check
is that last-mile link. (The named memo `reproducibility-program-2026-07-17.md`
in the dispatch brief does not exist in-repo; its "gap #3 / number → paper
transcription" is the natural extension of the clean-reproduction-extraction G4
break list, and this memo is filed against that program.)

## Product intent — the gap, stated as the product move

A scientist finishes the mechanical chain: the table is reduced, sealed in a
dossier, signed by `extract-recipe`. They then **type the numbers into a
manuscript** — a `.tex`, a `.md`, a results paragraph, a LaTeX `tabular`. That
transcription is where the chain breaks: a fat-fingered digit (`0.94` → `0.49`),
a stale copy from an earlier run, a rounded figure that changed a significant
digit — none of it is caught, because no code follows the sealed number into the
prose. The product one-liner
(`docs/design/onboarding-map.md`) — "what changed since last-known-good, answered
mechanically instead of by archaeology" — applied at the **very last inch**:
last-known-good is the *sealed table cell*, and the archaeology is a human
re-reading their own paper against a JSON blob.

cite-check answers, per number in the manuscript: **is this digit faithfully
transcribed from the sealed mechanical chain?** It DISCLOSES; it never gates — a
suspicious number is surfaced, never refused (bare-`y` / amplification doctrine).
It is verify-relay's sibling: verify-relay audits the *LLM's outgoing relay*
against the run corpus; cite-check audits the *human's manuscript* against the
*sealed* corpus. Same claim-extraction discipline, a different (sealed, narrower)
authority, and a different consumer (a reviewer's report, not a Stop hook).

## The contract

A read-only `query` primitive (the `run-story` / `trace` / `extract-recipe`
posture): no SSH, no scheduler, no write, no store. Derived state recomputed on
every call.

### Input — a manuscript + a sealed seed

`CiteCheckInput`:

- **the manuscript** — one of `manuscript_text` (the prose/table verbatim) or
  `manuscript_path` (a `.tex` / `.md` / `.txt` read tolerantly). The text whose
  numeric claims are audited.
- **the sealed seed** — exactly one of `run_id` / `campaign_id` / `aggregate_path`
  (the `extract-recipe` seed contract, reused verbatim via `_resolve_seed`). It
  names the **sealed table** whose values are the citing authority.
- `--experiment-dir` (default cwd).

### Authority — resolved from the SEALED chain, never re-derived

The authoritative citable-number pool is the **reduced table's result values** —
`metrics_aggregate.json`'s `aggregated_metrics` (`dict[run_id, dict[metric,
value]]`, `ops/aggregate_flow.py`) — read from the artifact **as sealed**, never
recomputed. Optionally widened by the sealed manifest's own numeric fields
(`run_count`, and the sidecar counts `extract-recipe` already projects). This is
the load-bearing difference from `extract-recipe`, which is FORBIDDEN from
reading `aggregated_metrics` values (`tests/contracts/test_extract_recipe_boundary.py::
test_op_never_reads_the_aggregated_metrics_body`): **cite-check MUST read the
values — checking a cited digit against the sealed digit is its whole job.** It
still never *interprets* them (no "best", no metric meaning); it only compares a
cited number to a sealed number for transcription fidelity. That keeps it inside
the Q1 substrate-not-semantics rule (`docs/internals/engineering-principles.md`):
COMPARISON of numbers under a tolerance is an explicitly-permitted core operation.
A pack `*.csv` stays OPAQUE (R2, the dossier no-parse boundary): its cells are
never parsed, so every manuscript number is uncitable-against-it, disclosed as a
gap — identical to `extract-recipe`'s `pack-csv-opaque`.

### Output — per-number disclosure, three buckets

`CiteCheckResult`: `clean` (bool), `claims_checked` (int), `findings`
(`list[CiteFinding]`), `sources_consulted`, `seed_kind`/`seed_ref`, plus a
code-rendered `markdown`. Each `CiteFinding` = `{claim, kind, detail,
nearest_chain_value}` with `kind`:

- **`matched`** — the cited number EQUALS a sealed chain value under the faithful
  render tolerance (`verify_relay.match_number`: exact / float-equality /
  pure-truncation-prefix / display-round-or-truncate). This digit in the paper
  IS the sealed table's digit. *(Reported for auditability; `clean` ignores it.)*
- **`mismatch`** — the cited number is a claim *about a specific sealed cell* and
  differs from that cell's value: "cited X, chain says Y." **← the alignment this
  memo cannot resolve without a ruling (§ Open question).**
- **`uncitable`** — the cited number matches no sealed value and cannot be aligned
  to a specific cell: "no chain value backs this digit." The honest default —
  cite-check does NOT guess whether it is a typo or an incidental prose number.

### Posture (settled, ruling-independent)

- **DISCLOSE, never gate.** A finding is surfaced; nothing is refused. No Stop
  hook, no block. (verify-relay's hook is a separate seam; cite-check has no
  actuation.)
- **NOT MCP-curated.** Like `extract-recipe` / `trace` / `provenance-manifest` /
  `run-story`, it is an operator/reviewer projection. Register in the CLI
  registry; keep OUT of `mcp_server._CURATED_EXTRA_VERBS`. Cites the
  MCP-is-projection ruling: the curated catalog is a deliberate
  human-amplification allowlist, and a publication-time transcription audit is a
  reviewer action, not a curated agent tool.
- **Read-only.** No SSH, no write, no store — pins mirror
  `test_extract_recipe_boundary.py`'s side-effect / import scans.

## Machinery it composes (reinvents nothing)

| Need | Reused from | How |
|---|---|---|
| seed → sealed table | `ops/extract_recipe.py::_resolve_seed` | run_id / campaign_id / aggregate_path → the `metrics_aggregate.json`; pack-csv stays opaque (R2) |
| the citable value pool | `ops/aggregate_flow.py` `aggregated_metrics` + `verify_relay._collect_source_numbers` | flatten the sealed table's values into `(strings, floats)` exactly as verify-relay pools a run corpus — but over the SEALED artifact only |
| number extraction + faithful-match | `verify_relay._NUM_RE` / `match_number` / `_nearest_number` | the ONE numeric grammar + tolerance; `nearest_chain_value` = `_nearest_number` (offered as context, never asserted as alignment) |
| the false-positive guard | `verify_relay` token discipline (below) | reused, not reinvented |
| deterministic render | `ops/relay_render.py` posture; a new `ops/cite_render.py` | code renders; the reviewer reads verbatim; LLM-free render path |

## The false-positive guard (settled, applies under either ruling)

The manuscript-side extraction MUST reuse verify-relay's discipline so a
path-shaped or label-embedded digit is never mistaken for a citable claim — the
class the dispatch brief names. Reused verbatim from `verify_relay`:

- **the numeric-literal grammar** (`_NUM_RE` / `_FULL_NUM_RE`) as the ONE
  definition of "what is a number," with the run-id/ident pre-pass so
  `run5` / `s2` / `v2` / `table3` / `/path/run3` are consumed as identifiers and
  never read as claims (`_is_run_id_like`, `_is_identifier_like`);
- **the ISO / month-day date consumers** (`_ISO_DATETIME_RE`, `_BARE_MONTH_DAY_RE`)
  — a date is not a result number;
- **the size-suffix consumer** (`_SIZE_SUFFIX_RE`) — `886M` is a rounded figure,
  not a citable count;
- **the conversational filter** — line-start `N.` list markers, `~`-prefixed
  durations;
- **the spelled-cardinal `≥ 13` threshold** (`_NUMBER_WORD_MIN_VALUE`) — a typo'd
  result restated in words is the same distortion, but `one..twelve` flood.

**Manuscript-specific extension** (new, and part of the open question's first
facet): a paper additionally carries dense *reference* numbers that are NOT
result claims — page numbers (`p. 12`), figure/table/section/equation refs
(`Table 3`, `Fig. 4`, `Eq. 5`, `Section 3.2`), citation years (`(Smith 2024)`),
bibliography markers (`[12]`), and hyperparameters stated in prose (`300 epochs`,
`lr = 0.001`). verify-relay never had to exclude these because an LLM relay does
not write them; a manuscript is saturated with them.

## The open question (the STOP) — manuscript-number → sealed-cell alignment

Producing the **`mismatch` bucket** ("cited X, chain says Y") requires knowing
that the cited X was *meant to be* a specific sealed cell Y. verify-relay's model
does not transfer, and there is no false-match-free mechanical resolution — two
distinct facets, both requiring a judgment call:

**Facet 1 — which manuscript numbers are result-claims at all?** verify-relay
classifies *every* non-matching number as a `number` mismatch when the pool is
non-empty, and `unverifiable` only when the pool is empty
(`verify_relay.verify_relay`). Transposed directly, cite-check's sealed pool is
always non-empty, so **every** incidental manuscript number (years, section refs,
hyperparameters, p-value thresholds, sample sizes) would flag. This is the exact
flood the codebase engineers against (the `_NUMBER_WORD_MIN_VALUE` rationale: "auditing
them would flood false positives and kill the hook's credibility"; the whole
ISO-date / size-suffix / list-marker carve-out history). The §-guard above
removes the *shaped* offenders, but "is this bare decimal a reported result or a
learning rate?" is an irreducible judgment.

**Facet 2 — mismatch vs uncitable.** To say "chain says Y" honestly you must
align the cited number to a specific cell. The only mechanical anchor is the
sealed table's own metric-name keys used as a windowed label vocabulary — and it
carries an irreducible false-match residual AND a false-negative gap:

- *false match*: "accuracy improved from `0.88` to `0.94`" anchors `0.88` to the
  `accuracy` cell (`0.94`) and reports a mismatch, though `0.88` is a legitimate
  baseline, not a transcription of that cell;
- *false negative*: sealed keys are terse (`qlike_sum`, `test_acc`) while prose is
  verbose ("the QLIKE loss", "test accuracy"), so most real transcription errors
  never anchor and fall to `uncitable` anyway.

Neither facet has a mechanical answer that is not a tuned threshold (window
width, "comparable precision", which prose numbers count). Guessing one would
either flood the report or bury an unratified heuristic in a publication-fidelity
tool. That is the maintainer's call — the BR-14 shape.

### Options + recommendation

- **Option A — label-anchored mismatch.** Flag `mismatch` only when a sealed
  metric-label token sits in a tight window before the number (reuse
  verify-relay's `_CANARY_WINDOW` + token-exact discipline) AND that cell's value
  differs at comparable precision; everything else non-matching is `uncitable`.
  Gives precise "chain says Y" for exact-label cases. Cost: the false-match
  residual + false-negative gap above; introduces window/precision thresholds.
- **Option B (RECOMMENDED for v1) — two-bucket disclosure, no alignment.** Ship
  `matched` / `uncitable` only, with `uncitable` carrying `nearest_chain_value` as
  pure CONTEXT (verify-relay's `nearest_source_value` precedent — offered, never
  asserted as alignment): "`0.49` — no sealed value matches (nearest sealed value:
  `0.94`)." Zero alignment, zero false-match; a transcription typo lands in
  `uncitable`-with-nearest, which a human resolves at a glance. Delivers the full
  last-mile value — *these paper numbers ARE / ARE NOT the sealed chain's
  numbers* — with no false-match risk, honoring DISCLOSE-never-gate. Facet 1 is
  still bounded by the §-guard + a conservative claim-shape filter (prefer
  decimals/percentages; treat bare small integers as low-signal), disclosed.

**Recommendation:** ship **Option B as cite-check v1** — the false-match-free
core is the safe, buildable, high-value deliverable and is the honest reading of
the contract's `matched` / `uncitable` buckets. Gate **Option A's label-anchored
`mismatch`** behind a maintainer ruling as an additive v2 refinement (it never
changes a `matched`; it only reclassifies some `uncitable` into `mismatch`), so
v2 lands without a schema break once the false-match tradeoff is ratified. This
mirrors verify-relay's own history: the machinery shipped verdict-only first, and
each sharper heuristic (number-words, size-suffix, canary-adjacency) landed as a
ratified, disclosed increment — never guessed up front.

## Build sketch (build-ready once ruled)

Full verb lifecycle, mirroring `extract-recipe`:

- **`ops/cite_check.py`** (role-root, composes `_resolve_seed` + the verify-relay
  pool/grammar/match helpers — imported, not copied) + **`ops/cite_render.py`**
  (deterministic markdown, `ops/relay_render.py` posture).
- **`_wire/queries/cite_check.py`** — `CiteCheckInput` / `CiteCheckResult` /
  `CiteFinding` (flat, no domain vocabulary in field names — the
  `extract_recipe` wire posture).
- **`schemas/cite_check.{input,output}.json`** via `build_schemas`.
- **`docs/primitives/cite-check.md`** (the template; cites the not-MCP-curated
  ruling in Usage).
- **regen** (say which ran): `python scripts/regen_all.py --write` covers
  `build_schemas` + `bake_operations_json` + frontmatter + the verb-module-map;
  `--check` in the gauntlet.
- **`tests/contracts/test_cite_check_boundary.py`** — modeled on
  `test_extract_recipe_boundary.py`: read-only side-effect + no-LLM-in-render
  scans, plus the transcription pins (exact table numbers → all `matched`; a
  typo'd digit → surfaced (`uncitable` under B / `mismatch` under A); a page /
  path-embedded digit NOT flagged — the false-positive guard).

## Drafted enforcement row (for `lifecycle-verdicts.md` — NOT edited here; contended)

> | cite-check reads the SEALED value pool, never re-derives, and never interprets a metric | `tests/contracts/test_cite_check_boundary.py` | the op re-runs a reducer / reads a live task tree instead of the sealed `metrics_aggregate.json`; the render path imports an LLM/prose module or reaches `_wire`; a wire field name is drawn from the domain-semantics forbidden set; or the authority pool is built from anything but the sealed artifact |

(Row DRAFTED only — `docs/internals/principles/lifecycle-verdicts.md` is
contended / in-flight per `docs/plans/backlog-2026-07-17.md` §0, so it is left
untouched.)

## Drift log

- **2026-07-17 — created (DESIGN-STOP).** cite-check scoped as the last-mile link
  of the clean-reproduction-extraction program (the "number → paper
  transcription" gap beyond G4). Contract, composed machinery
  (`extract_recipe._resolve_seed` + the `verify_relay` pool/grammar/match/token
  discipline + the sealed `aggregated_metrics` values), false-positive guard, and
  read-only / disclose-never-gate / not-MCP-curated posture all settled. Build
  STOPPED on one ruling: the manuscript-number → sealed-cell alignment for the
  `mismatch` bucket has no false-match-free mechanical resolution (two facets:
  which prose numbers are result-claims; mismatch vs uncitable). Recommendation:
  ship Option B (two-bucket, `nearest_chain_value` context, zero false-match) as
  v1; gate Option A (label-anchored `mismatch`) behind a maintainer ruling as an
  additive v2. No `src/**` change; no regen; no commit.
- **2026-07-17 — v1 BUILT (Option B).** Shipped the two-bucket audit under the
  standing recommendation-aligned delegation. `ops/cite_check.py` (role-root,
  composes `extract_recipe._resolve_seed` + the promoted-public `verify_relay`
  extraction discipline — `NUM_RE` / `match_number` / `nearest_number` /
  `collect_source_numbers` / the ISO-date / month-day / size-suffix / run-id-ident /
  conversational / spelled-cardinal consumers, imported not copied) + `ops/cite_render.py`
  (LLM-free deterministic markdown). Authority = the sealed `metrics_aggregate.json`
  `aggregated_metrics` VALUES, read as sealed (per-seed: the run's / the campaign's
  contributing runs' / the aggregate-path's table; a pack `*.csv` stays OPAQUE, R2 —
  every number uncitable-against-it). The false-positive guard reuses verify-relay's
  discipline verbatim PLUS the manuscript-specific reference exclusions (page /
  figure / table / section / equation / algorithm / theorem refs, citation years,
  `[12]` bibliography markers, path-embedded digits) and a conservative claim-shape
  filter (decimals / percentages / comma-grouped / large ints are high-signal; a
  bare small integer `< 1000` is low-signal and a non-matching one is skipped, so a
  prose hyperparameter never floods) — the Facet-1 bound, disclosed. DISCLOSE never
  gate; read-only; NOT MCP-curated (CLI registry only, out of `_CURATED_EXTRA_VERBS`).
  Boundary pins in `tests/contracts/test_cite_check_boundary.py` (reads the sealed
  values but never re-derives / never names a metric / no LLM in the render path / no
  domain-vocab wire field); behaviour + the false-positive battery in
  `tests/ops/test_cite_check.py`. `_SPEC_VERBS` gained `cite-check`; a sanctioned
  `lint_subject_imports.ROLE_ROOT_ALLOW` entry (`cite_check.py` → `ops`/`decision`)
  covers the verify-relay reach (the extract-recipe precedent). regen 8/8, gauntlet
  26/26, ruff/format/mypy clean. **Option A's label-anchored `mismatch` bucket
  REMAINS ruling-gated (declined for v1)** — the additive v2 that only reclassifies
  some `uncitable` into `mismatch`, never touching a `matched`.

- 2026-07-17 — **RULED: v1 stands; v2 (the mismatch bucket) DECLINED for now**
  (user: "I haven't hit this use case yet — overengineering to figure it out
  now"). The two-bucket contract is the shipped surface; revisit v2 only with
  real-manuscript evidence that `uncitable + nearest_chain_value` is not
  pointed enough.
