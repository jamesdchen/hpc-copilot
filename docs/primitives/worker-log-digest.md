---
name: worker-log-digest
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent worker-log-digest --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.worker_log_digest.worker_log_digest
---
# worker-log-digest

Produce a **code-rendered, deterministic digest of a LOCAL worker log**. The
premortem told the LLM to open raw worker logs and eye them for `[throttle]` /
`[fatal]` markers — an unmechanized reading of untrusted log text, the run-#9
judgment-in-prose strike class (run-#10 finding G2). This verb mechanizes that
scan: it counts the lines carrying each known engine marker, reports the total
line count, echoes the last N lines **verbatim**, and renders a markdown
projection the caller relays without re-interpreting.

No SSH — a local file only. Detached-worker logs land under
`.hpc/_detached/<verb>-<run_id>-<token>.log`; a per-task cluster log fetched
locally works too. Read-only, and **fail-open**: a missing or unreadable file
returns a clear diagnostic in the envelope, never a traceback.

## Where the marker vocabulary comes from

The markers are derived from what the engine actually emits — one definition, in
`hpc_agent.ops.worker_log_digest.KNOWN_MARKERS`, cited to the emitting modules:

- The per-task **dispatcher** (`hpc_agent/execution/mapreduce/dispatch.py`) tags
  every line with the `[dispatch]` prefix and a severity word — `FATAL`,
  `FAILED`, `ERROR`, `WARN`. That is the compute-node worker whose per-task log
  is the primary "worker log".
- The SSH **connection engine** (`hpc_agent/infra/ssh_engine.py`) tags a
  throttled connect with `[throttle]` — the exact marker the premortem named.

Counting lines that *contain* each marker turns the manual grep into a
deterministic reduction. Matched case-sensitively (the engine emits these exact
spellings).

## Inputs

A `WorkerLogDigestSpec` (`hpc_agent._wire.queries.worker_log_digest`):

- `log_path` (string, required) — path to the log to digest, relative to
  `--experiment-dir` or an absolute path that resolves **within** it. A path
  that escapes the experiment dir is rejected (`spec_invalid`).
- `tail_lines` (int, default `50`, `>= 0`) — how many trailing lines to echo
  verbatim. `0` echoes none; the marker counts and total still compute.

## Outputs

`data` is a `WorkerLogDigestResult`:

```
{
  "log_path": "<resolved absolute path>",
  "exists": true,
  "readable": true,
  "error": null,
  "total_lines": 9,
  "tail_lines_requested": 50,
  "marker_counts": { "[throttle]": 2, "[dispatch] FATAL": 1, ... },
  "tail": ["<verbatim line>", "..."],
  "render": "<markdown digest, relayed verbatim>"
}
```

`marker_counts` always carries every known marker (0 when absent) so the shape
is stable. `render` is the code-authored markdown the caller surfaces verbatim —
never paraphrased into freeform prose about what the run did.

## Errors

- `spec_invalid` — `log_path` resolves outside the experiment dir (traversal
  refused). A missing/unreadable file does **not** error: it fails open with
  `readable=false` and an `error` string.

## Idempotency

Pure read of a local file; no state, no key. Digesting the same log twice yields
byte-identical output.

## Notes

Raw log text is **untrusted data**. The digest only counts fixed marker
substrings and echoes bytes verbatim — it attaches no meaning to a line and
makes no verdict about the run. The verbatim tail is fenced with `~~~~` so a
triple-backtick inside the log cannot break the relayed markdown.
