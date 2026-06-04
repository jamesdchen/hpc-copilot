---
name: check-task-generator-mismatch
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent check-task-generator-mismatch --caller-task-generator <caller_task_generator>
    [--cached-task-generator <cached_task_generator>]
  python: hpc_agent.ops.check_task_generator_mismatch.check_task_generator_mismatch
---
# check-task-generator-mismatch

Canonical-JSON compare between a caller-supplied `task_generator` and the
cached/derived one — the structured guard at `hpc-submit` SKILL.md Step 3.
A stale `interview.json` left in `experiment_dir` from earlier dev work
encodes its own `task_generator` (e.g. 8 seeds); if the caller passes a
different one this invocation (e.g. 100 seeds), the cached one must not
silently win and shrink a 100-task request to an 8-task submission. This
verb makes the divergence detectable in one call.

## Inputs / outputs

See `hpc_agent/schemas/check_task_generator_mismatch.{input,output}.json`.
Input requires `caller_task_generator`; `cached_task_generator` is
optional. Both are accepted as JSON object strings on the CLI (parsed
before comparison) or as already-parsed dicts on the in-process path.

`match` is the short-circuit signal most callers read first:

- `true` → the cached interview is authoritative (or there was nothing to
  diverge from); Step 3 continues.
- `false` → the seam where `hpc-submit` branches on
  `on_task_generator_mismatch` (`fail` / `refresh` / `prefer-caller`).

On a mismatch, both canonical forms and their `sha256` are returned under
`caller` / `cached` so the caller can surface BOTH shapes in a
`task_generator_mismatch` envelope.

## Canonical comparison, not Python `==`

Both generators are normalized with
`json.dumps(..., sort_keys=True, separators=(",", ":"))` — the same
recursive key-sort idiom `state.run_sha` hashes task kwargs with — so two
generators that differ only in key order or whitespace compare equal. The
comparison is on *content*, never dict insertion order. The `sha256` of
each canonical form is a cheap downstream equality / logging key.

## The vacuous-match path

When `cached_task_generator` is absent (`null` / omitted), there is no
cached generator to diverge from: `match` is `true` with
`reason: "no_cached_generator"` and `cached: null`. This is the Step 3
case "caller did NOT supply a generator vs. a cached one" inverted — when
only the caller's generator exists, it is authoritative by default, NOT a
mismatch.

## requires_ssh: False

Pure local content comparison — no SSH, no disk reads, no mutation.

## Why this exists

Step 3's mismatch guard used to be prose the agent walked by hand: "sort
keys, then compare — or sha256 of the canonicalized JSON". Folding the
canonicalization + compare into one verb removes the seam where an agent
could compare by raw `==` (and miss a key-order-equal match) or skip the
check and let a stale generator silently win.
