Help the user scaffold a new HPC executor for their experiment repo. The command is conversational: discover what's already there, ask what they want to build, produce the file, smoke-test it, and tell them the exact `/submit` command that will now work.

CLI shapes for every tool referenced below: see `docs/cli-contract.md`.

## Scope

Files produced by this command land in the **experiment repo** — the user's current working directory when they invoked `/build-executor`. They do NOT land in the `claude-hpc` framework repo. The templates under `templates/` in the framework repo are *sources* to copy from; never edit them in place.

Discovery uses the same contract as `/submit`: a file is an executor iff it parses and has (a) an `if __name__ == "__main__":` guard and (b) a CLI import (`argparse`, `click`, `typer`, or `fire`). No ABCs, no registry, no plugin protocol — `--help` is the interface.

## Arguments

`$ARGUMENTS` is free-form. Common forms:

| User says | Interpretation |
|-----------|---------------|
| (empty) | Start from Step 1, ask the user what to build |
| `"ml_elasticnet from ml_ridge"` | Mode (a): clone `ml_ridge.py` to `ml_elasticnet.py`, modify |
| `"scaffold ml_lasso"` | Mode (b): start from `templates/executor_template.py` |
| `"wrap scripts/my_train.py"` | Mode (c): generate a shim for the given user script |

Parse `$ARGUMENTS` before Step 1; skip ahead if intent is already clear.

## Step 1: Discover Existing Executors

Determine the experiment-repo root (the user's CWD). Then call the shared discovery helper — identical to what `/submit` uses so both commands see the same set of executors:

```python
from hpc_mapreduce import discover_executors
execs = discover_executors(".")   # returns list[ExecutorInfo]
```

`discover_executors` scans `executors/`, `scripts/`, and `src/` (in that order, collecting from every one that exists) and falls back to the repo root if none are present. Each returned `ExecutorInfo` has `path`, `name`, `cli_framework`, `imports`, and `docstring`.

For each executor, run `python <path> --help` to capture its concrete flag set (the helper only parses the source — it does not execute it). Present the inventory to the user:

```
Executors found in src/:
  ml_ridge.py    — argparse; uses sklearn — --horizon, --start, --end, --output-file
  ml_xgboost.py  — argparse; uses xgboost — --horizon, --start, --end, --output-file

What do you want to build?
  (a) Copy and modify one of the above
  (b) Scaffold a fresh executor from the hpc-mapreduce template
  (c) Wrap an already-written script (generate a shim only)
```

If `discover_executors` returns an empty list, skip the (a) option and note the directory was empty.

## Step 2: Ask What to Build

Based on the user's choice, branch to Step 3a, 3b, or 3c.

If the user is ambiguous ("make a ridge executor"), infer: existing `ml_ridge.py` matches → propose (a), propose a target filename, confirm.

## Step 3a: Copy and Modify an Existing Executor

1. Ask the user which source executor and what target path/name they want.
2. Copy the chosen file to the new path using the stdlib (`shutil.copyfile`, or read+write). The destination is always **under the user's experiment repo**, not the framework repo.
3. Read the destination file, then perform the user-described edits (model class, hyperparameters, features, etc.). Preserve the docstring style and section-header comments of the source.
4. Jump to Step 4 (verify).

## Step 3b: Scaffold From `executor_template.py`

1. Resolve the template path:
   ```bash
   python -c 'from hpc_mapreduce import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "templates" / "executor_template.py")'
   ```
2. Ask the user for the target path (e.g. `src/my_executor.py` or `executors/foo.py`).
3. Copy the template into the experiment repo at that path.
4. Walk the file and fill in every `# TODO:` marker based on what the user described — model type, data source, feature engineering, metric, etc. Keep the standard CLI args (`--data-path`, `--horizon`, `--start`, `--end`, `--output-file`) unless the user explicitly wants different flags.
5. Jump to Step 4 (verify).

## Step 3c: Wrap an Existing User Script (Shim Only)

Use this when the user already has a working script that does not match the grid-param CLI conventions. You will generate only a shim — the user's script is untouched.

1. Ask for the path to the user's script if not already given. Run `python <script> --help` and record its flags.
2. Resolve the shim template:
   ```bash
   python -c 'from hpc_mapreduce import _PACKAGE_ROOT; print(_PACKAGE_ROOT / "templates" / "shim_template.py")'
   ```
   (`chunking_shim.py` next to it is a concrete chunking example; pattern-match off it for data-length splits. For simpler translations, start from `shim_template.py`.)
3. Ask the user where the shim should live (default: `shims/<script_stem>_shim.py` in the experiment repo).
4. Copy the template to that path, then customize `translate()` to map `--chunk-id`/`--total-chunks` onto the user-script flags discovered in step 1.
5. The shim contract (see `hpc_mapreduce/map/shim.py` for the cache side — do not modify it here):
   - The shim script itself is a normal `.py` file that accepts `--chunk-id`, `--total-chunks`, then `--` and the downstream command.
   - Its `translate(chunk_id, total_chunks)` returns extra CLI args to append.
   - It invokes the downstream command via `subprocess.run` and forwards the return code.
6. Jump to Step 4 (verify).

## Step 4: Smoke-Test

Run `python <new_executor> --help` (or `python <new_shim> --chunk-id 0 --total-chunks 1 -- echo test` for a shim). Capture stdout. Report:

- `Smoke test OK: <path> --help returned <N> flags.`
- Echo the discovered flags back to the user so they can confirm the CLI matches their intent.

If the smoke test fails (ImportError, SyntaxError, `--help` non-zero exit), read the file you just wrote, fix the issue, and re-run. Do not finish with a broken file.

## Step 5: Tell the User How to Submit

Print the exact `/submit` invocation that will now work with the new executor. Examples:

```
Ready. Try:
  /submit run <new_name>
or with grid overrides:
  /submit run <new_name> horizon=[1,5,25]
```

For a shim, make the instruction explicit:

```
Shim generated at shims/<script>_shim.py. /submit will auto-detect it when
the profile's run command is set to:
  run: "python3 shims/<script>_shim.py -- python3 <user_script>"
```

## Step 6: Cache and Report

Save to Claude Code memory for this project:

- The directory where the new executor landed (so `/submit`'s discovery finds it next time).
- If (c): the mapping from downstream script -> shim path.

End with a concise report: what was created, where, and the `/submit` command that exercises it.

## Edge Cases

| Situation | Handling |
|-----------|----------|
| Target path already exists | Ask before overwriting; offer a dated suffix |
| `executors/`, `scripts/`, and `src/` all missing | Ask the user where executors should live; create that dir |
| User script has no `--help` | Wrap it anyway, but warn the smoke test is a plain run — skip `--help` |
| `discover_executors` returns 0 entries and user asked for (a) | Fall back to (b) and inform the user |
| Framework-repo path accidentally used as CWD | Detect via presence of `hpc_mapreduce/` and `commands/build-executor.md`; refuse and ask the user to `cd` into their experiment repo first |

## Do Not

- Do not create or edit files under the `hpc_mapreduce/` package, the `templates/` directory, or the `commands/` directory of the framework repo. Those are framework sources.
- Do not invent new protocols, ABCs, or required helper functions for the generated executor. The contract remains `argparse --help` + optional shim.
- Do not run the downstream training loop during smoke-testing — `--help` only.
