---
name: tag-session
verb: mutate
side_effects:
- file_write: <experiment>/.hpc/devx/session_tags.jsonl
idempotent: false
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent tag-session --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.devx_tag.tag_session
---
# tag-session

## Purpose

Mark this session as **data for the dev loop to ingest** — the ONE devx
affordance the wheel keeps after the 2026-07-28 dev-loop/product separation.
A session that notices friction ("the aggregate brief made me re-read the
log twice"), a good specimen ("this campaign is a clean end-to-end example"),
or a tool gap appends one opaque record to the per-experiment
`.hpc/devx/session_tags.jsonl` ledger. The maintainer's repo-side ingestion
tooling reads those ledgers later; **nothing in the product ever reads a tag
back to change behavior** — no gate, no journal, no decision path. A tag is
data ABOUT the session, never evidence (contrast `append-decision`, where the
record IS a gate's input). That inertness is the design: the verb is safe in
every wheel and freely delegable because it cannot launder anything.

## Inputs

A `TagSessionSpec` JSON spec with:

- `tags` (list[str], ≥1 entry, entries non-empty) — OPAQUE tag slugs (e.g.
  `["friction", "aggregate-ux"]`). Core attaches no meaning; the vocabulary
  belongs to the dev loop's ingestion tooling.
- `note` (str | null) — optional free-text context, recorded VERBATIM and
  never interpreted.
- `session_id` (str | null) — optional caller-supplied grouping id. Core
  mints nothing: the product has no session concept, and inventing one for a
  devx ledger would be scope the separation just removed.
- `run_id` (str | null) — optional run the tag is about, OPAQUE here (not
  validated against the runs store — a tag naming a run that failed to exist
  is itself data).

`experiment_dir` arrives through the standard `--experiment-dir` CLI arg.

## Outputs

A `TagSessionResult` with:

- `path` (str) — absolute path of the ledger appended to.
- `record` (dict) — the record as written: `{ts, tags, note, session_id,
  run_id}`, `ts` stamped at append time (UTC ISO-8601).
- `count` (int) — total parseable records in the ledger after the append.

## Errors

None beyond standard spec validation (`spec_invalid` from the schema layer —
empty `tags`, blank tag entries, unknown fields). The append itself has no
refusal conditions: the ledger accepts any opaque record.

## Idempotency

**Not idempotent** — the ledger is append-only, so an immediate retry of a
succeeded call appends a second record. Honest duplication in an ingestion
ledger beats a dedup scheme the consumer would have to trust blind; the dev
loop's tooling dedups on read if it cares.

## Notes

- Storage: `state/devx_tags.py`, one flock-append JSONL ledger per experiment
  (`infra.io.append_jsonl_line` — the same append-only, whole-line-atomic,
  crash-durable discipline as every ledger in the package).
- The wheel/dev-loop boundary this verb is the sanctioned crossing of is
  mechanized by `tests/contracts/test_wheel_product_boundary.py` (no
  maintainer-flagged asset ships in the package; the repo-level maintainer
  surface stays lint-covered).
- Pure local write; no SSH, no cluster contact.
