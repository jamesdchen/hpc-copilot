---
status: retro
---
# Proving run #2 — structural hardening: close the agent's improvisation affordances

**Status:** DRAFT — findings + plan from proving run #2. Implementation held until
the run completes (the demo venv is editable-linked to this tree, so editing
code mid-run mutates the running flow).
**Date:** 2026-07-04
**Origin:** Proving run #2 against real UCLA Hoffman2 (demo `C:\Users\james\demo-hpc`,
`monte_carlo_pi`, 10 seeds), driven through the block-drive / MCP surface.
Companion to [human-amplification-blocks.md](../human-amplification-blocks.md); this
hardens its §1 principle where that principle *leaks in practice*.

---

## 1. Thesis

human-amplification-blocks.md §1 says the LLM "never decides, never executes a
transition past a decision point, and never interprets raw data." Proving run #2
showed the **surfaces still let it try** — and every time it did, it flailed.

The ~10 distinct failures this run surfaced are **one behavior with four
enablers**: the agent stepped into execution work that code should own —
hand-building the submit spec, `find /`-ing the disk for schemas, base64-wrapping
a mangled `EXECUTOR`, spawning and racing two detached workers, tailing logs, and
narrating "still running" from elapsed time while the job had already failed.

The fix is **not** ten patches. It is to **close the improvisation affordances so
the block is genuinely the whole execution and the agent has nothing to build,
fetch, or poll.** The agent is a *relay*, not a *builder*. Every place it *can*
improvise execution is a place it *will*, and improvised execution is where every
failure lived.

## 2. Symptom → root inventory

| # | Symptom (observed this run) | Root move |
|---|---|---|
| 1 | `bash -lic` submit wrapper hung every no-PTY SSH submit (120 s timeout → misreported `dispatcher_failed`) | E (fixed) |
| 2 | Agent shelled legacy `run --workflow submit` (spawned a `claude -p` worker) instead of the `submit-s1` block | 3 |
| 3 | Agent ran `find /` for `append_decision.input.json` (5-min disk crawl) | 2 |
| 4 | `resolved.next_block` not defaulted → greenlight append → gate-reject → re-append | 1 |
| 5 | Agent hand-set `job_env["EXECUTOR"]` to the per-task one-liner → comma guard → base64 detour | 1 |
| 6 | Canary died `SyntaxError` — unquoted `$EXECUTOR` word-split (`-c import`) | 1 |
| 7 | No §5 watchdog armed (no OS-scheduler task, no `next_tick_due` stamped) | 4 |
| 8 | Two detached `submit-s2` workers racing the same run | 4 |
| 9 | Agent narrated "canary running / no result yet" while it had already failed | 2 + 4 |
| 10 | SSH round-trip contention (no ControlMaster on Windows + duplicate workers → connection timeouts) | 4 |
| 11 | `cat schemas/*.input.json`, reading site-packages source for contracts | 2 |

## 3. The four structural moves

### Move 1 — The block emits a complete, ready-to-run artifact; the agent never builds
*Closes: 4, 5, 6.*
- `build-submit-spec` **owns** `EXECUTOR` (always the dispatcher `python3
  .hpc/_hpc_dispatch.py`) and the sidecar (the per-task one-liner). The framework
  must **refuse an agent-supplied non-dispatcher `EXECUTOR`** — the symmetric
  guard already exists in the other direction (`write-run-sidecar` refuses
  dispatcher-shaped *sidecar* values); the missing half is refusing
  one-liner-shaped *`EXECUTOR`* values.
- The brief carries the **exact next action** (the MCP tool + arg skeleton, or a
  fully-formed command) — the agent *executes* it, never *constructs* it.
- Code defaults every mechanical field (`resolved.next_block` from the block
  Result's own `next_block`).
- Principle: **remove every place the agent can build.**

### Move 2 — Push information to the agent; never make it pull — and guards must *route*
*Closes: 3, 9, 11.*
- `hpc-agent describe <verb> --schema` returns the schema **content** (wire the
  existing `schema_for(name,"input")` resolver, `_kernel/registry/operations.py`);
  briefs embed it. No `find` / `cat` for a schema, ever. The package owns its
  schemas via `importlib.resources`; the filesystem path is not an API.
- Every guard **routes**, never merely forbids. Blocking `cat` / `python -c`
  *without* a sanctioned schema route is precisely what funneled the agent into
  the `find /` hang. Provide the route, don't tighten the block.
- Run **state** is read from reconcile/journal (which reflects the cluster's
  terminal markers), pushed to the agent as transitions. The agent never infers
  "still running" from elapsed time.

### Move 3 — Delete the legacy worker path; it cannot be a trap if it is gone
*Closes: 2.*
- §6: delete the `run` verb (`cli/spawn.py`), `_kernel/extension/worker_prompts/`,
  and reconcile the stale `docs/internals/submit-sequence.md` (which still
  diagrams `run --workflow submit → bare worker` as the canonical flow).
- Invalidate the stale skill-description cache so the injected skill listing
  matches the on-disk block-drive body (this run's agent followed a cached
  pre-block-drive description that literally said "hand off via `hpc-agent run
  --workflow submit`").

### Move 4 — Detached execution: single, watchdog-backed, reconcile-truthed
*Closes: 7, 8, 9 (state), 10.*
- **Idempotent-single:** a per-`(run, block)` lease so a second detached launch is
  refused or replaces the first — never two racing (this run had `submit-s2`
  running twice against one run, self-inflicting SSH contention).
- **Arm the §5 watchdog by default** at submit: stamp `next_tick_due` and install
  the OS-scheduler dead-man's switch (`doctor-install`) as part of the flow, not
  an optional extra. Progress then survives session/process death; no manual
  polling required.
- **Reconcile is the single source of truth.** The agent relays state
  transitions; it never tails a log or counts elapsed seconds. "No result yet"
  must come from reconcile, not from "the shell is still open."
- Fewer SSH round-trips + no duplicate workers → no connection exhaustion. (On
  Windows, named-pipe `ControlMaster` is unavailable, so cutting the round-trip
  *count* is the only lever; a duplicate worker doubles it.)

## 4. Mechanism confirmations (evidence)

- **Unquoted `$EXECUTOR` + comma `-v` (Move 1).** `cpu_array.sh` ships
  `qsub -t … -v …,EXECUTOR=…,… cpu_array.sh` (comma-delimited) and runs
  `time $EXECUTOR` (**unquoted**). So a `python3 -c "import argparse,…"` one-liner
  placed in `EXECUTOR` breaks twice: the comma truncates the `-v` value, and the
  word-split hands `-c` only `import` → `SyntaxError`. The only safe `EXECUTOR` is
  the comma-free, space-safe dispatcher; it then reads the one-liner from the
  sidecar JSON and runs it correctly.
- **`describe` returns only the schema name (Move 2).** `describe append-decision`
  yields `input_schema: "append_decision.input.json"` — a name, not content or a
  path. The MCP tool surface *does* embed the full schema
  (`_tool_input_schema → inputSchema`), so MCP callers are fine; CLI/`describe`
  callers must hunt. Hence `find /`.
- **`bash -lic` hang (Move E, fixed).** `-i` (interactive) on a no-PTY SSH exec
  channel blocks in terminal/job-control init until the 120 s `_execute_command`
  timeout, misreported as `dispatcher_failed`. `bash -lc` (login only) resolves
  `qsub` at `/u/systems/UGE8.6.4/bin/lx-amd64/qsub` and returns cleanly. Fixed in
  `infra/backends/_remote_base.py`; regression-pinned in `test_backends_sge_remote.py`.

## 5. Status & sequencing

- ✅ **Fixed:** `bash -lic → bash -lc` (submit wrapper). The exemplar of the whole
  class — a seam validated by design review, never by execution, until a real
  cluster forced it.
- ⏸ **Pending (held until this run completes):** Moves 1–4. Order by what gates
  the run: **Move 3 + Move 1 + Move 2 first** (they stop the flailing that keeps
  derailing submit), then **Move 4** (needed for the long main-array wait to be
  crash-durable).

Every item here is a *"seam validated by design review, not execution."* The
proving run is the forcing function; expect more of this class until the flow has
run end-to-end on a real cluster several times.

## 6. Conduct closure — every conduct rule gets a mechanized counterpart

Proving run #3 (2026-07-04) sharpened the thesis: the Class-2 failures are not
individual bugs but one structural tension — **the doctrine's constraint
surface was prose, while the agent's capability surface is everything.** Prose
drifts with every model/harness update; mechanisms don't. Extending
engineering-principles' "a guard the LLM itself satisfies is not a guard" from
code to conduct: every conduct rule below carries (or gets) a machine
counterpart. This is a finite closure, not whack-a-mole — the agent needs
exactly four things (information, something to await, specs built for it,
bounded actions), and each has an owner.

| # | Conduct rule | Mechanism | Status |
|---|---|---|---|
| 1 | Never hand off to a spawned LLM worker | §6 deletion — the path does not exist | shipped |
| 2 | Never advance past an ungreennlit gate | `block_gate.assert_greenlit_target` | shipped (pre-existing) |
| 3 | Never end the turn on an unconsumed greenlight | decision-rendezvous Stop hook | shipped (pre-existing) |
| 4 | Never place the per-task one-liner in `EXECUTOR` | `_check_executor_is_dispatcher` | shipped |
| 5 | Never hand-restate the greenlit successor | `next_block` default: parked `resume_cursor`, falling back to the static chain table (`infra/block_chain.ORDER`) — mode-independent (v2; v1's pending-decision-only derivation missed the MCP-direct mode and the papercut re-fired in run #3) | shipped |
| 6 | Never supply a defaultable spec field | block-owned defaults — sidecar synthesis defaults `result_dir_template` to `results/{run_id}/task_{task_id}` (``{task_id}`` is a reserved dispatcher render key: collision-free for any axis) instead of dying `SpecInvalid` for the agent to paper over | shipped |
| 7 | Never run mutating scheduler commands (ssh transport included) | `scheduler_write_fence` PreToolUse hook: blocks `qsub`/`sbatch`/`qdel`/`scancel`/`qmod`/`qalter` in command position — including inside `ssh`/nested shells — while read-only probes (`qstat`/`squeue`/`qacct`, plain `ssh`) pass ("consequences are gated, curiosity isn't" — decided 2026-07-04). Shipped by `install-commands` into `hooks.PreToolUse` | shipped |
| 8 | Never poll a detached worker on a timer / infer progress from elapsed time | `wait-detached` (blocking lease-pid wait, launched via harness backgrounding → the harness wakes the agent exactly once, at completion) + the reconcile-is-truth skill rule. Deliberately CLI-only (a blocking call would wedge the synchronous in-process MCP dispatch) | shipped |
| 9 | Never fabricate or divert a `resolved` field the brief didn't recommend | **provenance gate** (designed, next): blocks persist their brief durably at the decision boundary (code-side, both driving modes — the v1 lesson says never key this on block-drive-only state); `append-decision` then refuses a greenlight whose `resolved` diverges from the persisted brief's recommendations without either a prior nudge exchange or an explicit `provenance.overrides` naming the field | designed |
| 10 | Never relay numbers/state that don't match the journal | **mechanized reviewer** (designed, next): deterministic claim-extraction over the agent's outgoing relay, diffed against the brief/journal/reducer envelope; refuses the turn on mismatch. Code auditing the LLM against the durable record — vs. Claude Science's LLM-audits-LLM reviewer — is the moat stated as a feature | designed |

Rules 9–10 are the remaining trust seams; both are *post-hoc deterministic
checks over durable records*, which is the pattern every future conduct rule
should follow. When a new conduct rule cannot be given a mechanism, that is a
design smell in the rule.

See [human-amplification-blocks.md](../human-amplification-blocks.md) §1/§5/§6 and
[block-drive.md](../block-drive.md).
