---
name: hpc-build-executor
description: "Scaffold a new executor file from the starter template into the experiment repo."
allowed-tools: Bash Read Write
---

Agent-facing composition over the **[build-executor](../../docs/primitives/build-executor.md) primitive** (see that file for full input/output/error contract). Materializes the bundled starter template at a chosen path; the caller customizes it afterward.

## Steps

1. **Choose `--name`** (filename stem, no `.py`) and `--output-dir` (absolute path inside the experiment repo, NOT inside the framework repo).

2. **Invoke** [build-executor](../../docs/primitives/build-executor.md). Add `--force` only if intentionally overwriting an existing file.

3. **Parse the envelope** per the primitive's `outputs:` contract (`path`, `type`, `source`).

4. **On error envelopes**, branch by `error_code` per the primitive's frontmatter table — common: `spec_invalid` (destination exists; pass `--force` or pick a new name), `config_invalid` (template missing on disk; packaging bug, surface to caller), `executor_not_found` (output_dir parent unwritable).

5. **After scaffold succeeds**, use the Read tool to load `data.path`, then customize: fill in `compute(args)` with the experiment's actual computation. **Do not** add an argparse parser here — under the new contract the per-executor CLI flag list lives in `.hpc/tasks.py` `FLAGS["<importable_module_path>"]`, not in the executor file. The dispatcher in `.hpc/cli.py` parses argv at runtime and calls `compute(args)`.

6. **Smoke-test** by importing the new module and calling `compute()` with a minimal Namespace:

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
- After scaffolding and customizing, the executor is auto-discovered by [discover-executors](../../docs/primitives/discover-executors.md) (which `hpc-submit` invokes) if it lands in `executors/`, `scripts/`, or `src/` and either exports `compute(args)` (new contract) or has an `if __name__ == "__main__":` guard plus a CLI import (old contract; transitional). See `discover-executors`'s Notes for the contract classification rules.
- Per-task fan-out (Cartesian product, chunking, date windows, …) AND the new-contract executor's CLI flag list both live in `.hpc/tasks.py`, scaffolded by `/submit-hpc` Step 6 — not via this skill.
