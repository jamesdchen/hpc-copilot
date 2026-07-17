# Mutation testing with mutmut

Mutation testing surfaces tests that pass for the wrong reason — i.e.
tests that **mock around the function under test** instead of exercising
it. Concretely: the audit that landed in `claude/repo-audit-fixes-AP3FD`
found a sidecar-dataflow bug in `update_run_constraints` that the unit
test was masking by manually patching `ssh_target` into the sidecar
JSON. Mutmut would have surfaced it as a surviving mutant on the
`sidecar.get("ssh_target")` line — *if* mutmut could mutate the
function (see "limitations" below).

Not a CI gate. A full sweep of `src/` is hours of single-core work. Run
**curated, scoped** sweeps (see below) — never a full-tree sweep.

## Platform: mutmut is Linux-only (Windows is CI-only)

mutmut 3.x **cannot run on native Windows**: at import it does
`if platform.system() == "Windows": sys.exit(1)`, and it imports the POSIX-only
`resource` module — so even patching the guard fails. On this project's native-
Windows dev box the real sweep therefore runs **only in CI (Linux)** or under a
Linux checkout. This is fine: mutmut's value is the periodic signal, not an
inner-loop tool, and a full local sweep would freeze the box (against project
policy) anyway. `scripts/run_mutation.py` refuses to launch mutmut off Linux and
points you at the CI workflow; its `--dry-run` (config validation only) works
everywhere.

## The curated per-module runner (`scripts/run_mutation.py`)

The recommended entry point. It encapsulates a **curated module map** — a handful
of high-value, pure-logic modules, each paired with the focused test file(s) that
exercise it — so you never hand-edit `[tool.mutmut]` or assemble mutmut CLI args.
Per-module test selection (mutmut's `tests_dir` accepts individual test-file
paths, which feed `pytest_add_cli_args_test_selection`) keeps one module's scoped
sweep small enough to finish inside a single CI step, instead of re-running the
whole 8k-test suite per mutant.

```bash
python scripts/run_mutation.py --list                    # the module map
python scripts/run_mutation.py --module block-chain       # one scoped sweep (Linux)
python scripts/run_mutation.py --module block-chain --dry-run   # validate config (any OS)
```

The map (edit `MODULE_MAP` in the script to add/adjust; the CI matrix reads it
via `--keys`, so the two never drift):

| key | module | tests | notes |
|---|---|---|---|
| `block-chain` | `infra/block_chain.py` | `tests/ops/test_block_chain.py` | Deterministic successor tables + spec composition. Zero body-imports → fully mutmut-reachable. **Reference target.** |
| `attestation` | `state/attestation.py` | `tests/state/test_attestation.py` | Attestation kernel (validate/bind/reduce). Pure logic. |
| `describe-cache` | `state/describe_cache.py` | `tests/cli/test_describe_cache.py` | Build-content-keyed cache; guard-heavy. Some lazy imports blind mutmut. |
| `fast-path-cache` | `cli/_fast_path_cache.py` | `tests/cli/test_fast_dispatch.py` | CLI fast-path resolution cache. Guard + fingerprint logic. |
| `capabilities-cache` | `state/capabilities_cache.py` | `tests/cli/test_capabilities_cache.py` | Build+dist-keyed capabilities-envelope cache; guard-heavy, byte-identical to the walk. Some lazy imports blind mutmut. |
| `combiner` | `execution/mapreduce/combiner.py` | `tests/execution/mapreduce/test_combiner*.py` | The reduce/combine that computes every aggregate number. **Heavy (~650 lines)** — the slowest key. |

The runner backs up `pyproject.toml` to a sidecar, writes the scoped
`[tool.mutmut]` block, runs mutmut, and **always restores** the original in a
`finally` (a stale sidecar from an interrupted run is recovered on the next
start), so the committed defaults and the sibling `mutmut_shortlist.py` /
scheduled cluster-verb sweep are never perturbed.

**When to run it:** after hardening a module (did the new tests actually pin the
branches?); before trusting a test battery someone claims is thorough. **Non-
goals:** it is **not a CI gate** (`.github/workflows/mutation.yml`'s curated job
is `workflow_dispatch`-only and `continue-on-error`), and **survivors are LEADS,
not defects** — triage each per the checklist below; most are loose tests or
semantically-equivalent mutants, not bugs.

## Quickstart (raw mutmut, Linux)

Prefer `scripts/run_mutation.py` above; this is the underlying manual flow.

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

## The reachability shortlist + scheduled sweep (cluster verbs)

Limitation #1 above is the load-bearing one: the lazy-import pattern blinds
mutmut on exactly the high-severity cluster verbs (`submit_flow`,
`aggregate_flow`, transport), where a silent wrong-path costs real cluster
time. `scripts/mutmut_shortlist.py` turns that blind spot into an auditable
shortlist instead of a silent gap, and `.github/workflows/mutation.yml` runs a
scoped sweep on a schedule (weekly + `workflow_dispatch`, **never per-PR** — a
sweep re-runs the suite once per mutant and is far too slow for the merge
gate; the value is the periodic signal, uploaded as an artifact, never
blocking).

### `scripts/mutmut_shortlist.py` — two static (AST-only) jobs

- **`report` (default)** — for each cluster-verb module, classify every
  top-level function / method as mutmut-**reachable** (no body import; mutmut
  can mutate it) or mutmut-**BLIND** (a lazy import blocks it). The BLIND set
  is the **extraction shortlist**: the concrete functions whose module-scope
  import extraction would buy new mutation coverage. `--json` for machine
  output.

- **`paths`** — emit the newline-separated source paths to scope
  `[tool.mutmut].paths_to_mutate` to. `--changed-since REF` intersects the
  target set with `git diff --name-only REF` (the *paths-changed shortlist* —
  a dispatch can sweep just the cluster verbs a branch touched).
  `--apply-to-pyproject PATH` rewrites the `paths_to_mutate` array in place
  (a minimal line-based edit that preserves the sibling `also_copy` /
  `do_not_mutate` / `pytest_add_cli_args` keys); the scheduled workflow uses
  this on its **ephemeral** checkout, so the repo's `pyproject.toml` is never
  committed-scoped.

The target module list lives in `DEFAULT_TARGETS` in the script; override with
`--targets`. Neither mode runs mutmut or the suite — it is safe anywhere.

### Empirical shortlist (2026-07-16, HEAD `0592ed99`)

Running `report` over the five cluster-verb modules found **85 reachable, 47
blind** — the "few-to-zero mutants" framing in limitation #1 is true only for
the *biggest* primitives (`aggregate_flow` itself, `_aggregate_flow_impl`,
`_submit_flow_batch_locked` are all blind), not the modules wholesale. So a
scoped sweep is worth running **today**, before any extraction, on the 85
reachable functions; the 47-function extraction shortlist then grows coverage
incrementally.

### Do NOT bulk-extract the lazy imports

`submit_flow` / `aggregate_flow` / transport are latency-hot and heavily
churned; the lazy-import pattern is deliberate (avoids registry-import cycles
and keeps cold-start import cost down — see the latency plan). Extraction is
therefore **opportunistic and per-function**, only where a blind function
holds high-severity branch logic and its import can move to module scope
without creating a cycle or measurable cold-start cost. The shortlist names
the candidates; it does not license a sweep-wide refactor. When you do extract
one, re-run `report` to confirm it moved from BLIND to reachable, and add the
end-to-end test that the new mutants demand.

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

