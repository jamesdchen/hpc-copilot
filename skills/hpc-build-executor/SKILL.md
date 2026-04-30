---
name: hpc-build-executor
description: "Scaffold a new executor file from the starter template into the experiment repo."
allowed-tools: Bash Read Write
---

Materialize the bundled starter template (`plain` executor) at a chosen path in the experiment repo. The CLI copies the template; the caller customizes it afterward.

## Steps

1. Choose `--name` (filename stem, no `.py`) and `--output-dir` (absolute path inside the experiment repo, NOT inside the framework repo).

2. Scaffold:
   ```bash
   hpc-mapreduce build-executor --name <stem> --output-dir <dir>
   ```
   Add `--force` only if intentionally overwriting an existing file.

3. Parse the envelope. On `ok: true`:
   - `data.path` — absolute path of the new file.
   - `data.type` — confirms which template was used (`plain`).
   - `data.source` — path of the template the bytes came from.

4. On error envelopes:
   - `spec_invalid` (user) — destination already exists and `--force` not set, or `--type` is unrecognized. Either pick a new `--name` or pass `--force`.
   - `config_invalid` — template missing on disk (a packaging bug). Surface to caller; do not retry.
   - `executor_not_found` — the `--output-dir` parent path is unwritable.

5. After scaffold succeeds, use the Read tool to load `data.path`, then customize: fill in `compute()` and add domain-specific argparse flags (`--horizon`, `--alpha`, etc.).

6. Smoke-test by invoking `python <data.path> --help`. Non-zero exit means fix-then-retry.

## Notes

- This skill writes to the experiment repo only — never to the framework repo's `templates/` dir. Confirm `--output-dir` is the experiment repo before invoking.
- Not idempotent: each successful call writes a file. Re-running with `--force` overwrites; without `--force` it errors. The envelope reports `idempotent: false`.
- Read-only with respect to the cluster — no SSH, no journal writes, no qsub. SSH env passthrough is not required for this skill.
- After scaffolding and customizing, the executor is auto-discovered by `hpc-discover` / `hpc-submit` if it lands in `executors/`, `scripts/`, or `src/` and has both an `if __name__ == "__main__":` guard and a CLI import (argparse/click/typer/fire).
- Per-task fan-out (Cartesian product, chunking, date windows, …) is expressed inline in `.hpc/tasks.py`, scaffolded separately by `/submit` Step 6 — not via this skill.
- Exit codes: 0 ok, 1 user error (`spec_invalid`, `executor_not_found`), 3 internal.
