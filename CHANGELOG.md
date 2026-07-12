# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
on the wire surface enumerated in
[`docs/integrations/CONTRACT.md`](docs/integrations/CONTRACT.md).

Full history: entries older than the current minor series (0.10.x and
earlier) moved verbatim to `docs/changelog/` to keep this file a manageable
size (2026-07-09 reorg, `docs/internals/audit-2026-07-09.md` R3):

- [`docs/changelog/0.10.0-0.10.64.md`](docs/changelog/0.10.0-0.10.64.md)
- [`docs/changelog/0.6.0-0.9.0.md`](docs/changelog/0.6.0-0.9.0.md)
- [`docs/changelog/0.4.0-0.5.0.md`](docs/changelog/0.4.0-0.5.0.md)
- [`docs/changelog/0.3.0.md`](docs/changelog/0.3.0.md)
- [`docs/changelog/0.2.0.md`](docs/changelog/0.2.0.md)

## [Unreleased] — hpc-copilot fork: human-amplification block architecture

First implementation wave of the fork's guiding design
([`docs/design/human-amplification-blocks.md`](docs/design/human-amplification-blocks.md)):
workflows decompose into **blocks** that chain deterministically in code and
terminate at human decision points with code-digested **briefs**. No decision
point is resolved by the LLM; the LLM only drafts proposals over code-digested
evidence and relays the human's `y`/nudge. Registry grew 101 → 121 primitives.

### Added — MCP elicitation (the second capability-1 channel, 2026-07-08)

- The MCP server's hand-rolled JSON-RPC pump is now **bidirectional**
  (`docs/design/mcp-elicitation.md` D1): one daemon stdin-reader thread + a
  message queue give tool handlers a real blocking-with-timeout
  `_request_from_client` wait that services interleaved client requests
  inline (never head-of-line-blocking). No SDK, no new dependency.
- **Server-initiated `elicitation/create`** fires at ONE site: an
  `append-decision` authorship refusal (machine-readable
  `failure_features.authorship_evidence` marker) with a per-session client
  capability detected at `initialize`. The prompt is CODE-RENDERED (never
  model-authored), the response is filtered (free-text only,
  `is_harness_injected` refused) and appended harness-side, the identical
  invocation retries exactly once, and the model sees only
  `{elicitation: "captured", sha256}` — never the human's text.
  Decline/cancel/timeout degrade silently to the hook path; the authorship
  bar is unchanged (a channel, never a waiver).
- `harness-capabilities` evidence reshaped: `elicitation_server` (verified
  code capability, now `True`) + `elicitation_client: "per-session"` — the
  honest split a separate-process probe can report.

### Added — block verbs (thin orchestrators over the existing rings)

- **`submit-s1..s4`** — resolve (the ambiguity envelope surfaced as a brief;
  old `apply-safe-defaults` output becomes a **pre-filled recommendation**, never
  auto-applied) · stage & canary (stops at "canary green, est. N core-hours";
  core-hours wired from `infra/cost`) · submit & watch (post-greenlight main
  launch via `launch_main_array`, guarded by a code-drift check against the
  canary-time sidecar so "what runs" can't silently diverge from "what the human
  greenlit") · harvest. `submit_and_verify` gains `stop_after_canary` (default
  `False` — fused behavior byte-identical for existing callers).
- **`status-snapshot` / `status-watch`**, **`aggregate-check` / `aggregate-run`**
  (integrity issues surfaced, never auto-masked), **`campaign-greenlight` /
  `campaign-watch` / `campaign-complete`** (spec greenlit once, then async).
- **`next_block`** on every block Result — a machine-computed next-step
  suggestion (verb + why + spec hint); the human greenlights the *named* verb and
  `ops/block_gate.py` verifies the journaled greenlight names it, so a
  mis-sequenced call fails loudly. **`submit-speculate`** runs a speculative
  canary during S1 review (budget of 1, nudge-invalidation both free via the
  canary TTL cache).

### Added — opt-in continuous-async campaign refill (RFC #362, Phase 1)

- **`campaign-refill`** — the autonomous refill actor
  (`ops/campaign_refill.py`). Once a campaign is greenlit and its manifest sets
  `async_refill`, the pool is kept ~full instead of draining to zero at each
  iteration barrier: each tick calls `campaign-advance` authoritatively and, on
  `decision == "refill"`, resolves + detach-submits `refill_count` fresh
  iterations **sequentially** through `resolve-submit-inputs` (the per-slot
  sidecar write advances the async optuna scaffold's proposal index, so each
  slot gets a **distinct** trial) + `campaign-run` (the per-iteration spine).
  No new state files, no cursor — partial ticks self-correct via
  `in_flight`-shrinking `refill_count`. The greenlit manifest is the standing
  consent; `campaign-refill` refuses an un-greenlit campaign and carries no
  per-iteration human boundary.
- **Wiring:** `campaign-watch` gains a fourth no-boundary terminator
  `watching_refill` (split out of `watching_healthy`); `block-drive` chains
  `campaign-watch/watching_refill → campaign-refill` in code and ends the chain
  there (the next tick re-enters via `campaign-watch` — one step per tick).
  `load-context` routes a deterministic `kind="cli"` refill step when async is
  on, the manifest is greenlit, and advance decided `refill`.
- **Opt-in & default-safe:** with `async_refill` unset the behavior is
  byte-identical to the synchronous batch loop (property-tested); every new
  branch is dead unless the flag is set. **Not yet non-experimental:** the
  Phase-2 live-verify gate (`scripts/campaign_async_live_verify.py`, RFC §10)
  has not run on a real cluster.

### Added — §5 recovery machine

- **Watchdog / dead-man's switch:** every driver + monitor tick stamps
  `last_tick_at`/`next_tick_due` (initial deadline stamped at submit, so a
  never-ticked run is still detectable); new **`doctor`** verb (detection-only)
  surfaces stalled/orphaned runs as drafted re-arm proposals; **`doctor-install`**
  opt-in OS-scheduler installer (`schtasks`/cron) + notify. Session-death
  recovery rides the doctor.
- **Kill semantics:** new **`kill`** verb — journaled intent → backend-seam
  cancellation (new `build_cancel_cmd`: `scancel`/`qdel`/PBS) → verified against
  the scheduler → honest "N requested, M confirmed gone"; a full kill settles
  through `reconcile`/`settle` (one-definition rule). Kill telemetry line added
  to the monitor summary.
- **Guaranteed harvest:** every terminal path — complete/failed/timeout/
  abandoned/kill/abnormal-exit — ends in a best-effort, loud code-harvest
  (`harvest_on_terminal`, durable `<run_id>.harvest.jsonl`); the poll loop is
  wrapped in `try/finally`; reconcile harvests on verdict *transitions* only.
- **Cluster-side watcher (`watcher-install`):** install-time probe ladder
  (crontab → scrontab → self-resubmitting job → loud none); a stdlib-only
  cluster script writes a heartbeat and alarms on a stale `last_read`, folded
  into the existing reporter SSH call at zero extra round-trip.
- **Telemetry contract:** every emitted field declares cumulative vs per-tick
  delta (`FIELD_KIND` + `scripts/lint_telemetry_labels.py`, wired into
  pre-commit + CI). **Campaign loud-fail default:** the per-task resubmit
  backstop now fires by default (cap 2, manifest-overridable); manifest gains
  `anomaly_policy` + `greenlit`/`greenlit_at`; `campaign-advance` emits a typed
  `anomaly_brief`.

### Added — §2 decision journal

- **`append-decision` / `read-decisions`** over append-only per-scope
  `decisions.jsonl` — one record per `y`/nudge exchange (evidence digest,
  proposal, response, resolved decision): the durable "why the run took its
  shape" record, generalizing the failure-only `verdict_history`.

### Added — never-stall + surface

- **Detach-by-contract:** `detach: true` default on the scheduler-bound block
  verbs — the parent returns a handle immediately and a fully-detached child (no
  `claude -p`, no LLM) owns the poll; briefs arrive via the journal + tail-loop /
  doctor / cluster-watcher. Survives session death.
- **MCP surface:** `hpc-agent mcp-serve` is the preferred block-invocation
  surface (typed tools, no shell affordance, cancel/raw-submit structurally
  unreachable). A **warm in-process runner** (default) reuses the loaded registry
  instead of a per-call subprocess cold start; a **curated catalog** derives the
  block toolset from the `next_block` field (no hardcoded list). `install-commands`
  registers it.

### Changed

- **Skill/slash prose inverted to the `y`/nudge norm.** The four workflow skills
  shrink to single-sentence block starts + a propose→`y`/nudge relay loop; the
  "no `[Y/n]` / deterministic resolution" doctrine survives only *inside* blocks.
  `docs/internals/skill-policy.md` rewritten. The `claude -p` worker is **stranded**
  from routing (left on disk; physical deletion + the #137 OAuth machinery are a
  later pass, gated on a proving run).

### Removed — stranded `runtime-prior` wire model + schema

- Deleted `_wire/queries/runtime_prior.py` (`RuntimePriorResult`) and
  `schemas/runtime_prior.output.json`. `read-runtime-prior` is an
  **optional plugin-only** verb (core never registers it; `resolve-resources`
  probes it and treats an unregistered verb as a normal cold-start), so — like
  the other plugin-only verb `plan-submit` — its output contract belongs in the
  providing plugin, not core. The model was imported nowhere and the schema was
  loaded by no verb, `$ref`, or `describe`/`validate_output` consumer: a pure
  wire-surface removal, no behavior change. (`resolve-resources`'s probe is
  untouched — it hand-parses the envelope and never validated against the schema.)

### Fixed — block verbs' shared output schema is now reachable (`describe` + `validate_output`)

- The eleven human-amplification block verbs share four output shapes named
  for the shape, not the verb: `submit-s1..s4` → `submit_block.output.json`,
  `aggregate-check`/`aggregate-run` → `aggregate_block.output.json`,
  `status-snapshot`/`status-watch` → `status_block.output.json`,
  `campaign-greenlight`/`campaign-watch`/`campaign-complete` →
  `campaign_block.output.json`. The schema-resolution convention keys off the
  verb name (`submit_s1.output.json`…), so it could never find these files —
  every one of the eleven reported `output_schema: null` in the catalog, so
  `describe` omitted the output contract and `validate_output` silently skipped
  the block outputs (drift would have gone uncaught). Activated the dormant
  `SchemaRef.output` field (docstring already reserved it for "future output
  validation") and taught both resolvers — `operations.schema_for` (catalog /
  `describe`) and `contract.schema._output_schema_for` (`validate_output`) — to
  prefer it over the convention, so they stay in lockstep. Each block verb now
  declares `SchemaRef(input=…, output=…)`; convention-named verbs are unchanged.
  A contract test pins that both resolvers agree on the same existing file for
  every block verb. No new schema files — the four already existed, just
  unreachable. `_kernel/registry/operations.py`, `_kernel/contract/schema.py`,
  `cli/_dispatch.py`, `ops/{submit,aggregate,status}_blocks.py`,
  `meta/campaign/blocks.py`, `tests/contract/test_schema_roundtrip.py`.
- **Symmetric orphan guard for output schemas.** Added
  `test_no_orphan_output_schemas`, the mirror of the existing
  `test_no_orphan_input_schemas`: every `*.output.json` must back a CLI verb
  (catalog `output_schema`, now honoring the block override above) or sit on a
  small documented cross-cutting allow-list (`inspect_cluster`, `worker`,
  `worker.strict`). This is the guard that would have caught the stranded
  `runtime_prior.output.json` mechanically instead of by hand-audit — a new
  stranded output schema now fails CI instead of accreting silently.


### Added — persist opaque per-trial params for provenance; warm-start stays a documented strategy pattern (#369)

- **A run's resolved params are now recoverable from its sidecar.** The framework persisted only `cmd_sha` (a one-way hash) + `trial_tokens` per run, so you could **not** recover what params a run actually used without recomputing from `tasks.py` — a real provenance/reproducibility gap. `compute-run-id` now also surfaces `trial_params` (the task-ordered resolved params each `resolve(i)` returned, with `RESERVED_TASK_KEYS` stripped — i.e. the exact `cmd_sha` pre-image), `write-run-sidecar` persists it on the run sidecar (omitted when absent, same compact-write discipline as `trial_tokens`), and `prior_records()` / `parent_records()` re-surface it paired with each iteration's `metrics`. Fully experiment-agnostic: the framework records the params **verbatim and never interprets them** (CI covers this with synthetic, meaningless dicts and no optimizer installed). `incorporation/build/compute_run_id.py`, `state/runs.py`, `_wire/actions/write_run_sidecar.py`, `ops/write_run_sidecar.py`, `execution/mapreduce/reduce/history.py`.
- **Warm-start is left a documented strategy-level pattern, not a framework subsystem.** Pairing `(trial_params, metrics)` is the data an optimizer needs to seed a fresh study, but the seeding is optimizer-specific (~10 lines in a scaffold's `_propose`, no optimizer in core) and the framework **cannot judge relevance** — it can filter a prior corpus only on *structure* (param-key set + objective key), never *transferability* (same data regime? comparable objective scale?). **Structural compatibility ≠ transferability**; frictionless framework warm-start with a structural-only filter would be a footgun. So the relevance call stays with whoever assembles the corpus — the user. The warm-start pattern + the explicit relevance caveat are documented in the strategy-authoring contract: `docs/design/campaign-seam.md`, `docs/primitives/scaffold-strategy.md`, and the `hpc-campaign` SKILL. No new manifest/scaffold warm-start surface lands; default behavior is unchanged.

### Added — stale `.hpc/` scaffold caught at submit, not as a cluster ImportError (#364)

- **The generated `.hpc/` scaffold is now generator-version-stamped, and a stale scaffold is refused at submit instead of surfacing as a runtime `ImportError` on a compute node.** A `.hpc/` scaffold built by an *older* hpc-agent could survive an upgrade and fail far downstream on the cluster — the observed case was a pre-reorg `.hpc/_build_tasks.py` importing `from hpc_agent.template import ...` after `hpc_agent.template` was consolidated away. hpc-agent already version-stamps sidecars/manifests/journal records; this closes the same gap for the generated scaffold. `build-tasks-py` now stamps the generating `hpc_agent.__version__` into `.hpc/.scaffold_meta.json` (`incorporation/build/scaffold_meta.py`), and a new `validate-scaffold-staleness` atom — wired into the `validate-campaign` pre-submit gate and run **unconditionally** — performs a cheap, **local (no-SSH)** check: when the stamp matches the installed version it is a byte-identical no-op (it never scans an import); otherwise it scans the generated files' `hpc_agent.*` imports against the installed package and refuses with an `error`-severity `stale_scaffold` finding when an import no longer resolves or a pre-reorg `_build_tasks.py` (stale by construction) is present. The remediation points at regenerating the framework-owned scaffold (re-run onboarding / `build-tasks-py --force`), never hand-editing the generated file. An unstamped (legacy) scaffold whose imports all resolve is **not** refused — "unknown generator → verify, don't refuse." `ops/validate/scaffold_staleness.py`, `_wire/validators/validate_scaffold_staleness.py`.

### Added — pure-API reduction honors `mode` / `aggregate_cmd` (#342)

- **A pure-API backend (`requires_ssh = False`) is no longer locked into the numeric weighted-mean.** `aggregate-flow`'s reduction *choice* (mean vs. a custom reducer command) now follows the spec `mode`, independent of reduction *location* (local vs. cluster, which follows the backend's `requires_ssh`). New `local-reduce` runs the reducer-contract command (`docs/reference/reducer-contract.md`) as a LOCAL subprocess over the artifacts `fetch_results` shipped back (`$HPC_RESULTS_DIR` / `$HPC_RUN_ID` / `$HPC_AGGREGATED_OUTPUT`), mirroring the SSH `cluster-reduce` envelope. `ops/aggregate/{local_reduce,_reducer_contract}.py`.

### Added — SSH connection-rate throttle (`safe_interval`, opt-in)

- **New `HPC_SSH_SAFE_INTERVAL` enforces a minimum gap between SSH connection *opens* to a host** (`infra/ssh_throttle.py`, wired into `ssh_run` plus the rsync push/pull/deploy entry points in `transport.py`). A cluster's fail2ban / connection-rate limiter counts how *often* an IP connects — which neither `ConnectTimeout` (per-connection duration) nor `IdentitiesOnly` (auth attempts per connection) bounds. When calls bunch up (retry storms, parallel probes) the throttle spaces them to one-open-per-interval; when they're naturally spaced it sleeps ≈0. Thread-safe (concurrent submits to one host serialize through the interval rather than firing at once). **Default off** — ControlMaster multiplexing already collapses the happy path; set e.g. `HPC_SSH_SAFE_INTERVAL=30` for a rate-limiting cluster, or when multiplexing is unavailable. Modelled on AiiDA's `safe_interval`. (ban-driver hardening; see the connection-storm tracking issue)

### Changed — SSH `ConnectTimeout` bound (ban-driver hardening)

- **Every ssh-family call now pins `-o ConnectTimeout=15` (default).** OpenSSH ships no `ConnectTimeout`, so a misconfigured/unreachable host (wrong `HostName`, a hostname matching no ssh-config key, a down login node) hung until `infra.remote`'s `SSH_TIMEOUT_SEC` (60s) subprocess hard-kill. A burst of such slow failures from one IP is exactly what a cluster's fail2ban / connection-rate limiter bans. `ssh_options._ssh_connect_opts()` bounds only the **connect phase** — spliced into `ssh_argv("ssh")`, `ssh_argv("scp")`, and rsync's own ssh (`_rsync_rsh_env`) — so a connect failure surfaces fast while a legitimately long-running remote command keeps the full `SSH_TIMEOUT_SEC` command budget. Tunable via `HPC_SSH_CONNECT_TIMEOUT` (positive integer seconds, or `default` to drop the override; a bad value warns and falls back). Built-in behaviour is otherwise unchanged — this only caps a previously-unbounded wait.

### Added — connection-storm hardening: batched + paced status polling

- **Batched status query (#2).** `HPCBackend.batch_status(states) -> {job_id: TaskStatus}` (default `NotImplementedError`; implemented for all four families by `ProfileBackend` as a classmethod over `parse_scheduler_states` output) folds raw scheduler tokens into `TaskStatus` values in bulk — finer than `classify_scheduler_state`'s alive/error/held, splitting a live token into `running` vs `pending` (queued/held) vs `failed`; `complete` is never emitted (a finished job leaves the live queue, so the caller infers it from absence). New `batch-status` query primitive (`hpc-agent batch-status`) enumerates the journal's in-flight runs, groups them by `(ssh_target, scheduler)`, and issues ONE `qstat -u $USER` / `squeue` per login node — distributing the parsed states back to each run. N runs on one login node now cost ONE scheduler query per tick instead of N (the Nextflow/Parsl "query the scheduler once for all jobs" idea). `infra.cluster_status.ssh_batch_scheduler_states` is the SSH transport seam. Read-only: it never mutates the journal.
- **Paced polling floor (#3).** `monitor-flow`'s blocking poll loop now applies a minimum poll-interval floor — `HPC_STATUS_POLL_INTERVAL_SEC` (default 10s, AiiDA's `minimum_job_poll_interval`) — as a hard lower bound on the spec's `poll_interval_seconds`, so no spec / campaign can poll faster than the floor and re-trigger the connection storm. The existing adaptive backoff cap is now env-tunable via `HPC_STATUS_POLL_MAX_SEC` (default 300s). Both knobs fall back to their defaults on a non-numeric / negative value; built-in behaviour is unchanged when unset (a spec already asking for ≥10s sees no difference). Pure deterministic code — no model in the loop.

### Added — deterministic detached drive mode: take the LLM out of the connection loop (connection-storm hardening #4)

- **`hpc-agent run --detached` / `HPC_AGENT_DRIVE=detached` (opt-in; default unchanged).** The recent cluster ban traced to *an LLM sitting in the connection loop*: `hpc-agent run --workflow status` spawns a `claude -p --bare` worker to **drive** the wait-until-terminal poll; the worker auto-backgrounds at 2 min, ends its turn mid-poll (so the run reports "no report"), and a fallback inline subagent then retries SSH in prose for ~21 min. The deterministic composite it was driving (`status-pipeline` → `monitor_flow`) already runs the whole poll loop in plain code with the connection owned by a single process — the principle `infra/retry.py` states ("the model is out of the loop"); the miss was the *drive layer*. The new **detached** drive mode launches that composite as a DETACHED `hpc-agent` subprocess (NOT a `claude -p` worker) that owns the connection and runs to terminal, and the orchestrator learns the outcome by **reading the journal**, never by spawning an LLM to poke SSH (mirrors DPDispatcher's submit-and-poke loop / jobflow-remote's Runner daemon). The detached child uses `start_new_session` (POSIX) / `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP` (Windows) so it OUTLIVES the orchestrator — the exact crash that killed the auto-backgrounded `submit-pipeline` ~1s after qsub in 0.10.63 no longer kills the poll.
- **Journal-read poll helper (`hpc_agent.state.journal_poll`).** `read_run_status` / `poll_until_terminal` read the per-run journal record (the same on-disk state `monitor_flow` writes as it polls) and report terminal `JournalStatus` — **cluster-free, no SSH**. Keys off the durable journal status, not the monitor-flow `lifecycle_state` envelope, so a timed-out-but-still-live run is correctly NOT terminal and the caller keeps waiting. Injectable `sleep`/`now` for hermetic tests.
- **Scope + safety.** Landed slice: the `status` workflow's blocking wait path (the lifecycle the LLM sat in). `submit`/`aggregate` keep the default worker (deferred — see `docs/workflows/code-driven-orchestration.md`). The flag/env is **opt-in**; the proven `--bare` worker stays the default. Unlike `--inline`, detached is NOT refused when a worker can authenticate (it spawns no LLM, so the #155 context-isolation guard does not apply). Unsupported shapes are refused with `spec_invalid`. Stays entirely in the drive/worker/CLI layer — no `ops/monitor`, backend-status, or `infra/{remote,ssh_*}` changes. New env var documented in `docs/reference/env-vars.md`; `tests/_kernel/lifecycle/test_detached_drive.py`.

### Removed — §6 worker physical deletion (proving-run-2-hardening Move 3)

The `claude -p` bare-worker spawn transport is physically deleted; workflows are driven exclusively via the block-drive chain. Proving run #2 demonstrated the path was still reachable and taken by default (the driving agent shelled `hpc-agent run --workflow submit`, spawning a worker that hung on OAuth auth) — it cannot be a trap if it is gone. **Deleted:** the `run` verb (`cli/spawn.py`, Tier-3), `_kernel/lifecycle/{invoke,run,llm_resolver}.py`, `_kernel/extension/spawn_prompt.py` + `worker_prompts/` (the four workflow procedures), the legacy campaign resolver seam (`meta/campaign/{driver,deterministic_resolver}.py` + the `hpc-campaign-driver` console script), the `hpc-worker` subagent definition + its Bash fence, and `scripts/count_llm_touchpoints.py` + baseline (its subject was the worker prompts). **Kept (importer-verified):** `_wire/spawn_contract.py` (decision-kernel/strict-schema/block-drive contract — `WorkerReport`/`DECISION_POINTS` and the derived `worker.*.output.json` schemas stay), `drive._stamp_driver_tick` + `_DEFAULT_DRIVER_TICK_CADENCE_SECONDS` (§5 watchdog stamps consumed by `submit/runner` and `block_drive`), and `structured`/`chat_models` (the raw model-call seam). **Edited:** `drive.py` trimmed to the deterministic tick substrate (an `agent`-kind delegate now always plans `skip` routing to block-drive); `describe` no longer serves worker procedures; `load-context`'s delegate block routes `agent` steps to block-drive (`spawn_request` retained as an always-`None` wire-compat key). ~30 files, −4,700 lines; full suite green.

### Fixed

- **Remote submit wrapper `bash -lic` → `bash -lc`: the interactive flag hung every SSH submit on no-PTY clusters** (proving-run #2, 2026-07). `_remote_base.py::_execute_command` wrapped the remote `cd + qsub|sbatch` in `bash -lic` — login **and interactive** — to source the cluster profile that lands the scheduler binary on `PATH` (commit cafb160b). But an interactive bash on an ssh *exec* channel (no PTY; `ssh_run` allocates none) blocks in terminal/job-control init and hangs until the 120 s `_execute_command` timeout fires, which the flow then misreports as `dispatcher_failed` / `canary_failed` — the submit never reaches the scheduler (empty `qstat`/`qacct`), and per-executor-command retries chase a phantom cause. Login shell alone suffices: on Hoffman2/UGE `bash -lc` resolves `qsub` at `/u/systems/UGE8.6.4/bin/lx-amd64/qsub` and returns cleanly (`bash -lic` hangs). Dropped `-i`; a cluster that genuinely exposes the scheduler `PATH` only via an interactivity-guarded `~/.bashrc` must carry it in the preamble (`conda_source`/`modules`), never a globally-hanging `-i`. Regression-pinned in `test_backends_sge_remote.py` (`["bash", "-lc"]`, no `-i`). Covers both SGE and SLURM remote backends (shared mixin).
- **`reconcile`: a crashed-submit orphan (valid jobless sidecar, no journal record) is benign `no_run_record`, not `journal_corrupt`** (#356). A submit that crashed before `submit_and_record` leaves a valid jobless sidecar that was never registered in the journal. `reconcile` treated "sidecar present + no journal record" as a hard `journal_corrupt`, forcing the operator to hand-`rm` the residue before re-submitting. It now splits that branch on the sidecar read: valid JSON + no `job_ids` + no record → a benign `OrphanedReconcile` surfaced as a `no_run_record` `lifecycle_state` (a successful envelope, no SSH, no sibling cascade; `last_status.next_step` says to proceed with a fresh submit); a sidecar that DID land `job_ids` → the stranded-ids `journal_corrupt` + `submit-spec` hint (unchanged); a missing/malformed/schema-incompat sidecar → bare `journal_corrupt` (unchanged). The #328 invariant holds — the benign branch fires only on a provably benign read, so it can never mask a real corruption. A fresh submit over a benign orphan already proceeds (the runner's `cmd_sha` dedup falls through), now regression-pinned. New `no_run_record` value on the `reconcile.output.json` `lifecycle_state` enum; `/submit-hpc` Step 1b branches on it to proceed.
- **`local-reduce` test helpers quote `sys.executable`** (#347) so the pure-API aggregate suite passes on install paths containing a space (e.g. a checkout under `...\CC Allowed\...`). Test-only; `local_reduce`'s shell-command contract is unchanged.

