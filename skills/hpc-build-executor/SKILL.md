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

5. After scaffold succeeds, use the Read tool to load `data.path`, then customize: fill in `compute(args)` with the experiment's actual computation. **Do not** add an argparse parser here — under the new contract the per-executor CLI flag list lives in `.hpc/tasks.py` `FLAGS["<importable_module_path>"]`, not in the executor file. The dispatcher in `.hpc/cli.py` parses argv at runtime and calls `compute(args)`.

6. Smoke-test by importing the new module and calling `compute()` with a minimal Namespace:
   ```bash
   python -c "import argparse, importlib.util, sys; \
              spec = importlib.util.spec_from_file_location('m', '<data.path>'); \
              m = importlib.util.module_from_spec(spec); sys.modules['m'] = m; \
              spec.loader.exec_module(m); \
              m.compute(argparse.Namespace(output_file='/tmp/smoke.csv'))"
   ```
   Non-zero exit means fix-then-retry. (`--help` is not a useful smoke test for the new template — there's no `__main__` block; the dispatcher is the entry point.)

## Notes

- This skill writes to the experiment repo only — never to the framework repo's `templates/` dir. Confirm `--output-dir` is the experiment repo before invoking.
- Not idempotent: each successful call writes a file. Re-running with `--force` overwrites; without `--force` it errors. The envelope reports `idempotent: false`.
- Read-only with respect to the cluster — no SSH, no journal writes, no qsub. SSH env passthrough is not required for this skill.
- After scaffolding and customizing, the executor is auto-discovered by `hpc-discover` / `hpc-submit` if it lands in `executors/`, `scripts/`, or `src/` and exports `compute(args)` (new contract) — or has an `if __name__ == "__main__":` guard plus a CLI import (old contract; transitional).
- Per-task fan-out (Cartesian product, chunking, date windows, …) AND the new-contract executor's CLI flag list both live in `.hpc/tasks.py`, scaffolded by `/submit-hpc` Step 6 — not via this skill.
- Exit codes: 0 ok, 1 user error (`spec_invalid`, `executor_not_found`), 3 internal.
