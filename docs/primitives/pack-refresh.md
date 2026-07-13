---
name: pack-refresh
verb: mutate
side_effects:
- file_write: <experiment>/<pack>/manifest.json
- file_write: <experiment>/.hpc/packs/<pack>.decisions.jsonl
idempotent: false
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent pack-refresh --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.pack.refresh_op.pack_refresh
---
# pack-refresh

Mechanically **re-seal + rebind** every opted-in domain pack whose manifest is
stale, then report which receipt slots must be re-earned — the auto-remedy the
2026-07-10 ruling authorises ("the pack gate MAY auto-remedy; latency is to be
OBLITERATED", `docs/design/domain-packs.md` drift log). The seal is for the
ARCHIVE — the journal records old shas, the drift event, and the new bind — not a
speed bump for humans building fast, so the remedy runs in code with zero human
turns.

Given an experiment dir (opt-in read from `interview.json`; not opted in → empty
and silent), the verb:

1. detects which **bound** packs' manifests are stale against on-disk bytes — the
   **minimal** set (editing one pack's content never forces another's rebuild;
   staleness is SEMANTIC — a moved file sha / an added-or-removed swept file /
   changed name·version·seams·fills_slots, never whitespace);
2. re-seals each stale manifest **generically** from its declarative `sweep.json`
   recipe (`state/pack_sweep.py`) — pure hashing over the recipe's `pack_files` +
   `sweep` globs, byte-identical to the pack's own `build_*_pack.py`; **DP2 holds,
   core never executes a pack build/check script**;
3. rebinds each via the existing `pack-bind` path, journaling old→new shas;
4. reports, per pack, what moved (old/new manifest sha, added/removed/changed
   files) and every caller-authored receipt slot now un-cleared plus its
   **caller-side check command** (from the interview `receipt_bindings` entry's
   `check` field) — **core never runs the check itself** (DP2).

## The recipe is data, not domain logic

`sweep.json` is the pack's build RECIPE — a list of files and globs — never sealed
content (a Makefile is not one of its own targets). Core reads it as data,
resolves its globs against the pack root, and hashes raw bytes; it never imports or
runs the pack's `build_*_pack.py`. A manifest with no sibling `sweep.json` is left
untouched and reported (`recipe_found: false`) — re-run the pack's own build.

## Rebind is drift — receipts must be re-earned

Re-sealing moves the manifest sha, so a re-bound pack's covered receipts read
**stale** by construction. That is the point: the report's `slots_to_reearn` names
each one and its check command so a driving skill re-emits the receipt with
`pack-record-receipt` (or the pack's own check script) and retries — the caller-side
half core cannot do.

## Boundary

Core reads the recipe + bytes and hashes them — nothing more. It never interprets a
declared value's meaning, never runs a pack check, and never invents a receipt. An
experiment that never opts into a pack behaves byte-identically to today.
