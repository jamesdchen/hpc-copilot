# Comparison: Bohrium + SciMaster (arXiv:2512.20469) vs. hpc-agent

> **Status:** analysis. No core changes proposed here directly; this records
> the seam comparison against the Bohrium + SciMaster platform paper, names
> the two ideas worth borrowing, and ties one of them (heterogeneous backends)
> to work **already shipped** in this repo. The actionable proposal — a
> unified `trace` query verb — is sketched in §4 and is the only new surface
> suggested.

## 1. What the paper is

[*Bohrium + SciMaster: Building the Infrastructure and Ecosystem for Agentic
Science at Scale*](https://arxiv.org/abs/2512.20469) (Zeng et al., DP
Technology / DeepModeling, Dec 2025) describes a **vertically-integrated,
mostly-proprietary cloud platform**, not a tool:

- **Bohrium** — the execution substrate ("the HuggingFace of AI-for-Science").
  Turns data, software, compute, and *lab equipment* into "agent-ready
  capabilities" for Reading / Computing / Experiment, behind a capability +
  tool registry with observability and governance. Three named subsystems:
  - **Science Navigator** — literature / patent retrieval
  - **Lebesgue** — unified compute orchestration across **cloud / HPC / AI
    accelerators**
  - **UniLabOS** — programmable wet-lab robotics
- **SciMaster** — the orchestration runtime: long-horizon, multi-agent,
  **DAG-style traceability**, with an explicit split between *reasoning*
  (tool-augmented models) and *governed execution* on Bohrium. Plus a
  "scientific intelligence substrate" (a model hierarchy + a knowledge base,
  SciencePedia). The paper demos 11 "master agents" at multi-million-task
  scale and reports order-of-magnitude cycle-time reductions.

Exactly **one** of those boxes — **Lebesgue** — solves the same problem
hpc-agent solves. The rest (literature, lab robots, model zoo, knowledge
graph) is out of scope for this project.

## 2. The mapping (we independently reached the same thesis)

hpc-agent encodes the same core thesis as Bohrium's Lebesgue + tool
registry — *"make compute an agent-ready capability via stable interfaces plus
recorded execution traces"* — scoped to SGE/SLURM/PBS and self-hosted:

| Bohrium / SciMaster concept | hpc-agent equivalent |
|---|---|
| Lebesgue: unified compute orchestration | `infra/backends/` + `infra/remote.py` + `ops/submit_flow.py` |
| "Agent-ready capabilities" via stable interfaces | `@primitive` registry + JSON envelope + `mcp-serve` |
| Capability / tool registry | `_kernel/registry/` + `hpc_agent.plugins` entry-points |
| DAG-style traceability / execution traces | `state/journal.py`, `provenance-manifest`, `<run_id>.monitor.jsonl`, `state/stages.py` |
| Reasoning vs. governed execution split | the decide/act boundary (`docs/architecture.md` §"The decide / act boundary") |
| SciencePedia / cross-run knowledge | `interview` + `recall` memory primitives |
| Multi-agent long-horizon orchestration | `/campaign-hpc` closed loop |
| Heterogeneous substrates (cloud / HPC / accelerators) | backend registry + `github-actions` plugin (§3) |

## 3. "Redundant?" and "could I have just used it?"

**Redundancy is external, not internal.** Nothing in this repo is dead weight
against the paper. Conceptually the whole stack is a *narrow, open,
self-hostable re-implementation of one Bohrium slice* (Lebesgue + the
capability registry + execution tracing) for the scheduler families we
actually target. There is no duplication to delete inside the codebase.

**Could the project have used Bohrium instead? For our use case, no.** The
target here is parameter sweeps on *institutional* clusters (Hoffman2/UCLA SGE,
Discovery/USC SLURM), self-hosted, open-source, pointed at compute we already
have. Bohrium is a managed commercial cloud tied to its own substrate;
Lebesgue orchestrates *their* compute, not an existing university SGE queue the
way hpc-agent SSHes in and `qsub`s. Adopting it means moving jobs onto their
platform and taking on a vertical stack (lab robotics, literature, model
hierarchy) we don't need. The only world where "yes" holds is one where the
requirement were "I want a full managed AI-for-science cloud and I'm happy
living in their ecosystem" — a different requirement than the one this repo is
built for.

The paper therefore **validates** the architecture (we arrived at the same
agent-ready-compute thesis independently) rather than obsoleting it.

## 4. Steal #1 — execution traces as a first-class, queryable surface

Bohrium's headline is that recorded execution traces become **"execution-
grounded signals"** mined at scale, and SciMaster sells **DAG-style
traceability** as a product feature. We already record everything Bohrium
records — we just don't *join* it into one replayable, agent-queryable graph.

### What exists today (five disjoint trace surfaces)

| Surface | Location | Owner |
|---|---|---|
| Per-run journal records | `~/.claude/hpc/<repo_hash>/` (`index.json` + per-run) | `state/journal.py`, `state/run_record.py` |
| Per-run sidecars (cmd, wave map, `cmd_sha`, trial params) | `.hpc/runs/<run_id>.json` | `state/runs.py` |
| Per-run monitor event stream | `<run_id>.monitor.jsonl` | `_kernel/extension/telemetry.py` (`monitor-jsonl` sink) |
| Per-campaign provenance manifest | `.hpc/provenance/<cid>.json` (signed) | `ops/provenance_manifest.py` |
| Campaign manifest / cursor | `.hpc/campaigns/<cid>/{manifest,cursor}.json` | `meta/campaign/` |

An OTel/OTLP sink already exists (`telemetry.py` `"otel"`), so live spans/
metrics export is solved. What's missing is a **single read verb** that
assembles these into one DAG for a campaign or run — the thing an agent (or a
human, or a paper appendix) asks "show me exactly what produced this result and
in what order."

### Proposed: `hpc-agent trace`

A pure `verb="query"`, read-only, idempotent primitive — no new persisted
state, fully derived from the sources above (same discipline as
`provenance-manifest`, which recomputes from sidecars every call).

```
hpc-agent trace --campaign-id <cid> [--experiment-dir <dir>] [--format dag|flat]
hpc-agent trace --run-id <id>       [--experiment-dir <dir>]
```

**Output** — one envelope whose `data` is a DAG:

- **nodes**: one per `{campaign, run, wave, task, decision}`. Each carries the
  provenance fingerprint already on the sidecar (`cmd_sha`, `tasks_py_sha`,
  `data_sha`, `env_hash`, `cluster`, `profile`), its lifecycle status from the
  journal, and timing (`submitted_at`, terminal time from the `.monitor.jsonl`
  stream).
- **edges**: `wave -> wave` scheduler dependencies (the stagger from
  `infra/throughput.py:build_wave_map`), `resubmit-of` (recover flow),
  `refill-of` (async-refill campaigns), and `run -> campaign` membership
  (`state/index.py:find_runs_by_campaign`).
- the signed `signature` from `provenance-manifest` as the DAG's root attest.

**Why it's cheap and on-brand:** every field already exists on disk; the verb
is a join + topological assembly, not new instrumentation. It composes
`provenance-manifest`, `campaign-status`, and `reconcile-journal` via
`composes=[...]` and shows up in the operations catalog as the canonical
"explain this campaign" surface. It is the read-side complement to the OTel
write-side sink: OTel streams the trace live to Grafana; `trace` reconstructs
it after the fact for replay/audit/agent-consumption.

**Non-goals:** it does not *reason* over the trace (no scoring, no anomaly
detection) — that stays in the calling agent, exactly as `recall` returns
observed ranges and leaves the reasoning to its caller. It introduces no new
file under `.hpc/`; the DAG is computed and returned, never persisted.

## 5. Steal #3 — heterogeneous backends — **already in progress**

Lebesgue's differentiator is one interface over cloud + HPC + AI accelerators.
This repo is *already* executing that idea through the backend registry seam,
not just planning it:

- **`hpc-agent-github-actions` plugin** (`examples/plugins/hpc-agent-github-actions/`)
  — a registered `@register("github-actions")` `HPCBackend` that fans a task
  array onto GitHub Actions runners with **no SSH and no shared filesystem**:
  - "scheduler" = the Actions REST API; "array of N tasks" = one workflow run
    whose matrix has N cells; "job id" = the Actions run id; results come back
    as **artifacts** instead of over a mount.
  - `requires_ssh = False` flips the submit/preflight/monitor/aggregate flows
    off their SSH/rsync paths onto the backend's `alive_job_ids` /
    `task_statuses` / `fetch_results` / `fetch_logs` hooks.
  - **account-pool rotation** (`HPC_GHA_POOL`): on a quota/billing `403` the
    backend advances to the next `owner/repo=TOKEN_ENV` account and
    re-dispatches — durable state is local (the Optuna study + completed-
    iteration sidecars), so rotation loses nothing. This is a crowd-compute
    pattern Lebesgue would call "burst to additional capacity."
  - CI builds and tests it in an isolated `plugins` job (offline, fake API
    client) and `actionlint`s the shipped workflow template.
- **`docs/proposals/crowd-compute-backend.md`** — the seam analysis for the
  *next* substrates (Vast.ai / SaladCloud / Akash). Two host edits it needed
  (config validation accepting any plugin-registered backend name; registry
  dispatch in `build_remote_backend` via `HPCBackend.from_build_context`) have
  **already landed**. The transport-agnostic experiment contract
  (`.hpc/tasks.py` + `HPC_KW_*` / `RESULT_DIR` / `HPC_TASK_ID`) is the surface
  it protects, and the existing `preempted` error code + selective-resubmit
  flow already model interruptible nodes.

**Assessment vs. Lebesgue.** The *abstraction* matches: a registry of backends
behind one submit/monitor/aggregate contract, dispatch-on-capability instead of
`if scheduler == ...`. The gap is breadth and governance, not architecture:

| Lebesgue has | hpc-agent has | Gap |
|---|---|---|
| Cloud + HPC + accelerators behind one API | SGE/SLURM/PBS (SSH) + GitHub Actions (API) | More backends (Vast/Salad/Akash) — already specced |
| Managed quota / billing governance | per-account pool rotation on `403` | Centralized budget across backends, not just rotate-on-exhaust |
| Unified observability across substrates | per-backend hooks + OTel sink | A backend-agnostic trace view — see §4 (`trace` would close this) |

So §4 (`trace`) and §5 (heterogeneous backends) are complementary: a
backend-agnostic `trace` verb is exactly what makes a multi-substrate fleet
(SSH cluster + Actions + a future Vast.ai backend) observable as *one*
campaign, which is the remaining piece between "we have heterogeneous
backends" and "we have Lebesgue's unified view."

## 6. Bottom line

- **Redundant:** nothing internally; the whole stack mirrors one Bohrium slice.
- **Steal:** (1) a unified `trace` query verb — high-value, cheap, derived from
  existing state; (3) keep extending the backend registry toward crowd/cloud
  substrates — **already underway** via the `github-actions` plugin and the
  crowd-compute proposal.
- **Could-have-used-it:** not for institutional, self-hosted HPC. The paper
  validates the design rather than replacing it.

## Sources

- [arXiv:2512.20469 — Bohrium + SciMaster](https://arxiv.org/abs/2512.20469)
- [Emergent Mind: Bohrium + SciMaster topic](https://www.emergentmind.com/topics/bohrium-scimaster)
- [Bohrium platform](https://www.bohrium.com/en)
</content>
</invoke>
