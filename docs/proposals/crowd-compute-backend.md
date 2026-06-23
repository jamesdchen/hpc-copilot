# Proposal: crowd-sourced compute backends (Vast.ai / SaladCloud / Akash)

Status: **proposal — no core changes**. This page records the seam
analysis for running hpc-agent task fan-outs on crowd-sourced compute
platforms, and what ships today to prepare for it (two skeletons under
[`examples/`](../../examples/README.md)). Nothing here changes runtime
behavior.

## Why

Crowd-compute marketplaces (Vast.ai, SaladCloud, Akash) rent idle
consumer GPUs at prices well under institutional-cloud rates. The
workload hpc-agent orchestrates — embarrassingly parallel array batches
with per-task sidecars — is exactly the shape those platforms accept,
*if* the task is containerized and tolerates interruption. Notably, the
existing `preempted` error code and selective-resubmit flow already
model interruptible nodes; that part of the contract transfers as-is.

## Where it plugs in (already exists)

- **Backend registry.** `hpc_agent.infra.backends.HPCBackend` +
  `register(name)` / `get_backend(name)`. The base class was widened
  (B5) precisely so callers dispatch on backend attributes and
  capability hooks instead of `if scheduler == "slurm"` branches. The
  override points an API-driven backend needs are explicit:
  `_execute_command` ("override for remote execution"), the
  `alive_job_ids` / `query_jobs` / `classify_scheduler_state` /
  `stderr_log_path` capability hooks, and `submit_plan` for
  backends with no shell submit command at all.
- **Plugin loading.** The `hpc_agent.plugins` entry-point group
  (`hpc_agent._kernel.registry.plugins`) imports a plugin's
  `primitive_modules` for their registration side effects — the same
  import that runs a `@register("<name>")` decorator on a backend
  class. A crowd backend therefore needs no host edit to *load*.
- **The experiment contract is already transport-agnostic.**
  `.hpc/tasks.py` (`total()` / `resolve()`) and the dispatcher env
  contract (`HPC_KW_*`, `RESULT_DIR`, `HPC_TASK_ID`) assume nothing
  about SSH or schedulers. This is the surface to protect; see
  [`docs/integrations/CONTRACT.md`](../integrations/CONTRACT.md).

Why a plugin and not core: the four-question boundary test in
[`docs/internals/engineering-principles.md`](../internals/engineering-principles.md).
Platform-SDK knowledge fails Q4 — its correctness is only testable
against the real API/SDK — so it belongs in a plugin whose CI carries
the dependency. Q2's growth-trigger rule applies the day a second
crowd platform lands: collapse any platform-name branching into a
registry, never a second inline branch.

## What breaks (the honest list)

Two load-bearing assumptions do not survive contact with crowd
platforms, and two host seams are not yet pluggable:

1. **SSH-to-a-login-node transport.** `infra/remote.py` /
   `infra/transport.py`: `deploy_runtime` ships files via rsync/scp,
   monitoring shells out via `ssh_run`, `aggregate` pulls results with
   rsync. Salad and Akash are pure-API (no SSH target). Vast.ai rents
   SSH-able instances, which is why it is the first target — parts of
   the remote machinery carry over.
2. **Shared filesystem.** There is none; each task ships data in and
   results out. The container example under
   `examples/crowd-compute-executor/` keeps the in-container contract
   identical and pushes the ship-in/ship-out problem to the
   platform-side launcher, where it belongs.
3. **Config validation.** ~~Deferred core edit #1.~~ **Landed**: the
   `clusters.yaml` `scheduler:` validator
   (`infra/clusters.py::_require_pin_for_unknown_family`) now accepts,
   besides a known family or a pinned `scheduler_profile`, any name a
   loaded plugin registered — resolved through
   `infra.backends.registered_backend_names()`, which imports plugin
   `primitive_modules` for their `@register` side effect. Tests:
   `tests/infra/test_clusters.py::TestKnownSchedulerFamilies` and
   `tests/infra/backends/test_registered_backend_names.py`. Note
   `scheduler_resolve.py` still deliberately *probes* only curated
   families — a plugin backend is named in config, never auto-detected.
4. **Backend construction.** ~~Deferred core edit #2.~~ **Landed**:
   `remote_factory.build_remote_backend` now ends in a registry
   dispatch — a *backend_name* its inline ladder doesn't know but the
   registry does constructs itself via the new
   `HPCBackend.from_build_context(ctx)` classmethod hook, receiving a
   `BackendBuildContext` value object carrying every factory input
   (including the bound `ssh_run` transport, which an SSH-shaped
   marketplace backend may reuse and a pure-API one ignores). A
   registered backend that hasn't overridden the hook fails loud with
   `NotImplementedError`, per the capability-hook convention. Tests:
   `tests/infra/backends/test_build_context_seam.py`.

Both edits were small, reviewed changes to declared seams — consistent
with "core dispatches, never branches." What remains before a crowd
backend can run real work is entirely plugin-side: the platform API
calls behind the skeleton's stubs.

### Known follow-up: `resolve_scheduler_profile`

`reduce/status.py::resolve_scheduler_profile` carries its own
`_KNOWN_SCHEDULER_FAMILIES` gate and raises `SpecInvalid` for an
unknown, unpinned family. It does **not** consult
`registered_backend_names()`, so it disagrees with the config
validator about whether a plugin name needs a pin. This is currently
inert: the helper has no production call site (the live submit/recover
path reads `scheduler_profile` straight off the spec and never calls
it), so the disagreement cannot surface today. It is left as-is rather
than papered over because the right reconciliation is not "return a
golden profile for `vastai`" — a pure-API backend has *no* profile;
it is for the caller to skip profile resolution for registered
non-profile backends. When a real plugin first needs profile
resolution, that is the seam to revisit.

## Trust model

Crowd nodes are untrusted: results can be wrong or malicious.
Validation lives at the layers the integrator already owns —
`verify-canary` gates fan-out platform-agnostically, and redundant
execution / output checks fit in `tasks.py` and the combiner. Core
gains no per-platform trust logic.

## What ships now

- [`examples/crowd-compute-executor/`](../../examples/crowd-compute-executor/) —
  a stdlib-only containerized executor proving the dispatcher env
  contract is platform-neutral. The same image runs under the SLURM
  dispatcher (Apptainer) or a crowd platform.
- [`examples/plugins/hpc-agent-vastai/`](../../examples/plugins/hpc-agent-vastai/) —
  a skeleton plugin package showing the full registration path
  (entry point → manifest → `@register("vastai")` backend subclass)
  with every capability hook stubbed and documented. It is honest
  scaffolding: every method raises `NotImplementedError` with the
  mapping it expects, and nothing pretends to talk to an API.

## Order of work (when implementation starts)

1. Vast.ai first (SSH-shaped; `_remote_base` partially reuses).
2. ~~Core edit #1 (config seam) and #2 (construction seam)~~ — both
   landed, see "What breaks" items 3–4. No core work remains.
3. Platform API calls + a recorded-fixture CI suite in the plugin repo.
4. Salad/Akash later via the same seams; registry-collapse rule
   applies at member #2.
