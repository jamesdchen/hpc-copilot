# Code Deduplication Audit Checklist

Audit goal: identify duplicated code across the composite design — primitives (`hpc_mapreduce/`), agent-facing CLI (`agent_cli.py`, `executor_cli.py`, `skills/`), and human-facing UX (`slash_commands/`). Findings are appended per directory as parallel sonnet agents complete their passes.

Status legend: `[ ]` pending · `[~]` in-progress · `[x]` audited

## Directories under audit

- [x] `hpc_mapreduce/` (top-level modules)
  - [x] `__init__.py` — clean (pure re-exports)
  - [x] `__main__.py`
  - [x] `_time.py` — canonical `utcnow_iso` exists but is bypassed in 5 files; see cross-file note
  - [x] `agent_cli.py` — line 525-530 (`_last_status_age_seconds`) calls `datetime.now(timezone.utc)` raw instead of `_time.utcnow_iso`; subcommand list (213-232) duplicates the `operations.py` frontmatter catalog (drift risk); lines 1811-1818 inline `error_code` strings ("spec_invalid", "internal") that duplicate `slash_commands/errors.py` constants
  - [x] `executor_cli.py` — clean (declarative flag-spec only)
  - [x] `operations.py` — clean source-of-truth
  - **Cross-file**: raw `datetime.now(timezone.utc).isoformat()` repeated at `infra/inspect.py:243,383,714`, `job/planner.py:252,302,548`, `job/runtime_prior.py:361`, `job/calibration.py:335`, `agent_cli.py:525`, `slash_commands/session.py:114` — collapse to `_time.utcnow_iso()` (precision differs — raw omits `timespec="seconds"`)

- [x] `hpc_mapreduce/campaign/` — clean
  - [x] `__init__.py`
  - [x] `dirs.py`

- [x] `hpc_mapreduce/infra/`
  - [x] `__init__.py`
  - [x] `clusters.py`
  - [x] `gpu.py` — inline SSH in `_run_qstat` (136-144) bypasses `infra.remote.ssh_run`; hardcodes 10s timeout vs canonical 60s
  - [x] `inspect.py` — `_parse_mem_to_gb` (663-673) belongs in `query.py`/`backends/parsing.py`; sacct parsing (189-226) duplicates `query.py:173-213`; `_bucket_tenants_by_node` (296-329) re-does the same row splitting; raw UTC isoformat at 243,383,714
  - [x] `remote.py`
  - [x] `backends/__init__.py`
  - [x] `backends/query.py`
  - [x] `backends/sge.py` — `_build_command` (34-59) byte-identical to `sge_remote.py:74-100`; `_build_dependency_flag` (29-32) identical to `sge_remote.py:69-72`; `JOB_ID_REGEX` repeated
  - [x] `backends/sge_remote.py` — should inherit from `SGEBackend`; `_execute_command`/`_setup_log_dir` (102-115) identical to slurm_remote
  - [x] `backends/slurm.py` — `_build_command` (36-69) byte-identical to `slurm_remote.py:83-118`; `_build_dependency_flag` (31-34) identical to `slurm_remote.py:78-81`
  - [x] `backends/slurm_remote.py` — should inherit from `SlurmBackend`; SSH shim (120-133) duplicates sge_remote
  - **Cross-file extractions**: per-scheduler module-level `_build_qsub_command` / `_build_sbatch_command`; new `backends/remote_base.py` `RemoteHPCBackend` mixin; `_parse_mem_to_gb` and shared sacct row parser into `backends/parsing.py`; replace `gpu._run_qstat` SSH inline with `infra.remote.ssh_run`

- [x] `hpc_mapreduce/job/`
  - [x] `__init__.py`
  - [x] `aggregate_flow.py` — `_split_ssh_target` (line 70) identical to `submit_flow.py:68`; raw sidecar JSON read (192-199) bypasses `runs.read_run_sidecar`
  - [x] `backfill.py` — `recommend_walltime_sec` (106-126) inlines logic that `_gather_usable` (240-256) already encapsulates
  - [x] `blacklist.py` — `_with_locked_doc` (121-175) byte-identical to `runtime_prior.py:116-169`; `_parse_iso` (68-77) duplicated in `calibration.py` + `planner.py`
  - [x] `calibration.py` — `_coerce_pos_int` (280-288) duplicated in `runtime_prior.py:376-384`; `_parse_iso` (272-276) is a stricter copy of `blacklist._parse_iso`; raw UTC isoformat at 335
  - [x] `constraints.py`
  - [x] `discover.py` — clean (AST-only; truly distinct from `stages.py` importlib path)
  - [x] `monitor_flow.py` — sidecar JSON read raw (218-225) bypasses `runs.read_run_sidecar`
  - [x] `planner.py` — inline `fromisoformat`+`Z`-replace+tzinfo idiom at 299-300 and 543-546; raw UTC isoformat at 252,302,548
  - [x] `resubmit.py` — clean (correctly delegates to `throughput.compute_submission_plan`)
  - [x] `runs.py` — should expose `wave_map` from `read_run_sidecar` so flows stop reading raw JSON
  - [x] `runtime_prior.py` — `_with_locked_doc` (116-169) byte-identical to `blacklist.py:121-175`; `_coerce_pos_int` (376-384) duplicated in `calibration.py`; raw UTC isoformat at 361
  - [x] `stages.py`
  - [x] `submit_flow.py` — defines `_split_ssh_target` (line 68) identical to `aggregate_flow.py:70`
  - [x] `throughput.py`
  - **Cross-file extractions**: `_split_ssh_target` → `infra.remote`; `_with_locked_doc` → new `hpc_mapreduce/_io.py atomic_locked_update`; `_parse_iso`/`parse_iso_utc` → `_time` (alongside `utcnow_iso`); `_coerce_pos_int` → `runtime_prior` (used by `calibration`); add `wave_map` to `runs.read_run_sidecar` return shape

- [x] `hpc_mapreduce/map/`
  - [x] `__init__.py`
  - [x] `combiner.py` — `_neumaier_sum` (61-77) verbatim copy of `reduce/metrics.py:24-41` (combiner self-acknowledges via comment); `_grid_key` (51-58) mirrors `reduce/metrics._run_id` (116-118); `_load_tasks_module` (104-110) reproduces canonical `hpc_mapreduce.load_tasks_module`; `_format_result_dir` (113-115) reproduces `dispatch._format_result_dir` without the KeyError guard
  - [x] `dispatch.py` — `_load_tasks_module` (40-46) duplicates `combiner.py` + canonical `__init__.py:199`; `_format_result_dir` (49-62) is the reference also reproduced in `combiner.py` and `reduce/status.py:614-617`
  - [x] `metrics_io.py`

- [x] `hpc_mapreduce/reduce/`
  - [x] `__init__.py`
  - [x] `classify.py` — clean (only log-text classifier; doesn't overlap `job/blacklist.py`)
  - [x] `history.py` — clean (delegates to `find_existing_runs`)
  - [x] `metrics.py` — `_neumaier_sum` (24-41) duplicated in `combiner.py`; `_weighted_mean` loop body inlined twice (44-91 in `reduce_metrics`, 169-190 in `reduce_partials`); `_run_id` (116-118) duplicates `combiner._grid_key`
  - [x] `status.py` — `_build_per_task_dict_from_sidecar` (614-617) inlines `template.format(**ctx)` already factored as `dispatch._format_result_dir`
  - [x] `tui.py` — clean (delegates to `reduce/status.py`)
  - **Cross-file extractions**: `neumaier_sum` → new `hpc_mapreduce/_math.py` (combiner stays stdlib-only via deploy-time inline OR import-and-fallback); `grid_key` → new `hpc_mapreduce/_grid.py`; `format_result_dir` → `job/runs.py` using `dispatch.py`'s KeyError-guarded variant as canonical; `_weighted_mean` → private helper in `reduce/metrics.py` consumed by both `reduce_metrics` and `reduce_partials`

- [x] `slash_commands/` (human-facing surface)
  - [x] `__init__.py`
  - [x] `errors.py` — single canonical source for HpcError subclasses (no duplication; agent_cli should consume more strictly per top-level note)
  - [x] `runner.py` — `_ssh_status_report` (172) and `_read_remote_sidecar` (822) inline same `json.loads` + `JSONDecodeError → RemoteCommandFailed` pattern
  - [x] `session.py` — local `_utcnow_iso` (line 114) duplicates `_time.utcnow_iso` (runner.py already aliases the canonical one)
  - [x] `commands/aggregate-hpc.md` — full Steps 0-7 (~280 lines); contract with `skills/hpc-aggregate/SKILL.md` overlaps in invocation, `aggregated_metrics` parsing, `escalation_reason`/`failed_waves` handling, idempotency notes
  - [x] `commands/campaign-hpc.md` — `submit-flow → monitor-flow → aggregate-flow` loop, `campaign_id` slug regex `^[A-Za-z0-9._\-]+$`, `tasks.total() == 0` stop, `MAX_RUNS` retention overlap with `skills/hpc-campaign/SKILL.md`
  - [x] `commands/monitor-hpc.md` — `lifecycle_state` decision table (`complete → aggregate`, `failed → resubmit-failed`, `abandoned → reconcile-journal`, `in_flight → re-poll`, `timeout → re-invoke`) byte-equivalent to `skills/hpc-status/SKILL.md`
  - [x] `commands/preflight.md` — 5-check remediation table (`ssh_auth_sock`, `ssh_on_path`, `rsync_on_path`, `clusters_yaml_parses`, `cluster_known`, `cluster_tcp_22`) duplicated verbatim in `skills/hpc-preflight/SKILL.md`
  - [x] `commands/submit-hpc.md` — Steps 1-10 (~700 lines); shared procedure (build spec, invoke atom, parse `data.deduped`, branch on `spec_invalid`/`ssh_unreachable`) duplicated in `skills/hpc-submit/SKILL.md`

- [x] `skills/` (agent-facing surface)
  - [x] `hpc-aggregate/SKILL.md` — see paired entry above
  - [x] `hpc-build-executor/SKILL.md`
  - [x] `hpc-campaign/SKILL.md` — see paired entry above
  - [x] `hpc-preflight/SKILL.md` — see paired entry above
  - [x] `hpc-status/SKILL.md` — see paired entry above
  - [x] `hpc-submit/SKILL.md` — see paired entry above
  - **Cross-surface extractions**: extract canonical primitive contracts to `docs/primitives/<atom>.md` (already exist for many — push the SKILL/slash-command duplication INTO them):
    - `submit-flow.md` ← shared "build spec → invoke → parse `deduped` → error-branch" contract
    - `aggregate-flow.md` ← invoke + parse + escalation_reason branching
    - `poll-run-status.md` + `monitor-flow.md` ← `lifecycle_state` decision table
    - `check-preflight.md` ← 5-check remediation table (single source of truth)
    - `campaign-status.md` ← per-iteration triplet + `MAX_RUNS` retention + `tasks.total()==0` stop
  - Slash-command MDs keep human-interview steps; SKILL.md files become thin "see primitive doc + here is the agent invocation pattern" stubs.
  - In `slash_commands/session.py:114`, replace local `_utcnow_iso` with `from hpc_mapreduce._time import utcnow_iso as _utcnow_iso` (matches `runner.py` pattern).
  - In `agent_cli.py:1811-1818`, replace inline `error_code` strings with `_err_from_hpc(errors.SpecInvalid(...))` / `_err_from_hpc(errors.HpcError(...))`.

- [x] `docs/` — primitive docs are the natural home for the cross-surface canonical procedures (see slash/skills section); no internal duplication flagged

- [x] `scripts/`
  - [x] `build_operations_index.py` — repeats verb-order list at lines 25 & 88; inline side-effects renderer (line 64) is a stripped variant of `build_primitive_index.py`'s function (drops structured entries silently)
  - [x] `build_primitive_index.py` — `summarize_side_effects` (45-57) is the canonical version; verb-order helper (78-82) duplicated in `build_operations_index`
  - **Extraction**: `scripts/_shared.py` with `REPO_ROOT`, `VERB_ORDER`, `sort_verbs`, `summarize_side_effects`

- [x] `hpc_mapreduce/schemas/` — `envelope.json` exists but is never `$ref`'d
  - `run_id` / `combined_waves` / `failed_waves` triplet defined inline in `aggregate_flow.output.json:10-12`, `monitor_flow.output.json:10,20-21`, `reconcile.output.json:10,16-17`, `status.output.json:10,23-24`
  - `lifecycle_state` enum redefined in `monitor_flow.output.json:12-14`, `reconcile.output.json:12-14`, `status.output.json:12-17` (allowed-value sets differ slightly; structural boilerplate still repeats)
  - **Extraction**: add `$defs` (or new `run_sidecar_fields.json`) and `$ref` from each output schema

- [x] `hpc_mapreduce/templates/`
  - [x] `cli_dispatcher.py` — clean (no overlap with `executor_template`)
  - [x] `tasks_example.py`
  - [x] `starters/executor_template.py`
  - [x] `sge/cpu_array.sh` (67-77) / `sge/gpu_array.sh` (79-89) / `slurm/cpu_array.slurm` (74-84) / `slurm/gpu_array.slurm` (80-90) — `HPC_RUNTIME=uv` 7-line block verbatim across all four; conda setup + `PYTHONPATH` export also identical
  - GPU-template overlap: `sge/gpu_array.sh:92-103` and `slurm/gpu_array.slurm:93-104` share `CUDA_VISIBLE_DEVICES` warning + `PYTORCH_CUDA_ALLOC_CONF`; only `OMP_NUM_THREADS` differs
  - **Extraction**: `templates/common/hpc_preamble.sh` (sourced) + `templates/common/gpu_preamble.sh`

- [x] `tests/` — no `conftest.py` exists; sidecar fixture dict (`sidecar_schema_version: 1`, `run_id`, `cmd_sha: "deadbeef"*8`, `claude_hpc_version: "0.0.0+test"`, `submitted_at: "2026-01-01T00:00:00Z"`, `executor`, `result_dir_template`, `tasks_py_sha: "abc"`) hand-written in:
  - `test_cli_contract_combiner.py:43-54` (`_build_fixture`)
  - `test_cli_contract_dispatch.py:58-68` (`_stub_layout`)
  - `test_cli_contract_status.py:48-58` (`_build_minimal_run`)
  - `test_combiner.py:45-55` (inline)
  - `test_combiner_failures.py:50-60` (inline)
  - `test_dispatch.py:40-50` (inline)
  - `test_e2e_submit_dispatch_status.py:74-80` (inline)
  - The `.hpc/tasks.py` stub (`_TASKS = [...]; def total(); def resolve(i)`) is repeated in all seven
  - **Extraction**: `tests/conftest.py` with `make_sidecar_json(tmp_path, **overrides)` fixture and `write_hpc_tasks(hpc_dir, tasks)` helper

---

## Top-priority dedup proposals (ranked by impact)

1. **Backends remote/local merge** — Make `RemoteSGEBackend` inherit from `SGEBackend` and `RemoteSlurmBackend` from `SlurmBackend`, with a `RemoteHPCBackend` mixin that overrides `_execute_command` + `_setup_log_dir`. Eliminates 4 byte-identical `_build_command` / `_build_dependency_flag` / `JOB_ID_REGEX` blocks across 4 files. Highest LOC payoff and tightest semantic risk surface.

2. **Atomic locked-doc helper** — `_with_locked_doc` is byte-identical between `job/blacklist.py:121-175` and `job/runtime_prior.py:116-169`. Hoist to `hpc_mapreduce/_io.py` as `atomic_locked_update(path, mutate)`. Both modules write JSON docs under flock; one canonical primitive eliminates 55 lines per copy and prevents drift on the lock-discipline path.

3. **`_time.utcnow_iso` adoption** — Eight raw `datetime.now(timezone.utc).isoformat()` callsites should call `_time.utcnow_iso`. Mechanical fix; resolves precision drift.

4. **Cross-surface canonical procedure** — Each operation (submit/aggregate/monitor/preflight/campaign) has both a slash-command MD and a SKILL MD describing the same atom invocation, error-branching, and contract. Hoist the shared contract into the existing `docs/primitives/<atom>.md`, leave human-interview steps in slash-command MDs, and make SKILL MDs reference the primitive docs.

5. **Map/Reduce shared math** — `_neumaier_sum`, `_weighted_mean`, `_grid_key`, `_load_tasks_module`, `_format_result_dir` all repeated between `map/` (cluster-side, stdlib-only) and `reduce/` (local). Either (a) bundle inline at deploy time and import normally locally, or (b) put them in `hpc_mapreduce/_math.py` + `_grid.py` with combiner falling back when the package is absent.

6. **SSH/parse helpers** — `_split_ssh_target` (job/submit_flow + job/aggregate_flow) → `infra.remote`; `_parse_mem_to_gb` (infra/inspect) and sacct row parser → `backends/parsing.py`; `gpu._run_qstat` SSH inline → `infra.remote.ssh_run`.

7. **Sidecar reads through the primitive** — `monitor_flow` and `aggregate_flow` open the sidecar JSON raw to grab `wave_map`. Add `wave_map` to `runs.read_run_sidecar`'s return; remove the inline reads.

---

## Findings by directory

(Populated above per-directory; full per-agent reports retained in run history.)
