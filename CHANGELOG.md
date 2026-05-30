# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
on the wire surface enumerated in
[`docs/integrations/CONTRACT.md`](docs/integrations/CONTRACT.md).

## Unreleased

### Fixed — Non-axis required executor params are now resolved + gated (#195)

When an executor's signature required a param the user didn't sweep (e.g. `samples` when only `seed` was an axis), the generated `tasks.py` `resolve(i)` returned only the axis kwargs — the cluster never exported `HPC_KW_SAMPLES`, and the templated executor command ran `--samples` with no value, crashing every task at argparse. Two layers now address it:

- **Resolve (interview):** entry points gained a `fixed_params` field (on both `register_run` and `shell_command` kinds). Constant non-axis kwargs declared there are baked into every materialized task's `resolve(i)` dict via the same `_INJECT` seam that threads frozen-config shas — so the param ships per-task with its JSON type preserved (an `int` stays an `int`). Like `frozen_configs`, it requires `task_generator` (the framework only threads constants into a materialized `tasks.py`). The `hpc-wrap-entry-point` skill grew a Step 5b that partitions signature params into axis / has-default / uncovered-required and emits `fixed_params` (using the executor's argparse default when present); `/submit-hpc` + `hpc-submit` surface an `uncovered_param` ambiguity so the value can be elicited when there's no default.
- **Gate (validate):** `validate-executor-signatures` now also checks the *reverse* direction — every required signature param (no default, not `*args`/`**kwargs`) must be covered by `resolve()`'s kwargs, else an `uncovered_required_param` error finding. The submit flow already runs this validator, so an uncovered param is now refused statically at submit time instead of failing N cluster tasks. The check skips cleanly when no task was sampled (no false positives). Same "intake refuses structurally broken specs" family as #171 / #184 / #186 / #191 / #192.

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

- **`infra/remote.py` re-exports removed.** PR #131 split the 1000+-line module into `infra/ssh_validation.py`, `infra/ssh_options.py`, and `infra/transport.py`, leaving re-exports back on `infra/remote.py` for backwards compatibility. PR #133 migrated every internal caller (host + `hpc-agent-pro` + tests) to the new paths and then deleted the re-exports. External callers using `from hpc_agent.infra.remote import rsync_push` (and similar for `rsync_pull`, `deploy_runtime`, `run_combiner`, `run_combiner_checked`, `validate_ssh_target`, `parse_remote_json`, `DEFAULT_RSYNC_EXCLUDES`) must update to `infra.transport` / `infra.ssh_validation`. `ssh_run` stays on `infra.remote`.
- **`state/runs.py` re-exports removed.** Same pattern: PR #131 extracted `state/run_sha.py` (`compute_cmd_sha`, `compute_tasks_py_sha`) and `state/wave_map.py` (`derive_wave_map`). PR #133 deleted the re-exports. External callers using `from hpc_agent.state.runs import compute_cmd_sha` must update to `state.run_sha`.
- **`hpc_agent.incorporation.template` back-compat shim deleted.** Was a re-export to `hpc_agent.experiment_kit` after the post-reorg cleanup; sat in place 2+ releases, firing a `DeprecationWarning` at every import (~13 per pytest run from pkgutil discovery). Removed in PR #132. External callers using `from hpc_agent.incorporation.template import <name>` must update to `from hpc_agent.experiment_kit import <name>`.

### Changed — `hpc-agent-pro` module naming aligned with host

The plugin's `_schema_models/` package was renamed to `_wire/` to match the host's post-Pydantic-migration name (PR #132). Internal change to the (unpublished) pro package only; no external impact. Includes the corresponding pyproject lint-ignore + pre-commit hook updates.

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
  proper primitive shipped in the pro wheel — see Added below.

### Added — `install-cron` primitive in `hpc-agent-pro`

`hpc-agent install-cron --ssh-target <target> --experiment-dir <dir>`
installs the wait-predictor crontab entries (snapshot every 5 minutes,
training daily at 03:00) idempotently. Fingerprinted by target module
path so re-running detects existing entries and skips. The three
cron-invoked modules — `snapshot_squeue`, `train_wait_predictor`,
`extract_sacct_history` — moved from the top-level `scripts/`
directories into `hpc_agent_pro._cron/`, so a plain
`pip install hpc-agent-pro` ships everything the cron lines need. The
cron commands use `python -m hpc_agent_pro._cron.<module>` so they
work in any pip-installed environment without an editable source
checkout.

`hpc-agent setup` now detects the pro plugin via the registry and
integrates the cron install into the setup flow:

* When pro is loaded and `--install-cron` is **not** passed: the
  envelope surfaces `data.pro_cron: {status: "available", command:
  "..."}` — a no-mutation recommendation pointing at the follow-up
  command.
* When pro is loaded and `--install-cron` **is** passed (with
  `--cluster <name>`): setup derives `ssh_target` from the cluster's
  `clusters.yaml` entry (`user@host`) and invokes the install-cron
  primitive directly, embedding its result in
  `data.pro_cron: {status: "installed", ...}`.
* When pro is not loaded: no `pro_cron` field; the recommendation is
  silent.

Pip install itself is unchanged — auto-modifying the user's crontab
during `pip install` would be a footgun (needs user-specific args,
side-effects in CI/Docker). The two-step (`pip install hpc-agent-pro`
→ `hpc-agent setup --cluster <name> --install-cron`) is the explicit
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

`hpc-agent-pro` declares its manifest at
`hpc-agent-pro/src/hpc_agent_pro/plugin.py:MANIFEST` (14 primitives,
overlays the `submit` worker prompt, registers a CLI subgroup). The
test helper `tests/_registry_helpers.py:pro_overlaid_workflows()`
reads the manifest's `worker_prompt_overlays` so the snapshot test in
`tests/worker_prompts/test_prefix_snapshot.py` no longer needs the
parallel `_PRO_OVERRIDDEN_WORKFLOWS` allowlist.

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
| `check_results`, `check_results_from_tasks`, `report_status`, `report_status_from_tasks`, `rollup_by_grid_point`, `detect_scheduler` | `hpc_agent.models.mapreduce.reduce.status` |
| `pick_gpu` | `hpc_agent.infra.gpu` |
| `reduce_metrics`, `reduce_by_grid_point`, `reduce_partials`, `reduce_resource_usage` | `hpc_agent.models.mapreduce.reduce.metrics` |
| `classify_failure` | `hpc_agent.models.mapreduce.reduce.classify` |
| `ExecutorInfo`, `discover_executors`, `is_executor_source` | `hpc_agent.state.discover` |
| `ClusterConstraints`, `parse_constraints` | `hpc_agent.infra.constraints` |
| `WorkloadSpec`, `SubmissionPlan`, `compute_submission_plan`, `build_wave_map` | `hpc_agent.infra.throughput` |
| `inspect_cluster` | `hpc_agent.infra.inspect` |
| `append_runtime_sample`, `roll_up_runtime_quantiles` | `hpc_agent.state.runtime_prior` (as `append_sample`, `roll_up_quantiles`) |
| `compact_task_ids`, `ResubmitBatch`, `ResubmitPlan`, `resubmit_plan` | `hpc_agent.ops.recover.batching` |
| `write_metrics` | `hpc_agent.models.mapreduce.metrics_io` |

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
shim so the ``hpc-agent-pro`` plugin and a handful of legacy import
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

### Added — pro plugin demos cross-package composition

Four new primitives in `hpc-agent-pro` exercise the
plugin-composes-core path:

- `plan-resubmit-overrides` (query) — promotes
  `plan_resubmit_overrides` to a wire-callable primitive.
- `smart-resubmit-flow` (workflow) — composes
  `plan-resubmit-overrides` (pro) + `resubmit-failed` (core); proves
  the cross-package compose path via lazy resolution against the
  merged registry.
- `apply-smart-submit-plan` (workflow) — code-ifies Step 4c-B of
  pro's `submit.md`: applies auto-pick + auto-apply rules from a
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
- **`hpc_agent.mapreduce` → `hpc_agent.models.mapreduce`**.
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

The `hpc-agent-pro` plugin is updated in lockstep with the reorg
(see its own changelog entry); no version pin changes are required
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

### Removed — scheduling-strategy layer extracted to `hpc-agent-pro`

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
