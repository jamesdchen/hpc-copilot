---
name: hpc-build-executor
description: "Scaffold a new executor or shim file from a starter template into the experiment repo."
allowed-tools: Bash Read Write
---

Materialize one of the bundled starter templates (`plain` executor, `chunked` shim, `date-window` shim, blank `shim`) at a chosen path in the experiment repo. The CLI copies the template; the caller customizes it afterward.

## Steps

1. Choose the template `--type` based on the parallelism kind:
   - `plain` — standard executor scaffold (argparse + `compute()` stub). Use for fresh executors.
   - `chunked` — one task per row-index range. Use when the downstream script accepts `--start`/`--end` row indices.
   - `date-window` — one task per (start, end) date pair. Use when the user has a backtest with `start`, `end`, `chunk_duration`.
   - `shim` — blank shim template. Use when none of the above fit and you will hand-write `translate()`.

2. Choose `--name` (filename stem, no `.py`) and `--output-dir` (absolute path inside the experiment repo, NOT inside the framework repo).

3. Scaffold:
   ```bash
   hpc-mapreduce build-executor --name <stem> --output-dir <dir> --type <plain|chunked|date-window|shim>
   ```
   Add `--force` only if intentionally overwriting an existing file.

4. Parse the envelope. On `ok: true`:
   - `data.path` — absolute path of the new file.
   - `data.type` — confirms which template was used.
   - `data.source` — path of the template the bytes came from.

5. On error envelopes:
   - `spec_invalid` (user) — destination already exists and `--force` not set, or `--type` is unrecognized. Either pick a new `--name` or pass `--force`.
   - `config_invalid` — template missing on disk (a packaging bug). Surface to caller; do not retry.
   - `executor_not_found` — the `--output-dir` parent path is unwritable.

6. After scaffold succeeds, use the Read tool to load `data.path`, then customize:
   - `plain` — fill in `compute()` and add domain-specific argparse flags (`--horizon`, `--alpha`, etc.).
   - `chunked` — fill in `_compute_total_items()` and adjust `translate()` to map `chunk_id` to the downstream script's slicing flags.
   - `date-window` — fill in module-level constants `START`, `END`, `CHUNK_DUR`, `START_ARG`, `END_ARG`.
   - `shim` — hand-write `translate(chunk_id, total_chunks)` to return downstream args.

7. Smoke-test by invoking `python <data.path> --help` (for plain) or `python <data.path> --chunk-id 0 --total-chunks 1 -- echo test` (for shims). Non-zero exit means fix-then-retry.

## Notes

- This skill writes to the experiment repo only — never to the framework repo's `templates/` dir. Confirm `--output-dir` is the experiment repo before invoking.
- Not idempotent: each successful call writes a file. Re-running with `--force` overwrites; without `--force` it errors. The envelope reports `idempotent: false`.
- Read-only with respect to the cluster — no SSH, no journal writes, no qsub. SSH env passthrough is not required for this skill.
- After scaffolding and customizing, the executor is auto-discovered by `hpc-discover` / `hpc-submit` if it lands in `executors/`, `scripts/`, or `src/` and has both an `if __name__ == "__main__":` guard and a CLI import (argparse/click/typer/fire).
- Exit codes: 0 ok, 1 user error (`spec_invalid`, `executor_not_found`), 3 internal.
