---
status: shipped
---
# Wave 4 — `block-drive`: the code-driven chain

**Status:** LANDED — wave-4 `block-drive` (the code-driven chain) shipped in
`34dd2047`; `hpc-agent block-drive` is now the registered production workflow
driver (`_kernel/lifecycle/block_drive.py`, console script in `pyproject.toml`).
Originally written as a SPEC — the next deliberate step after waves 1–3 (the block
architecture, certified 2026-07-03), gated on the proving run. Consolidates the
design iteration since wave 3; the checklist in §9 is retained as the historical
build record.
**Parent:** [`human-amplification-blocks.md`](human-amplification-blocks.md)
(the guiding principle; this spec is its §9, extracted).

---

## 0. What waves 1–3 already landed (the substrate this builds on)

Wave-4 changes the **control flow**, not the substrate. Already in the tree:

- **Blocks** — submit `s1..s4`, status `snapshot`/`watch`, aggregate
  `check`/`run`, campaign `greenlight`/`watch`/`complete`. Each returns
  `{block, stage_reached, needs_decision, reason, brief, next_block?, run_id?}`.
- **`next_block`** on every block Result (machine-computed successor);
  **greenlight gates** (`ops/block_gate.py::assert_greenlit_target`); the
  **decision journal** (`append-decision`/`read-decisions`, per-scope
  `decisions.jsonl` with `resolved` + `response`).
- The **§5 recovery machine** — watchdog tick stamps + `doctor` (+ `doctor-install`),
  first-class `kill`, guaranteed harvest, cluster `watcher-install`, telemetry
  contract.
- **Detach-by-contract** — `detach:true` default on the cluster-bound verbs
  (S2/S3/S4/speculate); a detached child (no LLM) owns the poll.
- **The warm MCP surface** — `mcp-serve` with the **in-process runner as
  default** (reuses the loaded registry; subprocess runner kept as isolation
  fallback + parity oracle), a **curated catalog derived from the `next_block`
  field**, registered by `install-commands`.
- **Prose inverted** — skills shrunk to block-start + `y`/nudge relay; the
  `claude -p` worker **stranded** from routing (on disk, deletion a later pass).

## 1. The problem wave-4 fixes

The LLM still *executes the deterministic transition*: a block returns its
`next_block`, the skill reads it, the LLM calls the next verb. But `next_block`
is deterministic — one valid successor — so the LLM adds no judgment there; it
is pure dispatch routed through context. This (a) contradicts the guiding
principle "the LLM never executes a transition past a decision point," and (b)
bloats the loop with a tool-call envelope per block. Wave-4 moves the sequencing
into code and collapses the LLM to a **translator at decision points only**.

## 2. The driver: a stateless resumable tick (NOT a parked process)

`block-drive` generalizes the campaign reconcile-tick driver to
submit/status/aggregate. One invocation:

1. chains the deterministic spans in code (S1-resolve → *decision*; or
   S2-canary → *decision*; …), consuming any already-journaled greenlight on
   the way (idempotent — re-reads the `y`, never re-asks);
2. at a decision point, writes `{brief, pending-decision marker, resume cursor}`
   to durable state and **exits**.

Nothing is held open between decisions. Durable state (journal + filesystem) is
the only thing carried — exactly like campaigns. **This is deliberately not a
blocking/parked detached process**: a resident driver that blocks on a response
file for the human-review duration would be a process that can die (needing
watchdog coverage), would hold memory for hours, would need a parked-vs-stalled
disambiguation, and would drag in Windows file-watch mechanics. The stateless
tick has none of that and reuses proven machinery.

## 3. The rendezvous data contract: the code consumes a spec, never a sentiment

**`y`/nudge is chat-level loop control; the code's only input is an approved
spec.** The digestion of natural language into a spec — including every nudge
redraft — happens entirely in chat *before* approval (the LLM's legitimate
translator role). Concretely:

- The driver materializes the proposed spec into the brief. The human nudges;
  the LLM folds the nudge into the block's **inputs** (never a hand-edited
  derived *output* — that is the fabricated-field bug class) and re-presents.
  Loop until `y`.
- On approval the LLM commits the approved input spec to the journal record's
  **`resolved`**. The code's next tick reads `resolved` (an approved spec / a
  pointer to an approved-debugged file) and runs it. **The code never reads a
  nudge string — it reads a spec.** This is the "code never interprets raw
  data / NL" invariant at the rendezvous.
- `response:"y"` is recorded only as the approval sentinel (audit trail); see
  §5 for why even that is not what the code keys on.

## 4. Re-run vs advance: routing derived from the spec, not carried as a token

Two true aspects combine: (a) a nudge must re-run its block to recompute derived
fields, a `y` must advance; (b) the code reads only a spec. They reconcile
because the routing is a **function of the spec**, computed from two things
already in the tree:

- **identity** — `cmd_sha` (the same key canary/speculation dedup uses);
- **field→stage ownership** — `ops/submit/field_partition.py`, which maps every
  field to the stage that resolves it.

The driver routes:

| Approved spec vs last-run inputs | Route |
|---|---|
| unchanged | **advance** to the code-determined `next_block` |
| changed, fields owned by the **current** block | **re-run** the block (recompute its derived fields, emit a fresh brief) |
| changed, fields owned by a **downstream** block | **advance, carrying the edit** (no needless re-run — the S2 "cap the cost" nudge edits *S3's* inputs) |

The LLM never picks the verb; the code does, from identity + ownership.
**Load-bearing dependency:** ownership must be complete for all four flows —
today `field_partition` covers submit only; status/aggregate/campaign field→stage
maps are a concrete wave-4 task. (Open: a nudge touching fields owned by an
*earlier* block is a rewind/cascade — re-run the owning block and everything
downstream; the cascade semantics, e.g. whether S2's canary re-fires, need
spec.)

## 5. Orchestration at a decision point, and what the Stop-hook addresses

Precedent: the existing `skill-return stop guard` (a `Stop` hook) fires when the
LLM is about to end its turn and a committed-but-unconsumed artifact sits on
disk, blocking the stop with a "continue" instruction. It exists because of a
real 2026-06-10 incident — a sub-skill emitted its return and *the turn ended
anyway*, stalling until a human typed "keep going." Wave-4 hits the same hazard
at the decision-commit boundary.

| Phase | What happens | Stop-hook |
|---|---|---|
| **1. Reach decision** | LLM invokes `block-drive`; code chains the span, hits block B's decision, writes `{brief, pending marker, resume cursor}`, exits returning the brief. (A PostToolUse hook, mirroring `skill-return autofetch`, can inject the brief so the LLM reliably has it.) | — |
| **2. Present, then wait** | LLM renders the brief as a proposal, ends its turn awaiting the human. | **Silent.** No `resolved` is committed → nothing to continue. Waiting for the human is a *valid* stop; the hook must not force continuation into a void. |
| **3a. Nudge** | LLM edits the input spec, re-presents → back to Phase 2. | Silent (still nothing committed). |
| **3b. `y`** | LLM commits the approved input spec to `resolved`. **This commit is the approval — and it arms the hook.** | — |
| **4. Advance** | Ideally the LLM invokes the next tick, which consumes `resolved`, routes (§4), runs the next span → Phase 1 or terminal. **Failure mode:** the LLM commits and then *ends its turn* ("recorded, done"), leaving the driver un-advanced — the 2026-06-10 stall. | **Fires.** Committed `resolved` is unconsumed → blocks the stop with `{decision:"block", reason:"approved spec committed for B — invoke block-drive"}`. Self-healing (the tick consumes `resolved`); loop-safe (`stop_hook_active` passes through). |
| **5. Out-of-session** | If the session died between commit and advance (or the answer was queued), the scheduled `doctor` tick finds the same committed-unconsumed `resolved` and advances. | — (doctor, not the hook; latency = doctor interval) |

**What the hook does and does not do:** it does **not** drive the loop (the
driver code does) and does **not** force continuation while waiting for the
human (Phase 2 is a deliberate, allowed stop). It closes exactly one fragile
seam — *once the human's approval is committed, the LLM cannot end its turn
without advancing the driver* — converting honor-system prose ("remember to fire
the next tick") into harness-enforced continuation. The **commit-is-the-approval**
design (§3) is what lets a single filesystem check distinguish "waiting for
human" (nothing committed → silent) from "committed but stalled" (`resolved`
unconsumed → force continue), with no heuristic about turn content.

**Parked ≠ stalled.** A driver legitimately awaiting a human decision is not
ticking but is not dead. The pending-decision marker flips the watchdog's read
(guiding doc §5) from "driver stalled — re-arm?" to "awaiting your decision
since T," so the `doctor` does not false-alarm a parked driver.

## 6. Surface consolidation

The four-surface taxonomy (slash · skill · sub-skill · worker prompt) collapses:

- The **worker prompt** is already stranded.
- The **slash command's** role ("start this workflow") is already projected as
  an **MCP prompt** (`mcp_server._PROMPT_NAMES`). So in an MCP harness the slash
  is Claude-Code-native sugar aliasing the prompt; the MCP prompt is the
  harness-agnostic canonical entry.
- The **skill's** only unique content is the relay-loop prose — which an MCP
  prompt can carry, so slash + skill unify into the prompt. And `block-drive`
  shrinks that prose to near-nothing: with code driving the chain, the LLM only
  translates at rendezvous points, so "answer `y` or nudge" migrates into the
  driver's **brief output**.

Endpoint: **one canonical entry (the workflow name + its thin start-the-driver
instruction, in the registry) + the typed block tools as substrate.** Both the
MCP prompt and the Claude-Code slash are **projections of that one source**, not
hand-authored files. This is the clean test for the slash: a slash earns its
place only if it carries something the canonical entry cannot — and after
wave-4 it does not (its whole job is the entry gesture, which is exactly what
the prompt is). So "humans like typing `/submit-hpc`" justifies a **generated
alias**, never a hand-synced markdown body: a projected slash cannot drift; a
bespoke slash file is the second copy `lint_skill_command_sync` exists to
police. Therefore the wave-4 move is *not* "keep the slash as sugar" — it is
**collapse to one source and project the slash from it (or drop it if it cannot
be projected)**. **`next_block` is re-homed** from an LLM affordance to the
driver's internal chaining table; the blocks remain the substrate the driver
composes (+ direct/debug/MCP use). The sequencing moves to code; the SoT is
projected, never copied.

## 7. Settled decisions since wave 3 (recorded; not all wave-4 build)

- **Enforcement lives in the verb, not the surface.** The greenlight gate, drift
  guard, idempotency, and "cancel/qsub are not primitives" fire at invocation
  regardless of MCP-vs-Bash — so no guard depends on which tool the model
  picked. MCP-preferred is ergonomics + easy-path-hardening, **not** a security
  boundary: the harness's Bash tool coexists and cannot be removed, so MCP-only
  is unenforceable; the verb-level guards are what hold.
- **The CLI is the invariant substrate, not removable.** Detach children
  (`Popen(["hpc-agent", …])`), `doctor-install`, `watcher-install`, the
  `hpc-block-drive` console script, cron/schtasks, cluster nodes, and
  non-MCP harnesses can only invoke the CLI. MCP is a *projection of* the
  registry + the interactive surface — never a replacement. Removing the CLI
  would invert the dependency graph and break detach + the recovery machine.
- **Latency is the process model, not the gates.** The decision gates are local
  file reads (sub-ms) — do not optimize them away. The cost was Python
  cold-start per verb call; the **in-process warm runner** (shipped) fixes it by
  reusing the loaded registry. Keep `_subprocess_cli_runner` (the rename from
  `_default_cli_runner` has LANDED — it is no longer the default) as isolation fallback +
  parity oracle; audit the in-process runner for cross-call global-state
  leakage; broaden the parity test beyond `find` to a mutating + a workflow verb.
- **Curated is an allowlist, so `--allow-mutations ∩ curated` is vestigial.**
  Drop the intersection (the list is the boundary; the block verbs are inherently
  mutating). If a least-privilege "watcher" surface is wanted, split curated into
  observe-vs-drive verbs *explicitly* — not via the verb-type flag, which
  mis-classifies read-only `workflow` blocks.
- **No watcher is load-bearing; reconcile is the backstop.** Reconcile
  re-derives ground truth on any tick, so watchers only shrink detection
  latency, never protect state; default = tail-loop + `doctor`, cluster watcher
  opt-in, no fourth. Full rationale:
  [`human-amplification-blocks.md`](human-amplification-blocks.md) §5.

## 8. Supersedes / re-homes

- **Supersedes** the proposed "fold `append-decision` into the block spec"
  refinement: a code-driven chain never calls `append-decision` between blocks,
  so there is nothing to fold — the driver writes the brief and reads `resolved`.
- **Re-homes** `next_block` (LLM affordance → driver chaining table) and the
  skill relay prose (→ the driver's brief output). Neither is discarded.

## 9. Wave-4 build checklist (gated on the proving run)

1. `block-drive` stateless tick: chain deterministic spans, emit
   `{brief, pending marker, resume cursor}` at decisions, consume `resolved` on
   resume. Reuse the campaign `drive_once` neutral-loop shape.
2. Rendezvous data contract: `resolved` = approved input spec; commit-is-approval;
   the LLM folds nudges into inputs only.
3. Identity + ownership routing (§4) — **and complete `field_partition`
   ownership for status/aggregate/campaign** (the load-bearing gap).
4. Generalize the `skill-return` Stop-hook + PostToolUse-autofetch pair to the
   decision rendezvous (§5); the parked-≠-stalled marker into the `doctor` read.
5. Surface consolidation (§6): one canonical entry in the registry; the MCP
   prompt AND the slash are *projected* from it (or the slash is dropped if it
   cannot be projected — never a hand-synced body); skill relay prose → brief.
6. Polish from §7: rename the subprocess runner, drop `allow-mutations ∩
   curated`, state-leak audit, broaden the parity test.

Blocks stay as the substrate throughout; nothing from waves 1–3 is deleted here
(the §6 worker deletion remains its own separate pass, gated on the same proving
run).
