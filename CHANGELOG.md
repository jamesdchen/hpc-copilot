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

### Added — the run queue, Phases 1+2 (run-queue plan §6, 2026-07-29)

The missing organ: intake + placement. Experiments queue as they come in and
the machine chooses the cluster, with every decision disclosed and every
gate binding exactly where it always did.

- **Phase 1 — the store + the authority.** `.hpc/queue/intake.jsonl`
  (append-only, `request_id` dedup so replays cannot double-enqueue) plus
  three verbs: `queue-run` (mutate, UNGATED — enqueueing spends nothing),
  `queue-status` (the R1 projection over the run stores; greenlight
  questions route through the same boundary-scoped predicate
  attention-queue uses), and `queue-advance` (pure placement authority:
  pin wins, hard constraints filter, least-loaded by the shared occupancy
  predicate, alphabetical tie-break — reason always disclosed, no item
  ever dropped or guessed). `queue-status`/`queue-advance` take an
  optional spec (the net-triage precedent): a bare CLI call is the
  whole-ledger read.
- **Phase 2 — the actor.** `queue-dispatch` consumes placements and starts
  each item's normal gated lifecycle: derive the computed run_id → ADOPT an
  existing run rather than resubmit (failed/abandoned deliberately not
  adopted so retries work) → per-cid dispatch lock (the E4 rule as a real
  flock) → durable-first placement append → the identical detached
  `campaign-run` launch refill always used. Placed-but-unstarted items
  (crash window) are re-actuated, never stranded. `campaign-refill` is now
  a LEDGER PRODUCER: resolve once → enqueue with the resolved identity →
  dispatch, all under the lock; its direct-submit path is deleted, and
  `campaign-advance`'s pool arithmetic counts queued intake through the
  shared predicate — closing the §10.S3 re-enqueue window.
- **The S1 placement consent leg.** Consent binds WHERE, not just what:
  `state/placement_drift.py` (membership over a cluster-key set; absent on
  either side disables — purely additive on the pre-migration corpus,
  measured: zero test failures), a `placement-changed` reason in
  `standing_consent_status`, sidecar-fed `current_placement` at every
  run-scope consumption site, campaign `placement_scope` composed from the
  run records' cluster stamps (never parsed from the cid), a record-time
  gate that refuses malformed shapes and typo'd keys with a near-miss hint,
  a paste-ready grant line on the authorship refusal, and an AST caller
  census (`tests/contracts/test_placement_leg_callers.py`, allowlist now
  EMPTY) so a future call site cannot silently skip the leg.
- **The morning digest's queue paragraph.** `status-snapshot` gains an
  additive `queue` section relaying `queue-advance`'s rows and rendered
  text VERBATIM (one authority, byte-equal — pinned) so a stuck item
  surfaces without being asked about; absent when the queue is empty,
  fail-open on any read surprise.
- **The Claude Science producer seam.** A coordinating agent (Claude
  Science, `docs/design/claude-science-integration.md`) can now enqueue
  experiments and observe, but never cross a gate — it plays the same
  queue-PRODUCER role `campaign-refill` does. A new `science` MCP catalog
  (`hpc-agent mcp-serve --catalog science --allow-mutations`) advertises
  EXACTLY `queue-run` + `queue-status` + `queue-advance` and is DISJOINT by
  construction from every gate-crossing verb — no `queue-dispatch`, no
  `submit-*`, no `append-decision`, no `block-drive` (the catalog IS the
  boundary; `tests/test_mcp_science.py` pins the disjointness in both
  mutation-flag states). It is a SEPARATE, narrower catalog than `curated`,
  not an edit to curated's membership — the two answer different "who is
  asking" trust questions. A boundary-free packaged skill
  (`slash_commands/skills/hpc-science-queue`) teaches the enqueue→observe
  loop and holds NO dispatch/approve logic; it declares `mcp-catalog:
  science` in frontmatter so `lint_skill_mcp_reachability` checks its
  MCP-direct verbs against the science surface, not curated. The human
  still approves dispatch at the cluster boundary exactly as when a human
  enqueues.

Intended default, ruled 2026-07-29: a standing consent composes NO
core-hours `budget_cap` — the composed caps are the morning-boundary expiry
plus the overnight `walltime_cap` only. Compute spend is unmetered until a
human names a cap (`budget_cap` opt-in), the right posture pre-GPU.

Adversarial review before landing (three lenses over Phase 2): 13 confirmed
findings, 7 root causes, all reproduced then fixed with regression tests —
led by the occupancy ledger half never RELEASING a slot (a completed K=4
campaign read occupied=4 forever and refill waited on an empty cluster),
plus a dispatch-lock reentrancy hole that waved a second thread through the
resolve window, and a composed placement that could name a cluster absent
from the active config and become un-grantable.

### Fixed — two workflow-plan relay bugs that made campaign-run silently ineffective (2026-07-29)

- The shared command helper appended `--experiment-dir` to EVERY relayed
  verb; `wait-detached` does not declare that flag
  (`CliShape.experiment_dir_arg=False`), so every relayed wait exited rc=2
  on `unrecognized arguments` and every detached block parked as
  `wait_failed`. `campaign-recon`'s `net-triage` probe carried the same bug
  and read back as a permanently anomalous probe. Both plans now declare a
  `RELAYS` table (verb + the flags that verb actually takes) and render
  every command from it.
- The plans' JSON parser returned the whole `{ok, idempotent, data}` CLI
  envelope while the drive loop read `tick.action` / `tick.brief` /
  `outcome` off its root — `undefined` for every field, so
  `awaiting_decision`, `terminal` and `detached` never matched and every
  tick fell through to `tick_budget_exhausted`. Replaced with a single
  `parseEnvelope` helper that unwraps `data` and branches explicitly on
  `ok:false` (new `tick_refused` park carrying the kernel's `error_code`
  + message).

### Added — the always-draining loop, run-queue Phase 3 (2026-07-29)

- **`queue-drain.js`** (plan §5/§7): a pure relay at the ledger level — one
  `queue-status` per pass, the drivable set as a mechanical field check over
  its published projections (`dispatched ∧ ¬terminal ∧ ¬held ∧
  ¬superseded_by ∧ (¬parked ∨ greenlight_unadvanced)`), a `block-drive` loop
  per drivable item with 480s-chunked `wait-detached` (labels carry the item
  id and the chunk index), `drive_attempts >= n` held rather than driven,
  parks RECORDED and left for the human, then re-status and repeat. Loop
  bound is `min(drivable, maxLoops, the status read's own ceiling)`,
  recomputed from each pass's relay and never remembered — a relaunch from
  scratch is always correct, and a pass with nothing drivable costs exactly
  one status relay.
- **`tests/contracts/test_workflow_plan_commands.py` — the execution-level
  plan gate.** The delegation sweep beside it is regex-only and never runs
  anything, which is how both bugs above shipped. This one materializes
  every declared relay into an argv and PARSES it with the real argparse
  tree (`cli.parser.build_parser`), and pins the envelope unwrapping against
  key names derived from envelopes `cli/_helpers` actually emits — executing
  each plan's own `parseEnvelope` over those bytes where a JS runtime is
  available. Both bug classes are in-suite fire paths with control arms.

### Added — the run queue, Phase 3 kernel substrate (run-queue plan §5/§7, 2026-07-29)

The always-draining loop is plan-side composition of shipped verbs; this is
the kernel that makes it cheap and decidable. No new verb — `queue-dispatch`
IS the tick.

- **The drivability formula is fully served by `queue-status` items[], and
  pinned.** `drivable := dispatched ∧ ¬terminal ∧ (¬parked ∨
  greenlight_unadvanced)` — all four fields were already correct; a contract
  test now pins them so a rename cannot silently degrade the plan's formula.
  Three fields the plan would otherwise have had to INFER are added: `held`
  (the run is parked on an ESCALATION verdict — a second human-wait axis with
  no boundary a greenlight can target, so a `parked`-only test relays ticks
  at it forever), `superseded_by` (the field `queue_occupancy.run_occupies`
  retires a slot on, non-empty for a window before the terminal status write
  lands), and `drive_attempts`. `held` joins `counts`; both new stop
  conditions are disclosed in `notes`. No `drivable` boolean is emitted —
  the policy is the plan's, and a kernel verdict would be a second
  definition of it.
- **Retryable(n) is KERNEL state, not a plan variable.**
  `RunRecord.drive_attempts` counts CONSECUTIVE agent-facing `block-drive`
  ticks that moved the run nothing (`awaiting_decision` / `skip`); anything
  that advanced / reran / chained / detached / reached a terminal resets it
  to 0. One write point (`ops/block_drive_op`, after a non-`dry_run` tick,
  via the single `journal.stamp_drive_attempt` definition), read back off
  disk by `queue-status`. Consecutive rather than cumulative so a healthy
  long run is never eventually refused; durable so a pass can die mid-flight
  and its successor reads the same number. The detach-child entry is
  deliberately NOT counted — a detached poll is not a relayed tick.
- **The relaunch-cheapness invariant (§7) is restored; S12's two O(history)
  legs are closed.** `queue-status` folded the WHOLE append-only intake
  ledger, and its occupancy leg `load_run`s every run file in the namespace.
  New `ops/queue/maintenance.groom_queue_stores` compacts the ledger of
  items whose runs have RETIRED (`state.queue_intake.compact_intake_ledger`,
  under the appenders' own flock, atomic replace, with an auditable
  watermark), then prunes terminal RunRecords nothing on the ledger still
  references — `state.index.prune_terminal_runs` gains a `protect` set and
  gets its FIRST production caller (it had existed, uncalled, since the
  journal did).
- **D10 — the WRITE authority grooms; the read paths never do.** Grooming
  runs in `queue-dispatch`, only on a tick that actually wrote a placement,
  and is reported as data on `QueueDispatchResult.maintenance`.
  `queue-status` / `queue-advance` keep `side_effects=[]`: a query that
  groomed the store it reports on is the F46 bug one layer up. A tick that
  dispatched NOTHING is charged nothing — that pass is exactly what the
  invariant is about. Ordering is load-bearing (compact, then prune the
  survivors' complement), retirement is decided by the ONE `run_occupies`
  predicate so compaction can never drop an item the pool arithmetic still
  counts, items the call is reporting on are exempt (adopting onto an
  already-`complete` run would otherwise erase its own ledger row mid-tick),
  unparseable lines are kept verbatim so `skipped_records` stays truthful,
  and grooming never raises — the items already started.

### Fixed — Phase 3 guards proven to fire (adversarial guards-fire review, 2026-07-29)

The queue-drain plan's drivability formula and retry ceiling are now pinned
by EXECUTING `queue-drain.js`'s own `isDrivable`/`attemptsOf` under node
against the real `queue-status` projection, instead of a Python paraphrase
that agreed with itself while the plan lost terms; `FLAG_RENDER` output is
pinned to the RELAYS table (the flag bug's signature, one layer down);
`validatePlan()` is executed rather than assumed; `groom_queue_stores`'
`protect=` wiring, the dispatch-level grooming disclosure, and the
detach-child retry-budget exemption each gained a test that fails against
their removal; `_count_drive_attempt` now catches `Exception` (a
type-corrupt `drive_attempts` raised `ValueError` and crashed a successful
drive), and the janitor import in `queue-dispatch` moved inside the
never-raises envelope. CI installs node and sets `HPC_REQUIRE_NODE=1`, so
an executed contract arm can no longer pass by silently skipping.

### Added — paste-ready answer menus on park briefs + park notifications (2026-07-29)

Every decision point now hands the human its answers, not just its
question. Park briefs carry an `answer_menu` composed at the one home
(`block_drive.park`): the bare-`y` default naming its materialized advance
target, the scope-naming approve line, and one paste-line per structured
recommendation — hint and menu share `greenlight_target` so they can never
name different targets, and anomaly terminators (`canary_failed`,
`watching_anomaly`) label the bare-`y` default an OVERRIDE instead of
hiding that it chain-forwards. `doctor --notify` raises park notifications
carrying the answer line over the existing channels, deduped per
`(run_id, park, awaiting_since)` — watchdog cadence is a replay no-op, a
re-park re-notifies, and a park and a stall of one run cannot collapse.
A mechanized census (`tests/contracts/test_bare_y_coverage.py`) walks
every boundary in `block_chain.SUCCESSORS` that can park and fails the
suite if one lacks a menu without a reasoned allowlist entry — with
negatives proving the census itself fires.

### Added — QoS submit-cap gate, config-based (2026-07-29)

The successor to the deleted `validate-self-qos-limit`, with its three
faults corrected. New optional cluster key `max_submit_jobs_per_user`
(the `max_walltime_sec` pattern — static site policy, hand-configured;
Discovery: normal=5000, gpu=100). When set, `submit-flow` refuses BEFORE
any rsync/deploy/qsub if journal-known in-flight tasks + the new array
(+canary) would meet the cap — with split guidance — and discloses
proximity at 70%. Targets the Slurm SUBMIT cap (`MaxSubmitJobsPerUser`,
the limit that hard-rejects; array elements each count), not the
running-jobs throttle the deleted validator compared against. No agent
in the data path (config + journal + spec, never model-fetched numbers);
externally-submitted jobs are not visible and the refusal says so;
unset = no check, with the scheduler's own rejection as backstop.

### Removed — BREAKING: two inert primitives deleted after verification (2026-07-29)

Both flagged inert in the agent_facing reachability audit, then resolved by
investigation rather than kept-by-default (the verify-a-guard-can-fire rule):

- **`decide-resubmit`** — superseded by design. Its only non-trivial branch
  required `resubmit_failed_threshold > 0`, a knob no spec field, config, or
  caller anywhere supplies, and the shipped posture is explicit that silent
  auto-resubmit is not a code path (the recommendation surface is the
  categorical anomaly table in `status-snapshot`). Hand-authored
  `decide_resubmit.*` schemas removed with it.
- **`validate-self-qos-limit`** — never wireable. Its designed feeder (the
  skill fetching `squeue` / `sacctmgr show qos` raw) was eliminated by the
  no-raw-ssh affordance removal, and no throttled verb exposes
  `MaxJobsPerUser`, so the guard could not fire from any surface. The
  lesson-6 self-DOS bug class it covered is recorded as knowingly unguarded
  in `docs/plans/backlog-2026-07-17.md` §4 (needs a Slurm QoS-cap probe verb
  first); the pure validator logic is recoverable from git history.

### Changed — BREAKING: verb-surface consolidation, no deprecation cycle (2026-07-29)

Seven registrations collapsed into three merged verbs (179 → 174 primitives;
user-ordered immediate merge-and-delete, skipping the deprecation ledger). The
implementations, their tests, and their design pins stay in place — only the
registration/CLI/wire surface merged; each old verb's spec + result shapes are
embedded in the merged verb's schema union (`build_schemas.py` skip-set
entries) rather than emitted as standalone files:

- **`discover`** replaces `discover-executors` / `discover-runs` /
  `discover-reducers`: `--kind executors|runs|reducers` (default `executors`,
  so bare `hpc-agent discover` behaves as before apart from a new `kind`
  result field). Each kind keeps its historical result key, so `data.runs`
  etc. consumers are unaffected; `--search-dirs` with `--kind runs` is refused
  loudly. `classify-axis-preflight` now subprocesses `discover --kind runs`
  (its `discover_runs` sub-result slot is unchanged).
- **`trace`** absorbs `trace-diff` / `trace-render` as spec modes: the bare
  lineage query is unchanged; `--spec {"mode": "render"|"diff", ...}` selects
  the data-trace projections (`trace.input.json` is the mode-discriminated
  union; `trace.output.json` the per-mode result union). A spec combined with
  `--campaign-id`/`--run-id` is refused.
- **`notebook-record`** replaces `notebook-record-config` /
  `notebook-record-receipt`: one kind-discriminated spec
  (`kind: "config"|"receipt"`) dispatching to the unchanged seats. The MCP
  curated/audit-loop verb lists and the `hpc-notebook-audit` skill now name
  the merged verb.

Removed schema files: `trace_diff.*`, `trace_render.*`,
`notebook_record_config.*`, `notebook_record_receipt.*` (their shapes live on
inside the union schemas). There is no alias or forwarder for the deleted
verb names — an external caller of one gets the CLI's unknown-verb error and
should consult `hpc-agent find` / `describe`.

### Fixed — campaign-run: chunked waits + fresh-relaunch parks (fable-sweep 2026-07-29)

An adversarial design sweep (six lenses over the banked run-queue design +
live kernel; verdict banked in `docs/plans/run-queue-placement-2026-07-28.md`
§8) confirmed two bugs in the shipped plan, both fixed:

- **The wait relay could never survive a real wait**: it relayed
  `wait-detached` with the CLI's 7200s default into a ~10-min-bounded
  harness command — killed before it could report its own timeout, parking
  every real detached block as `wait_failed`. Now chunked: `timeout_sec:
  480` per relay, `maxWaitChunks` (default 90 ≈ 12h) with a heartbeat log
  per chunk, chunk-indexed labels (distinct engine-cache identity), and a
  `wait_stalled` park on exhaustion.
- **`resumeFromRunId` across a park replays the park forever**: the engine
  replays cached calls with unchanged (prompt, opts) verbatim, and the
  parked tick completed successfully — so a resumed run returned the same
  `awaiting_decision` without one live call. Auto-resume is now a FRESH
  relaunch everywhere (plan meta, README, park `resume_hint`) — cheap
  because block-drive's state is durable; a fresh pass ticks from the
  current journal.

### Added — workflow plans ship as product; installer learns a workflows/ tree (2026-07-29)

The campaign workflow plans are researcher-lifecycle features, not dev
conveniences — but they lived only in this repo's `.claude/workflows/`,
invisible to an experiment repo. Now:

- **`src/hpc_agent/slash_commands/workflows/`** — `campaign-recon.js` +
  `campaign-run.js` (and the plan-author README) move into the package and
  ship in the wheel, beside the skills/commands/agents they compose with.
- **`agent_assets` installs a fourth tree**: `workflows/*.js` →
  `<claude_dir>/workflows/<name>.js`, reported as `workflows_installed`,
  manifest-owned (so a retired plan is pruned like any other asset). No
  `Skill(...)` grant — the Workflow tool resolves plans by name. The README
  is deliberately not installed (author contract, not a runtime asset).
- `tests/contracts/test_workflow_plan_delegation.py` sweeps the package
  location; the delegation doc and lifecycle map repoint. Auto-resume
  guidance also landed in the same files: a park is a question, not a stop
  — after the `y` is journaled the session relaunches with
  `resumeFromRunId` unprompted.

### Changed — docs reorganized: live plans split from history, odd-duck roots folded (2026-07-28)

- `docs/history/plans/` now holds every executed plan, finished sweep/triage,
  run runsheet, and retired handoff package (~20 items moved from
  `docs/plans/`, each with landing evidence); `docs/plans/` keeps only live/
  BANKED work. `docs/proposals/` → `docs/design/`; `docs/runbooks/` +
  `docs/workflows/` → `docs/internals/` (guides indexed by
  `docs/internals/workflows.md`). All moves via `git mv`; references updated
  repo-wide; the operational-docs contract pins
  (`tests/contracts/_doc_scan.py`) repointed with the drift log updated.
- `docs/README.md` rewritten as the per-root admission-rule map: a new doc
  must fit an existing root, or the reorg conversation comes first.

### Removed — dev-loop orchestration erased; devx augments Claude Code, never replaces it (2026-07-28)

User-ordered: the bespoke build-dynamics protocol is gone in full. Claude
Code natively runs dynamic workflows, so the dev loop needs no orchestration
machinery of its own — the repo's devx layer exists to AUGMENT that
experience (gates and data, not process):

- **Deleted**: `docs/plans/_TEMPLATE-handoff/` (the architect-memo +
  unit-specs handoff-package template), `scripts/check_handoff_disjointness.py`
  and `tests/scripts/test_check_handoff_disjointness.py`. Historical handoff
  packages under `docs/plans/` remain as records.
- **Added**: `scripts/devx_ingest.py` — the repo-side collector for the
  `tag-session` seam. Sweeps `~/.claude/projects/` (recovering each
  project's real cwd from the `cwd` field inside its transcripts — the
  munged directory name is lossy), joins session inventories to each
  experiment's `.hpc/devx/session_tags.jsonl` ledger, and emits one JSON
  report. A collector, not an interpreter.
- **Workflow intake discipline** (`.claude/workflows/README.md`): plans
  front-load their full input surface via `ARGS_CONTRACT`; the launching
  session resolves every field before launch and proposes the whole arg set
  warm — one confirm/correct exchange of diffs, never serial cold
  questions. Mid-run returns are parks only.

### Changed — dev-loop/product separation: the wheel ships product only (2026-07-28)

User-ordered: "the wheel build should not have dev loop stuff except for
tagging sessions for devx to ingest as data." Most of the dev loop already
lived repo-side (`scripts/`, `docs/plans/`, `.claude/workflows/` — never
packaged); this lands the two pieces that weren't:

- **The `release` skill moved out of the wheel** — from
  `src/hpc_agent/slash_commands/skills/release/` to the repo-level
  maintainer surface `.claude/skills/release/`. It stays repo-tracked (the
  pre-2026-07-04 untracked-copy drift cannot return) and stays under the
  agent-prose lints: `scripts/_agent_prose_targets.py` gains
  `MAINTAINER_SKILL_GLOB`, both content lints scan it in default runs, and
  their ALLOWLIST entries repoint. `lint_skill_command_sync` drops the
  `release` allowance (no longer on the shipped surface). The installer's
  `internal: true` skip (bug-sweep #58) stays live for plugin trees, its
  fire path now synthetic
  (`tests/cli/test_agent_assets_settings_permissions.py::test_install_tree_skips_an_internal_skill`).
- **`tag-session`, the one devx seam the product keeps** — a small `mutate`
  verb (`ops/devx_tag.py`, substrate `state/devx_tags.py`): append one
  opaque record (`{tags, note?, session_id?, run_id?}`, ts stamped) to the
  per-experiment `.hpc/devx/session_tags.jsonl` flock-append ledger, for
  the maintainer's repo-side tooling to ingest. Deliberately inert inside
  the product: nothing reads a tag back to change behavior — no gate,
  journal, or decision path consumes it, so it can launder nothing.
- **The boundary mechanized** —
  `tests/contracts/test_wheel_product_boundary.py`: no internal/maintainer-
  flagged skill in the shipped tree (checker = the installer's own
  `_skill_is_internal`, with its own fire paths), and the relocated
  maintainer surface provably covered by the prose lints' scan.

### Changed — delegation reworked: plan-driven relay joins the context firewall (2026-07-28)

User-ordered ("the context firewall is good, but I feel like the dynamic
workflow can do more"). `docs/design/agent-delegation.md` gains rule 5: a
`kind: 'script'` step in a validated `.claude/workflows/` plan may relay any
registry verb except the `append-decision` rendezvous lock — `block-drive`
ticks included — because the command is a pure authored template and the
model composes nothing. The load-bearing distinction shifts from *which side
of the execution path* to **who composes the invocation**. Freeform subagents
(`hpc-recon`, skill delegation sections) stay recon-only, unchanged.

- **`.claude/workflows/campaign-run.js`** — new flagship at the plan-relay
  level: drives one campaign through the block chain by relayed `block-drive`
  ticks and `wait-detached` waits, PARKS at every typed-`y` gate (a tick
  returning `awaiting_decision` ends the run with the brief pointer; the
  human journals the `y` inline; `resumeFromRunId` replays completed steps
  from cache). The workflow never passes a gate — the runtime refuses
  ungreenlit blocks regardless of caller, so parking is the enforced shape.
- **`.claude/workflows/campaign-recon.js`** — the recon firewall, mechanized:
  parallel fan-out of the delegable query verbs, one advisory result of exit
  codes + bounded outputs + pointers. Replaces `swarm-units.js` as the
  section flagship (the build-swarm plan and
  `docs/internals/swarm-units-workflow.md` were deleted by user direction;
  the rest of the bespoke build protocol followed the next day — see the
  "dev loop orchestration erased" entry).
- **`tests/contracts/test_workflow_plan_delegation.py`** — the boundary
  mechanized against the live registry: `append-decision` in no plan, in no
  section; mutating/workflow verbs only inside a plan's `COMMANDS` block;
  fire paths plus a control arm proving the rule-5 grant is real.

### Added — agent reincorporation at the recon-only level (2026-07-27)

Per `docs/plans/agent-delegation-2026-07-27.md` (user-ordered: "reincorporate
agents on some level, just not the same level that we were doing before"). The
retired level put an agent INSIDE the execution path; this one puts it only
BESIDE — a subagent is a **context firewall for read-only reconnaissance**, and
the verbose transcript (tool schemas, envelopes, render bytes) lives and dies
in the subagent while the main session gets a compact advisory brief:

- **`hpc-recon`, a read-only core-shipped agent** —
  `slash_commands/agents/hpc-recon.md`, the first agent core ships since the §6
  worker removal. `tools: Bash, Read, Grep, Glob` (no `Write`/`Edit` by
  charter); the body confines `Bash` to `hpc-agent` query/validate verbs and
  forbids paraphrasing a relay-VERBATIM render — render-bearing verbs come back
  as `render_path` + shas + counts. The existing `agent_assets`
  `agents/` walk installs it with zero machinery change.
- **Per-skill `## Delegation (hpc-recon)` sections + `Task`** on the five
  workflow skills (`hpc-submit`, `hpc-status`, `hpc-aggregate`, `hpc-campaign`,
  `hpc-notebook-audit`): one `- delegable:` bullet per handoff naming its verbs,
  one `- locked:` bullet restating the boundary (`append-decision`, the
  `y`/nudge rendezvous, sign-off and standing consent, every verbatim relay).
- **`docs/design/agent-delegation.md`** carries the doctrine and names the
  retired level as the recorded anti-pattern: **delegation never enters the
  trust chain** — a subagent's report is model-carried text, worth exactly
  nothing to the gates, which read journals, stores, and the utterance log. No
  gate changed, and none needed to; the `hpc-worker` spawn transport stays
  retired and is not coming back at any level.
- **`tests/contracts/test_agent_delegation_guidance.py`** mechanizes it: every
  verb a `- delegable:` bullet names must be `verb=query`/`verb=validate` in the
  LIVE primitive registry (resolved, never a hand list — a primitive decorated
  tomorrow is locked today), every section carries its `- locked:` restatement
  and `Task`, `hpc-recon.md` grants no write tool and keeps the never-paraphrase
  rule, and a synthetic skill offering `append-decision` is refused.

### Changed — context-footprint reduction, five levers (2026-07-27)

Per `docs/plans/context-footprint-2026-07-27.md` (user-ordered; the governing
rule: defer only what a BRANCH needs — the mainline stays inline, every
deferral disclosed with a pointer):

- **MCP `tools/list` schemas are structure-only.** The embedded spec schemas
  keep types/`required`/`enum`/property names but drop the nested per-field
  documentation prose; the spec property's description points at `describe` /
  `hpc-agent describe <verb>`, which still serve the full contract (the
  packaged schema files are untouched — the trim is a serve-time projection).
- **Oversized structural refusals offload their detail.** When a refusal's
  message+remediation exceeds 2000 bytes, the full text lands in a
  content-addressed `.hpc/briefs/refusal-<sha12>.txt` and the envelope
  carries a line-boundary truncation + the path. Authorship-marked
  (read-and-sign) refusals always stay inline whole; no existing `.hpc` ⇒ no
  offload (no-scaffold); any write error fails open to the full inline text.
- **Branch-gated skill guidance moved to `references/` files** read only when
  the branch is taken: hpc-submit's `revise-resolved` / `retarget-run`
  recovery arms, hpc-notebook-audit's interview-handoff on-ramp. A new
  error-severity `branch-reference-integrity` lint rule refuses dangling or
  orphan reference files.
- **`read-decisions` gained `digest: true`** — per-record identity/ordering
  metadata (ts, block, attestor, response sha12 + length, resolved key names)
  with the bodies omitted; the default response is byte-identical to before
  (the additive key is absent, not null). The submit/status preflight scans
  now use it.
- **Wire: `notebook-draft-context` no longer double-carries the template
  prose.** `template_sections[]` rows now carry `slug` + `source_sha12` (the
  audit's normalized sha) instead of the verbatim cell `source`; the prose
  rides once, in the `markdown` render the skill relays. The content-keyed
  cache self-heals (an old-shape payload fails validation and recomputes).

### Removed — the MCP elicitation channel; the inline chat relay is the read-and-sign surface (2026-07-27)

- **MCP elicitation removed wholesale (user-ruled).** In retrospect, relying on
  a third-party client's implementation of MCP elicitation was the wrong basis
  for the sign-off surface: form rendering is entirely client discretion (no
  markdown/sizing guarantee — `docs/design/mcp-elicitation-facts.md`), and the
  live harness rendered the dialog too small to carry an audit. Removed:
  `_kernel/extension/mcp_elicitation.py`; the bidirectional pump (`mcp-serve`
  is again a strict synchronous request → response server — reader thread,
  outbound-request wait, pending slot all gone); the `append-decision`
  elicit-then-retry firing site; the per-session `capabilities.elicitation`
  store + dark flag; `ELICITATION_SERVER_IMPLEMENTED` and the
  `elicitation_server` / `elicitation_client` evidence keys of
  `harness-capabilities`; `render_store.read_render_digest` / `RenderDigest`
  (popup-only consumers); the conformance kit's E7 legs; the elicitation test
  suites + `tests/_mcp_harness.py`. `HARNESS_CONTRACT_VERSION` 1.2.0 → 1.3.0
  (MINOR: the channel was optional and non-load-bearing; no conforming harness
  is invalidated — `docs/internals/harness-contract.md` "MCP elicitation …
  RETIRED"). The `failure_features.authorship_evidence` refusal marker
  survives (harness-agnostic refusal-cause metadata).
- **The inline chat relay is the read-and-sign surface.**
  `notebook-audit-view` with `full: true` now emits the inline review
  projection: the code diff rides with its highlighting intact while
  commented-out exposition runs collapse to disclosed elision lines
  (`… (N commented exposition line(s) elided — full text in the on-disk
  render)`); the content-addressed render file keeps the FULL exposition for
  out-of-chat auditing. The human reads the relay (or the render file) and
  types the sign-off in chat — the `UserPromptSubmit` capture hook is the one
  capability-1 channel, and the T8 gate's evidence tiers are unchanged.
- **Overnight standing consent gained a token-exact chat tier.** The gate's
  bound-capture-only posture (USER RULING 3) presumed the popup as the binding
  surface; with it retired, a typed chat consent now grants when it names the
  boundary token-exactly, every declared heal class, and the spec's `cmd_sha`
  by an 8+ hex prefix — a token derivable only from the refusal's
  code-rendered coverage brief, which is now rendered inline in the refusal.
  The bound tier remains for a conforming second harness's binding surface.

First implementation wave of the fork's guiding design
([`docs/design/human-amplification-blocks.md`](docs/design/human-amplification-blocks.md)):
workflows decompose into **blocks** that chain deterministically in code and
terminate at human decision points with code-digested **briefs**. No decision
point is resolved by the LLM; the LLM only drafts proposals over code-digested
evidence and relays the human's `y`/nudge. Registry grew 101 → 121 primitives.

### Added — packages swarm: MCP surface, latency, doc-honesty (2026-07-12)

Three coordinated packages settled against the tree by the architect memo
(`docs/history/plans/handoff-packages-2026-07-12/` handoff). All fail-open by construction; caches are
optimisations, never correctness gates.

- **MCP surface.** `poll-detached` — the instant, non-blocking snapshot of a
  detached worker (lease pid-liveness + journal status + block-terminal
  presence; local, no SSH), the MCP-safe complement to the blocking
  `wait-detached`: it reports `running` / `exited_recorded` /
  `exited_unrecorded` / `no_lease`, naming the run-#12 dead-worker gap
  explicitly (`ops/monitor/poll_detached.py`,
  `_wire/queries/poll_detached.py`). The MCP-direct read/recovery verbs
  (`read-decisions`, `verify-relay`, `attention-queue`, `revise-resolved`,
  `poll-detached`) are now curated-reachable over MCP, pinned by a new
  reachability lint (`scripts/lint_skill_mcp_reachability.py`) that fails when
  a SKILL names a verb MCP-direct that isn't reachable. Relay parity is proven,
  not re-implemented: the autofetch hooks are unnecessary over MCP by
  construction (the envelope IS the structured tool result), pinned by
  envelope-parity tests, with the Stop-guard enforcement half named honestly as
  having no MCP equivalent. A second-client elicitation proof
  (`tests/test_mcp_elicitation_client_proof.py`) certifies capability-1 over a
  non-Claude client end to end.
- **Latency.** The persistent asyncssh SSH engine now defaults **on** under
  `mcp-serve` (the one long-lived process where a persistent connection
  amortises; every other verb is one-shot), honouring a user-preset engine and
  an `HPC_MCP_NO_SSH_ENGINE=1` opt-out, and falling back automatically on any
  engine trouble (`cli/mcp.py`). A cross-process on-disk `ClusterSnapshot`
  cache (`state/snapshot_cache.py`, 60s TTL, `HPC_NO_SNAPSHOT_CACHE=1` bypass)
  backs `inspect_cluster` between the in-process cache miss and the backend
  fetch; only successful (error-free) snapshots are cached. The CLI fast-path
  plugin gate is narrowed from "any plugin present" to "a CLI-shaping
  (`register_cli`) plugin present", so primitives-only plugins keep the fast
  path (`cli/dispatch.py`).
- **Doc-honesty.** `docs/internals/submit-sequence.md` and
  `docs/internals/code-driven-orchestration.md` rewritten against the live
  block-drive substrate (the deleted worker-prompt / resolver modules purged).
  New contract pins guard operational docs: console-script and
  `src/hpc_agent/...` path references in `docs/internals/` + `docs/workflows/`
  must resolve (`tests/contracts/test_doc_references.py`), and
  `docs/design/*.md` `status:` frontmatter must use the closed vocabulary
  {plan, shipped, superseded, partial} with no landed-banner on a `status:
  plan` doc (`tests/contracts/test_doc_status_headers.py`).

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
- **Scope + safety.** Landed slice: the `status` workflow's blocking wait path (the lifecycle the LLM sat in). `submit`/`aggregate` keep the default worker (deferred — see `docs/internals/code-driven-orchestration.md`). The flag/env is **opt-in**; the proven `--bare` worker stays the default. Unlike `--inline`, detached is NOT refused when a worker can authenticate (it spawns no LLM, so the #155 context-isolation guard does not apply). Unsupported shapes are refused with `spec_invalid`. Stays entirely in the drive/worker/CLI layer — no `ops/monitor`, backend-status, or `infra/{remote,ssh_*}` changes. New env var documented in `docs/reference/env-vars.md`; `tests/_kernel/lifecycle/test_detached_drive.py`.

### Removed — §6 worker physical deletion (proving-run-2-hardening Move 3)

The `claude -p` bare-worker spawn transport is physically deleted; workflows are driven exclusively via the block-drive chain. Proving run #2 demonstrated the path was still reachable and taken by default (the driving agent shelled `hpc-agent run --workflow submit`, spawning a worker that hung on OAuth auth) — it cannot be a trap if it is gone. **Deleted:** the `run` verb (`cli/spawn.py`, Tier-3), `_kernel/lifecycle/{invoke,run,llm_resolver}.py`, `_kernel/extension/spawn_prompt.py` + `worker_prompts/` (the four workflow procedures), the legacy campaign resolver seam (`meta/campaign/{driver,deterministic_resolver}.py` + the `hpc-campaign-driver` console script), the `hpc-worker` subagent definition + its Bash fence, and `scripts/count_llm_touchpoints.py` + baseline (its subject was the worker prompts). **Kept (importer-verified):** `_wire/spawn_contract.py` (decision-kernel/strict-schema/block-drive contract — `WorkerReport`/`DECISION_POINTS` and the derived `worker.*.output.json` schemas stay), `drive._stamp_driver_tick` + `_DEFAULT_DRIVER_TICK_CADENCE_SECONDS` (§5 watchdog stamps consumed by `submit/runner` and `block_drive`), and `structured`/`chat_models` (the raw model-call seam). **Edited:** `drive.py` trimmed to the deterministic tick substrate (an `agent`-kind delegate now always plans `skip` routing to block-drive); `describe` no longer serves worker procedures; `load-context`'s delegate block routes `agent` steps to block-drive (`spawn_request` retained as an always-`None` wire-compat key). ~30 files, −4,700 lines; full suite green.

### Fixed

- **Remote submit wrapper `bash -lic` → `bash -lc`: the interactive flag hung every SSH submit on no-PTY clusters** (proving-run #2, 2026-07). `_remote_base.py::_execute_command` wrapped the remote `cd + qsub|sbatch` in `bash -lic` — login **and interactive** — to source the cluster profile that lands the scheduler binary on `PATH` (commit cafb160b). But an interactive bash on an ssh *exec* channel (no PTY; `ssh_run` allocates none) blocks in terminal/job-control init and hangs until the 120 s `_execute_command` timeout fires, which the flow then misreports as `dispatcher_failed` / `canary_failed` — the submit never reaches the scheduler (empty `qstat`/`qacct`), and per-executor-command retries chase a phantom cause. Login shell alone suffices: on Hoffman2/UGE `bash -lc` resolves `qsub` at `/u/systems/UGE8.6.4/bin/lx-amd64/qsub` and returns cleanly (`bash -lic` hangs). Dropped `-i`; a cluster that genuinely exposes the scheduler `PATH` only via an interactivity-guarded `~/.bashrc` must carry it in the preamble (`conda_source`/`modules`), never a globally-hanging `-i`. Regression-pinned in `test_backends_sge_remote.py` (`["bash", "-lc"]`, no `-i`). Covers both SGE and SLURM remote backends (shared mixin).
- **`reconcile`: a crashed-submit orphan (valid jobless sidecar, no journal record) is benign `no_run_record`, not `journal_corrupt`** (#356). A submit that crashed before `submit_and_record` leaves a valid jobless sidecar that was never registered in the journal. `reconcile` treated "sidecar present + no journal record" as a hard `journal_corrupt`, forcing the operator to hand-`rm` the residue before re-submitting. It now splits that branch on the sidecar read: valid JSON + no `job_ids` + no record → a benign `OrphanedReconcile` surfaced as a `no_run_record` `lifecycle_state` (a successful envelope, no SSH, no sibling cascade; `last_status.next_step` says to proceed with a fresh submit); a sidecar that DID land `job_ids` → the stranded-ids `journal_corrupt` + `submit-spec` hint (unchanged); a missing/malformed/schema-incompat sidecar → bare `journal_corrupt` (unchanged). The #328 invariant holds — the benign branch fires only on a provably benign read, so it can never mask a real corruption. A fresh submit over a benign orphan already proceeds (the runner's `cmd_sha` dedup falls through), now regression-pinned. New `no_run_record` value on the `reconcile.output.json` `lifecycle_state` enum; `/submit-hpc` Step 1b branches on it to proceed.
- **`local-reduce` test helpers quote `sys.executable`** (#347) so the pure-API aggregate suite passes on install paths containing a space (e.g. a checkout under `...\CC Allowed\...`). Test-only; `local_reduce`'s shell-command contract is unchanged.

