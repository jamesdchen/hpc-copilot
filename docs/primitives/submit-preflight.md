---
name: submit-preflight
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent submit-preflight --experiment-dir <experiment_dir> [--cluster <cluster>]
  python: hpc_agent.ops.submit_preflight.submit_preflight
---
# submit-preflight

Composite preflight at the top of every `hpc-submit` invocation: runs
`install-commands` → `load-context` → (when `--cluster` is supplied)
`check-preflight` as one CLI call. Mirror of `status-preflight` with a
cluster SSH-connectivity check on top — submit needs SSH at submit time
(`status` does not).

## Repurpose note

This verb previously fanned `export-package` + `plan-throughput` +
`validate-campaign` out in parallel — the framing the 2026-06-04 demo
agent improvised. Inspection of the canonical `worker_prompts/submit.md`
shows the trio is at three separate Steps (0 / 4b / 6c) with hard data
dependencies (plan-throughput needs `total_tasks` from grid expansion;
validate-campaign needs the assembled spec). It can't actually be
parallelised without restructuring the submit flow.

The repurposed verb is the genuinely-composable boilerplate the WS5
audit's `<skill>-preflight` row described: install + load + cluster-
connectivity. The trio stays at its original three Steps individually.

## Inputs / outputs

See `hpc_agent/schemas/submit_preflight.{input,output}.json`. Input
requires only `experiment_dir`; `cluster` is optional. Output carries a
`SubResult` per fanned-out sub-call under `data.install_commands`,
`data.load_context`, `data.check_preflight`.

## Internal composition

Sequential, plain `subprocess.run`. `install-commands` must succeed
before `load-context` can resolve framework paths reliably;
`check-preflight` runs last because it's the most expensive call (~5s
SSH round-trip on the slow path) and we want the cheap local checks to
fail-fast.

## Cluster SSH check

When `--cluster` is supplied, `check-preflight` runs with the same flag,
which triggers its `cluster_tcp_22` + `cluster_ssh_echo` probes. The
ssh_echo probe runs an actual `ssh <host> echo ok` round-trip through
the production `ssh_argv` / multiplex / crypto path — so a green here
means the submit can actually talk to the cluster, not just that port 22
is open. Catches the 2026-06-04 class where TCP passed and `rsync push`
then failed mid-submit with `getsockname failed: Not a socket`.

Without `--cluster`, only the local-env checks fire (ssh agent, ssh/rsync
on PATH, clusters.yaml parses).

## Failure semantics

`overall: "pass"` iff every non-skipped sub-call returned `ok: true`.
Any non-skipped sub-call returning `ok: false` flips `overall: "fail"`.
The composite itself returns `ok: true` at the outer envelope; the
failing sub-call's verbatim envelope is preserved under
`data.<subcall>.envelope` so the caller can read its `error_code` +
`remediation` without re-running.

Sibling work is preserved on failure — a `check-preflight` failure
doesn't lose the install-commands or load-context results.

## Why this exists

The agent's prose-discipline at the top of every `hpc-submit` used to be:
"Step 0: install-commands. Step 1: load-context." Step 0 omission
motivated the entire 0.10.2 release. Folding both (plus the cluster
ssh check) into one verb makes the omission structurally impossible.
