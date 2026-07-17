---
name: cite-check
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent cite-check --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.cite_check.cite_check
---
# cite-check

Audit a **manuscript's numbers** against a **sealed reduced table**: per number in
the paper, *is this digit faithfully transcribed from the sealed mechanical chain?*
This is the last-mile link of the clean-reproduction-extraction program — the
product one-liner ("what changed since last-known-good, answered mechanically
instead of by archaeology") applied at the **very last inch**: last-known-good is
the *sealed table cell*, and the archaeology is a human re-reading their own paper
against a JSON blob. cite-check is `verify-relay`'s sibling: verify-relay audits the
*LLM's outgoing relay* against the run corpus; cite-check audits the *human's
manuscript* against the *sealed* corpus.

It **DISCLOSES; it never gates** — a suspicious number is surfaced for a reviewer to
resolve, never refused (the bare-`y` / amplification doctrine). Read-only and
client-side: no SSH, no scheduler, no write. Derived state recomputed on every call.

It **composes the shipped machinery**, it reinvents nothing: the seed → sealed-table
resolution is `extract_recipe._resolve_seed` (the `run_id` / `campaign_id` /
`aggregate_path` seed contract, reused verbatim); the citing authority is the sealed
`metrics_aggregate.json`'s `aggregated_metrics` **values**, flattened by
`verify_relay.collect_source_numbers` and read **as sealed** (never re-derived); the
number grammar, the faithful-render tolerance (`match_number`), the nearest-value
context (`nearest_number`), and the false-positive discipline (ISO-date / month-day /
size-suffix / run-id-ident / conversational / spelled-cardinal consumers) are the
`verify-relay` originals imported, not copied.

This is the **load-bearing difference from `extract-recipe`**, which is *forbidden*
from reading `aggregated_metrics` values: cite-check **must** read the values —
comparing a cited digit to the sealed digit is its whole job. It still never
*interprets* a metric (no "best", no metric meaning); it only **compares** a number
to a number for transcription fidelity, an explicitly-permitted core operation (the
Q1 substrate-not-semantics rule). A pack `*.csv` stays **opaque** (its content is
never parsed) — every manuscript number is then uncitable-against-it.

## The false-positive guard (the unit's soul)

A manuscript is saturated with **reference** numbers that are NOT result claims — a
class verify-relay never had to exclude because an LLM relay does not write them.
cite-check consumes them before the number pass:

- **verify-relay's discipline, verbatim** — the `_NUM_RE` grammar with the
  run-id/ident pre-pass (`run-3`, `pi-train-d363e2a3`, `v2` are identifiers, not
  claims), the ISO / month-day date consumers, the size-suffix consumer (`886M` is a
  rounded figure), the conversational filter (`~2 minutes`, list markers), and the
  spelled-cardinal `>= 13` threshold.
- **manuscript-specific exclusions** (new) — page / figure / table / section /
  equation / algorithm / theorem refs (`Table 3`, `Fig. 4`, `Eq. 5`, `Section 3.2`,
  `p. 12`), academic citation years (`(Smith et al., 2024)`, `Jones (2021)`),
  bibliography markers (`[12]`, `[13, 14]`, `[15-17]`), and path-embedded digits
  (`results/2024/run.csv`).
- **a conservative claim-shape filter** — decimals / percentages / comma-grouped /
  large integers are high-signal citable shapes; a **bare small integer** (`300`
  epochs, `5` seeds) is low-signal and a non-matching one is skipped (counted, never
  flagged), so the report is not flooded. This is the disclosed bound on the
  irreducible Facet-1 judgment ("is this bare decimal a reported result or a learning
  rate?"), per the design.

## Inputs

A `CiteCheckInput` (`hpc_agent._wire.queries.cite_check`) — a manuscript + exactly
one sealed seed:

- `manuscript_text` (string) — the prose / table verbatim. Excludes
  `manuscript_path`.
- `manuscript_path` (path) — a `.tex` / `.md` / `.txt` read tolerantly. Excludes
  `manuscript_text`.
- `run_id` (string) — cite against this run's sealed table
  (`_aggregated/<run_id>/metrics_aggregate.json` `aggregated_metrics` values).
- `campaign_id` (string) — cite against this campaign's sealed tables (each
  contributing run's values).
- `aggregate_path` (path) — a sealed reduced-metrics artifact. A
  `metrics_aggregate.json` is read for its `aggregated_metrics` values; a pack `*.csv`
  is an **opaque** citation (never parsed).
- `--experiment-dir` (path, default cwd) — the experiment root.

## Outputs

`data` is a `CiteCheckResult`:

- `clean` (bool) — False iff any `uncitable` finding was surfaced (a `matched`
  finding never affects `clean`).
- `claims_checked` (int) — count of extracted numeric claims evaluated (references
  and low-signal bare small integers are filtered out before the count).
- `findings` — one `CiteFinding` per evaluated claim: `{claim, kind, detail,
  nearest_chain_value}` with `kind` one of:
  - `matched` — the cited number equals a sealed value under the faithful-render
    tolerance (exact / float-equality / pure-truncation-prefix / display
    round-or-truncate). Reported for auditability; `clean` ignores it.
  - `uncitable` — no sealed value backs the digit. `nearest_chain_value` carries the
    closest sealed value as **context** (offered, never asserted as an alignment to a
    specific cell).
- `sources_consulted` — the sealed `metrics_aggregate.json` artifacts whose values
  were pooled (an absent / opaque artifact yields the empty list, honestly).
- `seed_kind` / `seed_ref` — which seed the sealed pool was resolved from.
- `markdown` — the code-rendered audit (deterministic; LLM-free render path).

The `mismatch` bucket (label-anchored "cited X, chain says Y") is a **ruling-gated,
additive v2** refinement (it only reclassifies some `uncitable` into `mismatch`; it
never changes a `matched`) and is NOT emitted by v1.

## Errors

- `spec_invalid` — not exactly one manuscript source, not exactly one seed, or an
  absent manuscript / aggregate path. Not retry-safe; fix the spec.

## Idempotency

A pure query with no side effects. Derived state recomputed from disk on every call.

## Boundary posture

cite-check **compares** a cited number to a sealed number for transcription fidelity —
it reads the sealed `aggregated_metrics` values but never re-derives them, never names
a metric, never picks a "best" run, never concludes. Pinned by
`tests/contracts/test_cite_check_boundary.py` (the `extract-recipe` precedent).

## Usage

```
hpc-agent cite-check --spec spec.json --experiment-dir .
```

where `spec.json` names a manuscript (`{"manuscript_text": "..."}` or
`{"manuscript_path": "paper.tex"}`) and exactly one seed (`{"run_id": "<id>"}`,
`{"campaign_id": "<id>"}`, or `{"aggregate_path": "<path>"}`). Like `extract-recipe` /
`trace` / `run-story`, cite-check is deliberately **NOT MCP-curated**: a
publication-time transcription audit is a reviewer action, and the curated catalog is
a deliberate human-amplification allowlist (the MCP-is-projection ruling), so the verb
is reachable through the CLI registry but not advertised as a curated tool.
