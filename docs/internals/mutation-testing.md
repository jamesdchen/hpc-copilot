# Mutation testing with mutmut

Mutation testing surfaces tests that pass for the wrong reason — i.e.
tests that **mock around the function under test** instead of exercising
it. Concretely: the audit that landed in `claude/repo-audit-fixes-AP3FD`
found a sidecar-dataflow bug in `update_run_constraints` that the unit
test was masking by manually patching `ssh_target` into the sidecar
JSON. Mutmut would have surfaced it as a surviving mutant on the
`sidecar.get("ssh_target")` line — *if* mutmut could mutate the
function (see "limitations" below).

Not in CI. A full sweep of `src/` is hours of single-core work. Run
targeted sweeps locally when auditing a specific subject.

## Quickstart

```bash
# 1. Install mutmut into the dev env.
uv pip install mutmut

# 2. Scope to one directory or file — edit
#    [tool.mutmut].paths_to_mutate in pyproject.toml. mutmut 3.x
#    doesn't expose --paths-to-mutate on the CLI; the config is the
#    only way to scope.

# 3. Wipe any previous mutants/ tree to start clean.
rm -rf mutants/

# 4. Run the sweep.
uv run mutmut run

# 5. List surviving mutants (the bugs your tests don't catch).
cat mutants/<source-file>.meta | jq '.exit_code_by_key'

# 6. Inspect one mutant.
uv run mutmut show <mutant-id>
```

Exit codes in the meta JSON:

- `1` — killed (the test suite caught the mutation)
- `0` — survived (mutation was undetected by any test)
- `34` — skipped (mutmut chose not to test this mutant)

## What to look at first

Surviving mutants cluster in branches the tests don't reach. Prioritise:

- **Functions with high-severity behavior** — primitives that touch the
  cluster (anything in `infra/remote.py`, `ops/submit_flow.py`,
  `ops/aggregate_flow.py`) where a silent wrong-path is expensive.
- **Functions whose tests mock the function under test** — grep tests
  for `mock.patch(<module-under-test>.<symbol>)` patterns; those are
  red flags for the test-masks-bug class.
- **Branches that exist only for error / edge conditions** — `if not
  job_ids: raise ...` style guards. Mutating `not` to a no-op tests
  whether anything actually exercises the guard.

## What to ignore

- **Pydantic `Field(default=...)` mutations** — flipping a default
  value is technically a live mutant but semantically meaningless to
  surface. The `[tool.mutmut].do_not_mutate` glob in `pyproject.toml`
  skips `_wire/` for that reason.
- **Defensive `_ = unused` lines** in tests.
- **`if __name__ == "__main__":` blocks** — pragma-no-cover territory.
- **`<dict>.get(key) or 0` fallback mutations** when the tests always
  pass a complete dict — the fallback is unexercised by design, and
  the mutation is semantically benign in production.

## Limitations of mutmut 3.x for this codebase

1. **Lazy imports inside functions block mutation.** A function with
   ``from X import Y`` in its body is silently skipped by mutmut.
   This codebase uses lazy imports heavily (most of `ops/*` does it
   to avoid registry-import cycles), so big primitives like
   `update_run_constraints`, `submit_flow`, `aggregate_flow` get few
   to zero mutants. Smaller helpers (`_validate_feature`,
   `_seconds_to_cron`, `_classify_state`) get full coverage.

2. **No CLI `--paths-to-mutate`.** Scoping is config-only; edit
   `pyproject.toml` for each sweep. Annoying but cheap.

3. **`pytest-xdist` workers don't inherit mutmut's process context.**
   The mutated source's trampoline does ``from mutmut.__main__ import …``
   which only resolves in mutmut's own pytest invocation. The
   `pytest_add_cli_args` setting in `pyproject.toml` overrides the
   project's `-n auto` addopts to force serial execution.

4. **Multiprocessing-using tests must be deselected.** Same root
   cause as #3. The config already deselects the two such tests in
   this repo (`test_concurrent_writers_lose_no_samples`,
   `test_atomic_locked_update`). Add new ones as they appear.

## Triage checklist when a mutant survives

1. **Read the mutant** with `mutmut show <id>` — what literal flipped?
2. **Find the test** that should have caught it. `pytest -k <function>`
   and trace.
3. **Decide**: is the mutant semantically equivalent? (Branch that's
   purely defensive; constant that's never read; fallback unreached
   in practice; etc.) If yes, document why and move on.
4. **Otherwise**: write a test that exercises the mutated branch
   end-to-end, not via mock-the-function-under-test.

The goal is **no surviving non-equivalent mutants in the directory
under audit**. A clean sweep is the test suite saying "I really did
exercise every branch."

## Empirical baseline

The first targeted run against `src/hpc_agent/ops/monitor/arm.py`
(the cadence-decision module) surfaced:

- `_seconds_to_cron`: 19/21 killed (90% — tight)
- `_classify_state`: 40/82 killed (49%)

Most `_classify_state` survivors fall into two patterns:

- **Off-by-one on threshold constants** — `queue_wait_sec >= 3600`
  vs `> 3600`. Tests probe well-inside-the-range values but not
  exactly-at-boundary. Real test gap.
- **`<dict>.get(key) or 0` fallback flips** — tests always pass a
  complete summary dict, so the `or 0` fallback is never exercised.
  Semantically benign in production but documents a coverage gap.

Neither category was a *bug*. Mutation testing in this codebase
mostly tells you where tests are loose, not where bugs hide. The
sidecar-field-reads lint and the prose-literal-drift lint
(`tests/contracts/test_lint_*.py`) are the higher-leverage tools
for catching the dataflow-style bugs the audit actually found.

