---
name: smoke-test-executor
verb: validate
side_effects:
- runs: user executor's compute(args) in a child python -c
- filesystem: <output_file> (whatever the executor writes)
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent smoke-test-executor --module-path <module_path> [--output-file <output_file>]
  python: hpc_agent.ops.smoke_test_executor.smoke_test_executor
---
# smoke-test-executor

Smoke-test a freshly-scaffolded executor: import its module from a file
path and call `compute(Namespace(output_file=...))` in a child process,
then report `{exit_code, stdout_tail, stderr_tail}` so `hpc-build-executor`
can branch deterministically (non-zero `exit_code` = fix-then-retry).

## Inputs / outputs

See `hpc_agent/schemas/smoke_test_executor.{input,output}.json`. Input
requires only `module_path` (typically `build-executor`'s `data.path`);
`output_file` defaults to a throwaway under `/tmp`. Output carries
`exit_code` (`null` only on timeout), `timed_out`, and the tailed
`stdout_tail` / `stderr_tail`.

## Why this exists

`hpc-build-executor`'s execution-style header forbids arbitrary
`python -c` / `bash -c` — auto-mode's permission classifier hard-blocks
those patterns *regardless of allow rules*, so issuing one stalls the
workflow on a non-bypassable prompt. Yet the skill's own Step 6 smoke
test *was* an inline `python -c "import argparse, importlib.util, sys;
... m.compute(...)"`. This verb folds that exact recipe into one
deterministic CLI call the classifier permits, removing the
self-contradiction.

The new-contract executor template has no `__main__` block — `compute`
IS the entry point — so `--help` is not a useful smoke test and a bare
import is insufficient. We must actually *call* `compute`, the same way
`.hpc/cli.py` dispatches it at runtime.

## Why a subprocess

The executor is unreviewed user code. It may `sys.exit`, segfault,
spin, or leak global state. Running it in a child process (rather than
`exec_module` in-process) keeps a crashing or exiting module from taking
down the CLI, bounds it with a `timeout_sec`, and gives a clean
stdout/stderr capture to tail back to the agent. The child runs the
canonical four-line load-and-call recipe; the parent reports its outcome.

The probe uses `sys.executable` for the child so it runs the same
interpreter — and therefore sees the same installed framework — as the
CLI process.

## Failure semantics

The composite returns `ok: true` at the outer envelope **even when the
probed executor fails** — a failing executor is a normal, expected
outcome the skill branches on, not a CLI error. The signal is
`data.exit_code`:

- `0` — the module imported and `compute()` ran clean. Proceed.
- non-zero — fix-then-retry; the traceback's final frames are in
  `data.stderr_tail`.
- `null` with `data.timed_out: true` — the probe exceeded `timeout_sec`
  and was killed (a module that spins or blocks on input). `stderr_tail`
  carries a `timed out after <N>s` marker plus whatever the child
  emitted before the kill.

The streams are tailed (last ~2000 chars each) so the envelope stays
small while still carrying the actionable tail of a traceback.
