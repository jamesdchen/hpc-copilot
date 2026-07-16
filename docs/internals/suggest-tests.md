# suggest-tests: an ADVISORY diff → test-selection tool

`scripts/suggest_tests.py` maps a git diff to the pytest modules most likely to
cover it, so an agent's inner **edit → test** loop can run a fast, focused slice
instead of the whole suite between edits.

## The advisory contract (MAINTAINER-RULED 2026-07-16)

This tool is **advisory only**. It never narrows what CI or a release runs:

- The **full suite stays mandatory** at CI and in the `/release` skill. Nothing
  here changes those gates — they run everything, always.
- Its own output announces this on every run
  (`advisory selection — CI runs everything; the full suite stays mandatory`),
  so a reader can never mistake a focused slice for a sufficient one.
- It **never silently drops** a changed source file. Any file no pass could map
  is printed **loudly** under `UNMAPPED` (the no-silent-caps rule). When in
  doubt the tool tells you to run *more*, never less.

Use it to shorten the feedback loop while iterating. Do **not** use its output
as a merge gate, a pre-push substitute, or evidence that a change is safe — the
full suite is the only such evidence.

## When to use it

- Mid-task, between edits, to re-run the handful of modules a change touches
  without paying for the full suite each iteration.
- To discover the non-obvious blast radius of a shared-predicate change (the
  cross-consumer map surfaces tests that never import the changed module).

## How it selects (three passes, unioned)

```
python scripts/suggest_tests.py [<ref>]   # <ref> defaults to HEAD
```

`<ref>` is the git ref the working tree (+ staged) is diffed against; `HEAD`
sees an agent's uncommitted edits. For each changed `src/hpc_agent/**/*.py`:

1. **Mirror-path** — `src/hpc_agent/<subject>/<module>.py → tests/<subject>/test_<module>*.py` (the
   `src/hpc_agent/` prefix drops, the leaf becomes `test_<leaf>*.py`). Only
   matches that exist on disk are kept.
2. **Import-graph** (AST over `tests/`, deterministic — no imports executed). A
   changed module is a hit if a test imports it **directly**, or imports a src
   module that itself imports the changed one (**one hop**, via a
   reverse-dependency map built over `src`). This catches the common case where
   the mirror path lies — e.g. `ops/decision/journal/verify_relay.py` is
   exercised by `tests/ops/test_verify_relay.py`, not a nonexistent
   `tests/ops/decision/journal/…`.
3. **Cross-consumer map** (`CROSS_CONSUMER` in the script) — a hand-curated
   table for shared predicates whose blast radius no path or import edge
   reveals. A `block_drive` lifecycle change fans out to every workflow's
   `test_blocks.py` and the attention/status projections even though those tests
   never import the kernel module.

The output is a pytest-ready argument list, each target annotated with which
pass(es) selected it, followed by the advisory disclaimer and any `UNMAPPED`
files.

## The seeded cross-consumer map

| Changed predicate | Consumers that must run |
|---|---|
| `_kernel/lifecycle/block_drive.py` | `tests/ops/attention/`, `tests/ops/status/test_snapshot_attention.py`, `tests/ops/monitor/test_blocks.py`, `tests/ops/aggregate/test_blocks.py`, `tests/meta/campaign/test_blocks.py`, `tests/ops/test_block_gate_and_speculate.py`, `tests/ops/test_block_chain.py` |
| `infra/clusters.py` | `tests/infra/`, `tests/ops/submit/` |
| `infra/io.py` | `tests/state/`, `tests/ops/decision/` |
| `ops/decision/journal/verify_relay.py` | `tests/ops/test_verify_relay.py`, `tests/_kernel/hooks/` |

Add an entry only as a reviewed decision, with a comment in the script citing
the shared predicate. The block-drive row is seeded from a live lesson: a
None-marker boundary change there went CI-red across every workflow's block
wiring (`94c0c484`) without any of those tests importing the driver.

## Tests

Fire paths pinned in `tests/scripts/test_suggest_tests.py`: mirror mapping,
import-graph hit, cross-consumer hit, unmapped-surfaces-loudly, and the advisory
line's presence.
