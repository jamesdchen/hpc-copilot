# Mutation testing with mutmut

Mutation testing surfaces tests that pass for the wrong reason — i.e.
tests that **mock around the function under test** instead of exercising
it. Concretely: the audit that landed in `claude/repo-audit-fixes-AP3FD`
found a sidecar-dataflow bug in `update_run_constraints` that the unit
test was masking by manually patching `ssh_target` into the sidecar
JSON. Mutmut would have surfaced it as a surviving mutant on the
`sidecar.get("ssh_target")` line.

Not in CI. A full sweep of `src/` is hours of single-core work. Run
targeted sweeps locally when auditing a specific subject.

## Quickstart

```bash
# 1. Install dev deps including mutmut.
uv pip install mutmut

# 2. Scope to one subject — full src/ is too slow.
uv run mutmut run --paths-to-mutate src/hpc_agent/ops/monitor/

# 3. List surviving mutants (the bugs your tests don't catch).
uv run mutmut results

# 4. Inspect one mutant.
uv run mutmut show <id>

# 5. After fixing tests (or accepting a mutant as semantically inert),
#    re-run only the mutants that still survive:
uv run mutmut run --rerun-all
```

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
  surface. The `[tool.mutmut].exclude` list in `pyproject.toml` skips
  `_wire/` for that reason.
- **Defensive `_ = unused` lines** in tests.
- **`if __name__ == "__main__":` blocks** — pragma-no-cover territory.

## Why mutmut and not (cosmic-ray / mutpy / etc.)

mutmut is the only mutation tool that ships sensible defaults for
Python 3.10+ and integrates with pytest out of the box. cosmic-ray
is more configurable but the YAML config is ~2x the LoC for the same
behavior. mutpy is Python-2-era and abandoned.

## Triage checklist when a mutant survives

1. **Read the mutant** with `mutmut show <id>` — what literal flipped?
2. **Find the test** that should have caught it. `pytest -k <function>`
   and trace.
3. **Decide**: is the mutant semantically equivalent? (Branch that's
   purely defensive; constant that's never read; etc.) If yes, mark it
   with a `# mutmut-skip` comment and document why.
4. **Otherwise**: write a test that exercises the mutated branch
   end-to-end, not via mock-the-function-under-test.

The goal is **no surviving non-equivalent mutants in the directory
under audit**. A clean sweep is the test suite saying "I really did
exercise every branch."
