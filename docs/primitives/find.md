---
name: find
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent find <query> [--limit <limit>]
  python: hpc_agent.cli.setup.find
---
## Purpose

Search the operations catalog by intent or a half-remembered name and return a thin candidate list. `find` is the middle discovery tier: it sits between the all-at-once dump (`capabilities --full`, which materializes every agent-facing primitive's doc body + schemas) and the single-contract fetch (`describe <name>`). The three-step contract for an agent is **find (explore) → describe (read one) → invoke** — so a headless loop can resolve "what's the name for submitting a batch?" without first loading the whole catalog into context every iteration.

## Inputs

- `query` (str) — an intent phrase (`"submit a batch"`) or a partial / misremembered primitive name (`"submit-batch"`). A blank or whitespace-only query matches nothing rather than dumping the catalog.
- `--limit` (int, default 15) — maximum candidates returned.

## Outputs

`{"query": <str>, "count": <int>, "matches": [{"name", "verb", "cli", "summary"}, ...]}`

Each match is a **thin** row — name, verb tier, CLI invocation string, and the one-line summary — and deliberately carries no schemas or doc bodies. Fetch those for a chosen name with `describe <name>`.

## Errors

None. An unmatched or blank query returns an empty `matches` list with `count: 0`, not an error.

## Idempotency

Pure read over the in-process `@primitive` registry — no side effects, fully replayable. Results change only when the installed package's primitive set or summaries change.

## Notes

Matching is stdlib-only (no index, no embeddings): a fuzzy `difflib.get_close_matches` pass over primitive *names* (catching `submit-batch` → `submit-flow-batch`) is unioned with a token / substring scan over `name + summary` (catching the intent phrase `submit a batch`). The union is returned in stable catalog order, capped at `--limit`. The `summary` field it ranks on is the primitive's `CliShape` help string, surfaced through the operations catalog.
