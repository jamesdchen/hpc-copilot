# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
on the wire surface enumerated in
[`docs/integrations/CONTRACT.md`](docs/integrations/CONTRACT.md).

## 0.10.65 — 2026-06-24

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

### Fixed

- **`local-reduce` test helpers quote `sys.executable`** (#347) so the pure-API aggregate suite passes on install paths containing a space (e.g. a checkout under `...\CC Allowed\...`). Test-only; `local_reduce`'s shell-command contract is unchanged.

## 0.10.64 — 2026-06-23

Release cut bundling the post-0.10.63 work that landed on `main` without a version bump (2026-06-11→23). The headline is the **crowd-sourced compute backends** workstream — making a pure-API backend a first-class citizen of the submit/monitor/aggregate flows — plus an MCP server, a repo-wide bug sweep, and test/doc/CI hardening. No new code in the cut itself.

### Added — crowd-compute backend seam: pure-API backends are first-class

- **`requires_ssh` capability + `fetch_results`/`fetch_logs` hooks (#336).** `HPCBackend.requires_ssh: bool = True` (the SSH ladder — SGE/SLURM/PBS — stays the default; a pure-API backend overrides to `False`). `fetch_results`/`fetch_logs` are instance hooks (default `NotImplementedError`, like `alive_job_ids`) — the artifact-based replacement for the aggregate flow's rsync pull and the logs ssh-tail. `backend_requires_ssh(name)` reads the capability off the *class* (the prelude branches before an instance exists); unknown names conservatively return `True`. `submit-flow`'s shared prelude (`_run_shared_prelude`) now branches on the capability: a pure-API backend skips the ssh probe / `command -v uv` / rsync+deploy wholesale. Additive — built-in SSH families are byte-for-byte unchanged.
- **GitHub Actions backend plugin (#334, #335).** A pure-API ("crowd-compute") `HPCBackend` that fans task arrays onto GitHub Actions runners over the REST API (stdlib `urllib`, zero extra deps) instead of an SSH cluster: `workflow_dispatch` submit, run-id resolution for `JOB_ID_REGEX`, liveness via `alive_job_ids`, artifact-based `fetch_results`/`fetch_logs`, and a `fan-out.yml` matrix template that resolves each cell's kwargs from `.hpc/tasks.py`. Account-pool rotation (`HPC_GHA_POOL`) continues a campaign on another account when one exhausts its Actions quota (durable state — Optuna study + sidecars — is local). Network paths ship unvalidated per the #269 discipline; registration/construction/submit-shaping/state-classifier are smoke-verified. #335 fixed two runnable README examples + slimmed the runner install.
- **Plugin-backend names accepted everywhere (#327 core edit + #338).** A `clusters.yaml` `scheduler:` may now name any backend a loaded plugin registered via `@register`, without pinning a `scheduler_profile`; an unknown/unregistered name is still rejected at config-load with two-path remediation. `registered_backend_names()` imports plugin primitive modules for their `@register` side effect, then `_wire/_shared` replaces the four-scheduler `Literal` for `Scheduler`/`BackendName` with `Annotated[str, AfterValidator(...)]` validating against the live registry (11 schemas widened to `{type: string}` — the valid set is install-dependent). `capabilities.supported_schedulers`, `scaffold_spec`, and the deterministic resolver now derive from the registry instead of a frozen tuple; `--scheduler` argparse `choices` dropped (validated downstream by `get_backend_class`).
- **Wave-based submission past the per-backend array cap (#339/#341).** `HPCBackend.max_array_size: int | None` declares a hard *platform* array ceiling (`None` for SSH families; the GHA plugin overrides to 256). submit-flow rejects a sweep whose `total_tasks` exceeds the effective cap (smaller of the platform cap and the cluster's declared `constraints.max_array_size`) with an actionable `SpecInvalid` *before* dispatch. `HPCBackend.submit_plan` revived from tested-but-dead into the live shared wave submitter: each `JobBatch` submits as one global sub-array via its `task_range`; waves submit in ascending order, each chained behind the prior wave's job ids (SUCCESS-only `afterok` on SLURM/PBS, completion-only fallback on SGE). Return shape `(wave, task_range, job_id)`.
- **Crowd-compute scaffolding + docs (#327).** `docs/proposals/crowd-compute-backend.md` (seam analysis: registry, `hpc_agent.plugins` entry point, transport-agnostic executor contract; what breaks — SSH transport, shared FS), `examples/crowd-compute-executor/` (stdlib-only containerized executor proving the `HPC_KW_*`/`RESULT_DIR`/`HPC_TASK_ID` dispatcher contract is platform-neutral), and `examples/plugins/hpc-agent-vastai/` (skeleton plugin walking entry point → manifest → `@register`, every compute method a documented `NotImplementedError`).

### Added — MCP server (#332)

`hpc-agent mcp-serve`: a Model Context Protocol server (JSON-RPC 2.0 over stdio) that projects the `@primitive` registry as MCP tools/resources/prompts. An additive projection, not a rewrite — discovery reads the live registry (no second tool list to drift), and `tools/call` drives `python -m hpc_agent <verb>` subprocesses, inheriting the envelope, 0/1/2/3 exit codes, JSON-Schema validation, and journal/idempotency verbatim. **Read-only by default** (only query/validate verbs exposed; mutating verbs require `--allow-mutations`); scheduler cancel/raw-submit are never registry primitives, so they are structurally unreachable regardless of the flag (pinned by a test). `--catalog` tiers discovery (find → describe → invoke) to avoid context bloat. Ships `docs/reference/mcp.md` + `tests/test_mcp_server.py`; reverses the prior "No MCP server" stance in `agent-surface.md`.

### Fixed — repo-wide Opus agent audit (#330)

Squashes the HIGH/MEDIUM correctness bugs from a per-leaf-directory + cross-directory audit. HIGH: balanced-brace JSON scanner is now string-literal aware (worker JSON with braces inside string values parses); status fingerprint excludes the volatile `checked_at` so adaptive poll backoff engages; GPU filter honors the caller's `preferred` list; `campaign_run` treats `parents_not_ready` as a submit-stop (was monitoring a never-submitted run and crashing); `clusters-list`/`-describe` stop emitting schema-invalid raw yaml; reduce/classify maps the newer failure-signature classes (uv/conda/module/output/mpi/undefined-var); `worker_prompts/status` points the resubmit flow at fields the envelope actually emits; submit runner no longer dedups against an orphan pre-qsub sidecar. Plus a ~25-site exception-handling sweep (degrade-gracefully clauses widened so `SchemaIncompat`/`SpecInvalid`/`ValidationError`/`YAMLError`/`UnicodeDecodeError` no longer escape skip-the-bad-input paths) and two MEDIUM fixes (wave-partial provenance keyed by filename; untrusted `interview.json` fields coerced to null).

### Fixed — lost-job-id hardening (#328)

Two follow-ups to the 0.10.63 fix. (1) Nothing pinned that `_ensure_run_sidecar` runs *before* the qsub — the pre-stamp silently no-ops without a sidecar, so a refactor could disable the guarantee undetected; new end-to-end test drives `submit_flow_batch` without pre-seeding a sidecar, kills `submit_and_record`, asserts the stamped id landed (mutation-verified). (2) `reconcile`'s no-run-record hint caught only `(OSError, JSONDecodeError)` but `read_run_sidecar` can raise `SchemaIncompat` (too-new sidecar), masking the actionable `JournalCorrupt`; broadened the best-effort catch to `Exception` (mutation-verified).

### Changed — organization sweep: doc↔code drift + orphaned gates wired (#331)

Structural pass (four parallel Opus agents over doc↔code drift, index integrity, gate-enforcement coverage, source-tree placement; every finding re-verified against code). Enforcement gaps closed: `lint_decision_content` (was in no gate and already failing — the shared inline-isolation block had drifted) re-scoped and wired into pre-commit + CI; `lint_text_io_encoding` + `lint_schema_versions` added to CI. Drift fixed across `boundary-contract.md` (15→16 surface names), `sync-checklist.md` (error_code 16→17, stale ResubmitCategory refs), `architecture.md` (bogus recover-flow row, mislocated `LifecycleState`/`FailureCategory`), five stale test-path citations, and the internals index.

### Changed — test suite hermeticity, coverage fidelity, flake resistance (#329)

(1) Autouse default-tier hermeticity fixture shadows cluster binaries (ssh/scp/rsync/ssh-add) with stubs so a non-`slow` test fails loudly and identically everywhere instead of passing/failing on whether the host ships the binary (slow tier opts back in). (2) Subprocess coverage wiring (`process_startup` `.pth` installer) makes CLI-spawning tests visible to coverage: 81.8%→82.8% overall, `cli/skill_returns` 19→66%. (3) `pytest-timeout` (300s default cap) + `pytest-randomly` (order shuffle). (4) Real scaffold-generator gaps closed (`template.py` 96→100%, `tasks_py.py` 84→90%).

### CI / Docs

- **Isolated offline plugins job + actionlint (#340).** A separate `plugins` CI job installs core + the example plugin and runs its offline suite against a fake API client (zero network/token) — kept *out* of the `test` matrix because installing a plugin shifts `registered_backend_names()` and would pollute the core suite. `actionlint` (pinned v1.7.12, retries, non-blocking) lints each plugin's workflow-template.
- **Campaign load-idempotency invariant + dry-run-local prose (`0d149a7f`).** A campaign strategy must index proposals by COMPLETED count, never by counting on-disk artifacts a prior `tasks.py` load created (validators / `compute_cmd_sha` / `--dry-run` all import and call `total()`/`resolve()`, so artifact-counting mints a phantom optimizer trial per validation pass). And `dry-run-local`'s `result_dir_collision` gate is severity=error (escalates to `overall="fail"`, a hard abort) — "non-blocking" only means it returns findings instead of raising, never that the collision is advisory.

## 0.10.63 — 2026-06-11

### Fixed — lost main-array job id: post-qsub sidecar pre-stamp + fabricated-id guard + worker wait discipline

The 2026-06-11 demo's main array (job 13610902) ran to 100/100 but was untrackable: the worker exited while its auto-backgrounded `submit-pipeline` was still executing, the harness killed the pipeline ~1s after the main qsub — before `submit_and_record` — so the scheduler id existed nowhere on disk (journal, local sidecar, remote sidecar all empty). The orchestrator's "recovery" was to mint the journal record with fabricated `job_ids: ["purged-completed"]`, poisoning every downstream alive-check/qacct probe. Three-layer fix:

- **Crash-safety pre-stamp** — `submit-flow` now persists the just-parsed job ids to the run sidecar IMMEDIATELY after each qsub (canary and main), before any other work. A process death in the qsub → record window leaves the real ids recoverable through every existing sidecar-reading path. Best-effort and idempotent under the canonical stamp inside `submit_and_record`.
- **`SchedulerJobId` boundary guard** — `SubmitSpec.job_ids`, `ResubmitSpec.new_job_ids`, and `WriteRunSidecarInput.job_ids` now require digit-leading scheduler-issued ids (SGE `13610902`, SLURM `8570940_3`, PBS `1234.pbs01`); prose placeholders like `purged-completed` fail `spec_invalid` at intake. Schemas regenerated. `reconcile`'s `no run record` remediation now names the sidecar's pre-stamped ids and the `submit-spec` mint path instead of leaving the agent to invent.
- **Worker wait discipline** — `worker_prompts/submit.md`: run `submit-pipeline` foreground with an explicit `timeout: 600000` (an un-timeouted call is auto-backgrounded at 2 min); if backgrounded anyway, poll the task output for the envelope; never end the run while the pipeline executes; never report success from canary side-state. `agents/hpc-worker.md` gains the generic your-exit-kills-your-background-tasks rule.

## 0.10.62 — 2026-06-11

### Fixed — review findings on the 0.10.59–0.10.61 work

A seven-angle review of the branch surfaced one real gap and two hardenings, all fixed with firing tests, plus two cleanups:

- **Provenance capture now covers pre-written sidecars.** `data_sha`/`env_hash` were computed only on `_ensure_run_sidecar`'s synthesize path — the normal flows (Step 6d / `resolve-submit-inputs` pre-write the sidecar) never captured them. New `backfill_run_sidecar_provenance` (`state/runs.py`, same post-write-update precedent and lock seam as `update_run_sidecar_job_ids`) fills **only null** fields at submit time — an explicitly recorded value is never overwritten and the write-first invariant is untouched. The capture itself extracted to `_spec_provenance`, shared by both paths.
- **The #269 flip's old-CLI failure now names its off-switch.** A `claude` CLI predating `--json-schema` rejects the flag; `_run_claude_worker` detects the unknown-option stderr shape and appends remediation naming `HPC_AGENT_WORKER_JSON_SCHEMA=0`.
- **`LlmJudgementResolver` never raises.** A third-party inner emitting a contract-invalid residue report (or a menu keyed on a point invalid for the workflow) made `parse_worker_report` raise out of the resolver, crashing the tick loop. The violation is now annotated on the returned report (`WORKER-REPORT CONTRACT VIOLATION`) and the park stands — a resolver's contract is to return, not raise.
- Cleanups: boolean env-flag parsing extracted to `infra/env_flags.py:env_flag` (the decode-schema gates and `HPC_AGENT_ALWAYS_CANARY` previously each inlined it); `write_provenance_manifest` returns `(path, written_object)` so the primitive no longer re-reads the file it just wrote.

Considered and deliberately kept: `ResidueAdjudication` stays a separate shape from the `CandidateAction` family (adjudication verdict vs candidate offer — different roles); a repo-wide canonical-JSON helper sweep is its own change.

## 0.10.61 — 2026-06-10

### Added — `LlmJudgementResolver`: code drives, an LLM adjudicates the parks

The middle resolver between the two extremes the headless tick-loop shipped with (spawn-a-whole-worker vs all-code halt-and-park). `hpc_agent._kernel.lifecycle.llm_resolver.LlmJudgementResolver` wraps any inner code `JudgementResolver`; when the inner parks (the exit-3 residue convention), it makes **one bounded `structured()` call** to adjudicate the residue against a caller-authored **closed menu** of candidate outcomes, records the verdict as a contract-valid judgement `WorkerDecision` (non-empty `why`, rejected alternatives — `parse_worker_report` validates the merged report), feeds the choice back through the `fields.resolved` channel, and retries the inner. Protocol guarantees, each pinned by a test: success/non-residue passes through with zero LLM calls; an un-menued residue parks with zero LLM calls (genuine interviews are not menu-shaped); off-menu choices are rejected by `post_validate` and repaired; a no-progress guard parks instead of spinning when the inner re-emits the residue; `"park"` is always offered and always honored; an exhausted `structured()` budget parks gracefully.

`DeterministicCampaignResolver` now honors `fields.resolved["path"]` — only when `classify-campaign-path` itself escalated (deterministic evidence always wins; the hint exists to break the tie code could not), recording the adjudication provenance in the decision's `why`. The e2e test drives the full bridge: classify escalates → one fake-model adjudication → the decide chain continues to the (stubbed) submit seam and advances the cursor — exactly one model call, zero worker spawns.

New guide [`docs/workflows/code-driven-orchestration.md`](docs/workflows/code-driven-orchestration.md) documents the third consumption style end-to-end: the `drive_once` + `StepTable` + `JudgementResolver` loop seam, the bridge, the pure-CLI escalation-as-data recipe (`DECISION_POINTS`, `candidate_actions`, `needs_decision`), DAG-frontier composition (and that recorded walks of that shape are the dag-kernel earn-it evidence), and the per-tick cost model.

## 0.10.60 — 2026-06-10

### Added — operator always-canary override (#283, last acceptance item)

`HPC_AGENT_ALWAYS_CANARY=1` fires a canary on **every** submit, winning over the agent-supplied `canary: false` opt-out and both auto-skips (the #263 tiny-batch threshold and the #249 cached-`cmd_sha` TTL). The #155/#275 operator-vs-agent boundary applied in the strengthening direction: the documented agent opt-out stays, but the override exists only as an env var — no spec field can express it, so an unattended loop cannot talk itself out of an operator's canary policy. This was #283's one remaining (optional) acceptance item.

### Added — Codex `--output-schema` live-validation mirror (#269 residual half)

`scripts/validate_worker_json_schema.py --harness codex` drives the production `CodexCliInvoker` end-to-end (execpolicy fence, strict `worker.strict.output.json` via `--output-schema`, `--output-last-message` report) with the gate forced on — the same two-question protocol (loop composition + schema acceptance) that validated Claude's flip in 0.10.59, ready to run wherever a `codex` CLI and credentials exist. Codex's `HPC_AGENT_CODEX_OUTPUT_SCHEMA` stays off by default until that run passes.

### Added — OTel metrics alongside spans (#313), on the merged #223 `otel` sink

The `claude/issue-223-otel-telemetry-sink` foundation branch is merged (the `otel`/`otlp` value for `HPC_TELEMETRY_SINK`, span export with `hpc.*` attributes), and the deferred metrics half lands on top: the same single `telemetry.record()` producer now also emits the `hpc.events` counter (per-event-kind, dimensioned by a bounded-enum label allowlist — `decision` / `error_class` / `disposition` / `lifecycle_state` / `ok`) and the `hpc.event.value` histogram (every numeric payload field, dimensioned by event + field). High-cardinality identifiers (`trial_token`, `run_id`, job ids, fingerprints) deliberately never become metric dimensions — they stay span attributes. Same deferred-import + fail-fast-on-missing-SDK pattern; the existing `hpc-agent[otel]` extra covers the metric exporter; an embedding host's global meter provider is respected.

### Added — provenance closed end-to-end (#312), on the merged #222 foundations

The `claude/issue-222-provenance-data-env` foundation branch is merged (`compute_data_sha` with DVC-pointer support, `compute_env_hash`, the sidecar v2 `data_sha`/`env_hash` fields, `build_provenance_manifest`/`write_provenance_manifest`, auto-captured `env_hash` at Step 6d) — with union conflict resolution against the DAG-kernel fields and the `models`→`execution` rename it predated. Both deferred gaps then close on top:

- **Gap 1 — `data_sha` auto-capture.** `SubmitFlowSpec` gains `input_datasets` (the same path(s) a `validate-input-dataset` gate names); when declared, the sidecar-synthesis step computes `data_sha = compute_data_sha(input_datasets, base_dir=experiment_dir)` exactly where `env_hash` is captured — no manual `write-run-sidecar` step. The undeclared-dataset decision: `data_sha` stays `null` ("not captured" must remain distinguishable from the real digest of an empty declaration); a declared-but-missing path contributes `compute_data_sha`'s existing `absent` sentinel *inside* the hash. Provenance only — never part of the dedup identity.
- **Gap 2 — agent-facing manifest surface.** New `provenance-manifest` primitive (`hpc-agent provenance-manifest --spec '{"campaign_id": ...}'`, verb `mutate`, idempotent by construction since the manifest is derived state recomputed from sidecars): wraps `write_provenance_manifest`, returns `{path, campaign_id, run_count, signature}` — the self-attesting digest a caller records to attest "these results came from exactly these {code, data, env, params}". An unknown campaign yields a well-formed `run_count: 0` manifest, not an error. Registry count: 94.

## 0.10.59 — 2026-06-10

### Changed — `--json-schema` worker output constraint is the default for Claude workers (#269)

The decode-time output constraint shipped opt-in because two questions were unanswerable offline (no worker credentials in the build sandbox). A live `claude -p` validation run (2026-06-10, Claude CLI 2.1.170) answered both, on the first attempt, twice:

- **Composition** — `--json-schema` constrains only the worker's *final* message: the worker executed a deterministic 3-step tool sequence (write file / read back / write transform — observable side effects on disk) and then emitted the schema-shaped report, with a token round-trip binding the constrained decode to work actually done in the loop.
- **Schema acceptance** — the CLI accepted the **lenient** `worker.output.json` (`additionalProperties: true`, no `required`) directly; the pre-authored strict variant was never needed for Claude (it remains Codex's shape, where `--output-schema` documents the strict requirement).

The harness is committed as `scripts/validate_worker_json_schema.py`: it exercises the production spawn path (`_run_claude_worker` argv assembly, temp-file + stdin prompt transport, JSON result-envelope unwrap) with the production schema loader, gives the worker a multi-step task with observable side effects, and verifies exit code, both side-effect files, schema validity of the final message (`WorkerReport.model_validate`), and the token round-trip — rerunnable for future CLI upgrades (`--mode bare|ambient` for API-key vs environment-managed auth).

The flip itself, per the #269 spec: `_worker_output_schema()` treats unset as **enabled** (`_decode_schema_enabled` gained a per-gate `default`), keeping `HPC_AGENT_WORKER_JSON_SCHEMA=0` as the documented off-switch back to the plain transport. `parse_worker_report`'s cross-field floor is unchanged — structural complement, not substitute. Per the per-harness discipline the gate split exists for, **Codex's `HPC_AGENT_CODEX_OUTPUT_SCHEMA` stays off by default** — it has had no live run; that residual half stays tracked in #269. Exact-argv tests now pin `--output-format json --json-schema <minified schema>` on the default Claude spawn (both the `--bare` and OAuth paths); the plain-transport test pins the off-switch; the #169 large-prompt argv guard exempts only the fixed ~2KB schema constant. `docs/reference/env-vars.md` updated to match.
## 0.10.58 — 2026-06-11

### Fixed — the skill-return "additive net" never fired: autofetch re-triggered onto the emit Bash call + new Stop guard

The 2026-06-10 demo re-surfaced the sub-skill boundary stall the 0.10.54 prose fix was supposed to mitigate: `hpc-wrap-entry-point` committed its return via `emit-skill-return`, the orchestrator ended its turn anyway, and the parent `/submit-hpc` procedure stalled until a human typed "keep going". Diagnosis: the prose is advisory, and the harness-side safety net — the `PostToolUse` autofetch hook — was a **structural no-op**. It matched the `Skill` tool, but Claude Code's Skill tool returns *immediately* (its tool result is the injected instructions); the sub-skill body, including the final `emit-skill-return`, runs **afterwards** as ordinary Bash calls. At `PostToolUse(Skill)` time the envelope cannot exist yet, so the hook never injected anything on a fresh run — and could only ever have injected a *stale* envelope from a prior run. Two-tier fix:

- **Tier 1 (retrigger)** — `skill_return_autofetch` now matches `Bash` and fires on the `emit-skill-return` command itself, the one event that coincides with the envelope existing. The skill name and `--experiment-dir` are parsed from the command (quoted/`=`/chained `&&` forms covered), falling back to the payload `cwd`. The installed command wraps the Python entry in a bash `case "$input" in *emit-skill-return*)` pre-filter so the every-Bash-call common path costs a bash builtin, not a ~300-500ms Windows interpreter start (#288 class).
- **Tier 2 (deterministic backstop)** — new `Stop` hook `skill_return_stop_guard`: when the agent is about to end its turn with a committed-but-unfetched envelope under `<cwd>/.hpc/_returns/`, it blocks the stop with a reason instructing `fetch-skill-return` + continue the parent's next step. `PostToolUse` hooks can't catch this failure mode (it is precisely "no further tool call happens"); `Stop` fires at the exact failure point. Self-healing (the fetch deletes the envelope) and loop-safe (`stop_hook_active` passes through).

`install-commands` wires both: `_merge_skill_return_hook` generalised to `_merge_hook_entry(event, entry, needle)`, reporting `settings_hook` (autofetch) + new `settings_stop_hook` (guard) — and heals a pre-0.10.58 `matcher: "Skill"` entry in place rather than appending a duplicate beside the dead one. Docs: the `skill-policy.md` seam section now records the Skill-tool timing lesson.

## 0.10.57 — 2026-06-10

### Added — the DAG walker's mechanical halves: readiness gate in submit-pipeline + dag-frontier

Two pieces of caller-side topology walking ([`docs/design/dag-kernel.md`](docs/design/dag-kernel.md) step 5) pass the prove-mechanical test by inspection — neither is a loop, neither embeds policy — so they convert to code now rather than waiting for walk history:

- **`submit-pipeline` composes `validate-parents-ready`.** When the embedded submit spec declares `parents`, the pipeline runs the readiness gate first and returns a typed `stage_reached: "parents_not_ready"` refusal (with `parent_states` + per-parent `parents_ready_findings`) before anything touches the cluster — previously the gate was composed only by skill prose, so nothing mechanical stopped a child from being submitted over a half-written parent. A 0-parent spec never reaches the gate (the pipeline-level degeneracy: pre-DAG behavior byte-for-byte unchanged, validator not even called — pinned by test). It is a gate, not a loop: wait/fix/drop-the-edge stays caller judgment. Same-subject composition (`ops`), so the `validate-campaign` exclusion rationale doesn't apply.
- **`dag-frontier`** (new query verb, `hpc-agent dag-frontier`): read-only reconstruction of the recorded run graph from sidecar `parent_run_ids` — per-node lifecycle state, the complete-runs frontier (eligible parents for the next submits), transitive `blocking_ancestors`, dangling-edge (`missing`, pruned parent) and forged-cycle safety. The ∀-nodes lift of `validate-parents-ready`; both share the new public `observe_run_state` so the surfaces cannot disagree. Deliberately NOT a walker — it computes and stops; it also instruments hand-walks, producing the uniform evidence the earn-it rule needs before any advance-tick/graph-runner composite is considered.

The advance tick and the full graph runner stay caller-side per the earn-it rule (zero recorded walks; mid-graph failure policy and concurrency are unsettled judgment).

Drive-by: `test_node_sha_properties.py`'s header still claimed `compose_node_sha` was "not yet wired into `find_run_by_cmd_sha` / sidecars" — false since 0.10.51; it now points at `test_node_sha_wiring.py` as the wiring contract.

## 0.10.56 — 2026-06-10

### Changed — submit canvass asks once: persisted submit policy + no speculative `data_axis`

`/submit-hpc`'s runtime-behaviour canvass now persists each explicit experiment-wide answer (`on_task_generator_mismatch`, `k_in_flight`, the resolved `data_axis` keyed by `run_signature_sha`) to `<experiment_dir>/.hpc/submit_policy.json` and skips any question the file already answers — those dialogs fire once per experiment, not once per submit; a repeat submit with a saturated policy asks nothing. Only explicit answers are recorded (a default accepted by silence stays re-askable), and a value restated in `$ARGUMENTS` overwrites the recorded one. `overwrite_prior_run` is deliberately never persisted — it answers for one specific prior run's state, so a sticky answer would silently mis-route future submits. The background dispatch also no longer speculates `Sequential` for an unclassifiable `data_axis`: the axis is a spec-build input (it changes the array decomposition), so guess-then-confirm paid for the speculative deploy *and* the confirmation dialog, plus a cancel + re-dispatch whenever the user picked a different kind — it now returns `needs_resolution` and the slash walks one dialog before any cluster work, recording the resolved kind in the policy keyed by `run_signature_sha`. Slash-side only: the `hpc-submit` skill contract for autonomous callers (`safe_default` resolution) is unchanged.

### Changed — `on_task_generator_mismatch=prefer-caller` removed

`prefer-caller` submitted the caller's `task_generator` WITHOUT rewriting `interview.json`, so the stale cached generator re-fired the mismatch on every subsequent submit — leniency that manufactured recurring dialogs. The mode is gone: either the interview is wrong (`refresh` rewrites it) or the caller is (`fail`, the default, stops the submit). Callers passing `prefer-caller` should pass `refresh` instead — it submits the same caller generator and heals the divergence at the source.

### Changed — `decide-resubmit` default threshold is `0.0`: auto-resubmit is an explicit opt-in

`resubmit_failed_threshold` defaulted to `0.10`, silently re-running failed tasks at up to 10% failure — a systematic bug under the line was re-run (and re-failed) without anyone deciding that. The default is now `0.0`: every failure escalates the resubmit/investigate/abandon choice (`safe_default: investigate`); a caller that wants automatic resubmission declares how much loss it may absorb by passing a threshold > 0. `hpc-status` Step 6, `/monitor-hpc`, and the input schema updated to match.

### Changed — malformed `interview.json` is a loud `spec_invalid`, not a silent fallthrough

`detect-entry-point` treated an unparseable (or non-object) `interview.json` as absent and fell through to the mature-repo probe — a corrupt file could silently change which entry point the worker targets. It now raises `spec_invalid` naming the parse error, with remediation: fix the JSON, or delete the file and re-run `hpc-agent interview`. The bulk `recall` scan still skips malformed files (one corrupt campaign must not kill a multi-root rollup); the per-experiment scan is where silence hides corruption.

## 0.10.55 — 2026-06-10

### Fixed — re-submit pre-clean wiped the scheduler `logs/` dir, demoting it to a file

The scheduler's per-task stdout/stderr directory (`qsub`/`sbatch -o <remote>/logs`) was not in `PROTECTED_OUTPUT_DIRS`, so a re-submit's `--delete` / remote pre-clean `find -delete`d `logs/` along with the rest of the local-absent tree. The next array job's `-o <remote>/logs` then had no directory present and the scheduler created `logs` as a *single file* — every task's stream concatenated into one 24KB blob instead of `*.o<job>.<task>` per-task entries, breaking `cluster-logs` tailing. Added `logs/` to `PROTECTED_OUTPUT_DIRS` (force-unioned into every push's effective excludes), alongside `results/` and `_combiner/`.

## 0.10.54 — 2026-06-09

### Fixed — deploy `--delete` / pre-clean could wipe the cluster runtime under a custom `rsync_excludes`

The `deploy_runtime`-placed framework files (`.hpc/templates/`, `_hpc_dispatch.py`, `_hpc_combiner.py`, `hpc_agent/`) were protected from a push's `--delete` / remote pre-clean **only** via `DEFAULT_RSYNC_EXCLUDES`, which a caller-supplied `exclude` (the `rsync_excludes` spec field, or any non-`None` list) *replaces*. A push whose exclude set lacked them — or a re-submit pre-cleaning an in-flight run — `find -delete`d `.hpc/templates/`; every array task then died at preamble-source time with `hpc_preamble.sh: No such file or directory` (a ~26ms exit-1 on SGE) while the canary that ran before the wipe passed. New `PROTECTED_RUNTIME_FILES` is force-unioned into every push's effective exclude set — exactly like the `clusters.yaml` credential guard and `PROTECTED_OUTPUT_DIRS` — so no caller exclude can drop the runtime protection.

### Fixed — sub-skill `Then stop` ended the agent's turn at every composition boundary

Each composed sub-skill (`hpc-classify-axis`, `hpc-build-executor`, `hpc-aggregate`, `hpc-wrap-entry-point`, `hpc-status`) ended its emit step with `Then **stop**`, which the model reads as *end the turn* — so `/submit-hpc` yielded control back to the user after every `Skill(<sub>)` return and needed a manual "keep going" nudge (the 0.10.5 / 0.10.11 prose fixes targeted narration, not this literal `stop`). Reworded to **hand control back to the parent without ending your turn**, preserving the documented manual `fetch-skill-return` as the parent's next action — the autofetch hook stays the additive safety net it was designed to be.

## 0.10.53 — 2026-06-10

### Fixed — Windows CI: parent_records test asserted POSIX path separators

`test_records_in_declared_order_with_lineage` checked `result_dirs` with a string `endswith("results/<run_id>/task_0")`; on the windows leg the resolved dirs carry backslashes and the assert failed (PR #323, both CI runs). The assert now compares `Path` objects against the expected `tmp_path`-rooted path — exact and OS-agnostic. The other DAG-kernel test files carry no separator-sensitive asserts (audited).

## 0.10.52 — 2026-06-10

### Changed — DAG kernel doc promoted from proposal to design record

With the kernel's code live (0.10.51), `docs/proposals/dag-kernel.md` was stale paper — a "proposal" for shipped behavior. Promoted to [`docs/design/dag-kernel.md`](docs/design/dag-kernel.md) with an implemented status, following the `campaign-seam.md` precedent (implemented designs live in `docs/design/`, proposals are for unshipped work). Every reference repointed (docstrings, primitive doc, tests, regenerated schema descriptions, earlier CHANGELOG links), and two stale in-doc claims fixed: the kernel table and `compose_node_sha`'s docstring still described the identity function as "unwired" — it is wired through `resolve_node_sha` → sidecar → dedup since 0.10.51.

## 0.10.51 — 2026-06-10

### Added — DAG kernel wired in: lineage, identity, readiness (steps 1–4)

The `docs/design/dag-kernel.md` wiring plan, landed up to the caller-side topology step. A run may now declare `parents` (run_ids whose outputs it consumes) on `SubmitFlowSpec`; the four pieces that follow are all experiment-agnostic (paths and lifecycle, never content):

- **Identity** — at sidecar-write, `state.runs.resolve_node_sha` reads each parent's recorded identity (its `node_sha`, else bare `cmd_sha`) and composes this run's `node_sha` via `compose_node_sha`. `node_sha` + `parent_run_ids` persist as additive v2 sidecar fields. Identity is *derived* from on-disk sidecars, never caller-asserted (a supplied `node_sha` could decouple a child from its real ancestry); a missing parent or a non-64-hex digest raises `SpecInvalid`.
- **Dedup** — `find_run_by_cmd_sha` gained a `node_sha` arg and matches on the *effective* identity (`node_sha or cmd_sha`) on both sides. A parented re-submit dedups only against the same params AND ancestry, so a stale child computed from a since-changed parent is never replayed; a bare query skips parented sidecars. `node_sha=None` (every pre-DAG caller) is byte-for-byte the historical bare-`cmd_sha` path. Threaded through `submit_flow` → `submit_and_record` behind the same opt-in gate as the #207 code-drift lever.
- **Readiness** — `validate-parents-ready` (`ops.validate.parents_ready`): the ∀-parents quantifier over sidecar presence + journal lifecycle, ok iff every parent is `complete`. Pure-local `validate` primitive, composed before a parented submit like `validate-stochastic-marker` before a campaign submit; one finding per not-ready parent (`parent_run_missing` / `parent_not_terminal` / `parent_failed`) and a full `parent_states` frontier.
- **Lineage** — `parent_records(experiment_dir, parent_run_ids)` in `reduce.history`: `prior_records`'s record shape resolved from an explicit dependency set (ordered, deduped, fails loud on a missing parent), for a child's `tasks.py` to locate its inputs at module load.

The 0-parent degeneracy means a submit that declares no `parents` is unchanged: identity is its bare `cmd_sha`, the new sidecar keys are omitted, the dedup query is the historical one. Step 5 (walking the graph and firing submits) stays caller-side by design — the agent surface or an external orchestrator, per the campaign-driver precedent. Tests: `tests/state/test_node_sha_wiring.py`, `tests/state/test_parent_records.py`, `tests/ops/validate/test_validate_parents_ready.py`.

## 0.10.50 — 2026-06-10

### Added — DAG-kernel proposal + recursive-identity prototype

[`docs/design/dag-kernel.md`](docs/design/dag-kernel.md) scopes what survives the four-question boundary test for inter-run dependency (revisiting `campaign-seam.md`'s "true DAG pipelines" exclusion as a spec, not a feature): partial order over opaque submit specs, parent-quantified readiness, set-valued lineage, recursive identity. Edge meaning, conditional topology (`total() == 0` stays the agnostic veto), and stage vocabulary remain caller-owned.

Of the four, only recursive identity existed in no form, and the other three are unsafe to wire without it: bare-`cmd_sha` dedup over a run graph replays a stale child after an ancestor's params change. Landed as `state.run_sha.compose_node_sha` — the Merkle step over canonical JSON, with 0-parent degeneracy (`node_sha == cmd_sha`, so no existing run's identity changes), set semantics for parents, and ancestor propagation, pinned by a Hypothesis property suite (`tests/state/test_node_sha_properties.py`). **Deliberately unwired**: submits still key dedup on bare `cmd_sha` until the proposal's `parents` field lands on the submit spec.

Drive-by: `run_sha`'s module docstring claimed `compute_cmd_sha` was re-exported from `state.runs` — it isn't (`runs.py`'s pointer comment says to import from `run_sha` directly); the stale sentence is gone.

## 0.10.49 — 2026-06-09

### Fixed — CI green on Python 3.10

The 0.10.47 dependency-ban contract test imported `tomllib` unconditionally, failing the `test (3.10)` CI leg (`tomllib` is stdlib from 3.11; the repo floor is 3.10). The test now `pytest.importorskip`s it — the contract checks a static file, so the 3.11+ matrix legs enforcing it is sufficient.

## 0.10.48 — 2026-06-09

### Fixed — review findings on the 0.10.47 hardening

A seven-angle review of 0.10.47 surfaced one real parity gap and three accuracy/robustness nits, all fixed with firing tests:

- **`scripts/lint_subject_imports.py` had both evasion holes that 0.10.47 closed in the knowledge lint** — it skipped all relative imports (the comment claimed climbs would be "caught elsewhere"; nothing catches them), and the `from hpc_agent.<role> import <subject>` alias form mapped to no subject at all. Both spellings are now resolved/expanded; alias-derived candidates are checked against the real subject directories so re-exported helpers don't false-positive.
- **Growth trigger counts only public members**: a shared `_common.py` — the natural shape of the collapse refactor itself — no longer arms the trigger.
- **One banned-libraries table**: import roots and PyPI dist names are now paired in `_BANNED_LIBRARIES`, so a future sklearn/scikit-learn-style name mismatch cannot silently slip the pyproject dependency check.
- Enforcement map's Q3 row no longer over-credits `lint_schema_versions.py`/`_guard.py` (adjacent mechanisms, not static enforcers of that row); the new lint test imports `tests._paths.REPO_ROOT` instead of a depth-fragile parent climb.

## 0.10.47 — 2026-06-09

### Changed — CLAUDE.md retired: lessons solidified into infrastructure

The repo's `CLAUDE.md` asserted three present-tense facts; an audit found two had silently rotted (`_FAILURE_CATEGORY_PATTERNS` was long gone — collapsed into `CLASSIFIER_CATEGORIES` — and the deploy-ship list omitted `executor_cli.py`), while every mechanized check from the same era still held. Conclusion applied: lessons that can fire live in CI; only irreducible judgment stays prose — and not in an auto-loaded file that restates checkable facts.

- **`scripts/lint_library_knowledge.py` hardened** — three gaps closed, each with a firing test:
  - the `from parent import package` alias form (`from hpc_agent.experiment_kit import solver_adapters`) bound the knowledge package invisibly to the lint, which only examined the `from` clause;
  - relative imports were skipped wholesale on the false premise that they "stay inside their own package" — `from ..experiment_kit.solver_adapters import petsc` climbs parents; they are now resolved against the importing file's package;
  - the **growth trigger is enforced, not remembered**: each knowledge package declares its registry assembly point; the moment the family has ≥ 2 member modules, any *other* assembly point still binding a member module by name fails with the collapse remediation. Inert at one member (today), fires the day adapter #2 lands.
- **Question 4 enforced** (`tests/contract/test_no_heavy_toplevel_imports.py`): `petsc4py`/`mpi4py` join the banned module-level roots, and a new contract test asserts no banned library ever enters `pyproject.toml` dependencies or extras — core encodes library *knowledge* via crafted fixtures and must verify it without the library installed.
- **Irreducible prose** (the "verify a guard can fire" heuristic, question 1, the case history) moved to `docs/internals/engineering-principles.md` with an enforcement map naming the lint/test holding each line, corrected facts (the standalone-ship list now cites its source of truth, `transport._build_deploy_items`, and includes `executor_cli.py` — also fixed in `infra/parsing.py`'s docstring), and a drift log recording why prose alone failed.
- `CLAUDE.md` references in `scripts/lint_skills.py` and `tests/contract/test_lint_skills.py` re-pointed at the docs.

## 0.10.46 — 2026-06-09

### Added — the library-knowledge boundary: principle, enforcement lint, and a corrected misattribution

Codifies the standard that 0.10.44/0.10.45 were built to, applies it to the pre-existing code, and makes it enforceable rather than remembered:

- **CLAUDE.md gains the four-question boundary test** for third-party-library knowledge in core: (1) substrate, not semantics; (2) core dispatches, never branches — library names only at declared assembly points; (3) import-safe per runtime surface (control plane / cluster env / standalone-shipped files have different budgets); (4) core CI verifies it without the library installed. Includes the growth trigger: a knowledge family's second member collapses inline branching into the family's registry/dispatcher.
- **New `scripts/lint_library_knowledge.py`** (pre-commit + CI) enforces question 2: any import of a knowledge package (`experiment_kit/solver_adapters`, `experiment_kit/axis_matcher/matchers` — root or submodule, top-level or lazy) outside the package itself must be a declared assembly point in the lint's `KNOWLEDGE_PACKAGES` list; violations carry the remediation. List hygiene is enforced both ways: an assembly point that vanished or no longer imports its package also fails, so the list cannot rot into fiction. Current assembly points: `checkpoint_formats.py`, `wrap_entry_point.py`, `detect_entry_point.py` (solver adapters); `_classifier.py` (matchers).
- **`infra/parsing.py`'s false rationale corrected at the source.** CLAUDE.md has long recorded that this module was misattributed as "cluster-side", yet the module docstring still claimed its helpers "ship to the cluster". Verified against `deploy_runtime` (ships only `dispatch.py`, `combiner.py`, `metrics_io.py`, and the shell templates) and the module's actual importers (all control-plane): the stdlib-only rule now stands on its true merits, and the CLAUDE.md example records the meta-lesson — a recorded lesson whose source still asserts the false claim leaves the trap armed.
- Audit outcome for the existing tree under the new standard: the pandas/EMA/window/stencil matchers PASS all four questions (AST-only pattern knowledge, single `_classifier.py` dispatch point, fixture-tested with no pandas import anywhere in src or tests) — they are now lint-locked rather than precedent-justified. The solver adapters' two inline `"petsc"` branches are declared (not incidental) assembly points, with the registry collapse scheduled for adapter #2 per the CLAUDE.md trigger.
- Tests: `tests/scripts/test_lint_library_knowledge.py` — real tree clean, undeclared/lazy imports fire with remediation, intra-package imports free, both stale-entry modes fire.

## 0.10.45 — 2026-06-09

### Added — format-aware checkpoint verification + resume (petsc_binary round-trips end to end)

0.10.44's PETSc adapter introduced a second on-disk checkpoint format; the canary verifier and the dispatcher's resume-point finder were still pickle-only, so a petsc run's checkpoints were invisible to both. This closes that gap with a format seam:

- **New `experiment_kit/checkpoint_formats.py`** — the format-agnostic contract (`CheckpointFormat`: discover newest artifact + verify one) and the assembly of known formats. `describe_latest_checkpoint()` finds the newest artifact across formats (mtime, ties to format order) and returns the probe verdict: `{status, path, format, level, ...}`. The `pickle` entry preserves the historical probe semantics verbatim (newest file reported; loading walks newest→oldest, so one corrupt file doesn't fail the verdict; `level: "loadable"`, `next_iteration` present). The `petsc_binary` entry delegates to the adapter. Boundary: this assembly list is the ONE core location that names adapter formats; everything PETSc-specific stays in `solver_adapters/petsc.py`.
- **Adapter-owned structural verifier** — `verify_petsc_binary()` walks the PETSc binary Vec block structure (`VEC_FILE_CLASSID` + row count + per-flavor scalar sizes: double/single/complex) without importing petsc4py. Honesty contract: `level: "structural"` — it proves "a well-formed PETSc dump", not a reload (which would need the solver library the probe env may lack). A truncated trailing block after ≥1 complete block is still `ok` (preemption kill mid-append; the complete prefix is restorable). `latest_petsc_artifact()` discovers across both instrumentation paths (stepped monitor dumps, then wrapper `petsc-solution.bin`/`petsc-restart.bin`).
- **`verify-canary --verify-checkpoint` is format-aware** — the remote probe snippet now calls `describe_latest_checkpoint` (with a verbatim pickle-only fallback under `except ImportError`, so a new control plane still verifies runs on an older cluster-env hpc-agent), and the ok/unloadable verdicts surface the format and proof level (`resumes at iteration N` for pickle; `verified structurally: <detail>` for petsc_binary).
- **Cluster-side dispatcher resume widened** — `_hpc_dispatch.py`'s stdlib `_latest_checkpoint` now also scans `checkpoint-<n>.petscbin`, so a resumed petsc4py executor gets a concrete `HPC_RESUME_FROM` on `resubmit --from-checkpoint`. Equal-iteration ties resolve to pickle deterministically (listdir order is arbitrary; pre-petsc behavior preserved). Wrapper-path dumps are deliberately NOT scanned — the instrumented wrapper rotates and consumes those itself and never reads `HPC_RESUME_FROM`.
- Tests: the format seam (`test_checkpoint_formats.py`: per-format verdicts, mtime tie-break, JSON-serializable output, stable format names), structural-verifier rules (multi-block, truncated tail, scalar flavors, garbage rejection), dispatcher petscbin scan + tie determinism, and petsc-format canary verdicts.

## 0.10.44 — 2026-06-09

### Added — PETSc solver adapter: checkpoint injection for library-owned loops (#294 follow-up)

The checkpoint helpers assume the executor owns its iteration loop; a PETSc solve does not (``TSSolve``/``SNESSolve`` are C code), so there is no loop body to call ``should_checkpoint()`` from. New ``experiment_kit/solver_adapters/`` maps the checkpoint contract onto the hooks PETSc does expose — monitor callbacks and the ``PETSC_OPTIONS`` database — mirroring the two ``@register_run`` injection paths:

- **petsc4py (direct instrumentation)**: ``make_checkpoint_monitor()`` builds a TS/SNES monitor whose body is the existing ``should_checkpoint()`` cadence (walltime_margin / interval) plus an atomic PETSc-binary solution dump — ``ts.setMonitor(make_checkpoint_monitor())`` is the whole instrumentation. Dumps land as ``checkpoint-<step>.petscbin`` under the stable ``HPC_CHECKPOINT_DIR`` (the ``.petscbin`` suffix keeps them invisible to the pickle helpers); ``latest_petsc_checkpoint()`` is their discovery counterpart.
- **Opaque binaries (materialized wrapper)**: ``entry_point.solver`` (``{"kind": "petsc", "solver_object": "ts"|"snes", "resume_flag": ...}``) on a ``shell_command`` intent makes ``materialize_shell_wrapper`` render a checkpoint-instrumented wrapper that extends ``PETSC_OPTIONS`` with the per-step solution dump (``-ts_monitor_solution binary:<stable dir>/petsc-solution.bin``), caps the solve at 2 steps under ``HPC_CHECKPOINT_CANARY=1`` (``-ts_max_steps 2`` / ``-snes_max_it 2``), and — only when the app declared its restart flag — rotates the previous attempt's dump to ``petsc-restart.bin`` (``promote_restart()``) and appends ``<resume_flag> <path>`` to argv. The entry point stays opaque and untouched; resume is deliberately opt-in because loading a checkpoint is app-specific (there is no universal PETSc restart option).
- **Detection**: ``detect_petsc_solver()`` AST-matches a petsc4py import + ``PETSc.TS()``/``PETSc.SNES()`` construction + ``.solve()`` call (same matcher style as the stencil axis matcher), recording whether the script calls ``setFromOptions()`` (the options-injection capability gate). ``detect-entry-point`` surfaces a hit as an optional per-candidate ``solver: "petsc"`` field so onboarding can offer the instrumented wrapper.
- Honesty notes baked into the contract: the options-database path checkpoints per step (PETSc has no walltime awareness) — only the petsc4py monitor path gets walltime-margin semantics; ``PETSC_OPTIONS`` cannot carry whitespace paths (rejected loudly); the wire ``resume_flag`` is pattern-constrained to a CLI-flag shape (no argv injection).
- Tests: adapter detection/options/rotation/monitor seams (``tests/experiment_kit/solver_adapters/test_petsc.py``), instrumented-wrapper materialization + end-to-end env/argv injection (``test_interview.py``), candidate flagging (``test_detect_entry_point.py``). ``interview.input.json`` regenerated; ``detect_entry_point.output.json`` (hand-authored) gains the optional ``solver`` field.

## 0.10.43 — 2026-06-09

### Added — `resolve-resources` auto-derives the SGE parallel environment (#293)

Closes the one remaining in-scope item from the multi-rank workstream: an SGE MPI submit no longer needs the caller to hard-code `mpi.pe_name`. PR1 already enumerates each cluster's `parallel_environments` (SGE PEs / SLURM partitions / PBS queues, tagged `kind: mpi|smp|other`); this wires that into `resolve-resources` so the PE is selected from the cluster's own enumeration.

- **New pure selector `ops/recommend_pe.recommend_pe(parallel_environments, ranks)`** — considers only `source="pe"` + `kind="mpi"` entries (SLURM/PBS size from `--ntasks`/`select=` and need no `-pe` name), and among those with sufficient slot capacity (`raw.slots >= ranks`, or unknown capacity assumed usable) picks the **tightest fit** (smallest sufficient PE, deterministic on ties). Returns `(pe_name, rationale)`; `None` with a diagnostic rationale (`no_mpi_pe` / `no_pe_fits_ranks:…`) when nothing qualifies.
- **`resolve-resources` grows an MPI-aware path** mirroring its `recommend-partition` delegation: new `--mpi-ranks` / `--mpi-pe` args (+ programmatic `parallel_environments`, like `partitions`), a resolved `mpi_pe` output field, and a `provenance.mpi_pe` entry (`caller` / `not_mpi` / `no_parallel_environments_supplied` / `recommend_pe:<rationale>`). Caller override wins; absent `mpi_ranks` ⇒ `null` (not an MPI submit). The existing `build-submit-spec` SGE `pe_name` guard still backstops a missing PE.
- Tests: `tests/ops/test_recommend_pe.py` (tightest-fit, capacity filtering, smp/partition exclusion, unknown-slots, determinism) and a `TestMpiPe` class in `test_resolve_resources.py` (auto-derive / override / not-mpi / no-enumeration / ranks-exceed-capacity). Hand-authored input+output schemas, `operations.json`, and generated docs regenerated.

## 0.10.42 — 2026-06-09

### Added — MPI failure signatures + multi-rank canary (#293 PR4)

Fourth slice of the multi-rank workstream (#293) — the guardrails. PR2/PR3 made a multi-rank job submit and run; this PR makes its failure modes classifiable and proves the launch with a cheap canary before the full allocation queues.

- **Three multi-rank failure signatures** added to `failure_signatures.CATALOG`, each with a concrete remediation: `mpi_launcher_missing` (srun/mpirun/aprun not on PATH → load the MPI module / pick a provided launcher), `mpi_pe_invalid` (SGE rejected the parallel environment → pick a `kind=mpi` PE from inspect-cluster), `mpi_init_failed` (the MPI runtime couldn't start the ranks — *not enough slots*, `MPI_Init`/`MPI_ABORT`, ORTE/PMIx wire-up → lower ranks or fix the library/topology). Threaded through the full vocabulary chain so resubmit accepts them: the `FailureCategory` StrEnum, the `FailureCategoryResubmittable` wire Literal (and its regenerated `resubmit.input.json`), and `CLASSIFIER_CATEGORIES`. Priority 95 so a launch failure that also dumps a Python traceback still classifies structurally.
- **The MPI canary runs the smallest meaningful job** — `ranks=2` on one node (`ranks_per_node=2`), launcher/threads/walltime preserved — via `_mpi_canary_resources`, which `model_copy`-shrinks the spec rather than mutating it and overrides `HPC_MPI_RANKS` so the in-job launcher spawns 2 ranks too. This validates the launcher resolves and the MPI library loads before the full multi-node allocation queues; a non-MPI submit is untouched (returns the resources unchanged).
- The #293 coherence guards (`ranks_per_node` must divide `ranks`; SGE requires `pe_name`) landed with PR2's wire model; this PR completes the guard surface with runtime classification + the pre-flight canary.
- Tests: MPI classify cases (launcher/PE/slots/init, plus the traceback-precedence case) in `test_failure_signatures.py` (catalog size 15→18), and `_mpi_canary_resources` downsizing / non-MPI no-op / None-handling in `test_canary_gate.py`.

## 0.10.41 — 2026-06-09

### Added — multi-rank executor convention + dispatcher launcher (#293 PR3)

Third slice of the multi-rank workstream (#293). PR2 let a submit *request* a multi-rank job; this PR makes the executor and dispatcher actually run one, where the same single-process `compute` body becomes rank-aware without breaking any existing executor.

- **`@register_run(mpi=True)`** marks a multi-rank entry point. Its injected `compute` fills `rank` / `world_size` from the launcher environment (`mpi_rank_world()` reads `OMPI_COMM_WORLD_*` / `PMI_*` / `SLURM_PROCID`+`SLURM_NTASKS`, falling back to `(0, 1)` off-launcher) and **only rank 0 writes the per-task output** — the reducer still sees exactly one `metrics.json`. A plain `@register_run` is unchanged: it never injects those params and, being single-process rank 0, always writes. Reuses the existing `_make_compute` wrapper + `accepted`-kwarg filter — the rank/world injection rides the same `setdefault` seam as `resume_from`/`checkpoint_dir`.
- **`rank` / `world_size` are reserved framework-injected params** (`MPI_INJECTED_PARAMS`), excluded from synthesised CLI flags for mpi runs in both `flags_from_signature` and `flags_from_ast` — so they arrive from the launcher, not as `--rank` flags the dispatcher would have to supply. `discover_runs` reads `mpi=True` from the decorator (mirroring the existing `gpu=` AST detection) and threads it through `RunInfo` + the discover cache.
- **The dispatcher applies the launcher** (`execution/mapreduce/dispatch.py`): `_mpi_launch_prefix` reads `HPC_MPI_RANKS` / `HPC_MPI_LAUNCHER` and prefixes the *per-task* command with `srun --ntasks=N` / `mpirun -np N` / `aprun -n N`. Prefixing the inner command (not wrapping the whole template in `srun`) keeps the dispatcher's bookkeeping — sidecar, WIP dir, failure capture, SIGTERM forwarding — a single process while only the compute fans out to N ranks. The `mpi` template is correspondingly simplified to run the dispatcher once (no in-template `srun`).
- Tests: `tests/experiment_kit/test_mpi.py` (rank/world injection, rank-0 output gate, non-mpi unaffected, `mpi_rank_world` env matrix, flag exclusion, discover detection) and `tests/execution/mapreduce/test_mpi_launch.py` (`_mpi_launch_prefix` per launcher, empty/single-rank/unknown-launcher no-ops). MPI golden fixtures regenerated for the simplified template.

## 0.10.40 — 2026-06-09

### Added — MPI / multi-rank submit spec, resource flags, and single-job template (#293 PR2)

Second slice of the multi-rank workstream (#293). PR1 already taught `inspect-cluster` to enumerate `parallel_environments` (SGE PEs / SLURM partitions / PBS queues, tagged `kind=mpi|smp|other`); this PR lets a submit actually *request* a multi-rank job, where N ranks across M nodes are ONE unit of work rather than a fan-out of single-process tasks.

- **New optional `mpi` block on the submit spec.** `SubmitResources.mpi` (`MpiSpec`) carries `{ranks, ranks_per_node?, threads_per_rank, launcher (srun|mpirun|aprun), pe_name?}`. The same model is reused verbatim on `build-submit-spec`'s input. A wire validator refuses a `ranks_per_node` that does not evenly divide `ranks` (no integral node count — the #293 coherence guard), and `build-submit-spec` refuses an SGE mpi block without a `pe_name` (SGE routes multi-rank work through a parallel environment, not a generic flag).
- **`resource_flags` grows an MPI slot grammar**, reusing the existing per-family walltime/mem emitters so the two paths can't drift: SLURM `--nodes/--ntasks/--ntasks-per-node/--cpus-per-task`, SGE `-pe <pe_name> <ranks>`, PBS `select=<nodes>:ncpus=…:mpiprocs=…:ompthreads=…`. The single-node cpu/mem/cpus path is unchanged.
- **A single multi-rank job is submitted non-array.** `_build_command` gained an `array=False` path (no `--array`/`-t`, SLURM logs switch `%A_%a`→`%j`); a submit with an `mpi` block and `total_tasks == 1` takes it. An `mpi` block with `total_tasks > 1` stays the array-of-MPI power-user shape.
- **New `mpi` job template** per family (`render_script(kind="mpi")`, shipped as `.hpc/templates/mpi.{sh,slurm,pbs}`), built from one shared builder rather than four literals. It reuses the existing `hpc_preamble.sh` + `hpc_run_with_retry` unchanged — the only MPI-specific step folds the launcher + `$HPC_MPI_RANKS` into `$EXECUTOR`, so bounded retry / terminal-failure markers work identically. `build-submit-spec` selects it (over cpu/gpu array, independent of `is_gpu`) and stamps `HPC_MPI_RANKS` / `HPC_MPI_LAUNCHER` / `HPC_MPI_THREADS_PER_RANK`.
- Tests: `tests/infra/backends/test_mpi.py` (per-family resource flags, non-array command path, `MpiSpec` guards), MPI golden fixtures in `test_render_script_golden.py`, and `TestMpiBlock` in `test_submit_spec.py` (template selection, env stamping, resources emission, SGE pe_name guard). Deploy-manifest counts updated for the third template.

## 0.10.39 — 2026-06-09

### Added — budget-halt acknowledgement + campaign-level per-task resubmit cap (#224)

Closes the two remaining gaps in the budget governor / loop-safety work (#224). The accounting, the hard `stop_over_budget` ceiling, the consecutive-failure circuit breaker, and durable manifest caps already landed; what was missing was (1) an explicit-acknowledgement-to-resume mode after a budget halt, and (2) a per-task resubmit cap at the *campaign* level (the within-run `DEFAULT_AUTO_RETRY_POLICY` cap resets every fresh run).

- **`stop_over_budget` is now a halt the loop cannot silently pass.** `campaign-advance`'s budget rule consults a durable acknowledgement at `<campaign_dir>/budget_ack.json`: once a cap is met it keeps returning `stop_over_budget` with `needs_acknowledgement: true` until the spend is explicitly acknowledged. The new **`campaign-acknowledge-budget`** primitive writes that ack as a **snapshot of realised spend**. Because spend is monotonic, the ack authorises continuing only while spend stays at the snapshot — the next task that burns compute makes it stale and re-arms the halt, so a bare ack buys exactly one more leg (self-limiting, no infinite-bypass foot-gun). Passing raised caps (`--max-core-hours` etc.) enlarges the budget in the same audited gesture, written through to the manifest (other sections preserved) for durable headroom. Ack reads are conservative: a malformed/missing ack reads as "no acknowledgement" and can never relax the halt.
- **New `stop_resubmit_cap` decision.** A new `resubmit_cap` atom sums `RunRecord.retries[tid]["attempts"]` per task slot across all the campaign's runs (derived from existing journal state — no new persistence); `campaign-advance` emits `stop_resubmit_cap` when the worst slot meets the supplied cap. Like the circuit breaker it sits *after* `wait_in_flight` so an in-flight retry gets its chance first. Surfaced as `--max-task-resubmits` on `campaign-advance` / `campaign-init`, durable in the manifest under `stop_criteria.max_task_resubmits`, and defaulting from there when the arg is omitted — matching the existing caps.
- The decision ladder is now `stop_over_budget` → `wait_in_flight` → `stop_circuit_breaker` → `stop_resubmit_cap` → `stop_converged` → `continue`; the `DeterministicCampaignResolver` already treats any non-`continue` decision as a decided clean terminal, so both new halts flow through unchanged.
- Tests: `tests/meta/campaign/atoms/test_resubmit_cap.py` (per-slot accounting, manifest defaulting, in-flight precedence, init persistence) and `test_budget_ack.py` (halt-until-acked, acknowledge-then-continue, ack-goes-stale-on-more-spend, raise-cap-clears-halt + preserves other manifest sections).

## 0.10.38 — 2026-06-09

### Added — a deterministic, LLM-free campaign judgement resolver (#220 Phase 1)

The headless campaign driver advances a judgement (`kind="agent"`) step through an injected `JudgementResolver`; the default (`default_judgement_resolver`) spawns a fresh-context LLM worker (`claude -p` via `run_workflow`). #220's goal is to decouple the driver from Claude so any agent — or no agent — can drive it. This lands Phase 1: a **`DeterministicCampaignResolver`** that executes the campaign `decide` / cold-`submit` judgement steps **in code** by chaining the existing deterministic primitives, with **zero worker/LLM spawn** on the common `decided_by="code"` path.

- **New, injectable artifact — the default resolver is untouched (that's Phase 2).** A caller opts in by injecting it into `CampaignLoopConfig(resolver=...)`; the new `deterministic_campaign_config()` helper builds that config (default monitor/aggregate step table intact). It lives in `meta.campaign` because the dependency points campaign → drive (`drive.py` must not import campaign), mirroring how `driver.py` already supplies the loop's policy.
- **The chain (verified against the on-disk primitives, not assumed).** For `decide`: `classify-campaign-path` (manual grid vs strategy) → `campaign-advance` (the deterministic stop/continue ladder) → on `continue`, reconstruct the next iteration's submit context from the prior run's journal record (`ssh_target`/cluster/profile/remote_path) + sidecar config snapshot (executor/result_dir_template/env-activation/resources/runtime), run `resolve-submit-inputs` to build + validate the submit-flow spec and write the per-run sidecar, submit via an injected cluster-I/O seam, then `advance_cursor`. The `prepare-followup-specs` hypothesis in the issue was wrong (it pre-stages *monitor/aggregate* specs at submit time, not the next-iteration submit); the actual bridge is `resolve-submit-inputs` → submit.
- **Residue policy: halt-and-park, never guess (#231/#234/#240 posture).** When a backing primitive escalates — `classify-campaign-path` returns `unclassifiable` (`decided_by="judgement"`), `resolve-submit-inputs` returns `needs_decision` (live prior run / scaffold interview), or a cold submit has no prior run to rebuild from — the resolver surfaces the escalation as data in the synthesized `WorkerReport` and returns a non-zero exit so the neutral loop stops cleanly rather than blind-submitting. A non-`continue` `campaign-advance` decision (`stop_*`/`wait_in_flight`) is a *decided* clean terminal (exit 0), not residue.
- **The synthesized `WorkerReport` is contract-valid:** it round-trips through `parse_worker_report`, reporting only `code`-decided points (no `why` required) — the same validation the LLM path's reports pass.
- Tests: a new end-to-end suite (`tests/meta/campaign/test_deterministic_resolver_e2e.py`) drives the resolver over a seeded strategy-driven campaign and asserts the `continue`→next-submit path (cluster I/O stubbed, zero LLM spawn, cursor advanced), the unclassifiable-path and cold-submit residue cases (non-zero exit, escalation surfaced, no blind submit), the decided-stop clean terminal, and the report round-trip.

## 0.10.37 — 2026-06-09

### Added — surface the resolve-and-recover opt-in through the submit spec (#240, #234)

0.10.35 wired `maybe_resolve_and_recover` into the monitor's `FAILED` tick but left it "behavior-neutral until a run opts in" — and there was no way to opt in. The gate it reads, `RunRecord.auto_recover_on_failure`, had **no producer**: neither `SubmitFlowSpec` nor the `submit_and_record` sink carried the field, so it could only ever be its default `False` and the freshly-wired hook could never fire. This adds the missing producer so the seam is reachable, mirroring the blessed #299 `auto_resume_on_kill` keystone end to end:

- **`SubmitFlowSpec` gains `auto_recover_on_failure` (default `False`) + `max_auto_recovers` (default 2).** Same default-OFF zero-blast-radius posture as `auto_resume_on_kill`, and independent of it — `auto_resume_on_kill` stays preempt-only; enabling the general deterministic resolver is a deliberate separate choice. The five `*.input.json` schemas that embed the submit spec are regenerated.
- **`submit_flow` threads `spec.auto_recover_on_failure` / `spec.max_auto_recovers` into `submit_and_record`,** which persists them onto the journal `RunRecord` on both the fresh-submit and the journal-wiped reconstruction paths (so a cross-machine resubmit keeps the opt-in alive instead of silently reverting to default-OFF), exactly as the auto-resume keystone is carried.
- **No new behavior for existing runs:** a spec that does not set the opt-in writes `False`, and the monitor's resolve-and-recover hook stays computed-and-surfaced-only (no resubmit, no park) — the #283 posture is unchanged.
- Tests: a new end-to-end suite (`tests/ops/test_resolve_and_recover_opt_in_e2e.py`) drives the opt-in through the *real* submit plumbing into the persisted record and then through the *real* composite (only the failure fetch + `resubmit_flow` injected) to an actual code-verdict resubmit with the translated mem override; plus the default-OFF path proven side-effect-free through the same plumbing. This closes the gap the composite tests (which seed the record directly) and the monitor tests (which fake the composite) left open.
## 0.10.36 — 2026-06-09

### Changed — split the per-harness decode-schema gate; pre-author the strict WorkerReport variant (#269)

The decode-time output-schema accelerator was gated by a single env var, `HPC_AGENT_WORKER_JSON_SCHEMA`, shared by both the Claude (`--json-schema`) and Codex (`--output-schema`) workers. That coupling is a latent hazard for the #269 flip: turning the accelerator on by default is gated on a **live validation run, and that validation is per-harness** (whether the decode-schema composes with the agent loop and whether the CLI accepts the schema shape are separate empirical questions for each CLI). A shared gate would flip an unvalidated harness on as a side effect of validating the other. The gate is now split: `HPC_AGENT_WORKER_JSON_SCHEMA` governs Claude only; a new `HPC_AGENT_CODEX_OUTPUT_SCHEMA` governs Codex. Both remain **off by default** — no production behavior changes today; this lets #269 flip each harness independently once its own live run confirms.

The split also fixes a documented latent bug. Codex's `--output-schema` requires an **API-strict** schema (`additionalProperties:false` + all-required), but the shared path bound the lenient `worker.output.json` floor schema (a standing `TODO(#269/#304)` in `CodexCliInvoker._output_schema_args`). The Codex gate now binds a new checked-in `worker.strict.output.json`, **generated from the `WorkerReport` Pydantic model** by `scripts/build_schemas.py` (a new `DERIVED_REGISTRY` of schema→schema transforms, kept out of `SCHEMA_REGISTRY` because a strict schema doesn't accept a model's own minimal dump). The strictifier is the single canonical `to_strict_schema`, extracted to `hpc_agent._kernel.contract.strict_schema` and now shared by both the build script and the `openai-compat` runtime accelerator (`#304`) — one transform, not two that can diverge. Claude continues to bind the lenient `worker.output.json` (whether claude's mode needs strict is the open #269 question, unanswerable offline). `result` stays free-form in the strict variant (`additionalProperties: true`) — inherent to the model's free-form payload field; the floor validates it regardless. A drift test pins `worker.strict.output.json` to its generator, a strictness-invariant test pins the all-required/no-extras shape, and a gate-independence test pins that neither env var enables the other harness. `env-vars.md` documents both gates.

## 0.10.35 — 2026-06-09

### Added — wire the resolve-and-recover composite into the live monitor tick (#240, #234)

`maybe_resolve_and_recover` (the #240 live wiring of the #234 deterministic resolver) was built and tested but never called from any live site — it was inert. It is now invoked from `monitor_flow`'s `FAILED` seam, mirroring the blessed #299 `maybe_auto_resume` hook beside it. The two composites **partition** a FAILED tick without double-handling: auto-resume owns `preempted` clusters (and on a `"resume"` verdict `continue`s the loop before resolve-and-recover runs), while resolve-and-recover deliberately **skips** `preempted` (its `_DETERMINISTIC` set excludes it) and handles everything else. The wiring is **behavior-neutral until a run opts in**: `RunRecord.auto_recover_on_failure` defaults `False`, so a non-opted-in run computes the verdict-as-data and takes **no** side effect (no resubmit, no park), falling through to the existing `FAILED` surface. On a `decided_by="code"` verdict under cap (opt-in ON) the composite resubmits and the loop reloads the record and keeps polling, exactly as the auto-resume `"resume"` branch does; a `held` (judgement / over-cap) verdict enriches `escalation_reason` via the escalation-as-data path (#234). Every consulted tick surfaces the per-cluster dispositions into the monitor tick's `actions` log under `kind="resolve_and_recover"`. Injection seams (`resubmit`, `failures_fetcher`) pass through to the composite's existing defaults. Control-plane only; no wire surface changed.

## 0.10.34 — 2026-06-09

### Fixed — Codex worker authenticates with the scoped key (#305)

The `codex-cli` worker invoker gated its pre-spawn guard and auto-selection on `CODEX_API_KEY`, but Codex itself does not read that variable — it authenticates from `OPENAI_API_KEY` or a stored ChatGPT login in `~/.codex/auth.json`. The driver passed the environment through unchanged, so a worker selected on `CODEX_API_KEY` (the documented primary path) never conveyed that key to Codex: it silently fell back to an ambient `OPENAI_API_KEY` or the stored ChatGPT login — exactly the shadow hazard ([codex #3286](https://github.com/openai/codex/issues/3286)) the scoped key exists to avoid. The guard would pass while the worker authenticated with the wrong credential (or none). The invoker now maps `CODEX_API_KEY` onto the child's `OPENAI_API_KEY`, overriding any ambient value so the scoped key both authenticates the worker and out-ranks a stored login. The Gemini and Claude paths and the fence/sandbox/argv posture are unchanged.

## 0.10.33 — 2026-06-09

### Fixed — campaign compute-spend coverage accounting (#224 follow-up)

Two minor fixes to `consumed_compute_for_campaign` from review. (1) The two uncounted-run coverage buckets are now disjoint: a legacy sidecar with no `(profile, cluster)` join key was listed in **both** `runs_without_profile_cluster` and `runs_without_samples`, so a consumer summing the two double-counted it — `runs_without_samples` now excludes the legacy bucket, and `partial` independently accounts for `runs_without_profile_cluster` so an all-legacy campaign is still flagged partial (it previously could have read `partial: false`). (2) De-privatized the cross-package reach into `runtime_prior._cores_used_from_sample`: the effective-core estimate is now the public `runtime_prior.cores_used_from_sample` (in `__all__`), so the spend accounting consumes a supported surface instead of a private helper. No change to the spend numbers themselves.

## 0.10.32 — 2026-06-09

### Added — budget governor: real compute accounting for unattended campaigns (#224)

`campaign-budget`'s compute-spend ceiling was blind. `_spent_walltime_sec` always returned `0.0` — it read a sidecar key (`last_status['tasks']`) that never existed — so the `max_walltime_sec` cap could never fire and an unattended campaign could burn unbounded walltime. This lands real consumption accounting. A new `hpc_agent.meta.campaign.atoms.compute_spend.consumed_compute_for_campaign` joins the runtime-prior store (`.hpc/runtimes/<profile>__<cluster>.json`, the per-task `elapsed_sec` / `cpu_seconds_used` / `gpu_type` samples) to the campaign's runs on `run_id`: it groups the campaign's sidecars by `(profile, cluster)`, reads each runtime-prior file once, and attributes each run's samples. `campaign-budget`'s `spent` now carries the real consumption — `walltime_sec` (Σ per-task elapsed), `core_hours` (Σ elapsed×effective-cores/3600, cores via the same `cpu_seconds_used/elapsed` estimate the planner uses), and `gpu_hours` (Σ GPU-task elapsed/3600, a one-GPU-per-task lower bound since the sample carries no per-task GPU count) — and `max_walltime_sec` now actually exhausts the budget. Spend is honest about partial coverage: a `coverage` block reports `runs_without_samples` (accounted as zeros, never silently folded into a global 0), `runs_without_profile_cluster` (legacy v1 sidecars that can't be joined), and `tasks_missing_core_estimate`, with `partial: bool`. Failed tasks' walltime is still counted (spend, not successful-spend). A best-effort `projected` block adds consumed + (per-task observed mean × in-flight task count) for runs currently `in_flight` in the journal; future not-yet-submitted iterations are NOT projected (their task counts don't exist yet — fabricating a horizon would invent state) and `basis` says so. `projected` is advisory and never drives `exhausted` — only realised spend can exhaust a cap. New `tests/meta/campaign/atoms/test_budget_accounting.py` pins cross-run summing, the now-firing walltime cap, core-hours from cpu-seconds, partial coverage, failed-task walltime, and CPU-only (no gpu-hours). `campaign-budget` gains a `--max-core-hours` cap arg; `operations.json` / primitive docs / catalog re-baked.

### Added — durable `max_core_hours` cap surfaced through advance + manifest (#224)

The new core-hours budget cap is now threaded end-to-end, following the manifest's "land the primitive arg first" rule (the `campaign-budget --max-core-hours` arg landed above). `campaign-advance` gains `--max-core-hours` and passes it through to `campaign-budget`, so `stop_over_budget` fires on the core-hours cap exactly as it does for jobs/tasks/walltime. `campaign-init` gains `--max-core-hours`, persisting it into the manifest's `budget` block; the `CampaignManifest` Pydantic model's `_CampaignBudget` gains the `max_core_hours: float | None (ge=0)` field and `campaign_manifest.json` is regenerated. `campaign-budget` continues to default any cap left `None` from the manifest, so a campaign initialized with `--max-core-hours` is governed without re-supplying the cap on every advance. Existing `max_walltime_sec` / `max_jobs` / `max_tasks` / `max_iters` caps are unchanged — this is purely additive. Tests pin the core-hours cap firing through `campaign-advance`, the manifest default path, and `campaign-init` persistence.

### Added — loop-safety circuit breaker on N consecutive iteration failures (#224)

A new `stop_circuit_breaker` terminal decision halts an unattended campaign that is failing every iteration for a systemic reason (broken environment, dead node pool, an entry-point that no longer imports) — a fast halt the budget governor would only eventually catch via spend. **No new persistence.** The framework keeps no campaign-iteration *canary* signal distinct from the iteration's run lifecycle, so "canary failure" maps to the iteration's terminal journal status: a campaign iteration is one `campaign_id`-tagged run, and its disposition is `RunRecord.status` (`complete`/`failed`/`abandoned`/`in_flight`) — the same field `campaign-health` already counts as `n_failed`. New `hpc_agent.meta.campaign.atoms.circuit_breaker.consecutive_terminal_failures` walks the campaign's journal records (via `find_runs_by_campaign`, oldest-first) newest→oldest and counts the trailing run of terminal failures: `failed`/`abandoned` extend the streak, the first `complete` ends it, and `in_flight` (not yet terminal — a just-submitted retry carries no verdict) is skipped so it doesn't reset a real streak. `campaign-advance` gains `--circuit-breaker-failures N` (no framework default — omitted means no breaker, matching the budget-caps convention) and emits `stop_circuit_breaker` with a structured rationale (count, threshold, failing run_ids) via the same decision-kernel `CandidateAction` ladder as `_over_budget`/`_converged`. Precedence: budget → wait_in_flight → circuit_breaker → converged → continue — the breaker sits after `wait_in_flight` so an in-flight retry gets its chance and a live job is never orphaned by a stale streak. The threshold is surfaced durably: `campaign-init --circuit-breaker-failures` persists it into the manifest's `stop_criteria`, the `CampaignManifest` `_StopCriteria` model gains `circuit_breaker_failures: int | None (ge=1)` (schema regenerated), and `campaign-advance` defaults the arg from there when omitted. `campaign-advance`'s output gains a `circuit_breaker` block (`count`/`run_ids`/`last_status`/`threshold`). New `tests/meta/campaign/atoms/test_circuit_breaker.py` covers the streak helper (trailing count, complete-resets, in-flight-skipped), the end-to-end advance decision, under-threshold continue, no-arg never-fires, manifest defaulting, in-flight precedence, and init persistence.

### Deferred — campaign-level per-task resubmit ceiling (#224)

The fourth #224 loop-safety invariant — extending the run-scoped `DEFAULT_AUTO_RETRY_POLICY` `max_attempts` to a per-task *campaign-level* cap (so a task that fails every iteration eventually halts instead of resubmitting forever) — is deferred because it requires ambiguous NEW persistent state, which the #224 brief and the repo's "don't pre-build before demonstrated cost" discipline say to defer rather than guess. The existing attempt counter, `RunRecord.retries[task_id].attempts`, is strictly *run-scoped*: `task_id` is a per-run index (0..task_count-1) and the same `task_id` in two campaign iterations is a *different* trial (different hyperparameters), so the counter does not accumulate across iterations. Wiring a campaign-level cap would require two pieces of state that do not exist and whose shape is genuinely ambiguous: (1) a framework-interpretable *cross-iteration task identity* — the only cross-iteration per-task handle today is `trial_tokens`, which is explicitly **opaque** ("the framework never interprets it"), so the framework cannot define "the same task across iterations" without a strategy contributing a stable key whose semantics it would have to standardize; and (2) a durable per-identity cumulative attempt counter (a new persisted map keyed on that identity). Designing either by guessing would bake a schema the strategies can't honor. The circuit breaker (above) already provides a campaign-level systemic-failure halt from existing state; the per-task ceiling should land only alongside a deliberate cross-iteration task-identity contract. No code shipped for this item.

## 0.10.31 — 2026-06-08

### Changed — worker-prompt Claude-ism audit: Step 0b is CLI-only + harness-neutral (#305 follow-up)

The #305 follow-up that unblocks a live non-Claude worker run. The headless worker's contract (`docs/reference/agent-surface.md`) is that it drives the core by shelling `hpc-agent <verb>` and parsing JSON — `Bash(hpc-agent:*)` is the one surface every harness grants. But `submit.md` Step 0b told the worker to inspect files freestyle, which is fence-asymmetric: Claude's `_WORKER_ALLOWED_TOOLS` is a strict allowlist (`Read`/`Glob`/`Grep` permitted, shell `grep`/`cat`/`test` blocked), while the Codex/Gemini fences are denylists (shell `grep` fine, but no native `Read`/`Glob`/`Grep` tool) — so the Read/Glob/Grep instructions were inverted for non-Claude harnesses. Two changes close the gap. (1) `detect-entry-point` now also surfaces `interview.json`'s `_materialized.entry_point` block as an optional `materialized` field (`kind` ∈ `shell_command`/`register_run`/`python_module`, plus `run_name`/`wrapper_path`/`executor_cmd`/`module`/`function`/`data_axis` as the source block declares; the internal `frozen_shas` is not surfaced). It reads `<experiment_dir>/interview.json` (the canonical campaign-dir-root location, with a `.hpc/interview.json` fallback); absent/malformed/no-block leaves the field absent and the repo-scan output (`kind`/`candidates`/`decoration_found`) unchanged — additive only. So ONE `hpc-agent detect-entry-point` call answers all of Step 0b: honor a materialized wrapper (`materialized.kind`) AND run the mature-repo HAS_MAIN/HAS_REGISTER_RUN fallback probe (`candidates`/`decoration_found`). (2) `submit.md` Step 0b + the fallback probe are rewritten to route through that one verb — all `Read`/`Glob`/`Grep` and shell `grep`/`cat`/`test`/`python` instructions deleted; the kind→procedure table and the `mature_repo_needs_interview` anomaly+stop behavior are preserved, only the fact-gathering mechanism changes. Claude-only slash-command names in worker-facing instructions and worker-emitted remediation text are neutralized to the capability they name (`/wrap-entry-point-hpc` → "the entry-point wrap workflow", `/submit-hpc` → "unscoped / scoped-to-one-file submit", `/monitor-hpc` + `/aggregate-hpc` → "the status / aggregate workflow"). No fence widened (`_WORKER_ALLOWED_TOOLS` and `_CLUSTER_OP_DENY_COMMANDS` untouched) — the point is `Bash(hpc-agent:*)` was already allowed everywhere. The hand-authored `detect_entry_point.output.json` gains the optional `MaterializedEntryPoint` def; `operations.json` re-baked; the `submit.prefix.txt` worker-prompt fixture regenerated; the primitive doc updated. New tests cover the materialized block (each kind, both file locations, absent/malformed) and the unchanged repo-scan path.

## 0.10.30 — 2026-06-08

### Changed — neutralize the tick-loop's transport copy + add a programmatic entry (#220 follow-up)

Follow-up hardening on the #220 extraction, made live by #305. Three gaps closed in `src/hpc_agent/_kernel/lifecycle/drive.py`: (1) the "neutral" loop still advertised `claude -p` in two runtime-visible strings — the `--allow-agent-steps` skip reason and the `--help` text — even though `default_judgement_resolver` routes through `HPC_AGENT_INVOKER` and can now spawn a `codex-cli`/`gemini-cli` worker (#305); both are reworded to the transport-neutral "spawn a worker," with Claude-specificity left to the default resolver. (2) The loop was only reachable through argparse, so an external autonomous agent (Optuna/Ax/LangGraph) had to synthesize argv strings to drive it; a programmatic `drive_once(experiment_dir, *, step_table, resolver, allow_agent_steps, dry_run) -> int` is now the entry, and the argparse `drive()` is a thin wrapper over it. (3) The `JudgementResolver` contract is documented — an injected resolver owns the pre-spawn credential fail-fast (the default inherits it from `run_workflow`) and prompt-cache accounting (#244) is not carried by the 2-tuple. Also: `CampaignLoopConfig` is declared `frozen=True` but its default `step_table` had aliased the mutable `_CAMPAIGN_STEP_VERB` module global, so a caller mutating `config.step_table` would silently pollute it for every later config — the global is now a `MappingProxyType`, making the freeze honest. No wire or behavior change to the `hpc-campaign-driver` surface (the `{delegate, plan}` envelope and dispatch are unchanged); new tests cover `drive_once` routing/dry-run, the `drive()` argparse wrapper's argv→kwargs delegation, the immutable config default, and lock the transport-neutral skip reason. `docs/integrations/CONTRACT.md` updated.

## 0.10.29 — 2026-06-08

### Added — multi-harness WorkerInvoker drivers: Gemini CLI and Codex CLI (#305)

The headless worker can now run under OpenAI Codex CLI or Gemini CLI, not just Claude. Two new `WorkerInvoker` drivers (`hpc_agent._kernel.lifecycle.invoke`) join `claude-cli`/`claude-cli-oauth`, each normalizing the same four axes on its own config surface. **`codex-cli`** (`codex exec`): whole prompt on stdin (no append-system-prompt cache to split), final report read from `--output-last-message`; `--dangerously-bypass-approvals-and-sandbox` so the worker SSH/rsyncs out unattended; an `execpolicy` `.rules` fence (`decision="forbidden"`, strictest-severity-wins) re-imposing the no-`scancel`/no-exfil cluster-op deny; optional `--output-schema` gated behind `HPC_AGENT_WORKER_JSON_SCHEMA` (off by default); auth via `CODEX_API_KEY` (preferred over ambient `OPENAI_API_KEY`, which a stored ChatGPT login can shadow); model pinned to `gpt-5.4-mini`. **`gemini-cli`** (`gemini -p`): cacheable prefix as a full-replacement system prompt via `GEMINI_SYSTEM_MD`, suffix on stdin, output unwrapped from the `--output-format json` `{response, stats, error}` envelope; no sandbox backend selected (`GEMINI_SANDBOX` left unset); a Policy Engine TOML fence installed at the User/Admin tier (NOT workspace tier — upstream #18186 silently no-ops it) with deny entries out-ranking allow by `priority`; no CLI decode schema, so it leans entirely on the `parse_worker_report` floor (#304); auth via `GEMINI_API_KEY`/`GOOGLE_API_KEY`; model pinned to the concrete `gemini-2.5-flash`. Both surface `cache_stats=None` (the CLIs expose no cache-creation/cache-read split). Registered as `codex-cli`/`gemini-cli` and selectable via `HPC_AGENT_INVOKER`. Auto-selection is extended without changing existing behavior: Claude credentials still win (API key → `claude-cli`, then an OAuth file → `claude-cli-oauth`); only when no Claude credential is present does it fall through to `CODEX_API_KEY` → `codex-cli`, then Gemini creds → `gemini-cli`, then the unchanged `claude-cli` final fallback. The `WorkerInvoker` contract (all four axes + the auth-guard method + the `cache_stats=None` fallback) is documented in the module docstring; the new auth/model env vars and `HPC_AGENT_INVOKER` values are in `docs/reference/env-vars.md`. Contract-tested at the argv/transport/auth level (the existing Claude testing style); live end-to-end validation is deferred (no `codex`/`gemini` binary or key in this environment). Follow-ups noted but out of scope: `setup --harness {gemini,codex}` (the human slash-command reskin) and the worker-prompt Claude-ism audit (native-file-tool instructions in `submit.md`) that unblocks real e2e runs.

## 0.10.28 — 2026-06-08

### Changed — extract the headless tick-loop as neutral substrate the campaign configures (#220)

Internal refactor; no wire or behavior change. The generic loop that walks a campaign one `delegate` step per invocation had zero static coupling to `meta/campaign` but hardcoded two campaign-flavored bits inline: the `monitor`/`aggregate` → flow-verb map and the `claude -p` judgement path. Both are now injected seams — a `StepTable` (delegate-step → hpc-agent verb) and a `JudgementResolver` ((`spawn_request`, `experiment_dir`) → (`report`, `exit_code`)) — and the neutral mechanism (`load_context`, `plan_action`, the `cli`/`agent` dispatch, the new `drive()` loop body) moved to `src/hpc_agent/_kernel/lifecycle/drive.py`. The dependency now points `meta/campaign` → `_kernel/lifecycle/drive`, never the reverse; `drive` does not import `meta.campaign`. `meta/campaign/driver.py` is a thin shim that holds `CampaignLoopConfig` (the campaign step map + default resolver) and the campaign `main()`, and re-exports `plan_action` so the `hpc-campaign-driver` console script and existing importers are unchanged. Same "mechanism owns the protocol; the caller owns the policy" split `_kernel/decision/kernel.py` establishes one layer down. The `{delegate, plan}` envelope is byte-for-byte identical (verified via `hpc-campaign-driver --dry-run`); the `hpc-campaign-driver` entry point is preserved through the shim.

## 0.10.27 — 2026-06-09

### Changed — resolve-and-recover translates the resolver's fix to concrete overrides, and surfaces what it cannot apply (#240, #234)

The #240 auto-fire composite passed the resolver's suggested-fix dict through to `resubmit_flow` as `overrides` verbatim — but `render_overrides_to_extra_flags` consumes only concrete scheduler knobs (`mem_mb` / `walltime_sec` / `gpus` / `cpus`), while the resolver emits a `{action, factor, knob}` shape, so a `decided_by="code"` verdict resubmitted the *identical* config (burning the cap re-running the failing run). The composite now translates the fix into concrete overrides (`ops.resolve_and_recover_flow._concrete_overrides`): `increase-mem*` / `increase-walltime` scale the run sidecar's current `mem_mb` / `walltime_sec` by the fix's `factor`; `retry-on-different-node` needs no override (a fresh dispatch IS the fix). A fix `resubmit_flow` structurally cannot enact — the parallelism/width fixes `increase-parallelism` / `reduce-width` change a *task kwarg* (`tp_size` / `batch_size`), not a scheduler flag, and a `factor` with no current resource to scale — is now **surfaced as a `decided_by="code"` escalation** (the deterministic recommendation, parked for manual action) rather than resubmitted-identical. Task-id coercion is hardened too: a non-integer task id escalates the whole cluster instead of raising `ValueError` mid-loop and aborting the unattended monitor tick. (Folds in the prior post-merge fixes: `temporal_context.phase` is `"unknown"` — a retry count is not a *progress* signal, the meaning the model and `resolve()` assign `phase` — and `build_failure_features` constructs via `model_validate` to satisfy the typed fields.)

## 0.10.26 — 2026-06-09

### Added — submit-flow stamps run-constant `fixed_params` into `extra.spec_kwargs` so the #234 resolver's gpu_oom discriminator can fire (#240, #234, #195)

The context-keyed resolver (#234) routes a `gpu_oom` to the *right* fix by reading parallelism/width knobs (`tp_size` / `batch_size` / `n` / …) out of the sidecar's free-form `extra.spec_kwargs` pocket (via `ops.recover.features_glue._resource_spec_from_sidecar`), but nothing populated that pocket on real runs — so the discriminator could never fire and every `gpu_oom` fell back to the flat `increase-mem-per-gpu`. submit-flow's `_ensure_run_sidecar` now reads the run's declared run-constant task kwargs and stamps them as `extra={"spec_kwargs": …}` on the synthesized per-run sidecar. The source is the persisted interview artifact `interview.json` at the experiment/campaign workdir root, key `entry_point.fixed_params` (the #195 non-axis params the interview bakes into every task's `resolve()` kwargs); `record_interview` persists the intent verbatim, so `fixed_params` round-trips there directly. **Soundness:** only the declared `fixed_params` are stamped — a per-task *swept* axis value is never run-constant and could route a wrong fix, so the reader deliberately reads only `fixed_params` and never the `task_generator` axes. The read is defensive (absent / unreadable / no-`entry_point` / no-`fixed_params` ⇒ a clean no-op, never an error), so a hand-written `tasks.py` run — which legitimately has no `fixed_params` — keeps working and simply ships no `spec_kwargs` (an accepted, documented limitation: such runs do not get the parallelism/width discriminator). The canary sidecar mirror (`_mirror_canary_sidecar`) copies the main sidecar's `extra` so canary failures discriminate too. **Control-plane only** — no wire surface changed (`SubmitFlowSpec` is untouched; this is an internal read at sidecar-write time), and the cluster-side dispatcher is not touched.

### Added — resolve-and-recover auto-fire wires the #234 resolver into the recover path (#240, #234)

The context-keyed failure resolver (#234) and the escalation funnel primitives were built and tested but had no composite firing them on the recover path. New `hpc_agent.ops.resolve_and_recover_flow.maybe_resolve_and_recover` is that buildable wiring (#240), modelled on the blessed `auto_resume_flow` template — a `fetch_failures` query (injected for testability) → a pure decide gate → `resubmit_flow` action — but swapping auto-resume's preempted-only gate for the general `resolve()` keyed on the widened `(error_class, temporal_context, resource_spec)` evidence vector. Per cluster it builds the #230 `failure_features` (new pure glue `hpc_agent.ops.recover.features_glue.build_failure_features` / `build_escalation_cluster`), calls `resolve()`, and routes the verdict: a `decided_by="code"` verdict auto-resubmits with the resolver's refined overrides; a `decided_by="judgement"` verdict parks the run via `mark_pending_verdict` and surfaces the `Escalation` — one parked cluster never blocks resubmit of another cluster or run. `preempted` clusters are skipped (the auto-resume path owns them; the resolver's `_DETERMINISTIC` set excludes `preempted`), so the two composites never double-handle. The auto-act is **opt-in, default OFF, and hard-capped**, mirroring the auto-resume safety idiom: new `RunRecord` fields `auto_recover_on_failure` (opt-in), `max_auto_recovers` (cap, default 2), and `auto_recover_count` (counter, bumped per fired resubmit, on the `_UPDATABLE_FIELDS` whitelist). When opt-in is OFF the composite still computes and surfaces the decision-as-data verdict (no side effect) — no agent-facing field bypasses a safety step (#283). The `failure_features` glue sources `resource_spec` from the run sidecar's `resources` block merged with an optional `extra.spec_kwargs` pocket (values passed through as-written; `resolve._degree` coerces int-like strings), derives `temporal_context.phase` from whether the cluster's tasks have prior attempts in `record.retries`, and fills `attempts_this_episode` (max prior attempt count + the prior strategies) so the resolver's exhaustion fall-through can fire. This is additive: `auto_resume_flow`, `resolve`, and `fetch_failures`'s query contract are untouched, and the existing flat `annotate_clusters_with_retry_advice` path keeps working. The composite stays an `ops/` composite (no new agent-facing CLI verb / primitive).

## 0.10.25 — 2026-06-08

### Fixed — strict json_schema decode now guarantees an object root (#304, Phase 2)

`_to_strict_schema` strictified every object node but left the *root* as Pydantic emitted it. A normal `BaseModel` roots as a flat object (fine), but a `RootModel` wrapping a model emits a bare `{"$ref": …}` root, and a `RootModel` over a list/scalar emits a non-object root — both of which strict `response_format: {type: json_schema}` rejects, so `structured()` against such a model would have POSTed a payload the endpoint 400s on. The transform now inlines a root `$ref` (and a single-element `allOf: [{$ref}]` wrapper) so a `RootModel[Object]` resolves to a proper object root with `additionalProperties:false` + all-`required`, and raises a clear `SpecInvalid` (naming the `HPC_AGENT_MODEL_RESPONSE_FORMAT=json_object` fallback) on a genuinely non-object root instead of sending a doomed request. Normal `BaseModel` schemas (the common case, incl. `WorkerReport`) are unaffected — root promotion is a no-op for an already-object root.

## 0.10.24 — 2026-06-08

### Changed — lift the status/lifecycle vocabularies out of the worker-execution package (internal)

`_kernel/lifecycle/lifecycle.py` — the cross-cutting `JournalStatus` / `LifecycleState` / `TaskStatus` / `FailureCategory` StrEnum vocabularies (the canonical value sets the wire, state, and ops layers agree on) — lived inside `_kernel/lifecycle/`, the *worker / model-call execution* package, with a `lifecycle/lifecycle.py` package-module name collision. They are unrelated to execution, so they move to `_kernel/contract/vocabulary.py`, grouping with the other typed-contract modules (`task_id`, `layout`, `schema`). `_kernel/lifecycle/` now means exactly worker / model-call execution (`run`, `invoke`, `structured`, `chat_models`, `playbook`). Internal `_kernel` path change only — no public or wire surface; the ~13 importers and the two test modules (now `tests/_kernel/contract/test_vocabulary*.py`) were updated.

## 0.10.23 — 2026-06-08

### Added — OpenAI-compatible `ChatModel` with strict `json_schema` decode (#304, Phase 2)

The first real adapter behind the Phase-1 `ChatModel` boundary: a single OpenAI-compatible `/chat/completions` client (`hpc_agent._kernel.lifecycle.chat_models.openai_compat.OpenAICompatModel`) that targets DeepSeek-hosted, OpenAI, or a self-hosted vLLM by swapping `HPC_AGENT_MODEL_BASE_URL` / `HPC_AGENT_MODEL_API_KEY` / `HPC_AGENT_MODEL_NAME` — one wire shape, the endpoint is the only variable. The phase's core is the **accelerator**: in the default `json_schema` mode the offered schema is sent as a `response_format={"type":"json_schema", …, "strict":true}` **decode-time constraint**, so a conforming server *cannot* emit non-conforming tokens — the raw model-call sibling of the spawned worker's `--json-schema` gate (#269), not a prompt hint. Strictness is achieved by an `_to_strict_schema` boundary transform applied only to the decode-constraint **copy** (recursively `additionalProperties:false` + all-properties-`required` on every object node, refs preserved) — the **source Pydantic models are untouched**, and the parse-validate-repair floor in `structured()` still validates against the original lenient model, so a strict-decoded output is always a superset-constrained case of the lenient validate (the two never conflict). The floor remains the universal **backstop** in every mode (it still catches the semantic / `post_validate` errors a shape constraint can't express, and carries providers/schemas where strict isn't honoured). `HPC_AGENT_MODEL_RESPONSE_FORMAT` is the per-endpoint **downgrade knob**: `json_schema` (default, strict — OpenAI / vLLM), `json_object` (JSON-valid only + schema hint + floor, for json_object-only providers like DeepSeek-hosted), `none` (floor only). Transport / malformed-envelope failures raise a dedicated retry-safe `ModelEndpointError` (`network` category, new `model_endpoint_error` code) that propagates OUT of `structured()` uncaught — a transport error is not a validation failure to repair, and it is distinct from `StructuredOutputError` (a valid completion that failed the floor). **Zero new runtime dependencies** (stdlib `urllib.request` + `json`), the adapter registers lazily inside `get_model` and is **default-off** (it never auto-selects), and it ships **unvalidated against a live endpoint** behind a manual live-validation checklist in `docs/reference/env-vars.md` (the #269 discipline).

## 0.10.22 — 2026-06-08

### Added — provider-agnostic `structured()` boundary + repair-loop floor (#304, Phase 1)

The first raw model-call seam. Until now every model-facing path spawned an *agent* (a `claude -p --bare` tool loop via `WorkerInvoker`); `structured(model, schema, messages)` is its single-completion → validated-object sibling — the durable counterpart to `run_workflow`'s render→invoke→parse funnel. The floor is parse → extract JSON → validate against a Pydantic-generated schema → **on failure, feed the validation error back for a bounded number of repair turns, then hard-fail** (`StructuredOutputError`); the repair loop is net-new (every prior model-output check was single-pass validate-and-reject). A `ChatModel` Protocol plus `register_model` / `get_model` / `HPC_AGENT_MODEL` registry mirror the invoker layer, so a provider that supports native strict json-schema decoding can bind it as a swappable accelerator behind the boundary while the floor needs zero provider features. The JSON extractor is lifted to `_kernel/contract/json_extract.py` so the worker floor and `structured()` share one implementation. No real provider adapter and no new runtime dependency in this phase; semantic/referential checks stay in code via a `post_validate` hook.

### Changed — rename the cluster-side execution tier `models/` → `execution/` (#293 forward-design)

`src/hpc_agent/models/` was a documented architectural tier ("domain logic that runs on the cluster, not the laptop" — the array-dispatch + combine + reduce machinery and the cluster-deployed job templates, governed by `docs/reference/boundary-contract.md`), but the name read as "ML models" and collided with the `ChatModel` concept introduced by #304. The single `mapreduce` tenant is unchanged; the tier is renamed `execution/` (`hpc_agent.execution.mapreduce.*`) — a namespace with room for the cluster-side execution models still to come (MPI / multi-rank #293, many-tiny-task meta-scheduling #227). The previously-empty `execution/__init__.py` now documents the tier and its boundary contract so the intent is explicit rather than looking like an inert wrapper. Internal path change only (no public wire surface); external code importing `hpc_agent.models.mapreduce.*` directly must move to `hpc_agent.execution.*`.

## 0.10.21 — 2026-06-08

### Changed — refuse the hand-authored `skip_rsync_deploy` agent form (#283, instance #2)

`skip_rsync_deploy` was an agent-settable wire field on `SubmitFlowSpec`: an agent that set it `true` on a raw `submit-flow` spec ASSERTED "Phase 1 already deployed the same tree, nothing changed since," and a stale assertion silently launched the main array against whatever code the previous deploy shipped if the local tree had drifted (#185). That is the same class as `skip_preflight` (#275) and `--inline` (#155) — an agent-facing lever over a cluster-side safety step. The field is now off the wire (`extra="forbid"` refuses a hand-authored `skip_rsync_deploy`); the skip is operator/internal-only via `HPC_AGENT_SKIP_RSYNC_DEPLOY=1` or a Python-only `_skip_rsync_deploy` kwarg threaded by the trusted in-process caller (`submit_and_verify`'s post-canary main launch, where "Phase 1 just deployed the same tree" is a structural fact the code knows, not an assertion). `prepare-phase2-spec` drops the former `skip_rsync_deploy` flip (its wire output can no longer carry the field; the production agent flow uses in-process `submit-pipeline`/`submit-and-verify`, which skip the redundant deploy correctly). The `worker_prompts/submit.md` Phase-2 teaching is updated to state the skip is operator/internal-only and not a spec field.

### Added — lint guards the preflight/deploy-bypass teaching class (#283)

A new `tests/worker_prompts/test_prose_lints.py` lint refuses any worker-prompt example that sets a safety-bypass field (`skip_preflight`, `skip_rsync_deploy`) true. It matches the assignment form (`field: true` / `field=true`) while leaving the negative demotion prose ("there is no longer a `skip_preflight` field") legal — so a new bypass field added to a worker-prompt example fails CI the same way #275/#283 refused the field at the wire.

## 0.10.20 — 2026-06-08

### Added — `find` discovery tier so agents search for a name instead of dumping the catalog (#306)

Tool discovery was all-or-nothing: `capabilities` already inlines the entire ~90-row operations catalog in its default envelope, and `capabilities --full` additionally dumps every agent-facing primitive's doc body + input/output schemas — so the only way to learn a name was to materialize the whole surface into context. At the other end `describe <name>` fetches exactly one contract. The missing middle is a search step. New `hpc-agent find "<intent>"` returns a thin candidate list of `{name, verb, cli, summary}` — no schemas, no doc bodies — giving a headless loop the three-step economy **find (explore) → describe (read one) → invoke** without re-dumping the catalog each iteration. Matching is stdlib-only: a fuzzy `difflib.get_close_matches` pass over primitive names (`submit-batch` → `submit-flow-batch`) unioned with a token/substring scan over `name + summary` (the intent phrase `submit a batch`), returned in stable catalog order and capped at `--limit` (default 15); a blank query matches nothing rather than dumping everything. To feed the scan, the operations catalog projection (`operations_catalog()`) now carries a `summary` field — the primitive's `CliShape` help string — which also surfaces in `describe` output and the baked `operations.json`. It is deliberately *not* added to the fixed-width catalog tables (`capabilities --full`, `docs/generated/operations.md`): summaries run up to ~530 chars, which would blow the column width and destroy scannability — `find` is the surface that puts a summary next to a name without wrecking a table. The `find → describe` flow is taught in the integration docs (`docs/integrations/CONTRACT.md`, `docs/reference/agent-surface.md`) and, for the interactive path, in the `hpc-submit` skill's framework-state guidance — framed as a *sequential* pair, distinct from the skill's parallel independent-lookup batching.

### Changed (wire) — `capabilities` default envelope's `operations` block slimmed to the thin bootstrap row (#306)

Now that `find` carries `{name, verb, cli, summary}` on demand, the per-op rows in the default `capabilities` envelope no longer inline the forensic pointers (`python`, `input_schema`, `output_schema`) or the one-line `summary` — those move "behind `describe`" (fetch one full contract) and `find` (thin search). Each row keeps the machine-readable flags an orchestrator actually gates on at bootstrap: `name`, `verb`, `idempotent`, `side_effects`, `cli`, `agent_facing`. This shrinks the block from ~48 KB to ~19 KB for the ~90-primitive core (a 61% cut, and smaller than before `summary` was added), retiring the default-path context leak the issue flagged. **Breaking on the wire**: a consumer reading `capabilities().operations[].{python,input_schema,output_schema}` must switch to `hpc-agent describe <name>`; nothing in-tree depended on those fields. `capabilities.output.json` and `docs/reference/cli-spec.md` updated to match.

## 0.10.19 — 2026-06-07

### Fixed — canary no longer false-fails on a divined `expect_output` (output-side of #287)

A passing canary was gated as `missing_output` because the verification was handed a hand-built per-task metrics path. `verify-canary`'s `expect_output` is checked verbatim against the cluster, but the worker-prompt's submit-pipeline example hard-coded `"expect_output": "results/seed_42/metrics.json"` — no `{run_id}`, a literal `seed_42`, and (for a canary) missing the `-canary` run-id suffix. The agent copied it, and the framework dutifully checked a path that never existed while the real output sat under `results/<run_id>-canary/seed_0/metrics.json`. Two changes: (1) the landmine field is dropped from `worker_prompts/submit.md` — the per-task completion count already verifies the canary's output via the correct run-id; (2) `verify-canary` now refuses at the boundary any `expect_output` that doesn't reference the canary run_id, so a divined path fails loudly with a remediation instead of silently minting a false negative. Empirical 2026-06-07 demo: a clean canary (`pi≈3.141132`, exit 0) blocked the 100-task main array.

### Fixed — unknown CLI verb gives a compact "did you mean" instead of dumping every verb

Stock argparse answered an unknown subcommand by printing the usage line (all ~70 verbs) plus `invalid choice: 'X' (choose from <all of them again>)` — the entire CLI surface twice, a content-free tax on a spawned worker's context that buried the one useful hint. `_HpcArgumentParser.error()` intercepts that one error class and emits a single line (`unknown command 'check-preflight'. Did you mean: preflight…` via `difflib.get_close_matches`). Especially helps the seven primitives whose registry/doc name differs from the CLI verb (`check-preflight`→`preflight`, `discover-executors`→`discover`, …): the agent reads the long name in prose, and the close-match points it at the real verb.

## 0.10.18 — 2026-06-06

### Fixed — refuse a broken per-task EXECUTOR at the sidecar / build boundary

The cluster dispatcher `str.format`s only `result_dir_template`; it runs the per-task `executor` through the shell verbatim and exports each `tasks.resolve(i)` kwarg as `env[key.upper()]` (bare `$SEED` + `$HPC_KW_SEED`). Two silent-failure shapes slipped past every existing guard because the field the dispatcher actually reads — `sidecar.executor` — was only checked for the #162 dispatcher self-recursion:

- **`str.format` `{placeholder}` leakage** — e.g. `--output-file results/{run_id}/seed_{seed}/metrics.json`. The `{run_id}`/`{seed}` tokens never expand in the executor (only in `result_dir_template`) and reach the program literally, writing under a directory named `{run_id}`. New `_check_executor_format_placeholders` refuses them and points at `$RESULT_DIR` for per-task output (`${VAR}` / `{}` / `{a,b}` / `{1..9}` brace forms are excluded).
- **Wrong-case swept-kwarg `$ref`** — e.g. `--seed $seed` for the `seed` kwarg. The dispatcher exports `$SEED`, so `$seed` expands to empty → argparse error. New `_check_executor_kwarg_casing` refuses it, naming the correct `$SEED` / `$HPC_KW_SEED`; the existing #292 `$VAR` cross-check now also runs it.

Both checks apply to the real per-task command at BOTH boundaries: `build-submit-spec` (the `extra_env["EXECUTOR"]` path) and `write-run-sidecar` (the `sidecar.executor` the dispatcher reads) via the new public `check_per_task_executor` helper. The sidecar boundary deliberately omits the job_env-aware unset-var check (job_env isn't known at write time, and the per-task command legitimately inherits `MODULES`/`CONDA_*`/`REPO_DIR` at runtime). Empirical 2026-06-06 demo: a canary's correct `--seed $SEED` regressed on resubmit to `--seed $seed` + the `{run_id}/seed_{seed}` `--output-file`, and both shipped to qsub unflagged.

## 0.10.17 — 2026-06-06

Bundles five externally-landed PRs (#297, #300, #302, #303) alongside the #296 `repo_hash` fix — all in the 2026-06-06 window.

### Fixed — `build_submit_spec` guards resolve against `experiment_dir` + `$VAR` cross-check (#292, PR #297)

Bug A: the 0.10.11 bare-script-vs-`register_run` guard probed `Path(script).is_file()` CWD-relative, so it silently no-op'd whenever `build_submit_spec` ran in a worker whose CWD wasn't the experiment dir — breaking the contract the 0.10.11 CHANGELOG asserts. `experiment_dir` is now threaded through `build_submit_spec` (via the `--experiment-dir` injector); a relative `script` resolves against it, with the CWD-relative fallback preserved for invocations from the experiment dir. Bug B: a build-time cross-check refuses an EXECUTOR referencing a `$VAR` the cluster dispatcher never exports (the empirical `--samples $SAMPLES` where `samples` isn't a swept axis → expands empty → argparse dies). Covered references: swept-axis kwargs (bare + `HPC_KW_` forms from `.hpc/tasks.py`), framework identity/result vars, inherited cluster shell vars, and `:-`-defaulted refs. No-ops when the kwarg set can't be positively established, so it never false-refuses.

### Changed — SSH batching trio (#295, PR #297)

Three Windows-named-pipe-independent latency wins. (1) `cluster_ssh_echo`'s hardcoded 5s timeout now reads `HPC_CLUSTER_SSH_TIMEOUT` (default 15s) — stops false `cluster_ssh_timeout` failures under cluster load. (2) `_cluster_combined_probe` collapses the echo round-trip + the uv probe into a single ssh with sentinel tokens (`__HPC_ECHO_OK__` / `__HPC_UV_OK__` / `__HPC_UV_MISSING__`), routed through the spec's `ssh_target`; the standalone probes remain only as the no-`--cluster`/unreachable fallback. (3) `_sge_inspect` merges `qhost` + `qstat` into one ssh. Saves 1–2 cold handshakes per submit/inspect cycle regardless of whether ControlMaster multiplexing works.

### Added — auto-resume auto-fire wired into submit/journal/monitor (#299, PR #300)

Layer-2 of the #294 checkpoint-recovery work: resume from a preemption/walltime kill is now automatic (opt-in, default OFF) rather than a surfaced recommendation. `RunRecord` persists the resubmit keystone (script/backend/job_env) plus the policy (`auto_resume_on_kill`), cap (`max_auto_resumes`), and counter (`auto_resume_count`) — all with harmless defaults so pre-#299 records load unchanged (`from_dict` filters to known fields). `resubmit_flow` gains `from_checkpoint` (single-sources the `HPC_RESUME_FROM_CHECKPOINT=1` stamp) and `bypass_preempt_throttle` (the auto-resume path opts out of the manual back-off guard; the cap is its backstop). `ops/auto_resume_flow.maybe_auto_resume` is the composite — reads sidecar + record, calls `decide_auto_resume`, and on a resume verdict re-submits exactly the preempted ids from checkpoint and bumps the counter (request_id keyed on the preempt-mark generation so an immediate monitor re-entry dedups). `monitor_flow`'s FAILED seam consults the composite when the run opted in. `SubmitFlowSpec` gains `auto_resume_on_kill` + `max_auto_resumes` (schemas regenerated).

### Added — canary checkpoint write+kill round-trip verification (#294 PR4, PR #302)

Completes the #294 checkpoint-recovery workstream's canary-integration leg. Cluster-side `experiment_kit/checkpoint.py` writes a checkpoint when the framework requests it; `ops/verify_canary.py` gains a `--verify-checkpoint` mode that drives a write → kill → resume round-trip, so a malformed checkpoint format is caught by the canary before the main array burns hours. `submit_and_verify` / `verify_canary` wire schemas regenerated alongside `operations.json` + primitive docs.

### Fixed — unify task-id base at the scheduler membrane (#301 Phase 2, PR #303)

Normalizes the task-id base at the single scheduler-boundary seam (`infra/backends/query.py` + `_engine.py` + `_kernel/contract/task_id.py`) rather than per-consumer, so the `failures` / `status` / `aggregate_flow` queries, the reduce layer (`metrics` / `rollup` / `status` / `tui`), `cluster_logs`, and `auto_resume_flow` all read a consistent task-id space. Phase 2 of the membrane unification.

### Fixed — `repo_hash` is path-form-invariant on Windows (#296)

`state.run_record.repo_hash` was computing `sha256(str(Path(experiment_dir).resolve()))` and treating Bash MINGW (`/c/...`), WSL (`/mnt/c/...`), and the native backslash form as different namespaces. Empirical 2026-06-06: a submit issued via the Bash tool wrote the journal under one namespace; a reconcile call from the native session read from another; the run looked corrupt locally even though the cluster sidecar was fine. Four distinct hashes for the same logical dir: `C:\Users\james\demo-hpc` → `bc64a2106672`, `/c/Users/james/demo-hpc` → `74833c5d08f3`, `/mnt/c/Users/james/demo-hpc` → `806262f70e37`.

New `_canonicalize_for_hash` translates Bash MINGW and WSL prefixes into the Windows drive-letter form before `resolve()`, folds `/` → `\\`, and uppercases the drive letter. All three forms now produce the canonical `C:\Users\james\demo-hpc` string → identical hash. The translator only fires when `sys.platform == "win32"`; Linux / macOS behavior is unchanged. The canonical Windows form's hash is unchanged, so every existing `~/.claude/hpc/<hash>/` namespace dir continues to work — backward-compat preserved end-to-end (regression test `test_demo_hpc_hash_is_stable` pins the empirical `bc64a2106672` value).

11 new tests pin every branch: each of the three Windows forms agrees with the others, drive-letter case canonicalizes, distinct drives still hash distinctly, distinct paths under the same drive still hash distinctly, the cluster `/u/scratch/...` form remains distinct (it's a remote location, must NOT collide with any local namespace), POSIX is unchanged, repeats are deterministic, and the canonical demo-hpc value matches the pre-#296 hash.

## 0.10.16 — 2026-06-06

Prose trim pass + two shared-fixture follow-ups from 0.10.15.

### Changed — trim verbose prose from error messages, SKILL.md, and worker prompts

71 prose sites inventoried across error messages (raise sites with multi-sentence strings), SKILL.md / slash command markdown, and worker prompts; tight criterion applied (keep "what failed + how to fix", drop "why this happens" / empirical-incident parentheticals / cross-reference markers / multi-sentence repetition). 17 files trimmed. Aggregate word count: 18,549 → 15,151 (~18% reduction across the touched surfaces). Docstrings, CHANGELOG entries, and inline maintainer-facing comments unchanged. Worker prompt fixture snapshots regenerated alongside the worker_prompts/*.md edits.

### Fixed — shared test fixtures updated for the 0.10.15 `result_dir_template` per-task isolation guard

`tests/ops/test_resolve_submit_inputs.py::_sidecar_input` and `tests/ops/validate/test_validate_stochastic_marker.py` both used `result_dir_template = "results/{run_id}"` with `task_count = 4` — the exact shape the 0.10.15 `WriteRunSidecarInput` validator refuses. Updated both to `"results/{run_id}/task_{task_id}"`. The 0.10.15 release missed these shared fixtures; this release closes the gap so the validator's invariant holds across the test suite.

### Fixed — `scripts/llm_touchpoints_baseline.json` regenerated for the trimmed prose

`tests/contracts/test_llm_touchpoints.py::test_check_mode_reports_clean` pins the deterministic-touchpoint count against a baseline. The prose trim pass shrank some worker_prompts/*.md content, lowering the count from the previous baseline. Regenerated to the new count (82 total touchpoints).

## 0.10.15 — 2026-06-06

One fix off the 2026-06-06 demo failure trail.

### Fixed — refuse `result_dir_template` that renders to the same path for every task in a multi-task run

Empirical: orchestrator hand-built a sidecar with `result_dir_template = "results/{run_id}"` and `task_count = 100`. The dispatcher rendered the template per task — but `{run_id}` is constant per run, so every task wrote `metrics.json` into the same directory. The last writer won; the other 99 results were silently clobbered. The framework had every input to detect this at sidecar-write time.

New `@model_validator(mode="after")` on both `WriteRunSidecarInput` and `BuildSubmitSpecInput`: when `task_count > 1` (or `total_tasks > 1`) AND no per-task placeholder is present (anything other than `{run_id}`), refuse with a clear error naming the offending template, the task count, and both fix paths — `{task_id}` for guaranteed uniqueness, or any swept kwarg from `tasks.py` FLAGS like `{seed}`. The validator accepts templates containing `{task_id}` or any non-constant placeholder; `{run_id}`-only or literal-string templates are refused. Single-task runs (`task_count = 1`) bypass the check.

The guard fires at both boundaries: build-submit-spec time (one step earlier) AND sidecar-write time (defensive, in case the build path is bypassed by a primitive path that constructs the sidecar directly). Twelve new tests pin every branch — constant-only refused, literal refused, single-task allowed, `{task_id}` accepted, swept-kwarg accepted, error-message content, `None` template passes through to the build-time default.

Same class as #275 / #281 / #287 / #292 — the "agent hand-built a spec, framework accepted it" pattern, closed at one more boundary.

## 0.10.14 — 2026-06-06

Three independent fixes off the 2026-06-06 demo failure trail, plus the previously-committed install/load fan-out (#291) and scaffold-spec interview coverage from the same day.

### Added — `failure_features` on every canary-failed envelope, classified against an extended signature catalog

`ops/verify_canary.py` now attaches `failure_features: {cluster_log_tail, log_path, classified_error}` on every `ok=False` return path (`dispatcher_failed`, `import_error`, `oom_killed`, `traceback`, `reporter_unreachable`, `timeout`, `completed_unknown`, `missing_output`, `abandoned`). Moves the "look at the cluster log to know what actually failed" step out of SKILL.md prose (where agents forget it — empirical 2026-06-06 demo: agent recorded `dispatcher_failed` and stopped without inspecting any log) into framework code that runs on every failed canary. Five new entries in `ops/recover/failure_signatures.CATALOG` covering the empirical demo failures: `uv_not_on_path`, `conda_command_not_found`, `output_file_required`, `module_not_found_hpc_agent`, `undefined_var_expansion` — each with a `suggested_fix.action` + `suggested_fix.hint` so the envelope tells the agent what to do without a second tool call. New `CanaryFailureFeatures` Pydantic model on `VerifyCanaryResult`; schemas regenerated.

### Fixed — worker_prompts/submit.md no longer hardcodes `"runtime": "uv"` in the example spec

The submit preflight gate (`runtime_uv_preflight` via `_run_shared_prelude`) is structurally closed — every submit funnels through it, and `SubmitFlowSpec` has no `skip_preflight` field at all (0.10.9 #275 demote). The only remaining surface that kept producing the empirical "agent built a spec with `runtime: \"uv\"` against a no-uv cluster" failure was the worker prompt example. Dropped `"runtime": "uv"` from both example JSON snippets in `worker_prompts/submit.md`; added an explicit "Do NOT add `runtime: 'uv'` by default" rule with the gating criteria (uv lock file + operator-confirmed cluster env).

### Added — `install-commands` auto-merges `Skill(<name>)` allow rules for bundled skills (companion to #190)

Claude Code's auto-mode classifier silently denies `Skill(<name>)` calls from `/submit-hpc` / `/aggregate-hpc` / `/monitor-hpc` / `/campaign-hpc` despite `skipAutoPermissionPrompt: true` (the flag suppresses the explicit prompt but the classifier still gates risky tools). Empirical 2026-06-06 demo: `Skill(hpc-submit)` blocked back-to-back, orchestrator fell back to inline-mode execution. The framework already mitigates the analogous problem for the bare worker's `Bash(hpc-agent:*)` calls via `ops/memory/interview.py`'s `_maybe_write_claude_permissions` (project-scoped, #190); this release extends the same pattern to the orchestrator's Skill calls at user-global scope (orchestrator can invoke `/submit-hpc` from any CWD). New `_merge_skill_permissions` sibling of `_merge_skill_return_hook` in `agent_assets.py`: same additive + idempotent + skip-unparseable + dry-run contract, targets `permissions.allow` with one `Skill(<name>)` entry per bundled skill (tracks the actual installed skills set, so plugin-contributed skills get grants too). Matcher format chosen: `Skill(<name>)` per-skill mirroring the `Bash(<prefix>:*)` parameterised precedent — narrowest reasonable grant.

### Changed — `aggregate-preflight` + `status-preflight` fan `install-commands ∥ load-context` (#291)

(Already committed as `b63a67b3` on 2026-06-06; folded into 0.10.14.) The 2026-06-05 #289 audit classified the install→load ordering as a "strict data-dependent chain"; a focused source-walk verified it's INERT — install-commands writes only `~/.claude/{commands,skills,agents}/`, load-context reads only `$EXPERIMENT/.hpc/{runs,journal,campaigns}/`. Write- and read-disjoint. Now fanned on a `ThreadPoolExecutor` mirroring submit-preflight's pattern; threading.Barrier concurrency tests pin the fan-out shape; the misleading "Order pin" comments dropped from the test files.

### Added — `scaffold-spec --verb interview` (#287 follow-up)

(Already committed as `b63a67b3`.) The four verbs #287 named did not include `interview` — the entry verb for hpc-wrap-entry-point that the orchestrator hand-builds every onboarding. The 2026-06-05 demo burned 7m+ on schema-divination after `emit-skill-return` because the agent had no scaffold for `InterviewSpec`. New `_scaffold_interview` composes `detect-entry-point` + `clusters.yaml` + `compute-run-id` + filesystem glob of `configs/*.yaml`; emits a populated `register_run`-shape skeleton by default, switches to `shell_command` shape when detect-entry-point's candidate is non-Python. `data_axis_hint` is unconditionally omitted on `register_run` (#260 schema rejects it on that shape). Skeleton validates against `InterviewSpec` before return.

## 0.10.13 — 2026-06-05

Folds PR #290: parallel canvassing during worker startup (#286), `scaffold-spec` query verb (#287), heavy-import contract test (#288), and parallel-by-default fans of `check-preflight`'s two cluster probes + SGE `inspect-cluster`'s qhost/qstat (#289). No worker-prompt or skill-contract changes — `/submit-hpc` / `/aggregate-hpc` / `/monitor-hpc` SKILL.md gain a parallel-canvass prelude only.

### Added — main-agent / worker parallelism: parallel canvassing during worker startup (#286)

`/submit-hpc` no longer serialises human-thinking time behind worker-startup time. The slash now dispatches the `hpc-submit` skill in the **background** (Claude Code's `Agent` tool `run_in_background: true`, autonomous mode) and, in parallel, canvasses the predictable runtime-behaviour questions (`overwrite_prior_run`, `on_task_generator_mismatch`, the `data_axis` confirmation when the classifier is `unclassifiable`, `k_in_flight`) and runs local config validation (`clusters.yaml` coherence, `.hpc/axes.yaml` freshness, working-tree dirtiness) + surfaces recent history — none of which need worker output. At the **join** it reconciles the user's answers against the speculative dispatch: a no-op merge in the common case (the answers are runtime knobs the built spec doesn't depend on), or a cheap cancel + re-dispatch on the rare *spec conflict* (a cancelled dispatch has done preflight + maybe rsync, not the main-array `qsub`).

This is the layer **above** the in-worker pipeline parallelism of #277–#280 (which overlaps stages *inside* the worker). It is a **slash-side change only** — the `hpc-submit` skill and the worker contract are unchanged, so the `scripts/count_llm_touchpoints.py` baseline (which measures `worker_prompts/`, not the slashes) is unmoved. Piloted on `/submit-hpc` and ported to `/aggregate-hpc` (local results-tree summary ∥ load-context/reconcile/pull) and `/monitor-hpc` (journal snapshot ∥ poll loop). Design notes: [`docs/design/submit-parallel-canvass.md`](docs/design/submit-parallel-canvass.md).

### Added — `scaffold-spec`: break the schema-divination loop with a context-populated skeleton (#287)

New read-only `scaffold-spec` query verb. When an agent must invoke a verb that takes a `--spec` JSON, it had no way to get a valid skeleton — each missing field / wrong type / stray `extra=forbid` key surfaced ONE at a time as a `spec_invalid` envelope, so the agent walked the schema by failed-validation feedback (the 2026-06-05 demo burned 11 rounds on `resolve-submit-inputs`, 7 on `build-submit-spec`, 3 on `validate-campaign`). `scaffold-spec` composes the read-only context sources — `load-context` + `clusters.yaml` + `compute-run-id` + `discover-executors` — into a populated skeleton for the named verb, **validated against that verb's own input model before it is returned** (it refuses to emit a spec the verb would reject). The handful of fields context can't supply come back as schema-valid placeholders listed in `unresolved_fields`, with per-field `sources` provenance. The loop collapses to one scaffold + one edit + one invoke.

- Covers all four verbs #287 names: `build-submit-spec`, `validate-campaign`, `resolve-submit-inputs` (which reuses the `submit` + `sidecar` block builders), and `campaign-run` — the worst offender to hand-build. campaign-run's three nested workflow specs (submit-pipeline → submit-and-verify → submit-flow, status-pipeline → monitor-flow, aggregate-flow) are emitted as one structurally-valid skeleton with the run-identity + cluster fields threaded into all three levels; the non-derivable leaves (`job_env`'s EXECUTOR, `script`, …) come back in `unresolved_fields`.
- Emits only the **coherent** conda-activation pair: a `conda_env` without a `conda_source` (#281) crashes the cluster preamble, so the half-state is never produced.
- Read-only (`verb: query`, no side effects): the skeleton rides the envelope `data`, never written to disk. New `scaffold_spec.output.json`; docs at `docs/primitives/scaffold-spec.md`.

### Added — regression guard: heavy deps stay out of module-level imports (#288)

Audit of the per-verb Python startup tax. Every `hpc-agent <verb>` builds the CLI parser, which `pkgutil.walk_packages`-imports every module under the primitive-discovery roots — so a top-level `import pandas` / `numpy` in any CLI-reachable module would tax *every* verb's startup, not just the aggregate-side one that needs it. The audit found the hot path **already clean**: pandas / numpy / scipy / sklearn / matplotlib are imported nowhere in `src/`, and the lone `pyarrow.parquet` (`ops/validate/input_dataset.py`) is already function-local. The residual startup cost is pydantic + `importlib.metadata` (version + plugin entry-point scans) + transitive `asyncio` — the hot path the issue itself flags as not movable without a model-loading refactor.

Locked in with `tests/contract/test_no_heavy_toplevel_imports.py`: it AST-scans every framework module (excluding the user-facing `execution/mapreduce/templates/` scaffolds) and fails if any imports a heavy data/ML dep (`pandas`, `numpy`, `scipy`, `sklearn`, `pyarrow`, `torch`, `matplotlib`, …) at module level — so a future top-level import re-growing the tax trips in CI, with the function-local fix named in the failure message.

### Changed — parallel-by-default audit: fan check-preflight + SGE inspect-cluster ssh probes (#289)

Audit of independent stages running sequentially across the composites. Two concrete wins. **(1) check-preflight:** `check-preflight --cluster X --spec <uv-spec>` (the documented submit Step 7, `submit.md:227`) fired TWO independent cluster ssh round-trips back-to-back — the #275 `runtime_uv` probe and the `cluster_ssh_echo` functional probe. They now run **concurrently** on a `ThreadPoolExecutor` (the established `reconcile` pattern — concurrent ssh to one host via the multiplexed ControlMaster), so that pair's wall-clock is one RTT, not two, on every uv-cluster submit. The probes are extracted into `_runtime_uv_check` / `_cluster_ssh_echo_check`; the standalone paths (no `--cluster`, or tcp:22 unreachable) are preserved, and a `threading.Barrier` test pins the concurrency. **(2) inspect-cluster (SGE):** `_sge_inspect`'s `qhost` (node state) and `qstat` (co-tenants) are two independent ssh round-trips — neither reads the other's output — now fanned the same way for a second ~1-RTT win.

The rest of the audit found the high-value fans already in place and the remaining candidates genuinely sequential:

- **`reconcile`** already fans its three ssh calls (status + combined-waves + alive-check) on a thread pool; its `load_run` is a *prerequisite* (it provides `ssh_target`/`job_ids`), not an overlappable stage — the issue's "scheduler probe ∥ local read" win isn't available.
- **`submit-preflight`** already fans `check-preflight ∥ resolve-resources` (#277); **`monitor-flow`** already batches the per-tick query into one ssh (#251).
- **`aggregate-flow`** (combine → pull → reduce) and **`status`/`aggregate-preflight`** (install → load → reconcile, where reconcile is built *from* load-context's envelope) are strict data-dependent chains — not parallelisable without changing semantics.
- **`inspect-cluster` SLURM** is *not* fanned: `sacct -N <nodes>` is scoped to the node list `scontrol show node` returns, a genuine data dependency (only SGE's two probes are independent). **`discover`** walks `.hpc/*.py` with `ast.parse` — GIL-bound CPU, so a thread pool buys nothing; left sequential.

## 0.10.12 — 2026-06-05

Reconcile two-tier fix off the same demo session. Tier 1 = root cause (the bare-python reporter shape resurrected as a regression in the reconcile path), Tier 2 = defense in depth (route reporter-failure through `unable_to_verify`, the same lifecycle state #258 already added for alive-check failures).

### Fixed — reconcile threads `remote_activation` into the reporter probe (Tier 1)

`ops/monitor/reconcile.py::_reconcile_one` called `_ssh_status_report` without the `remote_activation` keyword, defaulting it to the empty string. The cluster-side reporter then ran under the login node's bare `python` (Hoffman2: `/usr/bin/python` 3.6.8, no `hpc_agent`), crashing with `No module named hpc_agent.execution.mapreduce.reduce`. The monitor-side `record_status` path (`ops/monitor/status.py:109-125`) already threaded `remote_activation_for_sidecar(_sidecar)` correctly — reconcile just didn't mirror it. Symmetric fix: read the sidecar, compute the activation prefix, pass it to the reporter call. Same bug shape as the 2026-06-03-handoff-withdrawn 0.7.5 "Bug B" — that withdrawal was correct for the monitor/status path; reconcile reborn the same hole independently.

### Fixed — reporter failure routes through `unable_to_verify` (Tier 2)

Pre-0.10.12 `_reconcile_one`'s verdict logic gated `unable_to_verify` solely on `alive_check_failed`. If the alive-check succeeded (scheduler answered "no jobs alive") AND the reporter raised, the verdict still routed through `abandoned` because the reporter exception was caught into a `summary = {"error": str(exc)}` dict but no flag influenced the verdict. The empirical 2026-06-05 demo: reporter died (Tier 1 cause), alive-check confirmed job gone, run marked `abandoned` — but the framework had no independent confirmation results didn't exist. A completed-but-reporter-broken run would have looked identical. Added a `reporter_failed` flag mirroring `alive_check_failed`; either failing routes through `unable_to_verify`. `abandoned` now requires BOTH probes clean + no alive jobs.

Two new tests pin both tiers: `test_reporter_failure_routes_through_unable_to_verify` (Tier 2 — reporter crashes with the empirical "No module named hpc_agent.execution.mapreduce.reduce" string, assert envelope = `unable_to_verify` not `abandoned`) and `test_reconcile_threads_remote_activation_to_reporter` (Tier 1 — assert the reporter call receives a non-empty `remote_activation` keyword).

## 0.10.11 — 2026-06-05

One upstream fix off the same demo session: tighten the 0.10.3 bare-script-against-register_run guard so the with-trailing-args shape is also refused.

### Fixed — bare-script EXECUTOR guard catches the with-args form too

`incorporation/build/submit_spec.py::_check_register_run_executor` was the 0.10.3 defensive guard (`bf0a4de7`) that refuses an `EXECUTOR` of the shape `python[3] <file>.py` when `<file>.py` is `@register_run`-decorated — that combination silently exits 2 in the cluster-side dispatcher because task kwargs flow through `HPC_KW_<NAME>` env vars, not argv. The guard's `if len(parts) != 2: return` gate was the gap: it only caught the no-args form (`python3 executors/foo.py`). The with-args shape (`python executors/monte_carlo_pi.py --samples 100000 --seed $SEED` — empirical from the 2026-06-05 demo where the orchestrator hand-built the spec, the divined cmd downgraded a `register_run` executor to the bare-script shape, the canary task crashed with `--output-file required`) slipped through.

Tightened to fire on any `python[3] <file>.py [...]` shape against a `register_run`-decorated file, regardless of trailing args. A flag *before* the script (`python -c "..."`, `python -m pkg`, `python -O file.py`) still short-circuits at the `script.endswith(".py")` check — those forms are presumed correct.

Closes the long tail of #275 / #281 / #287's "agent hand-builds the spec and the framework accepts it" pattern at this specific boundary.

### Changed — orchestrator SKILL.md prose: chain sequential `hpc-agent` calls with `&&`

Added a one-line execution-discipline bullet to all seven orchestrator-side SKILL.md (`hpc-submit`, `hpc-status`, `hpc-aggregate`, `hpc-campaign`, `hpc-classify-axis`, `hpc-wrap-entry-point`, `hpc-build-executor`): chain dependent `hpc-agent` calls with `&&` in one Bash block when the next call doesn't branch on prior structured output (e.g. `hpc-agent install-commands && hpc-agent load-context …`). Saves a round-trip + permission prompt per chained pair. Worker-side `hpc-worker.md` keeps its `PreToolUse` chaining block — the worker's one-verb-per-envelope discipline is a separate decision-boundary contract.

## 0.10.10 — 2026-06-05

Two upstream fixes off a live demo session: a broken `PostToolUse` skill-return hook on Windows, and an `interview`-CLI prose mismatch in the classify-axis skill that the agent kept hallucinating into an `--add-turn` flag.

### Fixed — skill-return autofetch hook command is bash-safe on Windows

`agent_assets._HOOK_COMMAND` baked the raw `sys.executable` into the `PostToolUse` hook command string. Claude Code runs hooks via `bash -c '<command>'`. Two failure modes on Windows:

- **Backslashes eaten.** `sys.executable` is a native backslash path (e.g. `C:\Users\james\.venv\Scripts\python.exe`). Bash interprets `\U`, `\j`, `\d` etc. as escape sequences and collapses them, producing `C:Usersjames.venvScriptspython.exe` → "command not found", which silently failed every `Skill` invocation's return-fetch.
- **Spaces split.** An interpreter under `C:/Program Files/...` (or any repo dir with a space) was split into two argv tokens.

Both fixed: the executable path is normalised to forward slashes and `shlex.quote`d. As a partner fix, `_merge_skill_return_hook` now **replaces** a stale entry whose command differs from the canonical install-time form (the pre-0.10.10 broken Windows entry) instead of treating it as "already-present" by module-path alone — so `hpc-agent install-commands` heals an existing broken settings.json on first re-run. New `"updated"` / `"dry-run-would-update"` actions complement `"added"` / `"already-present"`.

### Fixed — `hpc-classify-axis` Step 6 no longer implies a non-existent CLI surface

Step 6 of `hpc-classify-axis/SKILL.md` told the agent to "add a single turn (`role: agent`)" via the `interview` primitive. The `interview` CLI is one-shot (`--spec` / `--campaign-dir`) with no incremental add-turn surface, so the agent invented `--add-turn` + `--experiment-dir` and failed argparse every run (then printed "Step 6 skipped (transcript verb unavailable)" as its own fallback narration). Rewrote the step to be honest: the rationale is carried forward in the return envelope's `reasoning` field (Step 8); there is no separate transcript CLI call here. Slash-driven runs continue to write their own transcript turns.

## 0.10.9 — 2026-06-05

A control-flow-out-of-the-LLM batch: pipeline parallelism + guard tightening (PR #282 + the first half of PR #285) followed by four workflow composites that fold the deterministic worker-prompt spines into single typed calls (PR #285 stage 3).

### Added — submit-pipeline parallelism (#277, #278, #279, #280)

- **#277 `submit-preflight`**: the sequential `install-commands` → `load-context` prelude is preserved, but `check-preflight` and `resolve-resources` now fan out concurrently via `ThreadPoolExecutor`.
- **#280 `submit-flow`**: the `command -v uv` runtime probe runs in parallel with `rsync_push` + `deploy_runtime` via a shared `_run_shared_prelude`, instead of stacking ahead of the network-bound deploy.
- **#279 `prepare-phase2-spec`**: new primitive for the deterministic phase-2 spec flips (canary/canary_only off, skip_rsync_deploy on). Wires the worker canary gate to one `submit-and-verify` call — no agent in the submit→verify→submit loop.
- **#278 `prepare-followup-specs`**: new primitive that pre-stages `monitor_spec.json` + `aggregate_spec.json` at submit time so `hpc-status` / `hpc-aggregate` honor the cmd_sha-gated pre-staged spec and skip the interview.

### Changed — `skip_preflight` demoted to operator-only env var (#275, PR #282 + PR #285)

`skip_preflight` is no longer an agent-settable spec field. It was a bypass that silenced the runtime `command -v uv` guard, and the documented SKILL.md example had `skip_preflight: true` baked in, making the guard architecturally unreachable. Two-part fix:

- **Fix 1** — `check-preflight --spec <built submit-flow spec>` now runs the same `command -v uv` probe `submit-flow` runs (new `runtime_uv` check via `infra/runtime_preflight.py`), so a `runtime: "uv"` spec against a uv-less cluster is refused before any qsub.
- **Fix 2** — `skip_preflight` removed from `SubmitFlowSpec` / `SubmitFlowBatchSpec` / `build-submit-spec` (`extra="forbid"` rejects a stray field). The operator-opt-in path is the `HPC_AGENT_SKIP_PREFLIGHT=1` env var, matching the `HPC_AGENT_INVOKER=inline` precedent (#155). Internal post-canary main launches use a Python-only `_skip_preflight` kwarg.

### Fixed — abandoned journal records no longer block fresh submits (#276)

A monitor that gave up after a transient status-probe flake would mint a journal record with `status: "abandoned"` AND non-empty `job_ids`, and the dedup / canary-reuse paths keyed on `job_ids` presence rather than terminal status — so every subsequent submit hit "in-flight canary detected". Generalised the predicate to "terminal but not complete": added `state.journal.is_resubmittable_terminal` matching `TERMINAL_STATUSES - {complete} = {failed, abandoned}`. `complete` still dedups (idempotency); `in_flight` still blocks; `timeout` is deliberately excluded (it's a LifecycleState, never a JournalStatus); held runs (`pending_verdict`, #231/#234) still block (the escalation flow owns resubmission). The companion status-poll auto-recovery (bug 2 of #276 — `getsockname failed: Not a socket` on Windows OpenSSH) was already covered by 0.10.7's `run_with_named_pipe_retry` wrap; a regression test locks that.

### Fixed — incoherent `conda_env` without `conda_source` is unrepresentable (#281)

`build_submit_spec`'s env-activation guard accepted the partial `conda_env=set + conda_source=""` shape (the "all-empty refuses" `or` chain), and the preamble crashed at line 62 with `conda: command not found` because line 58's `if [ -n "$CONDA_SOURCE" ]` skipped the source step. Resolved env-activation now goes through one `Activation` value object that back-fills `conda_source` from `clusters.yaml` when only `conda_env` is supplied, making the incoherent state unrepresentable rather than merely rejected at the boundary.

### Added — workflow composites (control-flow-out-of-the-LLM, stage 3)

Four agent-facing composites fold a deterministic worker-prompt spine into ONE typed `stage_reached` call, so the agent stops hand-walking (and hand-branching) the verbs. Each is additive and sets `needs_decision=True` only on a genuine judgement gate (escalation-as-data, #231):

- **`submit-pipeline`** — `submit.md` Steps 7-10: `submit-and-verify` (the #160 canary gate) → `verify-submitted` (post-qsub health) → `prepare-followup-specs`. `stage_reached ∈ {deduped, canary_failed, verify_submitted_failed, complete}`.
- **`status-pipeline`** — `status.md` wait-until-terminal + lifecycle dispatch: `monitor-flow` → branch on `lifecycle_state`. `stage_reached ∈ {complete, timeout, failed, abandoned}`; only `failed`/`abandoned` escalate (`timeout` is budget-elapsed, re-invoke).
- **`campaign-run`** — one campaign iteration's `submit-pipeline → status-pipeline → aggregate-flow` spine (a composite-of-composites). A distinct `run_timeout` stage keeps a budget-elapsed run from being mislabeled a failure; it does NOT advance the campaign cursor (that stays a driver judgement).
- **`resolve-submit-inputs`** — `submit.md` Step 6 laptop-side input spine: ensure `.hpc/tasks.py` → `compute-run-id` → `find-prior-run` → `build-submit-spec` → `write-run-sidecar`. The `resolved` outcome is fully submit-ready (spec built AND per-run sidecar written, the #171 write-first precondition). The composite injects its `compute-run-id` `run_id`/`cmd_sha` into both the build-submit-spec and write-run-sidecar inputs (whose own values are placeholders), so the built spec + written sidecar always match the reported `run_id`.

### Changed — worker prompts wired to the composites (conservative)

`worker_prompts/submit.md` (Step 6 → `resolve-submit-inputs`, Steps 7-10 → `submit-pipeline`) and `status.md` (wait path → `status-pipeline`) now call the composites for their deterministic spines instead of hand-walking the verbs. The `DECISION_POINTS` judgement contract, the required-`why`-at-judgement-points rule, and the spawned-worker context isolation are unchanged — only the mechanical branches are folded. `scripts/count_llm_touchpoints.py`'s committed baseline drops accordingly (total 97 → 86); the regression gate ensures it can only go down.

## 0.10.8 — 2026-06-05

Two upstream fixes off the post-0.10.7 inert-guard punch list.

### Fixed — preflight ssh/scp probes follow production binary resolution (#271, PR #272)

`ops/preflight/check.py` previously probed bare `"ssh"` / `"scp"` via `shutil.which`, but production resolves the binary through `infra.ssh_options._ssh_binary()` / `_scp_binary()`, which on Windows prefer native `C:\Windows\System32\OpenSSH\{ssh,scp}.exe` (the binaries that reach the ssh-agent named pipe). Same different-binary-from-production class as the `cluster_ssh_echo` fix: on a Windows host with Git Bash's agent-blind ssh on PATH but native OpenSSH absent, preflight reported green while every production ssh/scp call would die. Now probes the resolved binary and names it in the detail string.

### Added — PBS node-level snapshot via `pbsnodes` parser (#215, PR #273)

PBS `inspect_cluster` previously returned a minimal `ClusterSnapshot` (`nodes=[]` + a `pbs_inspect_minimal` note), so the planner had no node-level backfill/throughput signal for PBS clusters. A family-keyed `pbsnodes` parser now populates the snapshot:

- **pbspro**: `pbsnodes -av` stanzas (`resources_available.*` / `resources_assigned.*`)
- **torque**: `pbsnodes -a` stanzas (`np` + packed status line: `ncpus` / `physmem` / `availmem` / `loadave`)

PBS node states map to `is_drained` conservatively (`down`/`offline`/`unknown`/`stale`/`provisioning`/`maintenance` ⇒ unusable; `free`/`job-busy`/`job-exclusive` stay up with busy-ness carried by `alloc_*` fields). `alloc_mem_pct` is clamped to `[0,1]` because PBS Pro reports `resources_available.mem` and `resources_assigned.mem` independently (no SLURM-style `AllocMem <= RealMemory` invariant), so an over-committed node would otherwise breach `inspect_cluster.output.json` and raise `OutputSchemaDrift`. CPU-only nodes no longer advertise a phantom `gpu:0` GRES — only emit `gres`/`gres_used` when count > 0, matching SLURM. The minimal snapshot is retained as the safe fallback for missing runner / non-zero `pbsnodes` exit / unparseable output.

## 0.10.7 — 2026-06-04

Post-0.10.6 carry-forward: SSH/qsub determinism fixes, the WS2–WS5 escalation-funnel batch, and one #240 punch-list cleanup.

### Added — SSH named-pipe runtime auto-fallback

`infra/ssh_options.py` adds `_NAMED_PIPE_RUNTIME_VERDICT` + `mark_named_pipe_broken` + `run_with_named_pipe_retry`; `ssh_run`, `rsync_push`, `_tar_ssh_push`, and `_remote_preclean` wrap their subprocess calls — on a detected `getsockname failed: Not a socket` stderr they mark the runtime verdict broken and retry once with multiplexing demoted to `ControlMaster=no`. Catches the syscall-layer failure the OpenSSH version probe cannot see.

### Added — `cluster_ssh_echo` functional preflight + cross-cluster qsub PATH

`ops/preflight/check.py` gains a `cluster_ssh_echo` round-trip that exercises the same `ssh_argv` / multiplex / crypto machinery production uses, replacing the inert `socket.create_connection((host, 22))` proxy guard. `infra/backends/_remote_base.py::_execute_command` wraps remote `cd + qsub|sbatch` in `bash -lic` so cluster `/etc/profile.d/*.sh` sources the scheduler binary onto PATH (Hoffman2 + most non-login ssh paths).

### Added — WS2/WS3/WS4 escalation-funnel batch

- WS2 `cli/skill_returns.py` + 5 per-skill `schemas/skill_returns/*` + sub-skill SKILL.md final-step rewrites: `emit-skill-return` / `fetch-skill-return` carries sub-skill results to the parent (`hpc-submit`, `hpc-campaign`).
- WS3 central recovery registry (`hpc_agent/recovery/registry.py`) keyed by `failure_features.error_class`; three ported kinds (`already_in_flight` / `submission_incomplete` / `spawn_worker_died`); `recoveries list` / `recoveries show --placeholders k1=v1,k2=v2`. Closes `verify-canary` `submission_incomplete` (`record.job_ids in (None, [])` now raises with a registry-backed remediation instead of silently classifying as `abandoned`).
- WS4 SKILL.md linter (`scripts/lint_skills.py`) + `tests/contract/{primitive_remediation,schema_roundtrip}` + strict_xfail catalogues + CI auto-regen on PR (`stefanzweifel/git-auto-commit-action@v5` with workflow-level cancel-in-progress).

### Added — WS5 composite preflight primitives (#270)

`submit-preflight` (`install-commands` → `load-context` → `check-preflight` when `--cluster` set), `status-preflight`, `aggregate-preflight` + 5 more composites land via #270; collapses the per-skill Step 0 boilerplate.

### Fixed — `_degree` accepts stringified ints (#240)

`ops/recover/resolve.py::_degree` now coerces non-negative integer-shaped `str` values; a producer that emits `{"tp_size": "2"}` would otherwise route a `gpu_oom` to `increase-mem-per-gpu` instead of `increase-parallelism`. Latent today (the resolver isn't wired into the recover path yet) — hardened ahead of that seam landing.

### Documented — `escalation` envelope block

`docs/reference/cli-spec.md` and `docs/reference/agent-surface.md` describe the optional top-level `escalation` block (success or error envelope) — `schemas/escalation.json` was already the source of truth; the prose reference was missing it.

## 0.10.6 — 2026-06-04

Two-PR batch (#246 + #266) addressing 17+ speedup and correctness issues filed during a single 2026-06-04 live demo session (#242-#265), plus a worker-prompt scaffold trim.

### Added — content-hash deploy cache + parallel scp

`deploy_runtime` records a cluster-side manifest (`.hpc/.deploy_state.json`) of sha256 + producing package version per shipped file, and skips any file whose sha AND package version still match. The (cache-filtered) copies fire through a `ThreadPoolExecutor` (max 4) reusing the single ssh ControlMaster, overlapping fork/exec + transfer latency. A version bump, missing/corrupt manifest, `use_cache=False`, or `HPC_NO_DEPLOY_CACHE=1` falls back to a full deploy. (#242, #245)

### Added — Windows multiplexing via named-pipe ControlPath (default on)

Native Windows OpenSSH ≥ 8.x named-pipe multiplexing is now the default; opt out with `HPC_SSH_NAMED_PIPE=0`. A one-time `ssh -V` probe falls back to the legacy `ControlMaster=no` / `ControlPath=none` override on positively-detected OpenSSH < 8.x. A separate `~/.ssh/config` scan detects a global `Host *` Unix-socket `ControlMaster` stanza (which the cli `-o` override does not reliably beat on Windows) and forces `HPC_NO_SSH_MULTIPLEX` semantics after warning once. `HPC_NO_SSH_MULTIPLEX=1` still wins everywhere. (#243)

### Added — prompt-cache hit instrumentation

`hpc-agent run --report-cache-stats` runs the worker with `--output-format json`, unwraps the report from the result envelope, and surfaces `cache_read` / `creation` / `input` / `output` token counts under `data.cache_stats`. Off by default; integration test gated behind `RUN_NETWORK_TESTS=1`. (#244)

### Added — task_generator-consistency guard

`hpc-submit` Step 3 compares cached `interview.json`'s task_generator against caller-supplied. Different → `spec_invalid: task_generator_mismatch` by default (no more silent 8-vs-100 drift); `refresh` rewrites the interview; `prefer-caller` reproduces the old behavior as an explicit opt-in. (#247)

### Added — `/aggregate-hpc` and `/submit-hpc` auto-reconcile

When the journal records `next_step_hint == "monitor"` but the run is gone cluster-side, both skills run `hpc-agent reconcile` against the cluster and branch on the result — terminal / abandoned / still-in-flight — rather than refusing on the journal alone. (#248, #257)

### Added — state caches: describe / discover-runs / preflight / canary

Four caches keyed by stable inputs, all bypass-able via `HPC_NO_<NAME>_CACHE=1`:

- `describe_cache.py` — `(pkg_version, verb_name)` → cached describe output. (#261)
- `discover_cache.py` — directory `mtime` → cached journal scan. (#264)
- `preflight_cache.py` — `(host, env_activation_hash, framework_version)` → cached "session healthy" within N-minute TTL. (#255)
- `canary_cache.py` — `(cmd_sha, env_activation_hash)` → "this cmd_sha was validated; skip canary within TTL". Pairs with `total_tasks <= threshold` auto-skip. (#249, #263)

### Added — afterok scheduler dependency for canary→main

When both jobs submit, main has `--depend=afterok:<canary_id>` (SGE / SLURM / PBS variants). Eliminates the orchestrator round-trip between canary terminal and main qsub; main also gets earlier fairshare positioning. (#250)

### Added — batched monitor-tick + adaptive polling + cluster-side reduce

- Per-tick monitor queries combine into one ssh invocation (one RTT for `qstat` + sidecar + results listing). (#251)
- Adaptive backoff on no-change ticks (30s → 60s → 120s); reset on state-change detection. (#253)
- Final reduce + summary moves cluster-side; aggregate pulls one `metrics_aggregate.json` instead of N `wave_*.json`. (#254)

### Added — rsync delta transfers + SSH cipher tuning

- `deploy_runtime` cache-miss path uses rsync deltas (tar|ssh fallback on hosts without rsync). (#252)
- Default cipher list bumped to `aes128-gcm,aes256-gcm` + ETM MACs. Tunable via `HPC_SSH_CIPHER` / `HPC_SSH_MAC` / `HPC_SSH_COMPRESSION`. (#256)

### Added — reconcile cascades to canary + `unable_to_verify` state

`hpc-agent reconcile --run-id <id>` also reconciles paired sibling entries sharing the same cmd_sha (e.g. `-canary`). Output adds an `unable_to_verify` lifecycle state distinct from `in_flight` for the case where the SSH/scheduler query itself failed. (#258)

### Added — inline-mode prompt path forwarding + sandbox preflight

- Inline-mode envelope writes large prompts (>4 KB) to `.hpc/_inline/<id>.prompt.md` and returns `prompt_path` instead of inlining; the orchestrator forwards the path to the subagent. (#262)
- Inline-mode worker runs an SSH preflight as Step 0; on sandbox-blocked SSH returns `spec_invalid: sandbox_blocks_cluster_ssh` upfront, not as a buried message in an `ok: true` envelope. (#265)

### Fixed — `data_axis_hint` prompt/schema divergence

`hpc-wrap-entry-point` SKILL.md now conditions `data_axis_hint` emission on `entry_point.kind == "shell_command"`, matching the schema's constraint. (#260)

### Fixed — skill prose: parallel TOOL CALLS, not shell concurrency; no PowerShell shelling

- "Batch independent tool calls" bullet clarified across all orchestrator skills + the worker scaffold: "parallel" means multiple Bash / Read / Grep tool-call blocks in one message, NOT `cmd1 & cmd2 & wait` / `parallel` / `xargs -P` inside one Bash call. (#259)
- The no-`python -c` / `bash -c` / `jq` / `cat` / `head` / `grep` / `find` rule (commit `69b1ee2d`) extended to `powershell -Command` / `pwsh -Command` / `cmd /c` / any shell-with-code-flag pattern. (#262)

### Changed — worker-prompt scaffold trimmed ~46%

The `cacheable_prefix` in `render_spawn_parts` shrunk from ~800 to ~430 tokens via section-by-section audit (opener, cd + load-context, adjustments preamble, 8 bullets → 6 via semantic merges, return contract, closing). Procedure body and structural shape unchanged. Saves cache-write tokens on first spawn per session and trims cache-read cost on subsequent spawns. Snapshot fixtures regenerated. (`dbad096c`)

## 0.10.5 — 2026-06-04

Skill-prose fixes for two empirical demo-loop friction points.

### Fixed — `hpc-submit` / `hpc-status` / `hpc-aggregate` auto-retry inline on spawn failure

Previously the skills banned BOTH preemptive `--inline` AND auto-retry inline on a real spawn failure, treating every inline-mode entry as a user opt-in. But 0.10.3's spawn-worker error now explicitly hints `Fallback: …HPC_AGENT_INVOKER=inline…` in the malformed-report remediation when the worker dies before emitting a report — typically because the workspace API key is over quota, separate from the caller's interactive Claude Code OAuth session. The skill should respond to that framework hint by automatically setting the env var and retrying, not pausing to ask. The `--inline` *flag* refusal (#155 guard) still applies; the env var bypasses it because it is the documented operator-opt-in form, and the framework's own hint is the signal that inline is the correct recovery path.

### Fixed — `hpc-submit` / `hpc-status` / `hpc-aggregate` skills: no narration at sub-skill boundaries

Empirical demo: every time a composed sub-skill (`hpc-wrap-entry-point`, `hpc-classify-axis`, `hpc-build-executor`, etc.) returned control, the parent agent emitted a human-readable summary ("Entry point onboarded. Now resolving data axis…"). That summary fires an end-of-turn signal to the harness, yielding control back to the user mid-procedure. SKILL prose now says: immediately chain to the next tool call without emitting prose at sub-skill returns. The user sees only the final envelope.

### Fixed — `hpc-submit` already_in_flight remediation names `reconcile`

When `load-context` says `next_step_hint == "monitor"`, the skill's `spec_invalid: already_in_flight` envelope used to give no path forward when the cluster state had been cleaned but the journal still said in_flight (empirical: post-cleanup of `$SCRATCH/run-bdae0357`, the journal still tracked `run-bdae0357-canary` as in-flight; the agent's offered options — `/monitor-hpc`, `--no-canary`, "force" — all missed the actual fix). Remediation now names three concrete recovery paths in order of safety: (a) `/monitor-hpc` for the normal case; (b) `hpc-agent reconcile --run-id <id> --scheduler <sched>` when the cluster state is gone (reconcile polls, sees the dir is missing, marks the journal `abandoned`, unblocks the next submit); (c) `--no-canary` only when the prior canary specifically is the in-flight one and was independently confirmed.

## 0.10.4 — 2026-06-04

One-line follow-up to 0.10.3's `register_run` auto-gen.

### Fixed — `register_run_executor_cmd` defaults `output_file` to `$RESULT_DIR/metrics.json`

The decorator-injected `compute(args)` wrapper writes a returned dict to `args.output_file` only when both the dict AND `output_file` are present. The dispatcher's `HPC_KW_*` convention carries only `tasks.resolve(i)` kwargs (per-task user data), never framework metadata like `output_file`. Without an explicit `setdefault`, the dispatcher exports `RESULT_DIR` but no `HPC_KW_OUTPUT_FILE`, so the dict gets silently dropped — exactly the empirical 0.10.3 demo case (100 tasks ran, only `_runtime.json` written, no `metrics.json`). Inject `output_file = $RESULT_DIR/metrics.json` via `setdefault` in the `-c` one-liner so the common `@register_run` pattern (function `return`s a dict) actually lands the result. An explicit user-supplied `output_file` (via FLAGS / `HPC_KW_OUTPUT_FILE`) still wins — the dict comprehension reads HPC_KW_* before the `setdefault` fires.

## 0.10.3 — 2026-06-04

Six landed fixes since 0.10.2, motivated by the live UCLA Hoffman2 demo loop. No wire-surface breaks.

### Fixed — `register_run` auto-generates `executor_cmd` (the metrics.json bug)

When `entry_point.kind == "register_run"`, the materialized interview previously contained only `{kind, run_name}` — no `executor_cmd`. The framework defaulted the per-task command to `python3 <file>`, which on the cluster ran the file as a script. The dispatcher passes kwargs only via `HPC_KW_*` env vars (never argv), so a file with no `__main__` block silently exited 0 without invoking the decorator-injected `compute(args)` (empirical case: 100 tasks ran, only `_runtime.json` written, no `metrics.json`). A file with an argparse `__main__` block failed with "required argument missing". Mirror `wrapper_executor_cmd`'s contract with a new `register_run_executor_cmd(campaign_dir, run_path)` helper that emits the same `python3 -c "...; argparse.Namespace(**HPC_KW_*); _m.compute(_n)"` one-liner — only the file path differs. `_validate_register_run_entry` now returns the matched path so the interview can thread it.

### Fixed — Defensive guard against bare-script `register_run` executor

Backstop for the auto-gen above: when `extra_env["EXECUTOR"]` is the naive `python3 <file>.py` shape AND the file imports `register_run` from `hpc_agent`, `build_submit_spec` refuses with `SpecInvalid` pointing at the canonical one-liner shape. Catches sidecars assembled outside the framework's interview path (manual JSON, older interview outputs, third-party assemblers).

### Fixed — `submit_spec` refuses relative `remote_path` and empty env-activation at the boundary

A half-resolved cluster config previously produced `REPO_DIR=monte_carlo_pi-bc3eb1b5` (relative) and empty `CONDA_*` / `MODULES` — the canary crashed cluster-side and the bad sidecar poisoned later submit dedup by `cmd_sha`. Two guards in `build_submit_spec`: `remote_path` must be absolute Unix (start with `/`); at least one of `modules` / `conda_source` / `conda_env` must be non-empty. Both errors point the operator at `hpc-agent setup --cluster <name>` since the empirical case is "clusters.yaml wasn't onboarded". Includes a new `tests/incorporation/build/conftest.py` autouse fixture that isolates these tests from the host's `~/.hpc-agent/clusters.yaml` (env leakage was masking the test-side validity of the fixtures).

### Fixed — Preflight `command -v uv` when `runtime=uv`

`submit_flow` now SSHes once with the cluster's activation sequence (`module load … && source $CONDA_SOURCE && conda activate $CONDA_ENV && command -v uv`) when any fresh spec asks for `runtime=uv`. If the probe fails, `SpecInvalid` at preflight with `~/.conda/envs/<env>/bin/pip install uv` remediation. Saves a wasted canary cycle when the cluster env doesn't have uv. No-op when `HPC_RUNTIME != "uv"` or `skip_preflight=True`.

### Added — `HPC_SSH_NAMED_PIPE=1` enables ControlMaster on Windows

Opt-in path for SSH connection multiplexing on Windows via named-pipe `ControlPath`. Windows OpenSSH ≥ 8.x supports `ControlPath=\\.\pipe\…` namespaces; only Unix-socket `ControlPath` fails on Windows (the framework already detects this and disables multiplexing). With `HPC_SSH_NAMED_PIPE=1` set on Windows, `_ssh_multiplex_opts()` emits `-o ControlMaster=auto -o ControlPath=\\.\pipe\openssh-hpc-cm-%C` + the existing `ControlPersist` logic. Default left off pending live validation. Each SSH call drops from ~1-2s to ~50ms after the first when enabled. `HPC_NO_SSH_MULTIPLEX=1` still wins. POSIX unaffected.

### Fixed — Worker-crash error surfaces `HPC_AGENT_INVOKER=inline` as fallback

When the spawned `--bare` worker dies before emitting a valid report (typical: workspace API key over quota, separate from the caller's interactive Claude Code OAuth session), the malformed-report error now appends a single-sentence hint naming inline mode as the natural recovery. Always-on; the operator can ignore it when the failure is for a different reason.

## 0.10.2 — 2026-06-04

Tiny follow-up release for one prose-layer fix that landed after the 0.10.1 cut. No code changes; just bundled-asset content the installed wheel ships into `~/.claude/skills/`.

### Fixed — `/submit-hpc`, `/monitor-hpc`, `/aggregate-hpc` skills get a Step 0 (idempotent `install-commands`)

When `~/.claude/agents/hpc-worker.md` is missing on a fresh machine, every hpc-submit / hpc-status / hpc-aggregate run failed at the handoff step when it tried to dispatch the rendered procedure to the named subagent. The orchestrator agent then fell back to running the procedure by hand — frequently inventing cluster commands like `python -m hpc_agent.execution.mapreduce.reduce.combine` (no such module). Add a Step 0 to all three skill prompts: run `hpc-agent install-commands` first. Idempotent (no-op when assets are already installed) and ~50ms when re-run, so safe to make a hard prerequisite. The 0-byte-collision auto-clear from 0.10.1 handles the empirically common stale-artifact case; non-empty file at the install path still raises a clear `FileExistsError` with remediation.

## 0.10.1 — 2026-06-04

Patch release with four agent-UX fixes surfaced during live Hoffman2 demos against 0.10.0: scanner gap on the SKILL.md-documented `register_run` import form, `items_x_seeds` requiring agents to discover `items: [{}]` as the no-frozen-config shape, `install-commands` raising on a stale 0-byte file at `~/.claude/{commands,skills,agents}`, and the inline `hpc-worker` PreToolUse hook shelling `jq` (absent on native Windows, which let the orchestrator agent fall back to inventing cluster commands). No wire-surface breaks; previous spellings keep working.

### Fixed — `discover_runs` scanner accepts `from hpc_agent import register_run`

`hpc_agent/__init__.py` lazily re-exports `register_run` and SKILL.md documents that as the canonical import form, but the `discover_runs` AST scanner only matched `from hpc_agent.experiment_kit import register_run` and `from hpc_agent import template`. An executor written against the documented spelling went undiscovered, and the agent then chased the wrong workaround (rewriting the import or pivoting to `shell_command`). The scanner now also binds `register_run` from the top-level `hpc_agent` ImportFrom node.

### Fixed — `items_x_seeds.items` defaults to `[{}]`

The pure seed-sweep case — no frozen kwargs, just N tasks parameterised by seed — required the caller to know that `items: [{}]` was the no-op shape against an otherwise unguessable required field. `_ItemsXSeedsParams.items` now defaults to `[{}]` via `default_factory`, so a seed-only request is just `{"kind": "items_x_seeds", "params": {"seeds": [...]}}`. The materializer collapses cleanly to `[{'seed': s} for s in _SEEDS]`. Explicit `items` still works for the cartesian case. `schemas/interview.input.json` regenerated.

### Fixed — `install-commands` auto-clears 0-byte collisions at `commands`/`skills`/`agents`

`~/.claude/{commands,skills,agents}` historically raised `FileExistsError` when one of those paths was a regular file rather than a directory — safe but high-friction for the empirically observed case of a 0-byte stale scaffold artifact (Windows touch-then-crash, abandoned old-version installs). The contract is now:

- missing or already a directory — no-op (unchanged)
- 0-byte regular file — silently unlinked, path reported in `result["cleared_collisions"]`; in `dry_run=True` reported but not unlinked
- any other non-directory — `FileExistsError` with the same remediation as before (unchanged for the case where the user might lose real content)

### Fixed — `hpc-worker` PreToolUse hook no longer requires `jq`

The hook that fences the inline `hpc-worker` subagent to `hpc-agent` / `git` invocations only used `jq` to extract `.tool_input.command` from the hook's stdin JSON. On native Windows that meant the hook failed (`jq: command not found`), the subagent was blocked, and the orchestrator agent fell back to running aggregate procedures by hand — frequently inventing cluster commands. The parse step now shells `python3 -c 'import json, sys; print(json.loads(sys.stdin.read()).get("tool_input", {}).get("command", ""))'`, which is already a hard requirement of any hpc-agent install. The fence behaviour itself is unchanged. The 13 `test_hpc_worker_fence.py` cases that previously skipped on Windows (no jq on PATH) now run.

## 0.10.0 — 2026-06-03

Highlights: PBS/Torque scheduler support via data-driven SchedulerProfiles (#202), Optuna/PBT scaffolds + campaign seam (#218, #219), structured `failure_features` schema wired into every ErrorEnvelope (#230, #237), single failure-classifier CATALOG (#236, #238), single-source vocabularies via `get_args` (#235), plus four polish fixes: schema-aware `spec_invalid` remediation + `register_run` discovery hint, worker stdout tail in `internal` envelopes, `verify-aggregation-complete --combiner-dir` default, and `aggregate-flow` `_combiner/`-missing diagnosis with three recovery paths. See `git log --oneline 0.9.0..` for the full per-commit history.

## 0.9.0 — 2026-06-01

Highlights: worker-prompt invoke-only fence (#203), local dry-run gate before the cluster canary (#205), behavioral eval harness for agent decisions (#204), `--invalidate-on-code-change` opt-in dedup lever + code-drift warning (#207), centralized resource clamp/ceil + canonical walltime formatter (#206), `ssh_run` returns at foreground-exit speed when a remote child holds the pipe (#209), generic-plugin `setup` integration / drop named-plugin coupling (#213), unambiguous Step 9 in `hpc-submit/SKILL.md`. Also rolls up the 0.7.2–0.8.1 chain that landed without per-version CHANGELOG entries; for those, `git log --oneline` on `main` is the authoritative history.

### Changed — `setup` plugin integration is fully generic; host names no plugin

The `setup` primitive carries no plugin-specific code. It invokes a
generic `run_plugin_setup_actions(context)` seam: any plugin may expose
a `run_setup_actions(context) -> Mapping | None` hook, which the host
calls blindly (passing `cluster` / `experiment_dir` / `install` /
`dry_run`) and collects under a `data.plugin_actions` field keyed by
plugin name. The host knows nothing about what a setup action does. On a
core-only install no plugin contributes and the field is absent.

### Fixed — verify-canary resolves a vanished canary fast instead of timing out (#193)

A canary that finished or failed fast and left the scheduler queue before the first status poll showed an all-zero *live* summary (nothing complete/failed/running/pending/unknown). The poll loop had no terminal condition for that, so it rode the full `wait_budget_sec` (30 min default) and reported `timeout` — a 30-minute agent-loop stall on a job that was already gone. The loop now detects the all-zero summary and, once it **persists** across consecutive polls (so the transient pre-registration window right after qsub doesn't false-trigger), breaks out and returns `failure_kind="completed_unknown"` (`ok=False`, so the two-phase gate still refuses the main array). The stderr scan still runs first, so a real marker (oom_killed, traceback) wins over the bland verdict. Unchanged: a genuinely slow/queued canary still waits to `timeout`; a persistently-broken reporter still surfaces `reporter_unreachable`. The `CanaryFailureKind` wire enum gained `completed_unknown` (and `reporter_unreachable`, which the code already returned but the `Literal` omitted); `verify_canary.output.json` regenerated.

### Fixed — status/aggregate/campaign worker reports survive validation (#194)

`parse_worker_report` rejects a worker envelope whose `decisions` entries carry a `point` outside `DECISION_POINTS[workflow]` — even when the workflow succeeded. #183 fixed this for the **submit** worker prompt; this completes the audit for the other three. Each of `status.md` / `aggregate.md` / `campaign.md` gained a **Reporting conventions** section enumerating its allowed point IDs and the strict-`decisions` / free-form-`anomalies` split, and every "record X in `decisions`" instruction was rewritten to use an allowed point ID with a descriptive `outcome` (e.g. aggregate's `unexpected_tasks_present` → a `completeness` decision with outcome `unexpected_tasks`; campaign's `stochastic_marker_missing` finding code → a `stochastic_marker` decision with outcome `missing`), routing free-form detail to `anomalies`. A new prose lint (`test_decisions_point_ids_are_in_the_allowlist`) scans every worker prompt and fails CI if a recorded point ID isn't in that workflow's allowlist — turning the class from "caught in a live demo" into "caught by CI". Worker-prompt snapshot fixtures regenerated.

### Fixed — `ssh_run` returns at foreground-exit speed when a remote child holds the pipe (#209)

`ssh_run`'s capture path drained each pipe to EOF via a single blocking `subprocess.run`, and EOF only arrives when the *last* writer closes the fd. A remote command that backgrounds a child inheriting ssh's stdout/stderr pipe kept that pipe open after the foreground process exited, so a "finished" job stalled the agent for the full `SSH_TIMEOUT_SEC` (60s) before erroring — for an unattended `status` poll, "hang 60s then raise" is materially worse than "return on exit". The capture path now funnels through a `select()`/close-pipes-on-exit reader (`_capture_via_select` → `_communicate_select`) that drains whatever stdout/stderr have ready while re-checking `proc.poll()` on a fixed cadence; the instant the foreground process exits it does one final non-blocking drain and stops, never waiting for EOF, so a lingering backgrounded child can't wedge the read. Technique borrowed (not the code, not the dependency) from `remotemanager`'s `CMD._communicate_with_select` (MIT). The `ssh_argv` seam — BatchMode, ControlMaster multiplexing, native-binary resolution, the Windows override — is untouched; `capture=False` streaming and Windows keep the blocking `subprocess.run` path (select(2) over pipes is POSIX-only). A POSIX real-subprocess regression test asserts a command that backgrounds a sleeper returns at foreground-exit speed, not at the timeout.

### Changed — resource value-coercion centralized into one render helper (#206)

Clamp-to-cluster-limits, round-up, and walltime→`HH:MM:SS` formatting were hand-rolled at every scheduler call site, with two independent `HH:MM:SS` implementations (`infra/backends/sge.py` and `ops/recover_flow.py`) that agreed for non-negative inputs but disagreed on negatives, and nothing stopping a third copy from drifting further. A new stdlib-only `@pure` helper `infra/resource_format.py` exposes `walltime_hms(seconds)` (the single canonical integer-seconds → `HH:MM:SS` formatter) and `coerce(value, *, minimum, maximum, ceil, fmt)` (the declarative None-passthrough → `math.ceil` → clamp → optional-format pipeline; `fmt="time"` delegates to `walltime_hms`). The SGE/SLURM backends and the throughput planner now route through it, so the walltime format and the clamp/ceil policy each have exactly one auditable implementation instead of N. Behaviour-preserving — verified byte-for-byte against the existing pinning tests; the `{{TOKEN}}` template syntax is untouched.

### Added — `--invalidate-on-code-change` opt-in dedup lever + code-drift warning (#207)

Confirmed and documented that the idempotency/dedup key `cmd_sha` is **parameter identity, not code identity**: it hashes only the materialized per-task kwargs (`resolve(i)` for every `i`), so editing an executor's body with unchanged swept params keeps the same `cmd_sha` and a re-submit dedups against the prior run **by design** — the swept params define the experiment; the executor body is provenance, recorded separately as `tasks_py_sha`. Default behaviour is unchanged. Two additions make code-iteration safe when wanted: a new opt-in `--invalidate-on-code-change` submit lever (threaded through `SubmitSpec` / `submit-flow` / `submit_and_record` → `find_run_by_cmd_sha`) folds the run's `tasks_py_sha` into the dedup decision so a code-only change forces a fresh run; and, even with the lever off, a `UserWarning` now fires when a matching `cmd_sha` is found but the recorded `tasks_py_sha` differs ("deduping against run X, but the code changed since…") — a safety net that never alters the dedup decision on its own. Schemas regenerated for the new spec field.

### Added — `dry-run-local`: a local pre-flight execution gate before the cluster canary (#205)

Every existing pre-submit gate is static/structural — the earliest the user's executor is actually *run* is the cluster-side canary, **after** rsync + deploy + sbatch/qsub. The new `dry-run-local` validator catches the broken-grid class (bad import, mis-wired `HPC_KW_*` arg, broken `result_dir_template`) **before any SSH**. It does two things: a **default-on template-render check** that renders `result_dir_template` for the sampled ids exactly as the cluster dispatcher's `_format_result_dir` and flags unfilled `{field}` placeholders (a per-task `KeyError` cluster-side → every task dies) and cross-id `result_dir` collisions (a silent `metrics.json` overwrite the combiner under-counts); and an **opt-in executor smoke-exec** (`smoke=true`) that runs the executor once locally under the dispatcher's `HPC_KW_*` env contract with a hard timeout, classifying import / non-zero-exit / timeout failures with the captured stderr tail. It emits the standard `ValidatorFinding` envelope and is composed into the `validate-campaign` cascade (which `/submit-hpc` runs upstream of the canary). Scoped to "broken code, not broken cluster" — it complements the canary, never replaces it.

### Added — behavioral eval harness for the agent decision surface (#204)

Adds `tests/eval/`: a behavioral regression harness that grades **agent decisions** (given a natural-language request + a fixture repo, does the agent resolve the right submit spec — cluster, grid/axes, wave plan, resources?) rather than prose. A stdlib-only, float-tolerant `recursive_compare` grades the resolved spec structurally — exact where it must be (`cluster`, `grid_points`), tolerant where it should be (resources) — against version-controlled gold snapshots, re-baselined with `HPC_EVAL_REGEN=1`. The default offline tier drives the deterministic half of the `/submit-hpc` decision against self-contained fixture repos and runs fully offline with no API key; the live-LLM tier reuses the existing `slow` marker and skips without `ANTHROPIC_API_KEY`, so default CI stays free and offline. Ships six seed cases across submit/campaign.

## 0.8.0 — 2026-05-29

### Fixed — Non-axis required executor params are now resolved + gated (#195)

When an executor's signature required a param the user didn't sweep (e.g. `samples` when only `seed` was an axis), the generated `tasks.py` `resolve(i)` returned only the axis kwargs — the cluster never exported `HPC_KW_SAMPLES`, and the templated executor command ran `--samples` with no value, crashing every task at argparse. Two layers now address it:

- **Resolve (interview):** entry points gained a `fixed_params` field (on both `register_run` and `shell_command` kinds). Constant non-axis kwargs declared there are baked into every materialized task's `resolve(i)` dict via the same `_INJECT` seam that threads frozen-config shas — so the param ships per-task with its JSON type preserved (an `int` stays an `int`). Like `frozen_configs`, it requires `task_generator` (the framework only threads constants into a materialized `tasks.py`). The `hpc-wrap-entry-point` skill grew a Step 5b that partitions signature params into axis / has-default / uncovered-required and emits `fixed_params` (using the executor's argparse default when present); `/submit-hpc` + `hpc-submit` surface an `uncovered_param` ambiguity so the value can be elicited when there's no default.
- **Gate (validate):** `validate-executor-signatures` now also checks the *reverse* direction — every required signature param (no default, not `*args`/`**kwargs`) must be covered by `resolve()`'s kwargs, else an `uncovered_required_param` error finding. The submit flow already runs this validator, so an uncovered param is now refused statically at submit time instead of failing N cluster tasks. The check skips cleanly when no task was sampled (no false positives). Same "intake refuses structurally broken specs" family as #171 / #184 / #186 / #191 / #192.
- **Defense-in-depth (dispatcher):** the cluster-side dispatcher now diffs the `$HPC_KW_*` references in the executor command against the kwargs it actually exported and warns (to the job log, naming the param + remediation) on any that would expand to empty. This catches the residual case the signature gate can't see — a command template referencing a var outside the executor function's introspectable signature. Stdlib-only, restricted to the framework-owned `HPC_KW_` namespace (a bare `$HOME`/`$SAMPLES` is left alone). Warns rather than aborts (the canary surfaces the failure; `${HPC_KW_X:-default}` guard forms are legitimately safe).

### Added — Onboarding grants the bare worker the `hpc-agent` CLI permission (#190)

A spawned `claude -p --bare` worker runs headlessly with no human to approve permission prompts, so without an allow rule Claude Code's auto-mode classifier blocked its first `hpc-agent ...` Bash call and the default worker path silently degraded for every new install (the inline-subagent fallback masked it). `hpc-agent interview` (onboarding) now writes/merges a **project-scoped `<campaign_dir>/.claude/settings.json`** granting `Bash(hpc-agent:*)` — Claude Code merges it on top of the user-global config, so anyone launching `claude` from the experiment dir gets the grant with zero manual config and no global mutation. The merge is idempotent and non-destructive: an existing settings.json keeps every other key and allow entry; the rule is appended (deduped) and only reported as a written artifact when newly added. The README documents the user-global equivalent for `claude` launched outside an experiment dir. (`pip`/`uv` are deliberately never made to mutate `~/.claude/settings.json` — not portable, and hostile even where supported.)

### Fixed — Two silent-canary failure modes refused at intake (#191, #192)

Both surfaced on the inline-subagent submit path, where a worker-constructed fields-file handed `submit-flow` a structurally-broken spec the cluster "succeeded" on in milliseconds — the canary passed and the main array fired the same no-op qsub.

- **#192 (root cause): `pass_env_keys=[]` forwarded zero env vars to qsub.** `infra/backends/remote_factory.py` used `pass_env_keys if pass_env_keys is not None else job_env_keys`, and `[] is not None` is `True`, so an explicit empty list stripped *every* var (`$EXECUTOR`/`$CONDA_ENV`/`$REPO_DIR`) on the way to `qsub -v` — even a correctly-set `EXECUTOR`. Now `[]`/`()` and `None` are equivalent ("forward all") at the factory, and `SubmitFlowSpec` **refuses `pass_env_keys=[]` at construction** with an actionable message (omit / `null` = forward all; a non-empty list restricts). `[]` is the natural-feeling JSON "no override", so this footgun was waiting for the first agent-built spec to hit it.
- **#191 (defense-in-depth): empty `job_env["EXECUTOR"]` shipped silently.** `submit-flow` now refuses an empty/missing job-script `EXECUTOR` at intake (`_ensure_job_script_executor`, before any qsub) — non-emptiness only, deliberately *not* runnability, since the job-script EXECUTOR is *supposed* to be the dispatcher command (unlike the sidecar's per-task executor). All four array templates (`sge/{cpu,gpu}_array.sh`, `slurm/{cpu,gpu}_array.slurm`) also fence `$EXECUTOR` with `: "${EXECUTOR:?...}"` so a job reaching the node with EXECUTOR unset fails loudly instead of running `time` with no command and exiting 0.

The submit worker prompt's Step 6d spec sketch now shows `"pass_env_keys": null` (was `[...]`) with explicit notes on both footguns; schemas + the submit worker-prompt fixture regenerated. Same "intake refuses structurally broken specs" family as #171 / #184 / #186.

### Added — Inline subagent is pinned to a small model via a shipped definition

Inline mode now routes to a **named, model-pinned subagent** rather than an ad-hoc one. A new `hpc-worker` subagent definition (`model: haiku` in its own frontmatter) ships in the package and installs to `~/.claude/agents/hpc-worker.md` via `hpc-agent install-commands`. The inline envelope's `data.instructions` directs the caller to dispatch to `hpc-worker` first; because the model pin rides with the definition, the harness enforces it regardless of the caller's model — a true pin, not a prose suggestion. The envelope also gains a structured `data.subagent` hint (`{preferred_name, model, task}`) so a harness can route programmatically without parsing prose.

The capability ladder degrades gracefully: `hpc-worker` (pinned) → a generic `Agent`-tool subagent (model-hinted to haiku where the tool allows a per-call model) → in-context. `install-commands` (and `install_agent_assets`) now copy an `agents/` asset tree alongside `commands/` + `skills/` and report `agents_installed` in their result; plugins can overlay their own `agents/` the same way. The model pin mirrors the `claude -p` worker's `_WORKER_MODEL` (haiku), so cost/latency match across the spawn and inline paths — though note the inline subagent inherits the caller's session sandbox/environment, so it is not as environment-controlled as the `--bare` spawn (the subagent definition records a sandbox-block escalation caveat).

**Isolation ceiling documented (don't over-promise).** The inline `data.instructions` and the `hpc-submit`/`hpc-status`/`hpc-aggregate` "Inline mode" sections now state explicitly that a subagent recovers *context* isolation but **not** *environment* isolation: it shares the session's sandbox posture and auto-loads project `CLAUDE.md`, unlike the default `--bare` `claude -p` spawn (which forces the sandbox off and strips `CLAUDE.md` for a reproducible-minimum context). A caller who needs that stronger isolation is pointed at the default spawn (drop `HPC_AGENT_INVOKER=inline`) rather than inline. The `hpc-worker` definition is also hardened so the handed-in procedure outranks any ambient `CLAUDE.md` / memory the session loads.

### Added — Inline mode can delegate to a subagent when the harness has one

The `hpc-agent run --inline` / `HPC_AGENT_INVOKER=inline` branch (the user opt-in that skips the fresh-context `claude -p` worker) now tells the calling agent to **delegate the rendered procedure to a single subagent when it has that capability** — Claude Code's `Agent` tool (formerly `Task`), or any harness equivalent — instead of always running it in the agent's own context. Dispatching the procedure into one isolated subagent recovers the context isolation the worker spawn would have given, without a second process or separate credentials.

The capability is gated, not assumed: the subagent path is taken only when such a tool is actually available, and a harness without one (a bare API caller, a notebook driver, the headless worker itself) falls back to the existing in-context execution — never erroring on a tool it lacks. The default (non-inline) transport is unchanged: it still forks a `claude -p` worker, and the #155 guard still refuses an agent-supplied `--inline` when a spawning worker can authenticate. The `Agent` tool was added to the `allowed-tools` of the `hpc-submit` / `hpc-status` / `hpc-aggregate` / `hpc-campaign` skills so Claude Code grants it; the addition is a no-op on harnesses that don't provide it. (Anthropic's new Dynamic Workflows feature was considered and deliberately not used here: it targets large subagent fan-out, is surface/plan-gated with no capability probe, and isn't reliably present across the harnesses hpc-agent supports — the plain, capability-gated subagent primitive is the portable fit for a single-procedure delegation.)

## 0.7.1 — 2026-05-27

### Fixed — Windows + Hoffman2 live-run hardening

Bugs surfaced by a real submit→monitor→aggregate run on UCLA Hoffman2 from a native-Windows Claude Code session (tracked in issue #135).

- **`register_run` is importable from the package root.** `from hpc_agent import register_run` — the form the `hpc-wrap-entry-point` skill documents — now works (resolved lazily to avoid an import cycle). It was never exported.
- **`~/.hpc-agent/clusters.yaml` user-level config tier.** Resolution is now explicit path > `HPC_CLUSTERS_CONFIG` > `~/.hpc-agent/clusters.yaml` > packaged default — one shared file instead of a per-repo copy.
- **The spawned worker forces the sandbox off.** The submit/monitor/aggregate worker SSH/rsyncs to a cluster (network the bubblewrap sandbox blocks on Linux/macOS, and which native Windows can't sandbox at all — it warned and corrupted the JSON report contract). It now runs unsandboxed regardless of the caller's global setting.
- **No hard SSH-agent precheck.** `status`/`aggregate` (and the dispatch-level gate) no longer hard-require a reachable agent before connecting — that blocked valid `IdentityFile` auth. `ssh_run` already uses `BatchMode=yes` (fails fast, no hang); a real auth failure is now enriched with agent state in the error remediation.
- **Control-plane remote Python activates the cluster env (#135 item 3).** The status reporter and the combiner run directly on the login node via `ssh_run` and never sourced the job preamble, so they hit the login node's bare `python` (lacking the framework). They now `module load` + `conda activate` the run's resolved env.
- **Stale Hoffman2 module default removed (#135 item 1).** Packaged `clusters.yaml` shipped `modules: [python/3.11.9]`, which isn't a valid modulefile on current Hoffman2 — every task failed. Default is now `modules: []` (provide Python via conda) with guidance.
- **Preflight rejects un-customized `clusters.yaml` (#135 item 2).** A `cluster_config_customized` check fails when the entry still carries `<your_user>` / `<your_scratch>` / `<your_env>` placeholders, instead of failing every task at submit time.
- **The canary fails loudly when the reporter is unreachable (#135 item 4).** A persistently-broken cluster-side reporter now yields `failure_kind="reporter_unreachable"` instead of masquerading as a `timeout`, so the main array isn't submitted against a cluster whose results can't be read.
- **`interview` materializes `tasks.py` into `.hpc/` (#135 item 5).** Generator-mode `tasks.py` is written to the canonical `<campaign_dir>/.hpc/tasks.py` (what `deploy_runtime`, the dispatcher, `build-tasks-py` and `RepoLayout` all read) rather than the campaign root, where deploy never found it. `interview.json` stays at the root.

## 0.7.0 — 2026-05-27

### Breaking — Re-exports removed, back-compat shim deleted

Three import-path changes that affect any code reaching into hpc-agent internals from the old paths.

- **`infra/remote.py` re-exports removed.** PR #131 split the 1000+-line module into `infra/ssh_validation.py`, `infra/ssh_options.py`, and `infra/transport.py`, leaving re-exports back on `infra/remote.py` for backwards compatibility. PR #133 migrated every internal caller (host + the optional plugin + tests) to the new paths and then deleted the re-exports. External callers using `from hpc_agent.infra.remote import rsync_push` (and similar for `rsync_pull`, `deploy_runtime`, `run_combiner`, `run_combiner_checked`, `validate_ssh_target`, `parse_remote_json`, `DEFAULT_RSYNC_EXCLUDES`) must update to `infra.transport` / `infra.ssh_validation`. `ssh_run` stays on `infra.remote`.
- **`state/runs.py` re-exports removed.** Same pattern: PR #131 extracted `state/run_sha.py` (`compute_cmd_sha`, `compute_tasks_py_sha`) and `state/wave_map.py` (`derive_wave_map`). PR #133 deleted the re-exports. External callers using `from hpc_agent.state.runs import compute_cmd_sha` must update to `state.run_sha`.
- **`hpc_agent.incorporation.template` back-compat shim deleted.** Was a re-export to `hpc_agent.experiment_kit` after the post-reorg cleanup; sat in place 2+ releases, firing a `DeprecationWarning` at every import (~13 per pytest run from pkgutil discovery). Removed in PR #132. External callers using `from hpc_agent.incorporation.template import <name>` must update to `from hpc_agent.experiment_kit import <name>`.

### Changed — plugin module naming aligned with host

The optional plugin's `_schema_models/` package was renamed to `_wire/` to match the host's post-Pydantic-migration name (PR #132). Internal change to the (unpublished) plugin package only; no external impact. Includes the corresponding pyproject lint-ignore + pre-commit hook updates.

### Improved — Windows pytest no longer drowns in pre-existing failures

40 pre-existing Windows-platform test failures (bash preamble, fcntl concurrent writers, `os.setpgrp` / `termios` / signal, Unix symlinks, SSH-gate tests whose env-var assumption no longer holds after 0.6.1's `infra.ssh_agent` named-pipe support) are now marked `@pytest.mark.skipif(sys.platform == "win32", ...)`. Net delta on Windows: 37 failed + 3 errors → 0 failed + 0 errors, 40 new skips. Linux CI behaviour unchanged — the tests still run there.

## 0.6.1 — 2026-05-27

### Fixed — Windows compatibility

Three concrete issues that broke `hpc-agent` for Windows users running native PowerShell + Windows OpenSSH:

- **SSH connection multiplexing now auto-disables on Windows.** `infra/remote.py:_ssh_multiplex_opts` previously emitted `-o ControlMaster=auto -o ControlPath=$XDG_RUNTIME_DIR/hpc-cm-%C` on every ssh invocation. ControlMaster uses Unix-domain sockets; native Windows OpenSSH fails on the multiplex socket with `getsockname failed: Not a socket / Read from remote host: Unknown error` — aborting every `submit`/`status`/`aggregate` call. The escape hatch `HPC_NO_SSH_MULTIPLEX=1` already existed but had to be discovered manually; now `sys.platform == "win32"` triggers it automatically. Same function also replaces the `or "/tmp"` fallback for `XDG_RUNTIME_DIR` with `tempfile.gettempdir()` so the non-Windows code is correct on systems without `/tmp`.

- **SSH-agent detection is now cross-platform.** `cli/_helpers.py:_require_ssh_agent` and `ops/preflight/check.py` previously hard-required `SSH_AUTH_SOCK` — a Unix convention. Windows OpenSSH uses a named pipe (`\\.\pipe\openssh-ssh-agent`) instead and never sets the env var, so every cluster-touching command was blocked on Windows even when the agent was reachable with a loaded key. A new `infra/ssh_agent` module provides `agent_available()` / `agent_detail()`: on Unix it preserves the existing `SSH_AUTH_SOCK` semantics verbatim; on Windows it probes the named-pipe agent via `ssh-add -l` (rc ∈ {0, 1} = reachable). The preflight check's `ssh_auth_sock` field name is unchanged for downstream consumer compatibility.

- **`hpc-agent setup` now gives a clear error when `~/.claude/skills` exists as a file.** Previously `_install_tree` in `agent_assets.py` called `mkdir(parents=True, exist_ok=True)` without first checking whether the eventual parent (`<claude_dir>/commands` or `<claude_dir>/skills`) existed as a non-directory. On Windows where a 0-byte `~/.claude/skills` file could shadow the intended directory, this surfaced as an opaque `FileExistsError [WinError 183]`. Now raises a `FileExistsError` with the conflicting path, what should be there, and a "move or remove the conflicting file, then re-run" remediation.

### Added — Three internals docs + decision-content drift lint

Three new docs under `docs/internals/`:

- **`parallelization-axes.md`** — the five-axis model (sweep dimensions, scheduling axis, wave structure, stage DAG, DataAxis). Lays out what each axis is for, how it operates, and how they compose at submit time. Explicitly clarifies that DataAxis is NOT the privileged axis — sweep dimensions are.
- **`state-model.md`** — canonical reference for what state files exist, what each contains, which primitives touch them. Per-user state under `~/.claude/hpc/<repo>/`; per-experiment state under `<exp>/.hpc/`. Plus a reverse index mapping each primitive to the files it reads/writes.
- **`submit-sequence.md`** — end-to-end walkthrough from `/submit-hpc` typed in chat to results landing in `aggregated.json`. Traces the slash → skill → bare worker → primitives → cluster pipeline.

Plus a new lint: `scripts/lint_decision_content.py` catches drift between markdown surfaces that paraphrase the same operational content. Marked blocks (`<!-- decision-content:<tag> start -->` ... `<!-- decision-content:<tag> end -->`) must be byte-identical across files; the lint enforces this with normalised whitespace. Currently covers the axis decision tree (shared by `hpc-classify-axis` SKILL.md Step 4b and `/submit-hpc`'s data-axis dialog).

Sibling docs updated:

- `docs/internals/skill-policy.md` — added a section explicitly noting that DataAxis is not the privileged parallelization axis (pointing readers at `parallelization-axes.md`). The framework's primary parallelism comes from user-declared sweep dimensions in `task_generator`; DataAxis is a niche secondary optimization.
- `docs/architecture.md` — added cross-cutting references to the three new internals docs and the `lint_decision_content.py` lint.
- `docs/internals/README.md` — index updated with the three new entries.

### Changed — Axis matcher narrows to Independent + BoundedHalo pattern library

Tightened the autonomous classification scope of
`hpc_agent.experiment_kit.axis_matcher` (and the `classify-axis-easy`
primitive that wraps it). The matcher's autonomous outputs are now
`independent`, `bounded_halo` (via a fixed pattern library), and
`sequential` (the safe default for unrecognized carried state), plus
the error/fallback states `unclassifiable` / `no_loop_detected` /
`function_not_found`. `associative` is no longer detected autonomously
— users who want to parallelize an inner reduction express it as a
sweep dimension in their `task_generator`, and the framework's
existing `combine-wave` machinery handles the map-reduce. The skill's
LLM fallback (Step 4b) still recognizes Associative for the long tail.

The previous rolling-window detector was over-conservative: it flagged
input-slicing patterns like `data[i-W:i]` as `needs_halo_expr`
(BoundedHalo) even when the loop body had no carried state. Such
loops refit a model from scratch each iteration; nothing is carried
output-to-input. They are now correctly classified as `independent`.
The defining characteristic of BoundedHalo is now framed precisely:
iteration N reads iteration N-1's *output* (carried state from prior
iterations' computations), not iteration N reading a window of the
*input* array.

The BoundedHalo pattern library covers five shapes — first-order
stencil (`u[i] = f(u[i-1])`; halo = 1), finite-order stencil
(`u[i] = a*u[i-1] + b*u[i-2]`; halo = K), bounded-window deque
(`deque(maxlen=W)`; halo = W), pandas rolling
(`.rolling(window=W).<agg>()`; halo = W, recognized both inside loops
and as a vectorized op with no explicit loop), and EMA / exponential
smoothing (`state = β*state + (1-β)*x`; halo ≈ `ceil(5/(1-β))` for
literal β, conservative `100` for parameter β). Patterns outside the
library fall back to `sequential` — the framework runs the inner loop
serially, which is safe (just slower).

Wire-shape: the matcher's `MatcherResult` and the `classify-axis-easy`
envelope's `data` now expose `halo_expr` (string in the axis-config
expression syntax) in place of `monoid`. The `kind` enum drops
`associative` and `needs_halo_expr`, and adds `bounded_halo` and
`sequential` as autonomous outputs.

### Added — Hybrid axis classifier (AST pattern-match + LLM fallback)

`hpc-classify-axis` now runs a stdlib-only AST pattern-matcher first
(new `classify-axis-easy` primitive, backed by
`hpc_agent.experiment_kit.axis_matcher`) and only falls through to the
LLM decision tree on `unclassifiable` / `no_loop_detected`. The matcher
recognises the canonical shapes — `functools.reduce` /
`itertools.accumulate`, append-only loops, `acc += x` accumulators, and
`data[i - W : i]` / `data.iloc[i - W : i]` / `data[max(0, i - W) : i]`
rolling windows — and returns a confident `{kind, evidence, monoid?,
tried}` envelope or `unclassifiable`. Conservative by design: an
uncertain match returns `unclassifiable`, never a wrong-but-confident
classification. Handles ~80% of common cases without LLM reasoning,
saving context budget on every cold-start submit. The skill's existing
decision tree is preserved verbatim as the long-tail fallback.

### Changed — Workflow skills return all ambiguities in one envelope

Refined the workflow-skill contract: skills no longer early-return on the
first unresolved field. They walk every resolution step, accumulate
ambiguities into a single `needs_resolution` envelope, and return the
full list in one round-trip. Each ambiguity entry carries:

```json
{
  "field": "<name>",
  "candidates": [...],
  "depends_on": [<dependency fields>],
  "safe_default": <value>
}
```

Callers resolve every entry at once and re-invoke. Bounded by dependency
DAG depth (~3 rounds max for HPC submission), not by ambiguity count.

This subsumes three earlier awkwardness points:

- **The `mode: "interview" | "autonomous"` flag is gone.** Caller-supplied
  fields are always authoritative; the slash interprets ambiguities as
  user dialogs; the autonomous caller (MARs experiment-runner) applies
  `safe_default` to every ambiguity and re-invokes. No mode-dependent
  branches in the skill body.
- **The skill no longer enumerates worker-surfaceable escalation codes.**
  Worker envelopes carry `safe_default` per ambiguity; the skill applies
  it generically. Adding a new escalation type doesn't require a skill
  update.
- **Multi-turn escalation state lives in the augmented spec.** Each
  re-invocation passes the resolved-so-far fields explicitly; there's no
  implicit conversation state to track.

The slash bodies' "On `spec_invalid`" sections become "On
`needs_resolution`" — topo-sort the ambiguities list, walk dialogs in
order, re-invoke once with everything filled.

### Changed — `hpc-status` skips the worker for one-shot snapshots

Refined: the worker-spawn boundary is "more than one LLM-driven step,"
not "every workflow." For `wait_terminal=false` (single primitive call),
the skill calls `hpc-agent status --run-id <id>` directly — no worker
spawn, no context-isolation overhead. For `wait_terminal=true` (blocking
poll), the skill hands off to `hpc-agent run status` so the poll loop's
intermediate state stays in the worker's private context (not the
caller's). Saves substantial overhead for MARs experiment-runners that
poll often.

### Changed — Lint catches slash↔skill input-shape drift

Added a check to `scripts/lint_skill_command_sync.py`: every field the
skill marks as Required in its Inputs table must appear in the slash
body. Catches the silent failure mode where a new required field gets
added to the skill but the slash invocation doesn't get updated.

### Changed — `skill-policy.md` clarifies what each layer decides

Added the experiment-aware vs experiment-agnostic split for decisions:

- **Skills make experiment-aware decisions** — which executor for *this*
  repo, which DataAxis for *this* run's loop, what walltime for *this*
  cmd_sha's runtime priors. The questions depend on the experiment.
- **Workers make experiment-agnostic decisions** — is there an in-flight
  run? is the spec cached? did the canary succeed? Plumbing-level
  branching that doesn't depend on the experiment's content.

Both layers branch; both layers make judgement calls. The split is *what
they decide on*, not *whether they decide*.

Also documented the worker-spawn principle: workflow skills hand off to
a bare worker only when the workflow has more than one LLM-driven step.
Single-step workflows call the primitive directly.

### Added — Workflow-skill layer between slashes and the execution worker

Resurrected the four workflow skills (`hpc-submit`, `hpc-status`,
`hpc-aggregate`, `hpc-campaign`) as the **decision layer** between
the human-elicitation slashes and the deterministic execution worker.
Previously the slashes shelled out directly to `hpc-agent run
<workflow>`; now they invoke the matching workflow skill via the
Skill tool, and the skill resolves decisions before handing off.

The architecture is now three layers, four surfaces:

| Layer | Surface | What it does |
|---|---|---|
| Interview | Slashes (`/submit-hpc`, etc.) | Propose-then-confirm dialogs with the user |
| Decision | Workflow skills (`hpc-submit`, etc.) + sub-skills (`hpc-classify-axis`, etc.) | Resolve every choice point; auto-resolve by default; compose sub-skills |
| Execution | Worker prompts (`worker_prompts/<workflow>.md`) | Deterministic action sequence; no decisions, no prompts |

Two consumers, one execution path:

- **Human**: types `/submit-hpc`; slash conducts the interview, invokes `hpc-submit` skill in `mode: "interview"` with user-resolved fields; skill auto-resolves the rest, shells out to `hpc-agent run submit`.
- **External agent** (MARs experiment-runner, notebook driver, cron worker): invokes `Skill("hpc-submit", { ..., mode: "autonomous" })` directly with whatever it pre-resolved; skill auto-resolves everything else and never returns `needs_human` (autonomous callers can't escalate to a human; the skill picks the most conservative interpretation and proceeds, recording the choice in `decisions`).

Why resurrect: the four workflow skills had existed previously (commit
`04a6290` "slash/skill surgery: separate human surface from agent
surface") but were deleted in `7a39b5e` because the only known
consumer at the time was hpc-agent's own `claude -p --bare` worker,
which has no Skill tool. The deletion missed external Claude agents
(MARs's experiment-runner, future MCP hosts) which DO have Skill
tools and want to delegate the entire HPC pipeline as one skill
invocation rather than orchestrating primitives + sub-skills
themselves. Re-adding the workflow-skill layer gives external agents
the natural entry point.

Files:
- New: `src/slash_commands/skills/hpc-{submit,status,aggregate,campaign}/SKILL.md`
- Rewritten: `src/slash_commands/commands/{submit,monitor,aggregate,campaign}-hpc.md` — slim to interview-only prose; invoke the matching workflow skill via the Skill tool.
- Updated: `scripts/lint_skill_command_sync.py` — `WORKFLOW_PAIRS` repopulated with the four pairs; `WORKFLOW_TRIGGER_SLASHES` emptied (no more thin trigger slashes); `_INVOKE_DIRECTIVE_RE` simplified (paired-skill invocation only).
- Updated: `docs/internals/skill-policy.md` — three-layer / four-surface framing.
- Updated: `docs/architecture.md` — "Agent surfaces" rewritten.

The execution layer (`worker_prompts/<workflow>.md`) is unchanged —
each worker prompt's `cacheable_prefix` snapshot test stays green.

### Changed — Slash surface condensed to four workflow triggers

The user-facing slash surface is now exactly `/submit-hpc`,
`/monitor-hpc`, `/aggregate-hpc`, `/campaign-hpc`. Five slashes
removed: the three paired interview slashes (`/hpc-axes-init`,
`/classify-axis-hpc`, `/wrap-entry-point-hpc`) plus `/setup-hpc` and
`/validate-campaign`. Their behaviors are preserved:

- **Entry-point onboarding / axis classification / axes-init dialogs**
  — moved into `/submit-hpc`'s escalation playbook. The worker
  escalates with `mature_repo_needs_interview`, `axis_unclassified`,
  `no_axes_yaml`, `ambiguous_entry_point`, or `ambiguous_run`; the
  in-chat agent walks the user through the matching dialog and then
  invokes the relevant skill (`hpc-wrap-entry-point`,
  `hpc-classify-axis`, `hpc-build-executor`) via the Skill tool with
  a fully-resolved spec. The skills themselves are unchanged and
  remain callable directly by other agent harnesses (MARs, notebook
  drivers, cron workers).
- **`validate-campaign` findings interpretation** — moved into
  `/campaign-hpc`'s body (severity handling, common-code response
  table, playbook.yaml schema). The `hpc-agent validate-campaign`
  primitive is unchanged; both `submit` and `campaign` workers
  continue to auto-invoke it as a pre-submit static gate.
- **`/setup-hpc`** — replaced by `hpc-agent setup --cluster <name>`
  (already a primitive). Each preflight check's `detail` field gained
  actionable remediation prose so the primitive's output is
  self-explanatory without a slash translating
  (`src/hpc_agent/ops/preflight/check.py`). The optional snapshot-cron
  install (for the LightGBM-residual queue-wait predictor) became a
  proper primitive shipped in an optional plugin — see Added below.

### Added — `hpc-agent setup` surfaces an optional plugin's setup action

`hpc-agent install-cron --ssh-target <target> --experiment-dir <dir>`
installs the wait-predictor crontab entries (snapshot every 5 minutes,
training daily at 03:00) idempotently. Fingerprinted by target module
path so re-running detects existing entries and skips. The primitive
and the three cron-invoked modules (`snapshot_squeue`,
`train_wait_predictor`, `extract_sacct_history`) ship in an optional
plugin, so installing that plugin is sufficient; no editable source
checkout is needed.

`hpc-agent setup` integrates a plugin's setup-time action into its flow:
when a plugin offering one is installed, `setup` surfaces a no-mutation
"available" recommendation, and — with `--install-cron --cluster <name>`
— invokes the action (deriving `ssh_target` from the cluster's
`clusters.yaml` entry) and embeds the result. On a core-only install the
hook is a silent no-op. *(The output shape was later reworked into a
generic `plugin_actions` field — see Unreleased.)*

Pip install itself is unchanged — auto-modifying the user's crontab
during `pip install` would be a footgun (needs user-specific args,
side-effects in CI/Docker). The two-step (install the plugin →
`hpc-agent setup --cluster <name> --install-cron`) is the explicit
form.

`scripts/lint_skill_command_sync.py` updated: `WORKFLOW_PAIRS` is now
empty by design; `SKILL_ONLY_OK` enumerates the three agent-only
skills. Frontmatter validation runs on every skill on disk, not just
paired ones.

### Changed — Skills are agent-autonomous; human elicitation moves to slash commands

hpc-agent has two consumers: humans (via slash commands in the user's
interactive chat) and other agents (e.g. a MARs experiment agent that
calls into hpc-agent without a human in the loop). The prior skill
policy framed skills as "experimenter-intent" surfaces that interview
the user via `[Y/n]` turns — which made every skill un-callable by any
non-chat consumer.

Flipped the model:

- **Skills** (`hpc-build-executor`, `hpc-classify-axis`,
  `hpc-wrap-entry-point`) are now the agent's decision logic.
  Deterministic given inputs. No `[Y/n]` prompts, no "Looks right?"
  turns. Ambiguity that can't be resolved becomes a `spec_invalid`
  envelope (e.g. `ambiguous_entry_point`, `ambiguous_run`) rather than
  a prompt — the caller decides what to do next.
- **Slash commands** (`/classify-axis-hpc`, `/wrap-entry-point-hpc`,
  `/hpc-axes-init`) absorbed the propose-then-confirm dialogs that used
  to live in the skill bodies. Each slash now elicits intent from the
  user and then invokes the paired skill with a fully-resolved spec,
  causing the skill's own elicitation paths to short-circuit.
- `docs/internals/skill-policy.md` rewritten around the two-consumer
  framing. The decision table's "experimenter-intent" column became
  "human-elicitation" (now exclusively the slash's domain); the
  "deterministic" column became "agent-autonomous decision" (now
  exclusively the skill's domain).
- `scripts/lint_skill_command_sync.py` renamed the category enum
  `experimenter-intent` → `agent-autonomous`; the skill's frontmatter
  `category:` field now witnesses this.

No wire-surface changes — the underlying primitives (`build-executor`,
`classify-axis`, `interview`) accept the same specs as before. The flip
is in the agent-facing markdown (skills + slashes + policy doc) only.

### Added — Explicit plugin overlay manifest

Plugins now self-declare their overlay contributions via a top-level
`MANIFEST = PluginManifest(...)`. Pre-Item-5, the overlay surface
(whether a plugin overrides a host worker-prompt procedure, whether
it registers CLI subcommands, what primitive names it claims) was an
implicit consequence of attribute-existence checks against the entry
point object — readers couldn't tell from a glance what the plugin
actually changes about the host.

`PluginManifest` (new, in `src/hpc_agent/_wire/plugin_manifest.py`)
carries `name`, `version`, `primitives`, `worker_prompt_overlays`,
and `cli_register`. The host's `hpc_agent.capabilities` envelope now
includes a `plugins` field projecting every loaded plugin's
manifest. A new pre-commit + CI gate
(`scripts/lint_plugin_manifests.py`) reconciles each manifest's
declarations against runtime reality (every declared primitive must
register at import time, every declared overlay must exist on disk,
the `cli_register` flag must match whether `register_cli` is
exposed).

The optional plugin declares its manifest at
its `plugin.py:MANIFEST` (14 primitives, overlays the `submit` worker
prompt, registers a CLI subgroup). The test helper
`tests/_registry_helpers.py:plugin_overlaid_workflows()` reads every
loaded plugin's manifest `worker_prompt_overlays` so the snapshot test
in `tests/worker_prompts/test_prefix_snapshot.py` no longer needs a
parallel hardcoded allowlist.

Plugins without a manifest still load — Item 5 ships the manifest as
informational metadata, not a hard requirement on first release —
but the loader emits a `DeprecationWarning` and the catalog projects
nothing for them.

### Changed — `hpc_agent` root namespace trim (one-release deprecation)

The root `__all__` was trimmed from 52 names to 15 — the integrator
surface enumerated in `docs/reference/boundary-contract.md`. The
remaining 37 names (per-run sidecars, remote execution, status /
reduce helpers, GPU selection, discovery, constraints, throughput,
smart-submit data layer, resubmit batching, per-task metrics writer)
are still importable from the root via a `__getattr__` deprecation
shim that emits a `DeprecationWarning` pointing at the canonical
home. The shim survives one release; a future PR will drop it.

External callers should migrate `from hpc_agent import X` →
`from hpc_agent.<canonical>.<module> import X`. The move table:

| Moved | Canonical home |
|---|---|
| `MAX_RUNS`, `SIDECAR_SCHEMA_VERSION`, `compute_cmd_sha`, `compute_tasks_py_sha`, `find_existing_runs`, `find_run_by_cmd_sha`, `prune_old_runs`, `read_run_sidecar`, `run_sidecar_path`, `write_run_sidecar` | `hpc_agent.state.runs` |
| `ssh_run`, `rsync_push`, `rsync_pull`, `deploy_runtime`, `run_combiner`, `run_combiner_checked` | `hpc_agent.infra.remote` |
| `check_results`, `check_results_from_tasks`, `report_status`, `report_status_from_tasks`, `rollup_by_grid_point`, `detect_scheduler` | `hpc_agent.execution.mapreduce.reduce.status` |
| `pick_gpu` | `hpc_agent.infra.gpu` |
| `reduce_metrics`, `reduce_by_grid_point`, `reduce_partials`, `reduce_resource_usage` | `hpc_agent.execution.mapreduce.reduce.metrics` |
| `classify_failure` | `hpc_agent.execution.mapreduce.reduce.classify` |
| `ExecutorInfo`, `discover_executors`, `is_executor_source` | `hpc_agent.state.discover` |
| `ClusterConstraints`, `parse_constraints` | `hpc_agent.infra.constraints` |
| `WorkloadSpec`, `SubmissionPlan`, `compute_submission_plan`, `build_wave_map` | `hpc_agent.infra.throughput` |
| `inspect_cluster` | `hpc_agent.infra.inspect` |
| `append_runtime_sample`, `roll_up_runtime_quantiles` | `hpc_agent.state.runtime_prior` (as `append_sample`, `roll_up_quantiles`) |
| `compact_task_ids`, `ResubmitBatch`, `ResubmitPlan`, `resubmit_plan` | `hpc_agent.ops.recover.batching` |
| `write_metrics` | `hpc_agent.execution.mapreduce.metrics_io` |

The 15 names retained at root: `_PACKAGE_ROOT`, `__version__`,
`RepoLayout`, `JournalLayout`, `get_template_path`,
`load_clusters_config`, `RUNS_SUBDIR`, `TASKS_FILENAME`,
`load_tasks_module`, `PrimitiveMeta`, `SideEffect`, `get_meta`,
`get_registry`, `primitive`, `register_primitives`.

### Changed — `hpc_agent.incorporation.template` → `hpc_agent.experiment_kit`

The researcher-facing notebook/parallelization surface (`@register_run`,
`DataAxis`, `plan_tasks`, `load_series`, `check_elision`, `Monoid`,
`save_artifact`, `export_notebook`, `discover_runs`, …) moved out from
under the architectural `incorporation/` namespace into its own
top-level package, `hpc_agent.experiment_kit`. Burying the user-
facing layer inside the framework scaffolding directory obscured it;
the new name advertises what it is.

Framework-internal scaffolding (`incorporation/build/`,
`axes_init.py`, `classify_axis.py`, `export_package.py`) stays under
`incorporation/`. The nine `.tmpl` files that `build-template`
injects into a target repo moved from
`incorporation/template/scaffold/*.tmpl` to
`incorporation/build/scaffolds/*.tmpl` so they live next to the
framework code that injects them (`build/template.py`) rather than
mixed in with researcher-facing modules.

The `experiment.ipynb.tmpl` lookup inside the injected
`.hpc/scaffold.py` and inside `build/template.py` itself was repointed
at the new path. The exported notebook runtime inliner
(`experiment_kit/notebook.py`) reads its own
`_runtime.py` via `Path(__file__).resolve().parent` — no path
constant to keep in sync.

A back-compat shim at `hpc_agent/incorporation/template/__init__.py`
re-exports the new package and emits a `DeprecationWarning`. External
callers should switch to `from hpc_agent.experiment_kit import ...`;
the shim will be removed in a future release.

### Changed — Tier-3 CLI verbs folded into the primitive registry

`capabilities`, `install-commands`, `setup`, and `describe` used to be
hand-written Tier-3 adapters wired by `cli/setup.py:register`. Each
now carries an `@primitive` decorator and is picked up by the
registry-walking parser at `cli/parser.py:_register_from_registry`.
The user-visible CLI surface is unchanged — same flags, same envelope
shapes, same exit codes — but the four verbs now appear in the
operations catalog (`hpc-agent capabilities`'s `operations` field, the
baked `operations.json`, the auto-generated frontmatter under
`docs/primitives/`) and in introspection tooling that walks
`get_registry()`. New primitive doc pages: `describe.md`,
`install-commands.md`, `setup.md` (the existing `capabilities.md` is
refreshed). `run` remains the lone Tier-3 verb — its semantics are
spawning a worker process, not invoking a primitive body.

`cli/setup.py:register` is retained as a no-op back-compat shim — the
registry walk picks up the four verbs from their decorators.

### Removed — `hpc_agent.runner` cross-subject re-export bridge (BREAKING)

`src/hpc_agent/runner.py` and `scripts/lint_runner_shim.py` are gone.
The bridge existed so an atom in one subject could call a primitive
from another by routing through the package root; once every such seam
was either pulled into a workflow file at the `ops/` or `meta/` role
root (workflows are exempt from the subject-imports lint) or extracted
to `infra/`, the shim was carrying no live callers. The pre-commit
hook (`lint-runner-shim`) and CI job that policed its contents are
removed in the same change, along with the `runner.py` block in
`docs/architecture.md` and the `hpc_agent.runner.*` cross-references
sprinkled through docstrings, `docs/internals/sync-checklist.md`,
`docs/reference/boundary-contract.md`, and the per-subject READMEs.

External integrators importing `from hpc_agent.runner import X` (or
`from hpc_agent import runner` + `runner.X`) must switch to the
canonical home:

| `hpc_agent.runner` re-export | Canonical home |
|---|---|
| `combine_wave` | `hpc_agent.ops.aggregate.combine.combine_wave` |
| `mark_terminal` | `hpc_agent.ops.monitor.reconcile.mark_terminal` |
| `reconcile` | `hpc_agent.ops.monitor.reconcile.reconcile` |
| `record_status` | `hpc_agent.ops.monitor.status.record_status` |
| `resubmit_failed` | `hpc_agent.ops.recover.runner.resubmit_failed` |
| `submit_and_record` | `hpc_agent.ops.submit.runner.submit_and_record` |
| `validate_executor_signatures` | `hpc_agent.ops.validate.executor_signatures.validate_executor_signatures` |
| `validate_input_dataset` | `hpc_agent.ops.validate.input_dataset.validate_input_dataset` |
| `validate_stochastic_marker` | `hpc_agent.ops.validate.stochastic_marker.validate_stochastic_marker` |
| `validate_walltime_against_history` | `hpc_agent.ops.validate.walltime_against_history.validate_walltime_against_history` |

## 0.6.0 — 2026-05-24

### Removed — `hpc_agent.agent_cli` back-compat shim (BREAKING)

The CLI orchestrator and per-domain ``cmd_*`` adapters moved out of
``hpc_agent/agent_cli.py`` during the PR-5c decomposition into
``hpc_agent.cli.<domain>``. The original module was kept as a re-export
shim so the optional plugin and a handful of legacy import
sites kept working. 0.6.0 deletes the shim.

External integrators using ``from hpc_agent.agent_cli import X`` (or
``from hpc_agent import agent_cli`` + ``agent_cli.X``) need to import
from the canonical submodule directly. Mapping:

- ``_EXIT_CODE_BY_CATEGORY``, ``EXIT_CLUSTER_ERROR``, ``EXIT_INTERNAL``,
  ``EXIT_OK``, ``EXIT_USER_ERROR``, ``_add_experiment_dir``,
  ``_add_run_id``, ``_add_spec_and_dry_run``, ``_emit``, ``_err``,
  ``_err_from_hpc``, ``_load_spec``, ``_meta_idempotent``, ``_ok``,
  ``_require_ssh_agent``, ``_validate_against_schema``
  → ``hpc_agent.cli._helpers``
- ``cmd_aggregate``
  → ``hpc_agent.cli.aggregate``
- ``_VERB_GROUPS``, ``_live_subcommands``, ``_print_group_help``,
  ``_strip_verb_group``, ``build_parser``, ``cmd_logs``, ``main``
  → ``hpc_agent.cli.dispatch``
- ``_preempted_summary_from_sidecar``, ``cmd_status``
  → ``hpc_agent.cli.lifecycle``
- ``_VALID_RESUBMIT_CATEGORIES``, ``cmd_resubmit``
  → ``hpc_agent.cli.recover``
- ``cmd_capabilities``, ``cmd_describe``, ``cmd_install_commands``,
  ``cmd_setup``
  → ``hpc_agent.cli.setup``
- ``cmd_run``
  → ``hpc_agent.cli.spawn``
- ``cmd_submit``, ``cmd_submit_flow``, ``cmd_submit_flow_batch``
  → ``hpc_agent.cli.submit``
- ``_last_status_age_seconds``
  → ``hpc_agent.ops.monitor.list_in_flight``

The ``hpc-agent`` console-script entry point (``pyproject.toml``)
already targets ``hpc_agent.cli.dispatch:main`` — no change there.

### Removed — `hpc_agent.state.session` back-compat barrel (BREAKING)

The Wave-4 reorg split `hpc_agent.state.session` into three canonical
submodules: `state.run_record`, `state.journal`, `state.index`. The
barrel was kept as a re-export shim through 0.5.0 so 60+ legacy import
sites kept working. 0.6.0 deletes the barrel.

External integrators using `from hpc_agent.state import session` (or
`from hpc_agent.state.session import RunRecord`, etc.) need to import
from the canonical submodule directly. Mapping:

- `RunRecord`, `HPC_HOMEDIR`, `SCHEMA_VERSION`, `TERMINAL_STATUSES`,
  `journal_dir`, `repo_hash`, `runs_dir`, `_atomic_write_json`,
  `_lock_path`, `_locked`, `_read_json`, `_run_path`, `_UPDATABLE_FIELDS`
  → `hpc_agent.state.run_record`
- `load_run`, `mark_run`, `update_run_record`, `update_run_status`,
  `upsert_run`, `_refresh_index_entry`
  → `hpc_agent.state.journal`
- `find_in_flight_runs`, `find_runs_by_campaign`, `prune_terminal_runs`,
  `_all_run_files`, `_index_is_stale`, `_read_index`, `_rebuild_index`
  → `hpc_agent.state.index`

### Removed — `hpc_agent.runner` legacy helper re-exports

Eleven helpers re-exported through `hpc_agent.runner` for back-compat
have been dropped. Functions still exist at their canonical homes;
update imports to point there:

- `annotate_clusters_with_retry_advice`, `fingerprint_stderr_tail`,
  `cluster_failures_by_fingerprint`, `DEFAULT_AUTO_RETRY_POLICY`
  → `hpc_agent.ops.recover.runner_failures`
- `build_provenance`, `verify_combiner_artifact`,
  `verify_per_task_outputs`, `write_remote_provenance`
  → `hpc_agent.ops.aggregate.runner`
- `derive_resubmit_request_id`
  → `hpc_agent.ops.recover.runner`
- `fetch_task_logs`
  → `hpc_agent.infra.cluster_logs`
- `build_job_env`
  → `hpc_agent.ops.submit.runner`

The `_BACK_COMPAT_NONPRIMITIVES` allow-list in
`scripts/lint_runner_shim.py` is also removed: `runner.py` now
re-exports only `@primitive`-decorated symbols, full stop.

### Changed — workflows-at-root structural move

Each workflow file moves from `ops/<subject>/flow.py` to
`ops/<subject>_flow.py` at the role root, alongside subject
directories. Files at role root aren't subjects per the existing
subject-imports lint (`parts < 2` short-circuits to `None`), so the
existing rule handles them naturally.

Moves (importer paths change, no behavior change):

- `hpc_agent.ops.aggregate.flow` → `hpc_agent.ops.aggregate_flow`
- `hpc_agent.ops.monitor.flow` → `hpc_agent.ops.monitor_flow`
- `hpc_agent.ops.submit.flow` → `hpc_agent.ops.submit_flow`
- `hpc_agent.ops.recover.flow` → `hpc_agent.ops.recover_flow`
- `hpc_agent.ops.aggregate.canary_verify` → `hpc_agent.ops.verify_canary`
- `hpc_agent.meta.campaign.validate` → `hpc_agent.meta.validate_campaign`

Five cross-subject seams that previously routed through
`hpc_agent.runner` (`monitor_flow.combine_wave`,
`validate_campaign.validate_*`) become direct imports — workflows at
role root can reach into subject dirs without crossing the lint.

### Added — `submit-and-verify` workflow

Composes `submit-flow` + `verify-canary` under one envelope. Submits a
run plus its 1-task canary, then waits for the canary to land terminal
before returning, so the caller branches once on `verified` instead of
orchestrating both halves. CLI: `hpc-agent submit-and-verify --spec
<path>`. Wire shape:
[`submit_and_verify.input.json`](src/hpc_agent/schemas/submit_and_verify.input.json),
[`submit_and_verify.output.json`](src/hpc_agent/schemas/submit_and_verify.output.json).

### Added — `recommend-partition` surfaced agent-facing

The function existed at `ops/submit/recommend_partition.py` with
non-trivial routing logic (priority tiers, debug-partition rules,
walltime-fit ranking), tests, a Pydantic spec/result, schemas — but
no agents could reach it. Flipped `agent_facing=False` → `True`, added
a `CliShape` so `hpc-agent recommend-partition --spec <path>` is now
a callable verb. Pre-submit advisor: agents call it standalone to
decide what to put in a submit spec's `partition` field.

### Added — plugin demos cross-package composition

Four new primitives in the optional plugin exercise the
plugin-composes-core path:

- `plan-resubmit-overrides` (query) — promotes
  `plan_resubmit_overrides` to a wire-callable primitive.
- `smart-resubmit-flow` (workflow) — composes
  `plan-resubmit-overrides` (plugin) + `resubmit-failed` (core); proves
  the cross-package compose path via lazy resolution against the
  merged registry.
- `apply-smart-submit-plan` (workflow) — code-ifies Step 4c-B of
  the plugin's `submit.md`: applies auto-pick + auto-apply rules from a
  `score-submit-plan` envelope, surfaces `walltime_split_confirm`
  as a pending decision when applicable.
- `run-pre-submit-gates` (workflow) — chains `check-preflight` +
  `validate-campaign` + `predict-start-time` under one envelope;
  short-circuits on the first failure.

### Changed — documentation refresh

- `docs/architecture.md` "Cross-subject composition" section rewritten
  to reflect the post-workflows-at-root state. Stale
  `runner.py` re-export table removed; non-goals subsection
  added.
- `docs/reference/config-precedence.md`: two `state/session.py`
  references updated to canonical homes.

## 0.5.0 — 2026-05-24

### Removed — deprecated `RepoLayout` forwarders

The 0.2.0-vintage forwarders carried an explicit "Remove in 0.4.0"
deprecation note that the 0.4.0 cut missed:

- `hpc_agent.framework_subdir(experiment_dir)` →
  `hpc_agent._kernel.contract.layout.RepoLayout(experiment_dir).hpc`
- `hpc_agent.runs_subdir(experiment_dir)` →
  `RepoLayout(experiment_dir).runs`
- `hpc_agent.tasks_path(experiment_dir)` →
  `RepoLayout(experiment_dir).tasks`

All three removed from `hpc_agent.__all__`. The boundary contract
(`docs/reference/boundary-contract.md`) is updated to recommend
`RepoLayout` directly. External integrators still importing these
names need a one-line switch to `RepoLayout`.

### Added — fresh-context recovery and headless campaigns

A step that lost its conversational memory (a subagent, a restarted
session, a cron tick) can now rebuild the workflow picture from disk
alone instead of trusting context that may be gone.

- **`load-context` primitive** — reconstructs the on-disk workflow
  context for an experiment (`hpc-agent load-context --experiment-dir
  <path>`): the latest run's v2 config snapshot, in-flight journal
  records, campaigns with their cursor iteration, a coarse
  `next_step_hint`, and non-fatal `warnings`. Pure read — no SSH, no
  scheduler, no writes. Carries a generated output schema
  (`load_context.output.json`). Multi-step skills now open with this
  call instead of caching run/campaign/cluster state in memory.
- **`delegate` block on `load-context`** — describes the next workflow
  step as a delegable unit of work: `kind` (`cli` for a deterministic
  monitor/aggregate step, `agent` for a judgement step), `step`,
  `run_id`, `campaign_id`, `experiment_dir`, `reason`, and a
  ready-to-hand-off `prompt`. One contract shared by an in-session
  orchestrator and the headless campaign driver. When a campaign is
  idle (no runs in flight), `load-context` emits
  `next_step_hint: "decide"` and a `kind="agent"` `decide` delegate
  carrying the campaign to advance.
- **`hpc_agent.meta.campaign.driver` — headless campaign driver** — advances
  exactly one campaign workflow step per invocation off the
  `load-context` `delegate` block. Installed as the `hpc-campaign-driver`
  console script (equivalently `python -m hpc_agent.meta.campaign.driver`).
  `kind: "cli"` steps run the matching `hpc-agent` verb directly with no
  LLM; `kind: "agent"` steps shell `claude -p` only when
  `--allow-agent-steps` is passed. Idempotent and cron-friendly — wrap
  it in cron or `/loop` to walk an unattended campaign.

### Changed — internal package reorganization (wire-stable)

The framework's internal layout has been reorganized into a layered
DAG of self-contained subjects under `ops/`, `meta/`, and `models/`,
with cross-cutting substrate at `infra/` and `state/` and the
framework kernel under `_kernel/`. The wire surface (CLI verbs,
envelope shapes, primitive names, JSON schemas) is **unchanged** —
every `hpc-agent <verb>` and every envelope key keeps working
identically. Internal Python import paths have moved; external
integrators that `from hpc_agent.X import Y` should consult the
new layout (`docs/architecture.md`).

Highlights for plugin authors and external integrators:

- **`hpc_agent._schema_models/` → `hpc_agent._wire/`** — Pydantic
  models renamed; subpath structure preserved verbatim.
- **`hpc_agent._internal/{primitive,operations,…}` → `hpc_agent._kernel/`** —
  registry / contract / lifecycle / extension modules grouped under
  the new kernel package. `_internal/{time,io}` moved to
  `hpc_agent.infra.{time,io}`.
- **`hpc_agent.atoms/`, `hpc_agent.flows/`, `hpc_agent.runner/`
  (package), `hpc_agent.planning/`** — deleted. Their contents moved
  into the matching subject under `ops/` (e.g. `flows/submit_flow.py`
  → `ops/submit/flow.py`) or to `infra/` / `state/` where they were
  helper-shaped. `hpc_agent.runner` survives as a single-file
  package-root module re-exporting the previous public surface for
  back-compat callers AND serving as the canonical bridge for
  cross-subject primitive calls.
- **`hpc_agent.campaign` → `hpc_agent.meta.campaign`**, including
  the `hpc-campaign-driver` console script entry point.
- **`hpc_agent.mapreduce` → `hpc_agent.execution.mapreduce`**.
- **`hpc_agent.worker_prompts` → `hpc_agent._kernel.extension.worker_prompts`**.
- **`hpc_agent._internal.session` → `hpc_agent.state.session`**
  (back-compat barrel re-exporting submodules `journal`, `run_record`,
  `index`).
- **New cross-subject discipline**: subjects under `ops/` and `meta/`
  may not import from each other directly. Helper-shaped sharing goes
  to `infra/`; cross-subject primitive *calls* route through
  `hpc_agent.runner`. The `@primitive(composes=[...])` parameter now
  accepts string primitive names (resolved via the registry at
  registration time), eliminating the need to import a callable just
  for declarative composition. CI enforces the rule via
  `scripts/lint_subject_imports.py` (no allow-list — every cross-
  subject reach is rejected).

The optional plugin is updated in lockstep with the reorg
(see its own changelog); no version pin changes are required
on the host.

### Changed — precondition gates on `monitor-flow` / `aggregate-flow`

- **`precondition_failed` error code** — `monitor-flow` and
  `aggregate-flow` now reject a run that is not in a valid state for the
  step with a structured `precondition_failed` envelope
  (`errors.PreconditionFailed`) instead of failing deep in the workflow.
- **Behavior change — `aggregate-flow` rejects a non-terminal run.**
  `aggregate-flow` now fails with `precondition_failed` when invoked on a
  run that has not reached a terminal state, unless
  `ensure_all_combined=false` is passed in the spec. Callers that
  intentionally aggregate a still-running run must opt out explicitly.

### Removed — `/monitor-hpc` exit contract and Stop-hook subsystem

- **`/monitor-hpc` `armed:` exit contract removed.** `/monitor-hpc` no
  longer has to emit a final `armed: <cron|loop|none> run_id=... cadence=...
  reason=...` line of stdout, and the `monitor_armed_check` Stop hook that
  blocked the agent from finishing without it is gone. The exact-string
  contract was fragile; self-scheduling now runs as a cron tick of the
  headless `hpc-campaign-driver` — each tick is a fresh process, so no
  exit contract is needed.
- **`hooks/` package and `hpc-agent hook-install` removed.** With
  `monitor-armed` gone, the hook-install framework had nothing left to
  manage, so the whole `hpc_agent.hooks` package and the `hook-install`
  CLI subcommand are deleted (`hpc-agent setup` no longer wires hooks;
  `--no-hooks` is gone). For monitoring that outlives the chat, schedule
  a cron (or `/loop`) that runs `hpc-campaign-driver` or re-invokes
  `/monitor-hpc`.
- **`decide-monitor-arm` retained, `armed_line` output dropped.** The
  primitive still picks the cron/loop/none arm mode + cadence + cron
  schedule + `cron_create_args` — its cadence-by-run-state table is
  reusable for choosing a cron interval for the driver — but the
  contract-specific `armed_line` field is removed from its output.

## 0.4.0 — 2026-05-21

### Added — interview-time `DataAxis` classification

A `@register_run` notebook now carries its parallel-decomposition
classification in `axes.yaml`, recorded once and reused across submits.

- **`axes.yaml` schema v2** (additive — every v1 file still validates):
  an optional `executors` block maps each `@register_run` function to
  its classified `DataAxis` (`independent` / `associative` /
  `bounded_halo` / `sequential`), the run's signature hash, and
  classification provenance.
- **`classify-axis` primitive** — records a resolved `DataAxis` into
  `axes.yaml`'s `executors` block (`hpc-agent classify-axis --spec`).
- **`hpc-classify-axis` skill** + `/classify-axis-hpc` command — the
  proposes-then-confirms classification interview.
- **`hpc_agent.template.axis_config`** — `data_axis_from_config` /
  `config_from_data_axis` (de)serialize a `DataAxis`; halo expressions
  are evaluated by a restricted-AST interpreter, never `eval()`.
- `RunInfo.run_signature_sha` — a stable signature fingerprint;
  `/submit-hpc` reuses a stored classification only while it matches.
- `recall` surfaces prior classifications (`data_axes`,
  `data_axis_kinds`) so a new interview pre-fills from similar past
  experiments.

### Added — submit-time build (`export-package`)

The experiment repo no longer commits generated code.

- **`export-package` primitive** — builds the `src/` package from
  `notebooks/{pipeline,executors,scripts}/*.ipynb` at submit / CI /
  repro time; convention-driven, content-hash cached, exporter
  auto-picked (strict-AST for executors, `# export`-marker for pipeline
  libraries).
- `export_notebook_markers` / `notebook_imports_runtime` added to
  `hpc_agent.template`.
- Scaffold templates flipped to a `.gitignore`d generated set (`src/`,
  `.hpc/tasks.py`, `.hpc/cli.py`, `.hpc/.build-cache.json`); CI builds
  first then runs lint / type-check / the serial-elision gate on the
  built output; `conftest.py` rebuilds `src/` on a fresh clone.

## 0.3.0 — 2026-05-20

### Removed — advisory/forecasting layer extracted to an optional plugin

The queue-wait forecasting and submit-planning layer is no longer part
of the `hpc-agent` package. Gone from the CLI: `plan-submit`,
`validate`, `inspect-cluster`, `runtime-prior`, `predict-start-time`,
`predict-queue-wait`, `best-submit-window`, `walltime-drift`,
`house-edge`, and `recommend-wait-alternative` — along with the
`forecast/` package, the submit planner, walltime arbitrage, and the
resubmit auto-right-sizer. `resubmit` now applies caller-supplied
resource overrides verbatim instead of computing them.

This is a breaking change to the CLI surface. The capability is
repackaged as an optional plugin discovered through the new
`hpc_agent.plugins` entry-point group; installing that plugin restores
every command above. The public package keeps the job-execution
surface (submit / monitor / aggregate / campaign / resubmit) and the
`inspect_cluster` / `roll_up_runtime_quantiles` library functions.

### Added — `plan-throughput` primitive

`hpc-agent plan-throughput --cluster <name> --total-tasks <n> [--est-task-duration-s <n>]`
— a pure-local `query` primitive that packs a task grid into
concurrency-bounded submission waves. It reads the cluster's scheduler
constraints from `clusters.yaml`, and returns the wave plan plus the
`wave_map` the per-run sidecar carries for the cluster-side combiner. It
is the deterministic core that `/submit-hpc` Step 4b previously did as
inline library calls (`compute_submission_plan` + `build_wave_map`);
Step 4b is now a single `invoke plan-throughput` step.

### Added — `hpc_agent.template`: experiment + parallelization layer

A new opt-in subpackage so a researcher can bring a notebook (or a
plain `run()` function) and have hpc-agent — not the experiment repo —
own parallelization. The core stays experiment-agnostic; nothing here
is on the default code path. Stdlib-only throughout.

Layer 1 — notebook / CLI helpers:

- `register_run` — decorator that marks an experiment entry point and
  injects a `compute(args)` wrapper (satisfying the executor contract)
  plus a module-level `_RUNS` registry. The cluster-runtime surface it
  needs (`register_run`, `compute`, `load_series`, `save_artifact`)
  lives in one self-contained, stdlib-only module, `_runtime`.
- `save_artifact(name, obj)` — write a large artifact under the
  per-task output directory (CWD fallback for local smoke tests).
- `export_notebook(ipynb, out_py)` — lift the importable surface of a
  `.ipynb` into a `.py` executor via a strict AST allowlist (imports,
  defs, classes, UPPERCASE-target assignments; everything else
  dropped). A `@register_run` notebook exports to a *self-contained*
  executor: the `hpc_agent.template` import is dropped and the
  stdlib-only `_runtime` source inlined verbatim, so the executor runs
  on a stdlib-only cluster with no `hpc-agent` install — the same
  inlining `.hpc/cli.py` does for `Flag`.
- `discover_runs(src_dir)` — find `@register_run` functions by AST
  walk, resolving bare / aliased / attribute decorator forms without
  importing the experiment's heavy dependencies.
- `flags_from_signature` / `flags_from_ast` / `flags_for_run` — the
  type → `Flag` mapping (`bool` → store-true, `X | None` → optional,
  `list[T]` → `nargs="+"`, `Literal[...]` → `choices`).

Layer 2 — parallelization planner:

- `DataAxis` cases — `Independent`, `Associative(monoid)`,
  `BoundedHalo(halo_fn)`, `Sequential` — classifying a series axis by
  whether it carries state and whether that state is associative.
- `plan_tasks(sweep, data_axis, chunks=, series_length=)` — applies
  the strategy and returns a `total()` / `resolve()` object for
  `.hpc/tasks.py`.
- `load_series(name)` — the halo-aware loader: the single seam that
  hands each task its slice without the experiment knowing it was
  chunked. `set_series_loader` / `current_slice` / `trim_emission`.
- `Monoid` / `Moments` / `SUM` / `MOMENTS` and `reduce_monoid` —
  monoid-reduce glue; non-associative aggregates (variance, Sharpe,
  QLIKE) reduce via sufficient statistics.
- `check_elision` / `assert_elision_equivalent` — the serial-elision
  harness: run an experiment whole and split N ways, assert equality.
  The backstop that makes automated `DataAxis` inference safe — wire
  it as a required CI gate.

`hpc_agent.executor_cli.Flag` / `flag()` gain an optional `action`
field (e.g. `store_true`) so boolean flags map cleanly; the inlined
copy in `cli_dispatcher.py` is kept in lock-step.

### Added — agent inference + `build-template` scaffold injection

- `/submit-hpc` (`skills/hpc-submit/SKILL.md`) — Step 3 now classifies
  an experiment's series axis as a `DataAxis` from a read of `run()`
  and its call graph: detect a series loop, classify it, gate on the
  serial-elision check. Default to `Sequential` on any uncertainty,
  bias halos large.
- `build-tasks-py` gains a planner mode — a `data_axis` field on the
  spec (`{kind, chunks, series_length, halo_expr?, monoid?}`) makes the
  primitive run `plan_tasks` at scaffold time and bake the resolved task
  list into a `_TASKS` literal. The generated `.hpc/tasks.py` imports
  only `executor_cli` — the same footprint as a cartesian one, so it
  loads inside the stdlib-only cluster dispatcher. The agent
  classifies; it never hand-writes `tasks.py`. `halo_expr` is validated
  to arithmetic-only over `params`.
- `build-template` — a new human-facing CLI command
  (`hpc-agent build-template [--repo-dir <dir>] [--force]`) that injects
  the experiment-template into a repo: `.hpc/template.mk` and
  `.hpc/scaffold.py` (framework-owned, re-injected every run,
  self-healing) plus the root files `Makefile`,
  `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `conftest.py`,
  and `pyproject.toml` (refuse-without-`--force`, with non-destructive
  `Makefile` / `pyproject.toml` handling). The scaffold lives inside
  hpc-agent — there is no separate template repo to clone. It is
  deliberately *not* a wire primitive: the experiment-template flow is
  built around researcher-authored notebooks, so it is exclusive to the
  human CLI entry point and absent from the integrator-agnostic
  primitive catalog headless orchestrators compose against.

### Removed (experiment-shaped surface that moved out to the caller)

Per the cleavage: hpc-agent owns the parallelization scaffolding;
the caller owns the experiment-specific layout, the experiment-type
vocabulary, and any meta-file enrichment. Net effect: hpc-agent no
longer reads or enriches anyone else's experiment-context file.

- `hpc_agent.state.discover.detect_experiment_tier()` — inferred
  Tier-1 (`probes/probe-*/probe.py`) vs Tier-2 (`runs/run-*/scripts/`)
  from path layout. Integrators that adopt that convention now
  detect the tier themselves and dispatch accordingly.
- `hpc_agent.state.discover.read_meta_json()` — read
  `<experiment-dir>/meta.json` as a dict. Two-line stdlib helper;
  callers reproduce it on their side.
- `agent_cli._build_meta_block()` and the `data.meta` field on the
  `hpc-agent discover` envelope — hpc-agent no longer enriches the
  discover envelope with `experiment_id` / `seed` / `purpose` /
  `tier` from `meta.json`. The envelope now returns just
  `data.executors`. Callers that want experiment context add it
  client-side.
- `agent_cli._overlay_meta_on_spec()` and the `hpc-agent submit
  --from-meta` flag — hpc-agent no longer overlays missing
  `profile` / `job_name` on the submit spec from
  `meta.json::experiment_id`. Callers populate the spec themselves
  before calling `submit`.
- The auto-narrow-to-`scripts/`-when-meta.json-present heuristic in
  `discover_executors`. The scanner now always walks
  `executors/` / `scripts/` / `src/` by default; callers that want a
  tighter scan pass `search_dirs=("scripts",)` explicitly.
- `tests/state/test_meta_json_layout.py` — covered the removed
  behavior.
- `TestSubmitFromMeta` class in `tests/cli/test_submit.py`.

Wire-level impact: MARs at the pinned re-pin commit (`ec041c6`) is
unaffected — `hpc-agent discover` is not in its listed dep-surface
subcommand set, and the `--from-meta` flag was never in MARs's listed
flag set. Other integrators that happened to consume these surfaces
need to reproduce the (small) logic on their side; see the migration
notes below.

### Added

- **`docs/integrations/CONTRACT.md`** — integrator-agnostic reference
  for the wire surface external agent harnesses compose against. Covers
  the spawn env block, the
  `find-prior-run` → `submit` → `monitor-summary` →
  `verify-aggregation-complete` workflow, the `error_code` → retry
  policy table, the `.hpc/tasks.py` boundary, the executor import
  allowlist, the dispatcher-side env vars, and the `lifecycle_state`
  values.
- **`hpc_agent.integration` constants module** — `RESULT_DIR_ENV`,
  `HPC_KW_PREFIX`, `LOCAL_DATA_DIR_ENV`, `JOURNAL_DIR_ENV`,
  `CLUSTERS_CONFIG_ENV`, `LIFECYCLE_STATES`, `ERROR_CODES`.
  Integrators import these instead of carrying string literals that
  drift.
- **`hpc-agent clusters describe <name> --strict`** — surfaces
  `clusters.yaml` keys not recognized by `ClusterConfig` under
  `data.unknown_keys`. Opt-in only — `ClusterConfig` itself stays
  `extra="ignore"` for back-compat (flipping the default would break
  every existing user's `clusters.yaml`).
- **Executor import-boundary allowlist** now includes
  `hpc_agent.executor_cli` alongside `hpc_agent.mapreduce.metrics_io`.
  The canonical `tasks_example.py` template already required this; the
  doc and lint test had drifted.

### Changed

- **README quick-start** is now explicit that the six slash commands
  (`/preflight`, `/submit-hpc`, `/monitor-hpc`, `/aggregate-hpc`,
  `/campaign-hpc`, `/hpc-axes-init`) are installed by `/setup_hpc` from
  templates under `src/slash_commands/commands/`. Previously the
  README implied they were available out of the box.
- **`docs/reference/python-api-contract.md`** corrected:
  `hpc_agent.state.runtime_prior.summarize` was a phantom — the real
  symbol is `roll_up_quantiles`.
- **Narrative docs and schema descriptions** genericized: references
  to a specific integrator are replaced with integrator-agnostic
  language.
- **Identifier and wire-field renames** to lift the legacy integrator
  name out of every surface. The private Python renames landed first;
  the wire fields followed once MARs migrated to the post-cleavage
  shape (its `mars_hpc.py` adapter no longer reads
  `data.mars_skill_paths` or writes `produced_by.kind == "mars"`):

  | Old name | New name | Surface |
  |---|---|---|
  | `hpc_agent.state.discover.detect_mars_tier` | `detect_experiment_tier` | Python (`__all__`) |
  | `_MARS_SKILL_NAMES` | `_SKILL_NAMES` | private constant in `atoms/capabilities.py`; back-compat re-export from `agent_cli.py` follows the renamed name |
  | `_mars_skill_paths()` | `_resolve_skill_paths()` | private helper |
  | `_MARS_CANDIDATE_DIRS` | `_META_CANDIDATE_DIRS` | private |
  | `_build_mars_meta_block()` | `_build_meta_block()` | private |
  | `data.mars_skill_paths` | `data.skill_paths` | `capabilities` envelope wire field |
  | `produced_by.kind == "mars"` | `produced_by.kind == "agent"` | `interview` input + `recall` output enum literal |

  `git grep -i 'mars' -- ':!CHANGELOG*'` now returns zero matches.

### Removed

- `docs/workflows/mars-integration.md` and
  `docs/workflows/mars/experiment-runner.snippet.md`. The
  integrator-facing content lives in
  `docs/integrations/CONTRACT.md`; references in `README.md`,
  `docs/README.md`, and `docs/internals/sync-checklist.md` point at
  the new file.
- `tests/contracts/test_docs_links.py`. Its sole job was guarding the
  deleted integration proposal docs.
- Every narrative-text "mars" mention from docs, comments, and
  docstrings — replaced with integrator-agnostic language. The file
  `tests/state/test_mars_layout.py` was renamed to
  `test_meta_json_layout.py`; its test-class names
  (`TestMarsLayoutFilter`, `TestDetectMarsTier`) became
  `TestMetaJsonLayoutFilter`, `TestDetectExperimentTier`.

### Audit pass — bug fixes across CLI, planning, flows, runner, mapreduce, forecast, schema, infra

A cross-subsystem audit (11 parallel reviewers, 217 modules) surfaced
~50 defects. The high- and medium-impact ones are now fixed; all 1938
tests pass.

**CLI (`agent_cli.py`)**
- `category="user-error"` (9 callsites) fell through `_EXIT_CODE_BY_CATEGORY`
  and returned exit 3 (internal) instead of 1 (user); the valid key is
  `"user"`. Every "user error" was reported as an internal error.
- `validate-campaign` returned bare `1` instead of `EXIT_USER_ERROR`.
- `_VERB_GROUPS` listed seven ops (`recommend-partition`,
  `recommend-wait-alternative`, `validate-executor-signatures`,
  `validate-input-dataset`, `validate-self-qos-limit`,
  `validate-walltime-against-history`) with no argparse parser;
  trimmed to only the registered ones.

**Planning**
- `validate.py` looked up clusters as `(cfg["clusters"]).get(name)`
  but the loader is flat — every `validate_submission` raised
  "unknown cluster". Fixed to match `planner.py` / `resubmit_planner.py`.
- `throughput.py` divided by zero when `total_tasks=0`; guard up front.
- `checkpoint_detect.py` could `rglob` the filesystem root when a
  `result_dir_template`'s first non-root segment is a placeholder.

**Campaign**
- `cursor.advance_cursor` discarded `atomic_locked_update`'s return
  and re-read the cursor outside the lock — concurrent bumps could
  observe a later iteration than the caller's own bump.
- `manifest.write_manifest` had no fsync and no advisory lock;
  routed through `atomic_locked_update` so concurrent `campaign_init`
  calls serialize on the same flock the cursor uses.
- `goal=""` is now preserved (was dropped because of `if goal:`).

**Mapreduce**
- `combiner.SUPPORTED_SCHEMA_VERSIONS=(1,)` while writers emit
  version 2; every production wave-combine was rejected. Tests
  masked the regression because `conftest.py` pinned v1 fixtures.
- `reduce/metrics.py` `_run_id` joined `params.values()` in
  insertion order; identical-content dicts grouped separately.
- `dispatch.py` promoted output files in `os.listdir` order, so a
  kill mid-promotion could leave `metrics.json` (the idempotency
  marker) in place while siblings remained in `_wip_/`. Now demoted
  to last.
- `dispatch.py` `prior_cmd_sha` could be unbound when
  `current_cmd_sha` was falsy.
- `reduce/history.py` `path.stat()` raced with sidecar deletion;
  now guarded.
- `metrics_io.write_metrics` missing `flush()/fsync()` before
  `os.replace`; node crash could leave a zero-byte `metrics.json`
  (which the dispatcher's idempotency check treats as "complete").
- `reduce/tui.py` raw stderr passed to Rich Table without escaping;
  log lines like `[red]Error[/red]` triggered MarkupError. Per-task
  dict write is now atomic.

**Flows**
- `monitor_flow.py` `FAILED` branch never called `runner.mark_terminal`
  — every monitor re-invocation re-polled the cluster until budget.
- `monitor_flow.py` escalated combiner waves (sentinel `10**9`) were
  retried indefinitely because `_newly_complete_waves` kept surfacing
  them. Skip waves past the sentinel.
- `aggregate_flow.py` best-effort runtime-ingest swallowed only
  `OSError`, not `JSONDecodeError`; corrupt sidecar crashed aggregate.
- `resubmit_flow.py` mid-loop `RemoteCommandFailed` orphaned
  already-submitted batches; retry would double-submit. Persist
  partial IDs to the journal before re-raising.
- `validate_campaign.py` `dataset_row_indices=[]` (empty list)
  silently bypassed dataset validation; only skip when `None`.

**Atoms**
- `canary_verify.py` picked scheduler via `"slurm" in cluster.lower()`
  — clusters like `discovery`, `hoffman2`, `cascade` mis-routed to
  SGE log paths. Now reads `scheduler` from `clusters.yaml` like the
  other atoms do.
- `recommend_partition.py` coalesced `None walltime_cap_sec` to 0,
  then routed every job to `debug_overrun_refused` with a "> 0s cap"
  message.
- `interview.py` generated `tasks.py` with division by `(_N - 1)`
  for both `numeric_linspace` and `numeric_logspace`; `n=1` crashed
  at resolve time.
- `build_executor.py` `read_text()`/`write_text()` use the locale
  codec; on HPC nodes with `LC_ALL=C` the UTF-8 template would
  raise or corrupt. Pinned `encoding="utf-8"`.
- `campaign_budget.py`/`campaign_converged.py` narrowed
  `except Exception` to `(OSError, ValueError, JSONDecodeError)`
  so `KeyboardInterrupt` isn't swallowed during long scans.

**Forecast**
- `drain_simulator.py` used `datetime.max` (naive) as the
  indefinite-job sentinel against tz-aware datetimes;
  `running_slots.sort()` raised `TypeError`. Now `datetime.max.replace(tzinfo=utc)`.
- `calibration.py` near-miss filter counted failed jobs in
  `[near_miss_ratio, cliff_ratio)`; docstring requires `exit_code == 0`.
- `backfill.py` probe cache key omitted `mem_mb` and `cpus` — two
  `ResourceTuple`s differing only in mem/cpus collided and returned
  the wrong ETA.
- `drift_detector.py` `insufficient_history` check now includes the
  filtered list size, not just `len(history)`.

**Runner**
- `failures.py` exit-code-130 fallback only overrode `"unknown"`;
  SLURM preempt notifications contain `"signal SIGTERM 15"` which
  trips the walltime regex, so preempted jobs were mis-advised
  `increase-walltime`. Now also overrides `"walltime"`.
- `update_constraints.py` overwrote the sidecar in place with
  `write_text`; interruption left a corrupt file. Switched to
  tempfile + fsync + replace.

**Infra**
- `infra/inspect/slurm.py` SLURM node names interpolated into a
  `shell=True` sacct command without quoting; `shlex.quote` added.
- `infra/inspect/slurm.py` `len(parts) < 8` guard but
  `_SACCT_BUCKET_FORMAT` has 9 columns — rows with exactly 8
  silently dropped.
- `infra/remote.ssh_run` missing `-o BatchMode=yes`; new-host-key or
  password prompts blocked until timeout instead of failing fast.
- `infra/slurm_reservations._slurm_time_to_iso` force-tagged SLURM
  timestamps as UTC even though slurmctld emits local time. Added
  `HPC_SLURM_TZ` env override and documented the assumption.

**Schema models (with regen of the committed JSON schemas)**
- `runtime_prior.RuntimePriorResult` had `extra="forbid"` but
  `roll_up_quantiles` returns `mem_quantiles_mb` and
  `cpu_cores_quantiles` — added the fields.
- `update_run_constraints.UpdateRunConstraintsSpec` now enforces
  `set_features` vs `add_features` mutual exclusion at the spec
  layer (function-level guard preserved as belt-and-suspenders).
- `aggregate_flow.AggregateFlowSpec` requires `summary_glob` when
  `pull_summaries=true`.
- `resubmit.ResubmitSpec` requires `script`, `backend`, `job_name`
  when `submit_to_cluster=true`.
- `axes.AxesConfig.homogeneous_axes` must be a subset of `axes`
  when both are present.
- `submit.SubmitResult.total_tasks` tightened to `ge=1` (matches
  spec).
- `validate.ValidateResult.scheduler` typed `Literal["sge","slurm"]`.

**State**
- `state/runs.py` `_warned_version_mismatch` was an unbounded set;
  a monitor watching a 10k-task campaign would grow it indefinitely.
  Replaced with a bounded LRU (`OrderedDict`, cap 1024).

**Runtime templates (`mapreduce/templates/runtime/{sge,slurm}/`)**
- Switched from `set -e` to `set -eo pipefail` on all four array
  templates and added an explicit
  `: "${SGE_TASK_ID:?...}"` / `: "${SLURM_ARRAY_TASK_ID:?...}"`
  guard so a missing scheduler-injected task id refuses to dispatch
  task -1.

**Scripts**
- `build_operations_index.py` crashed with `IndexError` when
  `hpc-agent` produced no stdout; now errors out cleanly.
- `build_schemas.py` first-seen-wins silently masked schema-name
  collisions; now raises `RuntimeError` listing both modules.
- `build_{schemas,operations_index,primitive_index,validate_des_predictor}.py`
  now `mkdir(parents=True)` before write.
- `train_wait_predictor.py` `feature_names` is the union of keys
  across all rows, not just `rows[0]`.
- `lint_primitive_modules.py` warns on stale `_PRIMITIVE_MODULES`
  entries that have no `@primitive` decorator.

**Dead-code removal**
- Removed unused `hpc_agent._internal.idempotency` resolver module (was never wired into production; only exercised by tests).

### Determinism — fidelity guardrails for parallel-vs-serial executor parity

The framework's value is "parallelize without changing what computes." This
release closes the realistic divergence sources between a serial run and
the same task running as part of a parallel array, while keeping every
guard overridable per-experiment.

**Cluster preamble**
- `hpc_preamble.sh` pins `PYTHONUNBUFFERED=1`, `PYTHONHASHSEED=0`,
  `PYTHONDONTWRITEBYTECODE=1`, `PYTHONIOENCODING=utf-8`,
  `LC_ALL=C.UTF-8`, `LANG=C.UTF-8` by default. Each overridable via
  `HPC_<NAME>`; empty string disables.
- `gpu_preamble.sh` pins `CUBLAS_WORKSPACE_CONFIG=:4096:8` (required
  for `torch.use_deterministic_algorithms`) and
  `XLA_FLAGS=--xla_gpu_deterministic_ops=true` (JAX).

**Dispatcher**
- `HPC_KW_NAMESPACE_ONLY=1` opt-in skips the bare-uppercase kwarg
  export, eliminating the `HOME=`/`PATH=` collision class. Recommended
  for new campaigns.
- `HPC_FORCE_RERUN=1` bypasses the `metrics.json` idempotency skip.
- `cmd_sha`-mismatch auto-rerun: each successful task stamps
  `<result_dir>/.hpc_cmd_sha`; on re-entry, a mismatch between the
  stamped sha and the sidecar's `cmd_sha` forces re-run. Code/kwarg
  changes never silently reuse a stale result.

**Validators**
- `build-tasks-py` rejects axis names whose uppercase form would
  shadow real env vars (`HOME`, `PATH`, `LD_LIBRARY_PATH`,
  `OMP_NUM_THREADS`, framework `HPC_*`, scheduler `SLURM_*`/`SGE_*`/
  `PBS_*`, ...) with a remediation message.
- `verify-canary` gains optional `--fingerprint <relpath>` that
  SHA256s a file under the canary's result_dir over SSH; lets callers
  diff against a local reference run to detect framework-induced
  divergence.

**Documentation**
- `docs/reference/boundary-contract.md` new "Determinism contract"
  section enumerating what the framework guarantees and what stays
  user-side, with a recipe for reproducing a task locally.
- `skills/hpc-submit/SKILL.md` Step 6b: namespaced-axis-naming
  guidance so the agent recommends prefixed kwargs at conversation
  time.
- `combiner.py` docstring: explicit order-invariance guarantee
  (`sorted()` iteration + Neumaier-compensated summation).
- `executor_template.py` scaffold: demonstrates seed-from-
  `HPC_TASK_ID`, `HPC_KW_*` reads, torch determinism flags,
  `np.random.default_rng` over `np.random.seed`.

### Refactor — repo audit: hygiene fixes across docs, infra, and lint gates

Multi-agent audit of the 542-file tree surfaced ~25 cross-cutting issues;
this lands the safe, contained ones. Behaviour-preserving — every
existing public API, primitive name, and schema file is unchanged.

**Doc hygiene**
- All 9 `_Documentation pending._` primitive doc bodies filled in
  (predict-start-time, recommend-partition, recommend-wait-alternative,
  update-run-constraints, validate-campaign, validate-executor-signatures,
  validate-input-dataset, validate-self-qos-limit,
  validate-walltime-against-history) using the agent-facing template.
- New CI gate `scripts/check_no_pending_primitive_docs.py` fails on any
  stub body; wired into pre-commit + GitHub Actions.
- `docs/forecast_design.md` → `docs/internals/queue-wait-predictor-architecture.md`
  (architecture doc now lives next to the operational notes).
- `docs/reference/cli-contract.md` → `docs/reference/python-api-contract.md`
  (file is about the Python API + sidecar schema, not the shell CLI; the
  rename clarifies its scope vs. cli-spec.md).
- New `docs/internals/README.md` index page.

**Lint gates**
- New `scripts/lint_skill_command_sync.py` cross-checks `skills/` against
  `src/slash_commands/commands/` — both surfaces describe the same
  workflows; lint pins the set as a tuple table.
- CI now also runs `build_schemas.py --check` (was pre-commit-only).

**Code dedup / cleanup**
- Three flock implementations (`session._locked`,
  `_io.advisory_flock`, `telemetry.flock_append`) unified — `_io`
  is the canonical implementation; the others are thin wrappers.
- `scripts/build_schemas.py` rewrites the 100-line hardcoded import +
  registry block as auto-discovery over `_schema_models/` (71 schemas
  rediscovered identically).
- Dead `_from_frontmatters` fallback in `_internal/operations.py`
  removed (registry has been the only SoT for several releases).
- `infra/backends/{sge,slurm}.py` now import `_sge_inspect` /
  `_slurm_inspect` from the relevant submodule directly instead of
  routing through `infra.inspect.__init__`'s underscore re-exports.
  Re-exports retained for tests that monkeypatch but flagged as
  deprecated public API.

**Configuration knobs**
- `HPC_SSH_TIMEOUT_SEC` and `HPC_RSYNC_TIMEOUT_SEC` env-var overrides
  for the previously hardcoded ssh / rsync subprocess timeouts.
- New optional cluster YAML keys `gpu_queues` and
  `excluded_gpu_queue_prefixes` make the Hoffman2-shaped GPU queue map
  in `infra/gpu.py` configurable per cluster (with the previous values
  retained as the fallback).

**Deferred (intentionally not in this commit)**
Audit also surfaced larger refactors that touch many files at once or
have non-trivial blast radius. They are tracked but not done here:
splitting `_internal/session.py` (cycles via lazy imports);
reorganizing `tests/` into subdirectories; splitting the five
600+ LOC monoliths (`planner.py`, `mapreduce/reduce/status.py`,
`forecast/{backfill,queue_wait_baseline,queue_simulator}.py`);
sub-packaging `_schema_models/` by domain; aligning atom / model /
schema names; dropping leading underscores in `_internal/`; promoting
survival atoms out of `forecast/`; splitting `settings.json`. The
audit recommendation to add eager re-exports in `flows/state/forecast/planning/__init__.py`
was rejected on testing — those packages share load-time edges with
`infra/clusters.py` and eager imports close a cycle on first
`import hpc_agent`. Each `__init__.py` now carries a docstring
explaining why submodule-explicit imports are intentional.

### Added — `cluster-reduce` primitive: stop bulk-pulling raw chunks

The 1200-chunk failure mode (per-task CSVs / pickles dragged across
the wire to local before reducing) is structurally eliminated by a
new workflow:

1. **`cluster-reduce`** primitive runs the user's reducer on the
   cluster and pulls only its single JSON output (KB, not GB).
2. **`aggregate-flow`** gains a `mode` parameter
   (`auto`/`combiner-only`/`cluster-reduce`); `auto` (the new
   default) routes to cluster-reduce when the run sidecar carries
   `aggregate_defaults.aggregate_cmd`, falls through to combiner-
   only otherwise. `pull_summaries` defaults to `False`.
3. **Reducer contract** documented at `docs/reference/reducer-contract.md`:
   any program that reads `$HPC_RUN_ID` + writes `$HPC_AGGREGATED_OUTPUT`
   (default `_aggregated/<run_id>.json`) is a valid reducer.

`/aggregate-hpc` Step 4 prose now points at `cluster-reduce` first;
Step 5 ("Download Summaries") tightened to default-off with explicit
"narrow glob only" guidance. `/campaign-hpc` Path B's iter-score
section recommends `cluster-reduce` over local helpers — bulk pulling
chunks doesn't scale past one campaign.

Schema: `schemas/cluster_reduce.output.json` carries the envelope
shape (parsed reducer JSON + cluster/local output paths + exit
diagnostics).

### Added — atomization push: 18 new primitives close prose-driven failure modes

Refactor the slash commands to route through CLI primitives instead of
agent-prose for everything that's not actual judgment. The agent's job
collapses to "call atom X, copy data verbatim"; primitives own the
*behavior*, prose owns only the *judgment* (interview, frame opaque
metrics, decide multi-candidate ties).

**New primitives** (49 total, was 31):

| Primitive | Replaces |
|---|---|
| `submit-flow-batch` | N×submit-flow loops; auto-dispatched when spec is `{specs: [...]}` |
| `build-submit-spec` | `/submit-hpc` Step 6d's 200-line "set this field" prose |
| `build-tasks-py` | Step 6b's "walk the user through writing tasks.py" |
| `discover-reducers` | "Look for aggregation scripts in the repo" prose at `/aggregate-hpc` Step 4 |
| `decide-monitor-arm` | `/monitor-hpc` Step 5: arm choice + cadence + cron schedule + `armed:` line (4 failure modes in one) |
| `monitor-summary` | Step 7 tick-summary framing drift |
| `summarize-submit-plan` | Step 5 plan confirmation framing |
| `verify-canary` | Step 7b/8 wait + grep + output-check protocol (most fragile multi-step in the slash command) |
| `verify-aggregation-complete` | Post-aggregate invariant gate (cross-run contamination, missing waves/tasks, provenance) |
| `suggest-setup-action` | Step 0 priority cascade (in-flight / reuse / interview / fresh) |
| `find-prior-run` | Step 6c `cmd_sha` resume detection |
| `prune-orphan-sidecars` | Half-baked sidecars from rate-limited submit batches |
| `axes-init` | Per-experiment `axes.yaml` for the warm-axis-picker |

**Behavior changes** in existing primitives:

- `submit-flow` auto-detects a `{specs: [...]}` wrapper and routes to
  `submit-flow-batch` internally. Single-spec callers see no change;
  multi-spec callers stop having to know about a separate CLI.
- `submit-flow-batch` does ONE rsync_push + ONE deploy_runtime + N
  qsubs (multiplexed via ssh ControlMaster), eliminating the
  `MaxStartups` rate-limit failure mode at campaign-time fan-out.
  Auto-prunes orphan sidecars before doing anything else; takes a
  per-repo advisory flock to serialize parallel shells.
- SSH primitives (`ssh_run`, `rsync_push`, `rsync_pull`,
  `deploy_runtime`) accept either `user@host` OR an OpenSSH `Host`
  alias as `ssh_target`. The alias form lets `IdentityFile` / `User`
  / `Hostname` from `~/.ssh/config` flow through. They also retry
  with exponential backoff (2s/4s/8s/16s) on `TimeoutError` and
  known sshd-throttle markers; permanent failures (auth, missing
  binary) surface immediately.
- `submit_and_record` finalizes the per-experiment sidecar's
  `job_ids` field after qsub returns. `is_orphan_sidecar` keys on
  this so half-baked sidecars (Step 6d wrote the file but qsub never
  ran) are distinguishable from journal-wipe-recovery sidecars.

**Runtime-prior pipeline** (formerly aspirational, now end-to-end):

Cluster-side dispatcher writes `<result_dir>/_runtime.json` per task
(timing + axis_bindings); combiner aggregates them into
`_combiner/wave_<N>.runtime.json`; `aggregate_flow` and `monitor_flow`
ingest into `<experiment>/.hpc/runtimes/<profile>.<cluster>.json`;
`pick_array_axis_warm` reads them back and picks the lowest-CV axis
for the next submit.

**Stop hook** (`monitor_armed_check`) backstops `/monitor-hpc`'s
`armed:` exit contract. Block message points at `decide-monitor-arm`
so the agent copies primitive output instead of hand-authoring.

**Schemas**: input/output JSON schemas for every new primitive
(`schemas/build_submit_spec.input.json`,
`schemas/decide_monitor_arm.{input,output}.json`, etc.). Symmetry
with `submit_flow.{input,output}.json`; downstream orchestrators can
sanity-check shapes without the CLI.

### Changed — `docs/` reorganized; new top-level `README.md` and workflow doc

The `docs/` tree moves from a flat 11-file bag into four purpose-specific
subdirs plus a top-level navigation index:

```
docs/
├── README.md                             (NEW; nav map)
├── workflows/                            (NEW)
│   ├── memory-across-campaigns.md       (NEW; interview ↔ recall flow)
│   ├── campaign.md                      (mv from docs/)
│   ├── mars-integration.md              (mv from docs/)
│   ├── migration-from-hpc-yaml.md       (mv from docs/)
│   └── mars/experiment-runner.snippet.md (mv from docs/mars/)
├── reference/                            (NEW; wire contracts)
│   ├── cli-spec.md                      (mv)
│   ├── cli-contract.md                  (mv)
│   ├── agent-surface.md                 (mv)
│   ├── boundary-contract.md             (mv)
│   └── config-precedence.md             (mv)
├── internals/                            (NEW)
│   ├── queue-wait-predictor.md          (mv)
│   └── sync-checklist.md                (mv)
├── primitives/                           (kept; hybrid auto/hand)
└── generated/                            (NEW; whole-file auto-gen)
    └── operations.md                    (mv from docs/)
```

`docs/primitives/` stays at top level because it's hybrid (frontmatter
auto-generated, body hand-written). `docs/generated/` is reserved for
whole-file auto-generated content; the only file there today is
`operations.md`, which gains an `<!-- AUTO-GENERATED. DO NOT EDIT BY
HAND. -->` sentinel at the top.

The `docs/README.md` enumerates the layout, points to entry-point docs
by audience (new user / MARs integrator / wire-contract reader / primitive
lookup), and tabulates which files / sections are auto-generated.

`docs/workflows/memory-across-campaigns.md` documents the
`interview` → `recall` feedback loop end-to-end: what `interview.json`
captures, the two-mode (validate / generator) operation of the interview
primitive, the five typed `task_generator` shapes, the three rollup tiers
recall returns, and the `~/.hpc-agent/config.json:experiment_roots`
default-root config.

Root `README.md` updated:
- Agent CLI block adds `interview` and `recall`
- New "Memory across campaigns" subsection under "How It Works" linking
  to the workflow doc
- Configuration section adds `~/.hpc-agent/config.json` entry

Touch-points: 47 files updated for path rewrites (skills, slash commands,
build scripts, tests, schemas, source comments). Build scripts updated:
`scripts/build_operations_index.py` writes to `docs/generated/operations.md`;
the others stay pointing at `docs/primitives/`.

### Changed — `recall` projection broadened, rollup tiers added, config-driven roots

The `recall` primitive grew from "list past campaigns" into the full
memory layer for next-interview grounding.

**Per-campaign projection** (Fix A) — `recall` summaries now include
`budget`, `abort_if`, `cluster_target`, and `task_generator: {kind, params}`
in addition to the existing identity / metadata fields. These are the
prior-decision fields the next interviewer would otherwise have to
re-read every interview.json for. `notes` and `transcript` stay out of
the projection — too verbose; the calling agent re-reads
`interview.json` directly when it needs them.

**Rollup tiers** (Fix B) — every recall response now carries a
`rollup` block with cross-campaign aggregations.

* **Tier 1 (always-on)** — `count`, histograms over `task_kind` /
  `operator` / `produced_by_kind` / `task_generator.kind` / `cluster`,
  `task_count` quantiles (linear-interp p50/p95/min/max),
  `materialized_at` envelope (earliest/latest). Computed from the same
  data already projected; no extra IO.
* **Tier 2 — `--include-runtime`** — walks each matched campaign's
  `.hpc/runtimes/*.json` files. Aggregates `elapsed_sec` (quantiles +
  n_samples) and `failure_rate` from `exit_code != 0` across every
  dispatched task across every matched campaign. Reports
  `campaigns_with_no_runtime` so the caller sees how much of the
  matched set was uninformative.
* **Tier 3 — `--include-generator-stats`** — buckets matched campaigns
  by `task_generator.kind` and reports observed parameter envelopes:
  `numeric_logspace` / `numeric_linspace` get `param_envelopes` (low /
  high / n ranges per parameter name); `cartesian_product` gets
  `axis_value_unions` (every value seen on each axis name);
  `enumerated` / `items_x_seeds` get count only. Most useful with
  `--task-kind` also set so buckets aren't noisy.

Observed ranges only — no recommendations. Recall is the memory layer;
reasoning over the ranges stays in the calling agent.

**Config-driven default `--root`** (Fix C) — `--root` is now optional.
When omitted, the primitive falls back to
`~/.hpc-agent/config.json:experiment_roots` (a JSON file with an
`experiment_roots: [path, …]` field). Both empty raises `spec_invalid`
with a clear message — no implicit cwd default. Multi-root support
(`recall_campaigns(roots: list[Path], ...)`) means the config can list
multiple campaign trees and they're walked together.

### Added — `recall` primitive: query past interview.json files

`hpc-agent recall --root <experiments-dir>` walks the tree for
`interview.json` files (produced by the interview primitive), filters
by `--task-kind` / `--operator` / `--since` (ISO-8601), and returns
recency-sorted summaries (goal, task_kind, task_count, operator,
materialized_at, cmd_sha). Substrate for "show me my last 5 LR sweeps"
prompts in the next interview — closes the memory loop between
campaigns.

Read-only and idempotent. No persistent index; the operator passes the
experiments root explicitly. Malformed `interview.json` files are
skipped silently. Hard-capped at 10K files per scan.

### Added — `interview.task_generator`: typed materializer for tasks.py

The `task_generator` field in `interview.input.json` is now a typed
`oneOf` (was a reserved bare-bones placeholder). When intent supplies a
`task_generator`, the primitive generates tasks.py from the recipe
instead of consuming an agent-written one. Five typed shapes:

- `enumerated` — items list verbatim (most agnostic; covers eval, RL,
  benchmark, data-shard campaigns).
- `cartesian_product` — full cross-product over named axes.
- `items_x_seeds` — items × seeds with `seed` merged into each item dict.
- `numeric_logspace` — `param` swept geometrically over `[low, high]`.
- `numeric_linspace` — `param` swept arithmetically over `[low, high]`.

The expected task count is computed pre-flight from the recipe and
cross-checked against `intent.task_count` *before* any disk write — a
recipe-vs-count mismatch never leaves a partial tasks.py behind.
Generator mode is byte-equivalently idempotent on re-run; an operator
who hand-edits the produced tasks.py drops `task_generator` from the
next intent and the primitive flips to validate-mode for the edited file.

### Added — `interview` primitive: persist campaign intent alongside tasks.py

The interview between hpc-agent and either MARs or a human now produces
a structured artifact (`<campaign-dir>/interview.json`) instead of only
chat-context that's gone the moment the campaign starts.

- `hpc-agent interview --spec <intent.json> --campaign-dir <dir>`
  validates an agent-written tasks.py against the recorded intent
  (asserts `tasks.total() == intent.task_count`, fingerprints with
  `cmd_sha`, samples `resolve(0)`/`resolve(n//2)`/`resolve(n-1)` as a
  dry-resolve preview), then persists the intent verbatim plus a
  `_materialized` block to interview.json. When intent supplies
  `cluster_target` or `budget`, meta.json is merged-into (existing
  operator-set keys win on conflict; `total_tasks` is always derived
  from `tasks.total()`).
- Schema (`schemas/interview.input.json`) is deliberately bare-bones:
  required fields are `goal`, `task_count`, `produced_by`. Optional:
  `task_kind` (free-text recall tag), `budget` (opaque dict so units
  match the campaign — gpu_hours, cpu_hours, task_count, credits),
  `abort_if`, `cluster_target`, `transcript`, `notes`. The schema does
  *not* enumerate search-space shapes (logspace / grid / seeds_x); doing
  so would narrow the existing experiment-agnostic `tasks.py` contract
  to ML-hyperparameter sweeps. The reserved `task_generator` field is a
  placeholder for an opt-in typed materializer (not implemented).
- Spine for future `cmd_recall`: interview.json captures provenance
  (`{kind: mars|human, session_sha, operator, at}`) and a `cmd_sha`
  fingerprint, so future "show me my last 5 LR sweeps" queries can index
  past campaigns by `task_kind` and surface their typed parameters.

### Changed — folded slash_commands Python runtime into hpc_agent

The atomic-ops layer (runner.py), journal storage (session.py), and
typed exception hierarchy (errors.py) moved out of `slash_commands/`
into `hpc_agent/`:

- `slash_commands/runner.py`  → `hpc_agent/orchestrator/runner.py`
- `slash_commands/errors.py`  → `hpc_agent/errors.py`
- `slash_commands/session.py` → `hpc_agent/_internal/session.py`

Plus:

- `hpc_agent/operations.py`  → `hpc_agent/_internal/operations.py`
  (framework-internal plumbing, not user-facing)

The motivation is layering: `hpc_agent/` is the framework, `slash_commands/`
is the human-UX surface. Pre-fold, 7 framework files imported FROM
`slash_commands/`, which is upside-down. Post-fold, `hpc_agent/`
is self-contained.

`slash_commands/` retains its directory and `__init__.py` so the
markdown command templates (`slash_commands/commands/*.md`) still
ship as package data. Users who clone the repo and run `claude code`
inside it pick up the slash commands; the `/setup_hpc` flow still
copies them into `~/.claude/commands/` for global install.

Imports updated across ~10 framework files and the test suite.
Primitive frontmatter `backed_by.python` paths regenerated. No
behavior changes — purely a rearrangement.

### Changed — repository layout switched to PyPA src layout

Both top-level Python packages now live under `src/`:

- `hpc_agent/` → `src/hpc_agent/`
- `slash_commands/` → `src/slash_commands/`

Import names are unchanged (`import hpc_agent`,
`import slash_commands.runner`); only the on-disk layout moved.
`pyproject.toml` declares `[tool.setuptools.packages.find].where =
["src"]` and `[tool.mypy].mypy_path = ["src"]` so the editable install
and type-checker continue to resolve the packages by import name.

The src layout prevents the "import works from cwd without
`pip install -e`" footgun, which had bitten us twice.

### Removed (BREAKING) — `hpc_mapreduce` deprecation shim

The `hpc_mapreduce` shim package, added when the package was renamed
to `hpc_agent` in the previous release, has been removed. Any code
still importing `hpc_mapreduce.X` must update to `hpc_agent.X`.

The CLI binary `hpc-agent <subcommand>` is unchanged — it was
always provided via `[project.scripts]` pointing at
`hpc_agent.agent_cli:main`, not via the shim. MARs and any other
agent harness that shells out to the binary needs no changes.

The shim's job was to give one release of grace for downstream
imports; that release has elapsed. Removing it eliminates the
`DeprecationWarning` pollution at every import and simplifies the
package layout.

### Added — walltime arbitrage and auto-daisy-chain (PR-C)

Two more survival defenses for the campus user submitting low-priority
jobs to clusters where higher-priority jobs consume most of the
resources. Both features fit the same pattern as PR-A and PR-B: they
don't make jobs more efficient — they help the campus user's *own*
jobs survive structural disadvantage. Both default-on but only fire
when they're safe.

**Cold-start walltime arbitrage.** A nominal walltime ask of 4:00:00
collides with every other 4:00:00 ask — the round numbers every
well-funded job requests are also the slots backfill schedulers
reserve. Asking 3:45:00 instead fits in backfill shadows the
4:00:00 jobs don't reach. The new helper
`hpc_agent.forecast.walltime_arbitrage.arbitrage_walltime`
subtracts 15min and floors to a 5min boundary; below a 1h floor the
ask passes through unchanged so short tasks aren't cliff-killed.
The planner (`plan_submit`) applies the trim only when the
`--test-only` lattice probe couldn't pin a winner (no priors path);
the lattice path supersedes arbitrage when priors exist. Per-cluster
opt-out via `walltime_arbitrage: false` in `clusters.yaml`. The plan
output gains a `walltime_arbitraged_from: <int_sec> | null` field
carrying the original ask when the trim fired.

**Auto-daisy-chain for tasks exceeding the cluster's hard ceiling.**
A walltime ask exceeding the cluster's hard scheduler ceiling fails
outright. Auto-daisy-chain splits the ask into N segments where each
segment N+1 holds on segment N (`--dependency=afterany:<id>` on
SLURM, `-hold_jid <id>` on SGE — `afterany` deliberately so a
preempted segment's exit-130 from PR-A still triggers segment N+1).
The trigger fires when the ask exceeds `max_walltime_sec - 1h` (the
1h buffer absorbs queue-wait variance between segments). No segment
cap — a 7-day task on a 24h cluster becomes ~8 segments; bounded
only by checkpointing actually working.

The chain is **default-off when checkpointing isn't detected** so we
don't silently waste compute. The new
`hpc_agent.planning.checkpoint_detect.detect_checkpointing`
helper walks past run output dirs (`<exp>/.hpc/runs/*/result_dirs`)
for files matching `checkpoint*`, `*.ckpt`, `state*.pkl`, `last*.pt`,
`latest*.pt`, `model*.{joblib,pkl,pt}`, `epoch_*.{pt,pkl}`. Returns
True only when a past run of `(profile, cluster)` actually produced
checkpoint-shaped files; False on no past runs, no matching files,
or any error. With detection False, the planner emits an explanatory
error telling the user how to opt in (add checkpointing OR set
`auto_daisy_chain: true` in clusters.yaml).

Per-cluster controls in `clusters.yaml`:

- `max_walltime_sec: <int>` — hard scheduler ceiling. Default 24h
  (Hoffman2 highp); USC CARC Discovery main is 48h. Required for
  chain-decision math.
- `auto_daisy_chain: true` — always chain (skip checkpoint scan).
- `auto_daisy_chain: false` — never chain on this cluster (kill
  switch). The "exceeds max walltime" error fires unmodified.
- (key absent) — defer to `detect_checkpointing` (the safe default).

The plan output gains two more fields: `daisy_chain_segments: <int> |
null` (segment count when chained) and `daisy_chain_dep_jobids:
<list[str]> | null` (the actual scheduler dep jobids; populated
post-submit by `submit_flow`, null at plan time).

New typed validator helpers in `hpc_agent.infra.clusters`:
`get_walltime_arbitrage`, `get_auto_daisy_chain`,
`get_max_walltime_sec`. Each rejects wrong-typed yaml values
(`walltime_arbitrage: "yes"` is a string, not a bool — fails loudly
at load time rather than silently disabling the feature).

### Added — dispatch resilience for the campus user (PR-A)

Three changes that help low-priority "campus user" jobs survive a
hostile shared HPC environment, where higher-priority work routinely
preempts the user's tasks. None of these change framework-internal
behaviour for non-preempted runs.

* `hpc_agent/mapreduce/dispatch.py` now traps `SIGTERM` from the
  scheduler. The handler logs `[hpc-agent] SIGTERM received;
  cluster preemption imminent` to stderr, writes
  `preempt: {at: <utcnow_iso>, grace_sec: <int>}` to the per-task
  entry of `<exp>/.hpc/runs/<run_id>.json`, forwards `SIGINT` to the
  executor subprocess so its except blocks run during the cluster's
  preemption window, waits up to `HPC_PREEMPT_GRACE_SEC` (default
  25s) for clean exit, then `sys.exit(130)`. Marks the run as bumped
  (not failed) so the agent harness can resubmit cleanly without
  surfacing a real failure to the user. Stays cluster-side
  stdlib-only.
* `hpc_agent/mapreduce/dispatch.py` skips invoking the executor on
  resubmit if `result_dir/metrics.json` already exists with non-zero
  size — the campus user resubmits a preempted task without redoing
  already-completed work. Convention: executors that don't call
  `hpc_agent.mapreduce.metrics_io.write_metrics` won't get
  free skip-on-resubmit.
* `slash_commands/errors.Preempted` is the new typed exception
  (`error_code: preempted`, `category: cluster`, `retry_safe: True`).
  Wired through the agent envelope (`error_code` enum in
  `hpc_agent/schemas/envelope.json`), the failure-signatures catalog
  (exit-code 130 → `error_class: preempted`, `suggested_fix: {action:
  resubmit-preempted}`), and `cmd_failures` (`preempted_count` /
  `preempted_task_ids` surfaced at the data top level). Also added to
  the canonical `FailureCategory` StrEnum so classifier-emits ⊆
  resubmit-accepts.

### Added — template defenses for low-priority campus jobs (PR-B)

Three survival defenses for the campus user submitting low-priority
jobs to UCLA Hoffman2 (UGE) and USC CARC (Slurm) where higher-priority
jobs consume most of the resources. These don't make jobs more
efficient — they help the campus user's *own* jobs survive structural
disadvantage.

**Thread caps in the shared template preamble.** All four templates
(SGE/SLURM × CPU/GPU) now source `hpc_agent/mapreduce/templates/common/hpc_preamble.sh`,
which exports `OMP_NUM_THREADS=1` plus the four sibling caps for MKL,
OpenBLAS, NumExpr and vecLib. Without this, a campus user running
NumPy on a 1-core allocation gets BLAS spawning 16 threads, blows past
the cgroup CPU limit, and gets killed by the OOM daemon. Per-experiment
override via `$HPC_OMP_NUM_THREADS=N` (and the per-library siblings
`HPC_MKL_NUM_THREADS` / `HPC_OPENBLAS_NUM_THREADS` / `HPC_NUMEXPR_NUM_THREADS`
/ `HPC_VECLIB_NUM_THREADS`) in the spec's `job_env`. The CPU/GPU array
templates' existing re-exports of `OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK`
/ `$NSLOTS` still take precedence for the multi-threaded case, since
they run after the preamble.

**NFS staging via `$LOCAL_DATA_DIR`.** When `$HPC_NFS_DATA_DIR` is set
in the cluster job's env, the preamble rsyncs that directory into
node-local SSD (`$SLURM_TMPDIR` / `$TMPDIR` / `/tmp`) and exports
`$LOCAL_DATA_DIR` for user code to read from. The contract is the
variable name: user executors should prefer `$LOCAL_DATA_DIR` when
set. Without this, a 200-task array all `open()`ing the same NFS files
at once is the textbook way to get the array throttled — local SSD
reads are ~100× faster and scale per-node, not per-cluster. Strictly
opt-in: users without an NFS dataset pay nothing.

`clusters.yaml` gains an optional `nfs_data_dir:` field per cluster.
When set, `submit_flow` injects it as `HPC_NFS_DATA_DIR` into the
cluster job's env so the staging block fires automatically. Caller-
supplied `job_env` wins via `setdefault`, so per-experiment dataset
overrides still work.

**Cold-start memory buffer in the smart planner.** When no usable
runtime prior exists for `(profile, cluster, gpu_type)`, the user's
`--mem` ask in MB is grown by `(1 + cold_start_mem_buffer)`, then
floored to the existing `floor_mb` minimum, so the OOM daemon doesn't
bump the campus user's brand-new run mid-write and leave a corrupt
result dir behind. This is the cold-start "I have no
idea how much memory you'll use" headroom; the smart planner takes
over once you have ≥5 successful samples per `(profile, cluster,
gpu_type)`, at which point the quantile-based shrink owns and the
buffer is no longer applied (the priors already encode the right
safety margin via `walltime_drift` calibration).

`clusters.yaml` gains an optional `cold_start_mem_buffer:` field per
cluster (default `0.15` = 15%). Set to `0.0` to opt out and preserve
the legacy "kept user default" behavior on cold start. New helpers
`hpc_agent.infra.clusters.get_cold_start_mem_buffer` and
`get_nfs_data_dir` parse and validate the new fields. Both new keys
are added to the boundary-contract allowlist as infra-shaped (they
describe how the cluster is configured, not what work the user wants
to run).

### Changed (deprecation) — `hpc_mapreduce` → `hpc_agent` package rename

The package import path has been renamed `hpc_mapreduce` → `hpc_agent`,
matching the distribution name in `pyproject.toml`. The package was
also split into 4 sub-packages reflecting their domains:

- `hpc_agent.mapreduce` — the actual mapreduce tool (dispatch, combine, reduce, templates)
- `hpc_agent.infra` — cluster communications (backends, ssh, inspect)
- `hpc_agent.orchestrator` — job submission orchestration (flow primitives, planner, runs, runtime priors)
- `hpc_agent.forecast` — predictive scheduling (queue-wait baseline, DES simulator, microstructure features)
- `hpc_agent._internal` — shared utilities (_io, _time, _version, _primitive, idempotency, layout, lifecycle, telemetry)
- `hpc_agent.atoms` — CLI-only primitive dispatchers

`hpc_mapreduce` continues to work as a deprecation shim for one release
— it emits a `DeprecationWarning` on import and forwards `*` from
`hpc_agent`. Update your imports to `hpc_agent` directly; the shim
will be removed in a future release.

The user-facing CLI binary `hpc-agent` is unchanged. Slash commands,
JSON envelope contracts, the `.hpc/tasks.py` user contract, JSON Schema
shapes (now under `hpc_agent/schemas/`), and the cluster-side
stdlib-only constraint on `dispatch.py` and `combiner.py` are all
preserved exactly.

The `cmd_capabilities` output's `python` field now reflects the new
module paths (e.g. `hpc_agent.flows.submit_flow.submit_flow`
instead of `hpc_mapreduce.job.submit_flow.submit_flow`); agents that
shell out by `cli` are unaffected.

### Removed (breaking) — SEGV blacklist feature

The SEGV blacklist (`hpc_agent.orchestrator.blacklist`, the
`record-segv-blacklist` primitive, the `record_segv` /
`get_active_blacklist` exports) has been removed. The smart planner no
longer consumes a blacklist signal; callers should drop any reference to
the feature.

- **`schemas/plan_submit.output.json`**: the top-level required field
  `blacklist_active_count` and the per-candidate optional property
  `blacklisted_nodes` are removed. Pinned consumers should re-pin against
  the current schema.
- **Public API**: `record_segv`, `get_active_blacklist` removed from
  `hpc_mapreduce.__all__`.
- **Docs**: `docs/primitives/record-segv-blacklist.md` deleted;
  `docs/generated/operations.md` and `docs/primitives/README.md` regenerated.

### Added — `hpc_mapreduce.layout` and `hpc_mapreduce.lifecycle` (B1, B2)

- **B1** `hpc_mapreduce.layout` introduces `RepoLayout(experiment_dir)`
  and `JournalLayout(experiment_dir)` — frozen dataclasses that replace
  eight scattered path helpers (`framework_subdir`, `runs_subdir`,
  `tasks_path`, `run_sidecar_path`, `_runs_dir`, `blacklist_path`,
  `runtime_path`, `journal_dir` / `runs_dir` / `_run_path`). The two
  classes are *types*, so the pre-B1 `runs_dir` (journal) vs
  `runs_subdir` (cluster sidecar) name collision — which caused the
  recent `wave_map` P0 — is now a static type error rather than a
  prose-only convention. The eight helpers are kept as deprecated
  forwarders for back-compat.
- **B2** `hpc_mapreduce.lifecycle` introduces four `StrEnum`
  vocabularies replacing the four scattered, drifting string sets that
  preceded them: `JournalStatus` (RunRecord status),
  `LifecycleState` (workflow envelope state), `TaskStatus` (per-task
  status), `FailureCategory` (failure-fingerprint vocabulary).
  `tests/test_lifecycle.py` cross-validates that the schema enums on
  `monitor_flow.output.json`, `status.output.json`, and
  `reconcile.output.json` match `LifecycleState`, that classifier
  emissions are a subset of `FailureCategory`, and — pinning the A4
  invariant — that classifier emissions are a subset of the resubmit
  path's accepted set. The drift class is now unrepresentable.

### Fixed — cross-cutting audit (A1–A11)

- **A1** `docs/primitives/check-preflight.md` frontmatter pointed
  `backed_by.python` at a non-existent module
  (`hpc_mapreduce.preflight.run`); routed to the canonical
  `hpc_mapreduce.agent_cli.cmd_preflight`.
- **A2** Four primitive docs claimed bogus per-experiment mirror writes
  that no implementation actually performed — `submit-spec.md`,
  `mark-run-terminal.md`, and `resubmit-failed.md` had stale "writes
  `<experiment_dir>/.hpc/runs/...` (mirror)" lines; `monitor-flow.md`
  declared the wrong path for `.monitor.jsonl` (the real path is in the
  journal dir, not the per-experiment dir). Frontmatter now matches
  reality.
- **A3** `hpc_mapreduce/schemas/status.output.json` and
  `reconcile.output.json` enums missed `timeout`, which
  `monitor_flow.output.json` already accepts. Consumers reading status
  output must accept any value the workflow primitive could plausibly
  produce; both schemas now include it.
- **A4** Failure-category vocabulary divergence: the auto-classifier in
  `slash_commands.runner.cluster_failures_by_fingerprint` emitted 5
  categories (`import_error`, `file_not_found`, `permission_denied`,
  `disk_full`, `python_traceback`) that the resubmit subcommand silently
  rejected. Took the union as canonical; added a regression test pinning
  the invariant.
- **A5** `slash_commands.runner.submit_and_record` now accepts an
  optional `cmd_sha` and dedups against `find_run_by_cmd_sha` when the
  journal is empty but the per-experiment sidecar still exists. Repairs
  the journal in-place so subsequent calls hit `load_run` directly.
- **A6** Authored four missing primitive docs:
  `docs/primitives/walltime-drift.md`, `house-edge.md`, `logs.md`,
  `failures.md`. Each follows the existing frontmatter contract and
  parses cleanly through `tests/test_primitive_frontmatter.py`.
- **A7** Replaced the hand-typed `subcommands` literal in
  `cmd_capabilities` with a derivation from the live argparse tree.
  `walltime-drift` and `house-edge` now appear automatically in
  capabilities output.
- **A8** Corrected the `hpc_mapreduce/agent_cli.py` module docstring,
  which falsely claimed stderr is JSON-per-line. Stderr is free-form
  diagnostic prose (`[dispatch] ERROR: ...`); only stdout carries
  envelopes.
- **A9** `_append_tick` in `hpc_mapreduce/job/monitor_flow.py` now
  acquires a flock on a sibling `.lock` file before writing the JSONL
  record. The slash-command surface and the workflow primitive both
  append to the same `<run_id>.monitor.jsonl`; without flock, two
  concurrent writers could interleave bytes mid-line. Best-effort no-op
  on platforms without `fcntl`.
- **A10** `read_run_sidecar` now warns once per
  `(run_id, sidecar_version)` when the sidecar's `hpc_agent_version`
  differs from the running package's `__version__`. Closes the loop on
  a previously-dead sidecar field; readers can find old sidecars in the
  wild.
- **A11** `hpc_mapreduce.infra.inspect.inspect_cluster` raised a bare
  `KeyError` for unknown clusters, which the envelope translator
  surfaced as `error_code: internal`. Replaced with
  `errors.ClusterUnknown` so the typed exception flows through
  `_err_from_hpc` to produce the documented `error_code: cluster_unknown`.

### Removed — `hpc_agent.campaign.run_campaign` asyncio loop and `defaults` callbacks

The closed-loop driver is now the slash-command surface itself: the
assistant repeatedly invokes `/submit-hpc campaign_id=<slug>` until
`tasks.total() == 0`. Concurrency is opt-in by firing more submits
before earlier ones land; the cluster scheduler runs them in parallel.
This eliminated three classes of complexity:

- **No asyncio mental model**: no `asyncio.run`, no `Awaitable`
  callbacks, no FIRST_COMPLETED gymnastics. Each iteration is a
  self-contained `/submit-hpc` invocation with the same approval
  prompts and failure surface as a one-shot submission.
- **No driver state to recover**: `run_campaign` previously needed
  `session.find_runs_by_campaign` + `await_completion` polling to
  re-discover in-flight runs after a network drop. The slash-command
  loop has nothing to recover — sidecars on disk are the only state.
- **No `submit_one` / `await_completion` / `should_submit` boilerplate**:
  the framework's `defaults.submit_via_cli` etc. existed only to wrap
  the CLI as async callables. Invoking the CLI directly works.

Removed:
- `hpc_mapreduce/campaign/loop.py` (`run_campaign`, `CampaignResult`)
- `hpc_mapreduce/campaign/defaults.py` (`submit_via_cli`,
  `poll_until_terminal`, `tasks_py_total_predicate`)
- `tests/test_campaign_loop.py`, `tests/test_campaign_defaults.py`,
  `tests/test_campaign_e2e.py`

Kept (the small surface that actually mattered):
- `campaign_id` field on submit specs and per-run sidecars.
- `HPC_CAMPAIGN_ID` env var threaded through scheduler templates.
- `hpc_agent.mapreduce.reduce.history.prior(...)` for reading per-iteration
  reduced metrics back inside `tasks.py`.
- `hpc_agent.campaign.campaign_dir(...)` for strategy-state
  placement (Optuna SQLite, PBT checkpoints).
- `hpc-agent campaign list / status` CLI inspection.

For the migration story (every capability the asyncio loop offered has
an equivalent in the slash-command pattern, including K-in-flight,
FIRST_COMPLETED-style waits via parallel `Bash` calls, wall-clock
budget caps via env var + `tasks.py`, and headless overnight runs via
`/loop`), see `docs/workflows/campaign.md` and `slash_commands/commands/campaign-hpc.md`.

### Changed — `/monitor-hpc` is now silent-by-default; per-tick observations land in `.hpc/runs/<run_id>.monitor.jsonl`

Each `/monitor-hpc` tick used to emit a multi-line summary every time
(grid rollup table, `summary` counts, "no change since X" line, etc.).
At 5-min monitoring on a 24-hour run, that's ~290 ticks of narration
the user never reads. The skill now writes a structured record per
tick to a tick-log JSONL file and emits **nothing** to console unless
an action was taken (auto-resubmit, second-strike combiner failure),
the lifecycle flipped to a terminal state, or the user must
intervene (code bug, unknown failure).

When the user comes back and asks "what happened" / "status" /
"summarize", `/monitor-hpc summary` reads the JSONL and emits a
single digest. See `slash_commands/commands/monitor-hpc.md` Step 7.

### Added — Smart `/hpc-submit`: resource-quality-aware constraint planning

`/hpc-submit` previously chose its `--constraint=` and `--time=` from
static cluster config. The new path consults a snapshot of the cluster
plus per-(profile, cluster) runtime priors plus a SEGV blacklist, then
hands the scorecard to Claude for cost-model judgment over candidate
constraints.

Three independently shippable Python modules and one planner integrate
into a single `plan-submit` CLI subcommand:

- **`hpc_mapreduce.infra.inspect`** — `inspect-cluster --cluster <c>`
  returns a per-node snapshot (`AllocMem%`, `CPULoad%`, `Gres`,
  `GresUsed`, `ActiveFeatures`, `State`, plus a co-tenant list from
  `sacct -N` / `qstat`). `is_stressed` is set when `AllocMem >= 0.80`
  or `CPULoad/CPUTot >= 0.80` (both tunable). 60s in-process cache so a
  single submit cycle pays the SSH cost once. Both SLURM and SGE are
  supported.
- **`hpc_agent.orchestrator.blacklist`** — append-only SEGV journal at
  `<repo>/.hpc/bad_nodes.<cluster>.json`. 7-day TTL, refreshed on
  repeat SEGVs. Atomic write under `fcntl.flock`. Evidence list capped
  at 5 most-recent entries per node. `record_segv()` is called by
  `/hpc-monitor` on `NODE_FAIL` / `exit -11`; `get_active()` is called
  by the planner with TTL filtering.
- **`hpc_agent.state.runtime_prior`** — append-only sample log at
  `<repo>/.hpc/runtimes/<profile>.<cluster>.json`. `roll_up_quantiles()`
  groups by `gpu_type` and computes p50 / p95 / p99 / mean / n_samples,
  with optional `cmd_sha` filter so a `.hpc/tasks.py` change can
  invalidate stale priors.
- **`hpc_agent.planning.planner`** — `plan-submit --profile <p>
  --cluster <c>` combines all three into the scorecard JSON the slash
  command hands to Claude. When no priors exist, `needs_canary: true`
  and `canary_plan` describes the 1-task probe to seed the priors.
- **CLI**: three new subcommands on `hpc-agent`: `inspect-cluster`,
  `runtime-prior`, `plan-submit`.
- **Slash commands**: `submit-hpc.md` gains a Step 4c describing the
  canary path and the cost rubric Claude applies. `monitor-hpc.md`
  gains a SEGV-detection branch that calls `record_segv()` so the
  blacklist is populated for future submits.

Tests in `tests/test_inspect_cluster.py`, `tests/test_blacklist.py`,
`tests/test_runtime_prior.py`, `tests/test_planner.py` cover the
parsing edge cases, TTL math, atomic write contract, quantile
computation with mixed sample counts, and an end-to-end planner shape
test using a fake cluster snapshot.

### Changed — `/status` slash command renamed to `/monitor-hpc`

The interactive Claude Code slash command at
`slash_commands/commands/status.md` is renamed to
`slash_commands/commands/monitor-hpc.md`. Users invoke it as
`/monitor-hpc` instead of `/status`. The CLI subcommand
(`hpc-agent status`) is unchanged — only the human-facing slash
command was renamed; programmatic callers (MARs, scripts, and the rest
of the slash-command pipeline) continue to use the same JSON envelope
and exit-code contract.

Every cross-reference in the slash-command markdown, the docs
(`cli-spec.md`, `cli-contract.md`, `sync-checklist.md`,
`migration-from-hpc-yaml.md`), and `README.md` was updated. The skill
at `skills/hpc-status/` keeps its name; the MARs skill-name registry
(`_MARS_SKILL_NAMES`) is unchanged.

### Added — campaign helper layer (Optuna-recipe ergonomics)

Five small, strategy-blind additions surfaced by walking through the
end-to-end Optuna recipe in `docs/workflows/campaign.md`. None bind the framework
to a specific tuning library; they collapse boilerplate the previous
shape made every user write themselves.

- **`hpc_agent.campaign.campaign_dir(experiment_dir, campaign_id)`** —
  canonical scratch directory `.hpc/campaigns/<cid>/`. Created
  idempotently. Reserved for strategy libraries to put state files
  (Optuna SQLite, PBT checkpoints, walk-forward cursor); the framework
  writes nothing inside.
- **`hpc_agent.campaign.defaults`** — three curried-function defaults
  for `run_campaign`'s callbacks:
  - `tasks_py_total_predicate(experiment_dir)` — re-imports `tasks.py`
    each call and returns `total() > 0`.
  - `poll_until_terminal(experiment_dir, poll_interval_seconds=30)` —
    awaits one run via subprocess `hpc-agent status` until the
    lifecycle state is terminal.
  - `submit_via_cli(spec_builder, experiment_dir)` — builds a spec via
    user callback, writes it to the campaign dir, shells out to
    `hpc-agent submit`. Returns the new run_id.
  Together they collapse a typical campaign driver from ~80 lines to ~5.
- **`on_iteration_done` callback on `run_campaign`** — fires once per
  iteration with `(run_id, status, raw_metrics)` so strategy libraries
  can wire their "tell" call (Optuna's `study.tell()`, PBT's drop, etc.)
  without polling externally. Optional; the framework computes
  `raw_metrics` via the v2 sidecar pipeline when `experiment_dir` is
  provided. Empty dict for failed iterations.
- **`hpc_agent.mapreduce.metrics_io.read_kw_env()`** — executor-side helper
  that returns `{lowercase_name: str_value}` for every `HPC_KW_*` env
  var the dispatcher exported. Stdlib-only; deployed alongside the
  executor.
- **Documented `cmd_sha` collision pattern** — for stochastic
  strategies (Optuna TPE, evolutionary), `resolve()` should include a
  unique-per-iteration value (e.g. `_optuna_trial_number`) so cmd_sha
  differs even when the strategy re-proposes the same params. Otherwise
  the framework dedups the submission silently. Doc-only fix.

### Added — closed-loop campaign primitive

The framework gains a small new primitive for adaptive iteration: a
**campaign** is a sequence of `/submit` invocations sharing a
`campaign_id` tag. The user's `.hpc/tasks.py` reads
`hpc_agent.mapreduce.reduce.history.prior(experiment_dir, campaign_id)` at
module load to learn what prior iterations of the same campaign produced
and decide what to run next. Strategies (Optuna, RandomSearch,
walk-forward, PBT, …) live as Python libraries the user imports inside
their own `tasks.py` — the framework ships **zero** strategy code.

Surface area:

- **`campaign_id`** — first-class field on the v2 sidecar and on the
  journal `RunRecord`. Set via `--campaign-id` on submit specs;
  filterable via `session.find_runs_by_campaign`.
- **`HPC_CAMPAIGN_ID`** — env var forwarded by every scheduler template
  (SGE/SLURM × CPU/GPU). Read by the user's `tasks.py` and executor on
  the cluster.
- **`hpc_agent.mapreduce.reduce.history`** — read-only accessor:
  - `prior(experiment_dir, campaign_id)` returns per-iteration reduced
    metric dicts, oldest-first. Pending iterations contribute `{}`.
  - `find_sidecars_by_campaign` and `result_dirs_for_sidecar` for
    callers that need the underlying primitives. None of these import
    `.hpc/tasks.py` (the loop's calling module), so no recursion.
- **`hpc_agent.campaign.run_campaign`** — asyncio in-flight queue.
  Maintains *concurrency* live submits, awaits the next-finished one
  (FIRST_COMPLETED), repeats until the user's `should_submit` predicate
  flips to False or a wall-clock budget elapses. Fully IO-injected
  (`submit_one`, `await_completion`, `should_submit`); no fixed
  Strategy/Context Protocol.
- **`hpc-agent campaign status` / `hpc-agent campaign list`** —
  read-only CLI subcommands, JSON envelopes pinned by
  `schemas/campaign.output.json`.
- **`/campaign`** — slash command with the conversational interview;
  scaffolds a campaign-aware `tasks.py` from the recipes in
  `docs/workflows/campaign.md` (random search, Optuna ask/tell, walk-forward).

Resume semantics: sidecars on disk are the only durable state. After a
network drop or laptop sleep, re-running the loop re-discovers in-flight
runs via `find_runs_by_campaign`, polls them to terminal state, and
continues. No separate state file.

Failure semantics: a single iteration's failure surfaces via `on_event`
with an `error` field; the loop continues. Reissuing failed iterations
is the strategy library's call.

Out of scope: cluster-side queue (one array job draining a shared-FS
task queue), cluster-resident campaign driver, per-campaign retention.
All future work.

### Changed — `hpc.yaml` absorbed into the per-run sidecar

- **`hpc.yaml` is gone.** Every load-bearing field has moved into the
  per-run sidecar at `.hpc/runs/<run_id>.json` (sidecar schema bumped to
  v2): `cluster`, `profile`, `project`, `remote_path`, `resources`,
  `env`, `env_group`, `constraints`, `gpu_fallback`, `max_retries`,
  `runtime`, `auto_retry`, `aggregate_defaults`. Multi-stage DAGs move
  to `.hpc/stages.py` (Python file exposing `def stages() -> list[dict]`,
  validated against `hpc_mapreduce/schemas/stages.input.json`). Auto-retry
  caps that used to live in `hpc.yaml profiles[*].auto_retry` now have
  conservative hardcoded defaults in
  `slash_commands.runner.DEFAULT_AUTO_RETRY_POLICY` with per-run override
  via the sidecar. `cmd_aggregate` reads `aggregate_defaults` from the
  sidecar instead of the yaml.
- **No deprecation cycle.** Old `hpc.yaml` files in user repos are now
  silently ignored — the agent never reads them. Users who hand-edited
  `hpc.yaml` should re-run `/submit` once; the new sidecar will capture
  their resolved config from then on.
- **Deleted**: `_hpc_yaml_auto_retry`, `_hpc_yaml_aggregate_defaults`,
  `docs/schema.md`, `tests/fixtures/hpc_multistage.yaml`,
  `tests/test_hpc_yaml.py`. The README's `hpc.yaml` section is removed.
- **v1 sidecars on disk continue to load** with v2 keys backfilled to
  `None`, so existing journals are not broken.

### Changed — major refactor: `.hpc/tasks.py` task model

- **Collapsed the manifest + per-axis shim model into a single
  user-written `.hpc/tasks.py`** exposing `total()` and
  `resolve(task_id)`. The framework no longer ships a manifest format,
  a `_hpc_dispatch.json`, a `MANIFEST_ALIAS`, a chunking shim, a
  date-window shim, or any axis-enum spec block (`grid:`, `chunking:`,
  `backtest:`). Per-experiment task definitions live as plain Python
  the agent walks the user through writing once, committed to git in
  `.hpc/tasks.py`. Per-run state moves to `.hpc/runs/<run_id>.json`
  sidecars. Generic framework artifacts (`_hpc_dispatch.py`,
  `_hpc_combiner.py`, scheduler templates) ship with the package and
  are scp'd directly to the cluster's `.hpc/` by `deploy_runtime` —
  the experiment repo never holds a copy.
- **`error_code: "manifest_invalid"` renamed to `"spec_invalid"`** to
  match the new layer it covers (the per-run sidecar plus
  `tasks.py`). `HpcError` subclass `ManifestInvalid` renamed to
  `SpecInvalid`. No back-compat alias — there are no MARs consumers
  yet to break.
- **Build-executor types reduced to `plain`.** The `chunked`,
  `date-window`, and `shim` starter templates and matching
  `--type` argparse values are gone; per-task fan-out lives inline in
  `.hpc/tasks.py`, scaffolded by `/submit` Step 6 from the canonical
  reference at `hpc_mapreduce/templates/tasks_example.py`.
- **CLI flag rename.** `--manifest <path>` is now `--run-id <id>` on
  every subcommand that addresses a per-run record.

### Added — reliability / correctness

- **Stale-cache age field.** `status` and `list-in-flight` envelopes
  now carry `last_status_age_seconds` so consumers (humans + agents)
  can flag stale snapshots without changing freshness contracts.
- **Wave-aware `last_status`.** The on-cluster reporter now emits a
  `waves` rollup keyed by wave id with `{complete, running, pending,
  failed, unknown, total}` buckets. `record_status` and `reconcile`
  carry it into the persisted `last_status`. New `rollup_by_wave`
  helper in `hpc_agent.mapreduce.reduce.status`.
- **`hpc-agent logs` subcommand.** Fetches per-task stderr from the
  cluster: `--task-id 7,12,42` for explicit ids or `--all-failed` for
  every failed task. Falls back through earlier `job_ids` when the
  latest has no log. Removes a daily friction point.
- **`hpc-agent failures` subcommand.** Triage tool: re-polls
  status, fetches stderr for failed tasks, strips volatile noise
  (timestamps, abs paths, pids, hex pointers), fingerprints the last
  non-empty line, and groups tasks sharing a fingerprint into clusters
  tagged with a category (`gpu_oom`, `walltime`, `import_error`, etc.).

### Added — robustness

- **Resubmit dedupe via `request_id`.** `resubmit_failed` now returns
  `(record, deduped, request_id)` and is idempotent on the (explicit
  or derived) `request_id`. A second call with the same spec returns
  `deduped: true` without incrementing per-task retry counters. A
  back-compat-default field `last_resubmit_request_id` was added to
  `RunRecord`.
- **`auto_retry` policy in hpc.yaml.** Per-category retry caps with
  optional resource multipliers (advisory). `hpc-agent failures`
  annotates each cluster with `retry_advice = {policy,
  eligible_task_ids, blocked_task_ids}`. The framework never resubmits
  on its own — it surfaces eligibility; the caller decides. See
  `docs/schema.md` for the full shape.

### Added — MARs integration proposal package

- **MARs integration proposal package.**
  - `docs/workflows/mars-integration.md` — Bun.spawn env block, `error_code` →
    retry-policy mapping, troubleshooting flow for the silent-hang
    failure mode, journal-coexistence rules.
  - `docs/workflows/mars/experiment-runner.snippet.md` — paste-ready section for
    MARs's `agents/experiment-runner.md` covering preflight → submit →
    status → aggregate, decision rule for delegating to hpc-agent, and
    the full retry table.
  - `tests/test_docs_links.py` — drift guard ensuring every `error_code`
    and required env var mentioned in the proposal docs matches the
    code (`slash_commands/errors.py` and `capabilities.required_env`).
- **`capabilities` envelope additions** (additive, schema-compatible):
  - `mars_skill_paths` — absolute paths to bundled `skills/hpc-*/SKILL.md`
    so consumers can discover them without hardcoding the package layout.
  - `required_env` — env vars consumers must forward
    (`SSH_AUTH_SOCK`, `HPC_JOURNAL_DIR`, `HPC_CLUSTERS_CONFIG`).
- **README**: collapsed the "Using with MARs" section to a link to
  `docs/workflows/mars-integration.md`; kept the SSH-passthrough warning visible.

### Added — MARs compat Tier 2

- **`submit --from-meta`.** Overlay missing `profile` / `job_name` on
  the submit spec from `<experiment-dir>/meta.json` `experiment_id`
  via `setdefault` semantics — never overwrites caller-supplied
  values, silent no-op when `meta.json` is absent or missing
  `experiment_id`. Removes the manual overlay step from the MARs
  experiment-runner snippet.
- **`HPC_RUNTIME` job-env wiring.** New
  `slash_commands.runner.build_job_env(spec, base_env)` returns
  `base_env` augmented with `HPC_RUNTIME=uv` when `runtime: "uv"` is
  on the submit spec. The `/submit` slash command now constructs the
  `qsub` / `sbatch` env via this helper instead of inlining the gate
  logic. Closes the dangling end of Tier 1's `runtime: uv` work.
- **Doc refresh + drift guards.** Removed two stale claims from the
  MARs proposal docs (resubmit idempotence in
  `experiment-runner.snippet.md`; `uv run` "known gap" caveat in
  `mars-integration.md`). Added sentinel tests in
  `tests/test_docs_links.py` pinning the corrected wording so a
  future editor accidentally reintroducing either phrase fails CI.

### Added — MARs compat Tier 1

- **`meta.json` ingestion**. The `discover` envelope now includes a `meta`
  block with `experiment_id`, `seed`, `purpose`, and `tier` whenever a
  `meta.json` file exists at the experiment-dir root. Callers stop reparsing
  the file themselves. New helper `read_meta_json(experiment_dir)` is the
  single seam — silent on parse failures, since hpc-agent is not the place
  to validate MARs's schema beyond the fields it surfaces.
- **Tier detection**. `discover` surfaces `tier: 1 | 2 | null` derived from
  path layout: `probes/probe-*` + `probe.py` → 1, `runs/run-*` + `scripts/`
  → 2, otherwise null. New `detect_mars_tier(experiment_dir)` helper.
- **MARs layout discovery filter**. When `meta.json` is present at the
  experiment-dir root, executor discovery narrows to `scripts/` (Tier-2
  entrypoints) and the root-level `probe.py` (Tier-1) — never `src/`,
  honoring MARs's modules-only contract for that directory. Default
  behavior unchanged when the marker is absent.
- **`runtime: uv` profile.** Opt-in cluster-side `uv run` for every
  task command, with a `uv sync` preamble in all four shipped job
  templates (SGE CPU/GPU, SLURM CPU/GPU) gated on the `HPC_RUNTIME=uv`
  env var. Honors MARs's #1 invariant ("ALWAYS `uv run` … NEVER
  `pip`"). Templates exit 2 with a clear diagnostic when uv is
  missing — much clearer than running tasks with the wrong Python.
- **`schemas/discover.output.json`**: new file pinning the discover
  envelope's data shape (`executors` required, `meta` optional).
- **`schemas/submit.input.json`**: optional `runtime` field (`"uv"` or
  null) so the journal can record the runtime alongside the rest of
  the spec.

### Changed

- **`status`, `aggregate`, `reconcile` fail fast when `SSH_AUTH_SOCK` is
  unset.** Previously these subcommands hung indefinitely on auth — the
  most common Bun.spawn failure mode for orchestrators. They now emit
  `error_code: "ssh_unreachable"` (category `network`, `retry_safe: True`,
  exit 2) immediately. `submit` (journal-only) and `resubmit`
  (journal-only) are not gated.
- **`aggregate` gains framework-agnostic plumbing guarantees.** Three
  optional, additive checks help both human `/aggregate` users and
  agent CLI callers catch silent partial-data combines:
  - `--require-outputs <template>` — pre-combiner SSH check that every
    per-task output named by the template (with `{task_id}` placeholder)
    exists. Refuses to combine on partial data; surfaces a new
    `error_code: "outputs_missing"` (category `cluster`, `retry_safe: True`)
    listing the absent paths.
  - `--expect-output <path>` — post-combiner check that the declared
    artifact exists and (for `.json` paths) is parseable. A combiner
    that exits 0 but writes nothing now surfaces as `combiner_failed`
    immediately instead of producing a silent "successful" aggregate.
  - **Provenance** — the success envelope's `data` block always carries
    a `provenance` object: `{run_id, wave, profile, cluster,
    combined_at}`. When `--expect-output` is set, hpc-agent also
    writes a `_provenance.json` sidecar next to the output on the
    cluster (best-effort; envelope is the source of truth).
- **`hpc.yaml` defaults for the new aggregate flags.** Set
  `results.require_outputs` and `results.expect_output` once per profile
  to enforce the precondition/postcondition automatically on every
  aggregate. Explicit CLI flags override hpc.yaml. The
  `slash_commands/commands/aggregate.md` prompt now points users at
  this.

## 0.2.0 — 2026-04

Major refactor adding agent-facing CLI alongside the existing Claude Code slash
commands. Both surfaces share the same atomic-ops layer at
`slash_commands/runner.py` so cross-surface state stays consistent.

### Added

- **`hpc-agent` CLI** (the agent surface). Subcommands: `submit`, `status`,
  `aggregate`, `reconcile`, `resubmit`, `preflight`, `discover`, `expand-grid`,
  `list-in-flight`, `clusters list|describe`, `capabilities`, `build-executor`.
  Stdout is a single-line JSON envelope; stderr is JSON-per-line log records.
  Exit codes: 0 ok, 1 user error, 2 cluster/network, 3 internal. Full schema
  in `docs/reference/cli-spec.md`; runtime-validatable JSON Schemas under
  `hpc_mapreduce/schemas/`. Both `python -m hpc_mapreduce <cmd>` and
  `hpc-agent <cmd>` work.
- **`/preflight` slash command** — health check matching the CLI subcommand,
  for Claude Code parity.
- **MARs SKILL.md files** under `skills/hpc-*/SKILL.md`. Drop-in workflow
  guidance for MARs orchestrator agents that invoke the CLI via the Bash tool.
- **Typed exception hierarchy** at `slash_commands/errors.py` (`HpcError` base
  with `error_code`, `retry_safe`, `category`, `remediation`). One source of
  classification, two presentations: CLI maps to envelope; slash commands let
  Claude format for the human.
- **SSH connection multiplexing** in `hpc_mapreduce/infra/remote.py`
  (`ControlMaster auto`, `ControlPersist 10m`). First call to a cluster opens
  the master socket; subsequent calls reuse it (~50ms vs ~500ms+). Opt-out
  via `HPC_NO_SSH_MULTIPLEX=1`.
- **Idempotent `submit`**. `submit_and_record` now returns
  `tuple[RunRecord, bool]`; the bool is `deduped`. Replaying a submit with the
  same spec returns the existing record and the cluster does not see duplicate
  qsub/sbatch calls. Removes a real correctness footgun for any caller that
  retries on transient network errors.
- **Last-status cache file** `<run_id>.last_status.json` written next to the
  journal record by `record_status`. Any consumer can read it for cheap
  cached state without re-issuing an SSH call.
- **Env-configurable state directories**:
  - `HPC_JOURNAL_DIR` overrides the journal location (default: `~/.claude/hpc/`).
  - `HPC_CLUSTERS_CONFIG` overrides the clusters.yaml path (default: shipped
    in the package).
- **`__version__`** on the `hpc_mapreduce` package, `--version` flag on the CLI.
- **`py.typed`** marker — `hpc_mapreduce` ships type hints to mypy/pyright.
- **New docs**: `docs/reference/cli-spec.md`, `docs/reference/config-precedence.md`,
  `docs/internals/sync-checklist.md`.

### Changed

- **Renamed `agent/` → `slash_commands/`.** Disambiguates from MARs' own
  `agents/` concept. The directory is Claude Code slash-command glue, not an
  LLM agent. All Python imports `from agent import ...` → `from slash_commands
  import ...`. Plugin manifest at `.claude/commands/setup_hpc.md` updated.
  In-flight runs and on-cluster jobs are unaffected by the rename.
- **Renamed `/monitor` slash command → `/status`.** Verb collision with the
  built-in Claude Code `Monitor` tool that MARs orchestrators have available;
  the new name is also more accurate (one-shot snapshot, not a streaming
  watcher). CLI matches: `hpc-agent status`. Existing in-flight runs and
  journal records pick up the rename without migration. Existing standalone
  Claude Code users will need to retrain on `/status`.
- **Relocated `config/clusters.yaml` → `hpc_mapreduce/config/clusters.yaml`**
  and `templates/{executor_template,chunking_shim,date_window_shim,shim_template}.py`
  → `hpc_mapreduce/templates/starters/`. Required for `pip install` from a
  wheel to ship with config and starter templates. **File contents are
  byte-identical** — only paths changed. Anyone who forked and customized
  these files will need to re-apply their edits at the new paths.
- **`_PACKAGE_ROOT` repurposed.** Previously the repo root; now points at the
  `hpc_mapreduce/` package directory itself. Affects path resolutions like
  `_PACKAGE_ROOT / "config" / "clusters.yaml"` (still works because the file
  moved into the package). External users importing `_PACKAGE_ROOT` to
  resolve paths in their own forks: audit your usages.
- **`submit_and_record` return type** changed from `RunRecord` to
  `tuple[RunRecord, bool]`. Callers must unpack: `record, deduped = ...`.
- **`pyproject.toml` packages discovery** changed from `["hpc_mapreduce*",
  "agent*"]` to `["hpc_mapreduce*", "slash_commands*"]`. Added
  `[project.scripts]` for the `hpc-agent` console entry, and
  `[tool.setuptools.package-data]` so wheels bundle config + templates +
  schemas.

### Not changed (deliberately)

- **No MCP server.** MARs agents invoke external code through the Bash tool;
  a CLI is the right shape for that pattern. MCP would require adding a
  wrapper tool inside MARs. Revisit only if MARs itself shifts to
  surfacing MCP servers as first-class agent tools.
- **No cancel/abort subcommand.** `settings.json` denies `scancel`/`qdel`.
  If you decide an experiment is bad, stop waiting on it; cluster jobs run
  to walltime.
- **No local-execution backend.** hpc-agent is the HPC-on-cluster path;
  MARs already iterates locally via uv/Docker.
- **No deprecation shim for old `agent.*` imports.** Standalone users invoke
  the package via slash commands (which we update atomically) or the new CLI;
  external scripts importing `agent.*` directly do a one-time migration to
  `slash_commands.*`. The version bump signals the break.

### Migration notes for current standalone users

A user who pulls 0.2.0 and continues using hpc-agent as a Claude Code plugin
will see:

- **All four legacy slash commands work identically** modulo the verb rename:
  `/submit`, `/status` (was `/monitor`), `/aggregate`, `/build-executor`. New
  `/preflight` is added.
- **In-flight runs** at `~/.claude/hpc/<hash>/runs/*.json` continue to load
  unchanged — `schema_version` stays at 1.
- **Cluster-side jobs** already submitted run to completion as before;
  `dispatch.py` and `combiner.py` are untouched.
- **Customized `config/clusters.yaml`** at the old location: re-apply at
  `hpc_mapreduce/config/clusters.yaml`, OR set `HPC_CLUSTERS_CONFIG=/path/to/yours`.
- **Customized templates** at `templates/*.py`: re-apply at
  `hpc_mapreduce/templates/starters/*.py`.
- A user mid-conversation in Claude Code during the upgrade may hit one
  transient error as the model attempts to call the old `agent.*` path.
  Restarting the session fixes it.
