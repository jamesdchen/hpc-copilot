---
status: retro
---
# Proving run #5 — structural hardening: the nudge is the last hand-authoring surface

**Status:** DRAFT — findings + plan from proving run #5. Waves 1–2 SHIPPED this
session (`aedf6fc1` + the wave-2 commit); waves 3–5 sequenced below. As with
[proving-run-2-hardening.md](proving-run-2-hardening.md), code is not edited
mid-run against the live demo tree — each wave lands behind a clean commit
boundary.
**Date:** 2026-07-05
**Origin:** Proving run #5 against real clusters (demo `C:\Users\james\demo-hpc`,
`monte_carlo_pi`, 20 seeds), driven through `/submit-hpc` → the block-drive
surface, across a discovery→hoffman2 cluster retarget under a live canary.
Companion to [human-amplification-blocks.md](../human-amplification-blocks.md) (the
§2 propose→`y`/nudge primitive) and [block-drive.md](../block-drive.md) (the §6
code-driven chain); this hardens both where they *leak in practice*.

---

## 1. Thesis

Proving run #2's thesis was "close the improvisation affordances so the block is
the whole execution and the agent has nothing to build, fetch, or poll." Waves
1–4 of that program landed. Run #5 shows the block-drive loop has **one builder
affordance left**, and it is the load-bearing one: **the nudge.**

human-amplification-blocks.md §2 defines the nudge as "the LLM digests it,
re-drafts, and re-presents." *Re-drafts* is the tell. The driver loop re-homed
sequencing, resolution, and results off the LLM — but at the one point where the
human changes their mind, the skill still instructs the model to **fold the
nudge into the block's inputs**, which in practice means the model hand-writes a
spec JSON file. Every structured-state failure in run #5 happened inside that
single step:

- the `job_env` activation block dropped to `{}` across a hand-carried retarget
  spec → the canary and every status poll died `exit 127` on a bare `python`
  the cluster env never provided (finding 13);
- `scope_id` improvised to `"run"` at the pre-mint S1 boundary (finding 4);
- `supersedes` **deleted** from a spec to get past a gate that had no
  satisfiable answer (finding 10);
- a five-step retarget (close out → re-resolve → re-mint → supersede → re-canary)
  hand-choreographed, and three of the five steps fumbled (findings 9, 10).

The fix is not more gates below the nudge. It is to **remove the last authoring
surface**: the LLM should name the *delta* ("use hoffman2 instead") — genuine
judgment — and a verb should apply that delta to the journaled `resolved` spec
and **re-derive everything the delta invalidates**. The model cannot drop
`job_env` because it never touches `job_env`; it names one field and code
recomputes the rest. This is the determinism boundary
([engineering-principles.md](../../internals/engineering-principles.md)) applied to
its last hold-out: *judgment in the LLM (the delta), mechanism in the verb (the
re-resolve).*

## 2. Symptom → root inventory

| # | Symptom (observed this run) | Root | Wave |
|---|---|---|---|
| 1 | Gate demanded the enumerated 20-seed list; "20 seeds" / "0 through 19" underivable | Gate had no range comprehension | 1 (shipped) |
| 2 | AskUserQuestion answers bypassed the utterance log → genuinely-human values invisible | Capture channel gap | 1 (shipped) |
| 3 | `<task-notification>` logged as a human utterance | Harness-injection pollution of the trust anchor | 1 (shipped) |
| 4 | `scope_id` improvised to `"run"` pre-mint | No sentinel for the pre-resolve boundary | 1 (shipped, skill) |
| 6 | Typed `0,1,…,19` collapsed to one grouped token | `\d[\d,_]*` over-greedy comma grouping | 1 (shipped) |
| 7 | `wait-detached --experiment-dir` → argparse exit 2 | Skill prose habit | 1 (shipped, skill) |
| 9 | "S2 on hoffman2" polled **discovery** for 31 min | Cluster absent from run_id / canary key / layer-1 dedup | 2 (shipped) |
| 10 | Supersession gate dead-ended on a canary-only attempt → `supersedes` dropped | Guard with no satisfiable answer | 2 (shipped) |
| 12 | 30-min silence while every poll died `exit 127`; doctor false-flagged a stall | Poll loop conflates deterministic-env with transient; canary loop stamps no liveness | 3 |
| 14 | `kill` confirmed the job dead on the scheduler but the record stayed `in_flight`; the auto-reconcile silently couldn't settle → agent hand-choreographed reconcile→supersede | `settle`/`reconcile` require the reporter even for a kill-confirmed run; a broken env blinds the settle path too | 3 |
| 13 | `job_env` activation dropped to `{}` across the retarget → `exit 127` (proven: the hand-built sidecar omitted `cluster` + `env.conda_env`) | Activation is authored into specs, not derived; hand-carry can drop it | 4 |
| 17 | sidecar `executor` was the bare string `"train.py"` (no interpreter, not executable) → `exit 127`. `_is_runnable_executor` returned True — it checks only non-empty AND not-the-dispatcher, never actual runnability (its own docstring example has the `python` prefix it fails to require) | A guard named for runnability that does not verify it (engineering-principles "verify a guard can fire") | 4 |
| 15 | Agent relayed "canary green / verified / 20" — stale state + a task count bled from the main array; relay-audit hook caught it but at ~6 correction round-trips | The block brief carries no code-rendered relay; the agent RECONSTRUCTS numbers/state from memory instead of rendering from the journal | 5 |
| — | The whole retarget hand-choreographed; three steps fumbled | **The nudge is a hand-authored spec** | 5 (root) |

Findings 4/7/9/10/13 and the retarget choreography are **one behavior**: the
agent authoring or hand-editing structured state at the nudge boundary. Waves
2–4 backstop the specific corruptions; **wave 5 removes the surface that lets
them happen.**

## 3. The structural moves

### Wave 1 — the utterance lock speaks the human's language *(shipped `aedf6fc1`)*
Range comprehension + comma tokenizer in the authorship gate; a PostToolUse
`answer_capture` hook for typed selector answers; a harness-injection filter on
the capture hook; skill sentinel + gate-refusal remedy prose. The lock held all
run; wave 1 removes its false-positive friction without weakening it.

### Wave 2 — cluster enters run identity; supersession has a satisfiable answer *(shipped)*
`_resolve_layer1` refuses an `in_flight` cross-cluster retarget (never silently
re-attaches to the old cluster's canary); the canary TTL key gains `cluster`;
the supersession gate splits known-but-clean (no-op pass) from unknown (typo
refusal) from live-canary (real close). `resolve-submit-inputs` surfaces a live
canary-only prior at S1 so the human meets the retarget fork one block earlier.
These are **backstops** — they catch the corruption even on the direct
MCP/CLI surface where no skill prose runs.

### Wave 3 — poll-loop honesty: fail fast, never blind, stamp liveness *(finding 12)*
Three edits, all along the path run #5 walked:
- **Escalate by failure class.** A poll that fails because the connection
  *succeeded* and the remote command exited deterministically (rc 126/127 —
  "command not found", the broken-env signature) is not transient and will never
  heal by waiting. `_classify_poll_failure` splits it from `SshUnreachable`/
  transient; K=3 consecutive deterministic-env failures escalate to the existing
  `reporter_unreachable` verdict in ~90 s instead of riding the full 30-min
  budget. Transient stays on the budget — that class belongs to the breaker.
- **An env-independent failure channel.** The dispatcher already writes
  `.hpc_failed/<run_id>.<task>.failed` markers precisely so state survives a
  broken env. `ssh_marker_scan` reads them with plain `sh` (no remote python, no
  activation), invoked only on the deterministic-env escalation path. Markers
  present → settle `canary_failed` with marker evidence; **absent → still
  escalate `reporter_unreachable`** (the scan proves failure only; a marker-less
  blind run is never called passed — the module's never-pass-unverified posture).
- **One tick-stamping definition.** `monitor_flow` stamps the §5 watchdog per
  poll; the canary loop stamped nothing after submit — two loops disagreeing on
  "what a tick means" (the #351 #4 pattern), which false-flagged the discovery
  canary as a stalled driver at 06:10 and left the sidecar frozen at its submit
  stamp. Promote the stamp to one shared helper both loops route through, and
  stamp poll *evidence* (error class + consecutive count) as durable state so
  `status-snapshot` shows "polling, last 3 polls rc=127" instead of a frozen
  timestamp.

### Wave 4 — activation is resolver-owned; remove the hand-carry affordance *(finding 13)*
**Step 0 (establish-which-it-is):** confirm *which* layer emptied `job_env`
before touching code — the demo `.hpc/specs/*.json` plus the resolver derivation
path. A fix at the wrong layer is a guard that never fires.
Then: the cluster-activation block (`CONDA_SOURCE`, `MODULES`, …) is derivable
from `(cluster, clusters.yaml)` and must not be authored into a spec at all.
Submit-time populates it from the cluster entry and **refuses** a spec whose
activation contradicts the entry (refusal, not silent override — the
`apply-safe-defaults` silent-actor pattern this repo already killed). The field
partition gains a third ownership class — *derived-activation* alongside
*caller-extra* — so genuine caller extras (WANDB keys) still pass through.
Reporter-side, extend the **single** `remote_activation_for_sidecar` precedence
(sidecar activation, else derive from `record.cluster`) rather than adding a
second lookup in `verify_canary` — the re-point-don't-duplicate lesson. With the
fallback in the one definition, an empty sidecar env cannot blind the reporter
even for already-damaged runs.

### Wave 5 — the nudge becomes a delta; the retarget becomes an arm *(root)*
The upstream fix waves 2–4 backstop.

- **`revise-resolved` verb (5.1).** Input: the journaled `resolved` spec plus a
  field-level patch `{field: value}`. It applies the patch and **re-runs the
  resolver**, re-deriving every field the delta invalidates (`job_env` from the
  new cluster, the route, the sidecar). The LLM authors only the patch — it
  *cannot* drop `job_env`, improvise `scope_id`, or hand-mangle `EXECUTOR`,
  because it never writes those fields. This closes findings 4/13 by
  construction and replaces the "fold the nudge into the inputs" skill prose with
  "name the field(s) that change."
- **Retarget recovery arm (5.2).** The anomaly terminators name recovery
  *actions*, but cluster-retarget was the one action with no verb — so the agent
  freelanced five steps and fumbled three. Add a chain-table arm composing
  `supersede(old)` → fresh `resolve(new run_name)` → re-canary: one verb, one
  journaled decision. It *composes* wave-2 supersession + the 9c resolve-liveness
  surface — the pieces already exist; wave 5 sequences them in code, not in the
  model.
- **The brief carries a code-rendered relay (5.3, finding 15).** The relay-audit
  Stop hook (conduct rule 10) is a *backstop*: it caught the agent relaying
  "canary green / verified / 20" against a journal reading `complete` / 16 / 1
  task — but the catch cost ~6 correction round-trips. The agent *reconstructed*
  the human-facing numbers from memory (the main array's task count bled into the
  canary summary; "green" went stale when the journal advanced to `complete`).
  Extend Move 1 ("the block emits a complete artifact; the agent never builds")
  to the RELAY: the block brief carries a `relay` summary — the human-facing
  state + numbers rendered by CODE from the journal at the moment the block
  returns — and the agent relays it verbatim. It cannot contradict the journal
  because the relay *is* the journal's rendering. The relay-audit hook then fires
  almost never (a true backstop), and the correction round-trips vanish. This is
  the latency fix: every place the LLM reconstructs state (a spec, a number, a
  relay) is a place it drifts and pays a correction loop — so code renders it and
  the LLM relays, never reconstructs.

## 4. The rendezvous contract change (§2 / §6)

Waves 5.1–5.2 change the propose→nudge loop's *mechanism* without changing its
*shape*. Today (§2): "on a nudge the LLM re-drafts and re-presents." After:

> On a nudge, the LLM extracts the **field delta** and calls `revise-resolved`;
> the verb re-resolves and returns the amended brief; the driver re-presents it.
> Loop until `y`.

The human-visible loop is identical — propose, `y`/nudge, re-present. What moves
off the LLM is the *authoring* of the amended spec. Two consequences to pin in
the block-drive chain table (§6):

1. A nudge that names a field the resolver owns (cluster, walltime, grid) routes
   through `revise-resolved`; a nudge that names an anomaly recovery
   (retarget, kill, resume) routes to the recovery arm. The **route is still a
   function of the spec** (block-drive §4) — the delta's target field selects
   the arm, computed in code, never a verb the model picks.
2. The journaled decision records the **delta**, not a re-authored `resolved`
   blob — so the audit trail shows *what the human changed*, and the gate diffs a
   delta against the prior `resolved`, not two full specs. This tightens the
   rule-9 brief-provenance gate rather than loosening it.

## 5. Status & sequencing

| Wave | Scope | Status |
|---|---|---|
| 1 | Utterance-lock UX (findings 1–8) | **shipped** `aedf6fc1` |
| 2 | Cluster identity + supersession (9, 10) | **shipped** `a8b0ee15` |
| 3 | Poll-loop honesty + kill-confirmed settle (12, 14) | **shipped** `d304b911` |
| 4 | Activation resolver-owned, reporter side (13) | **shipped** `d304b911` |
| 6 | Outputless false-green (16) | **shipped** `01c7eb08` |
| — | Authorship-lock categorical hole (25) | **shipped** `3e7953ad` |
| — | Task-count truth vs `tasks.total()` (21) | **shipped** `12cff110` |
| 7 | Submit-time coherence gate (17, 18, 19, 20, 24) | **shipped** `8568f075` |
| 5.3 | Code-rendered relay (15) | **shipped** `f6eb1ff4` |
| 5.1 / 5.2 | Nudge-as-delta verb + retarget arm (the root) | **designed (§3–§4), not yet mechanized** |
| — | Deferred: 13-residual (job-path activation refusal), 22 (data-axis elision gate), 26 (brief-provenance self-satisfiable), 27 (greenlight consumption) | reported; product / scope decisions pending |

Waves are committed independently, each behind its scoped green suite. The
backstops (2–7) landed first so the eventual surface change (5.1/5.2) has a net
under it, and so the direct MCP/CLI surface (external autonomous agents that
never run the skill prose) stays protected regardless.

### 5.1 The guard-integrity audit (findings 18–24)

Findings 9–17 came one demo segment at a time. Rather than keep chasing
instances, a **read-only audit swarm** (three lenses: cluster-execution fields,
result/identity fields, guard integrity) swept the entire hand-authored-spec
surface at once, testing every field against: *(1) agent-authorable? (2)
validated before the cluster round-trip? (3) cross-checked against derivable
truth (clusters.yaml / tasks.total() / the entry point)?* It confirmed 16/17 and
surfaced **six more of the same shape** — `ssh_target`↔`cluster` (18, finding-9's
true split-brain root), `backend`↔`scheduler` (19), unknown-cluster-to-`{}` (20),
`total_tasks`↔`tasks.total()` (21), the `Activation` non-conda-module proxy (24),
and the authorship lock's categorical hole (25) — several of which *re-opened*
findings marked closed. Wave 7 is their consolidated fix: a submit-time coherence
gate that refuses a spec contradicting derivable truth, loudly, before any SSH.
The lesson generalizes finding 17: **a guard named for a property that checks
only a structural proxy is a latent finding** — enumerate the class, don't chase
the instances.

## 6. Conduct closure

Run #5's real result: **every remaining conduct failure is a child of a
hand-authored spec.** The lock (run #4) proved the agent will not *fabricate* a
value; run #5 proved it will still *corrupt* one it is made to hand-carry. The
roadmap answer is to finish the #200 program all the way up — the last verb the
loop is missing is the one that owns spec *revision*, not just spec *creation*.
When `revise-resolved` and the retarget arm land, the LLM's entire structured-
state surface in the submit loop is: **name a field, name a delta, say `y`.**
