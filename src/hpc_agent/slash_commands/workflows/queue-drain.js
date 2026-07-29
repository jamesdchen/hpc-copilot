export const meta = {
  name: 'queue-drain',
  description:
    'Drain the run queue: relay one queue-status, compute the drivable set from its items[] fields in plan code, drive each drivable RUN with plan-relayed block-drive ticks (chunked detached waits), record every park and every skip and move on, then re-status and repeat until nothing is drivable — a PURE relay, the sibling of campaign-run at the ledger level (docs/plans/run-queue-placement-2026-07-28.md §5/§7, rule 5 of docs/design/agent-delegation.md)',
  whenToUse:
    "When the run queue has work and the tick/wait relay traffic should stay out of the main session's context. Pass args: {repo, maxLoops?, maxPasses?, maxTicks?, maxWaitChunks?, maxAttempts?, campaignBase?, cluster?}. INTAKE FIRST (README, 'Intake'): resolve every ARGS_CONTRACT field BEFORE launching — repo from cwd, the bounds from their defaults — and propose the WHOLE arg set in ONE confirm/correct exchange. RELAUNCH IS THE REFLEX: a pass is stateless and reads everything it needs from its OWN first queue-status relay, so relaunching FRESH at any time, for any reason, is always correct and always cheap — a pass that finds nothing drivable costs exactly one status relay and returns (the §7 relaunch-cheapness invariant). NEVER relaunch with resumeFromRunId: the engine replays cached calls verbatim, so a resumed pass returns the recorded parks forever without one live tick. The workflow NEVER passes a gate and never decides anything: admission, placement and sequencing are kernel code (queue-advance / queue-dispatch / block-drive), the drivable set is a mechanical field check over relayed items[], and every awaiting_decision is RECORDED and left for the human — this plan does not loop on a parked item, it moves to the next one. A tick that returns `skip` is recorded the same way, under its own skipped[] key with the kernel's reason: a skip is the kernel saying this tick moved nothing and naming why, and it reproduces on every retick, so re-ticking it only spends the item's budget. Two ledger items resolving to one computed run_id (queue-status's collides_with) are never driven together: one is driven, the sibling is reported under deferred[] and picked up by a later pass. AUTO-RESUME: the moment a y is journaled inline, relaunch this workflow FRESH in the same breath; the freshly-drivable item is picked up by the next pass.",
  phases: [
    { title: 'Status', detail: 'one queue-status relay per pass; the drivable set is computed from its items[] fields in plan code, never remembered across passes' },
    { title: 'Drive', detail: 'one block-drive loop per drivable RUN (items sharing a computed run_id are deduped, the sibling deferred), chunked wait-detached for detached blocks; a park or a skip is recorded and the loop ends' },
    { title: 'Report', detail: 'return the outcome buckets (parked / skipped / deferred / held / settled / failed, each with a count) + the last status snapshot when nothing is drivable' },
  ],
}

// ============================================================================
// PORTABLE PLAN
// ----------------------------------------------------------------------------
// Engine-neutral section; the adapter below is the only part that speaks the
// Claude Workflow API. Seam contract: README.md beside this file.
//
// This plan exercises the plan-relay level (docs/design/agent-delegation.md
// rule 5): its COMMANDS invoke workflow-kind verbs (queue-status, block-drive,
// wait-detached), which is legal ONLY here — the commands are authored
// templates rendered from the RELAYS table, the model composes nothing, and
// every typed-y gate parks back to the main session. append-decision appears
// in no plan, ever (rule 2; enforced by
// tests/contracts/test_workflow_plan_delegation.py).
//
// PURE RELAY — what this plan is NOT allowed to contain, and does not:
//   * no admission decision (queue-advance is the placement AUTHORITY),
//   * no placement choice (queue-dispatch is the ACTOR),
//   * no sequencing (block-drive chains the blocks in code),
//   * no interpretation of a brief, a failure, or a park.
// The one computation this plan does is `drivable`, and it is a MECHANICAL
// FIELD CHECK over the boolean projections queue-status already computed
// (dispatched / terminal / parked / greenlight_unadvanced) — not a judgment.
// If it ever needs a fact queue-status does not project, that is a kernel
// change, not a plan change.
//
// S8 (the post-greenlight race) — the first tick after a human `y` is the MAIN
// SESSION's inline tick, not this plan's. The main session ticks block-drive
// directly the moment the y commits (instant visible motion, one tick of
// transcript cost); the auto-resumed drain pass takes over from the SECOND
// tick. Those two tickers can overlap BY DESIGN, and each of the two overlapping
// relays is arbitrated by a DIFFERENT mechanism — say both, because they are not
// the same claim:
//   * the WAIT overlap is safe by construction: `wait-detached` is a query verb
//     with NO declared side effects (ops/monitor/wait_detached.py), a pure read
//     of the journal, so N concurrent waiters on one run are N reads. The
//     single-lease guard in _kernel/lifecycle/detached.py is NOT what makes this
//     safe — that lease keys `(run_id, block)` at _spawn_detached and guards
//     LAUNCHES, and neither ticker takes it on a non-detaching tick;
//   * the TICK overlap is arbitrated KERNEL-SIDE: consuming a parked boundary is
//     a COMPARE-AND-SWAP on the pending-decision marker's `(block,
//     awaiting_since)` under the journal's per-run lock
//     (_kernel/lifecycle/block_drive.py's `_consume_marker` →
//     state/journal.compare_and_clear_pending_decision). Exactly one driver turns
//     the marker it READ into an empty slot and runs the successor span; the
//     loser returns BEFORE the chain — no second span, no re-park, no raise —
//     and discloses the loss in its `reason` (reported as `advanced`, because the
//     boundary DID advance in that instant, so the futile-tick budget is not
//     charged for losing a race the design creates).
// This plan therefore never coordinates with the main session, and must never
// grow a "wait for the inline tick" step. Both mechanisms are the KERNEL's: if
// either ever weakened, the fix is kernel-side and this plan does not change.
//
// S5 (fresh relaunch) — every value this plan uses comes from ARGS or from the
// CURRENT pass's queue-status relay. Nothing is carried across passes, nothing
// is resumed, nothing is cached. A relaunch from scratch at any instant is
// equivalent to the pass that would have run next.

const ARGS_CONTRACT = {
  repo: 'absolute path of the experiment checkout (required; verbs run with --experiment-dir)',
  maxLoops:
    'optional bound on drivable items driven per pass (default 4); the pass drives min(drivable after run_id dedupe, maxLoops) — there is no third term, see the loop-bound comment',
  maxPasses:
    'optional bound on status→drive→re-status passes per invocation (default 10); hitting it returns action "pass_budget_exhausted" with the parks so far',
  maxTicks:
    'optional bound on block-drive ticks per ITEM per pass (default 25); hitting it records that item as "tick_budget_exhausted" and moves on',
  maxWaitChunks:
    'optional bound on wait-detached chunks per detached block (default 90; 480s per chunk ≈ 12h); hitting it records the item as "wait_stalled" and moves on',
  maxAttempts:
    "optional retryable(n) ceiling (default 3): an item whose status row carries drive_attempts >= this is HELD, not driven, and reported under held[]. drive_attempts is the kernel's durable count of consecutive ticks that moved the run nothing; a row that carries no counter reads as attempts=0",
  campaignBase: 'optional campaign-base filter passed to queue-status (relayed as a spec field, never a plan-side filter)',
  cluster: 'optional cluster filter passed to queue-status (relayed as a spec field, never a plan-side filter)',
  statusLimit: 'optional queue-status page limit (default 50); the ledger-derived ceiling on a pass',
}

const SCHEMAS = {
  SCRIPT_RESULT: {
    type: 'object',
    required: ['exit_code', 'output'],
    properties: {
      exit_code: { type: 'number' },
      output: { type: 'string' },
    },
  },
}

// Step vocabulary as in every plan here (see README). All three steps are
// kind: 'script' — there is deliberately NO agent-judgment step in this plan:
// draining is code (queue-status projections + block-drive sequencing) plus
// rule-fixed relays, and anything needing judgment (a gate, a red tick, a
// stuck wait) is RECORDED and returned to the main session rather than
// interpreted in-flight.
const STEPS = [
  {
    id: 'status',
    phase: 'Status',
    kind: 'script',
    run: 'once', // once per pass — the pass's entire state comes from this relay
    needs: [],
    isolation: 'shared-checkout',
    command: 'queueStatus',
    output: 'SCRIPT_RESULT',
    effort: 'low',
    abort_on_failure: true, // a status the CLI rejects must not be guessed at
  },
  {
    id: 'tick',
    phase: 'Drive',
    kind: 'script',
    run: 'fan-out', // one instance per drivable item per tick
    needs: ['status'],
    isolation: 'shared-checkout',
    command: 'blockDrive',
    output: 'SCRIPT_RESULT',
    effort: 'low',
    abort_on_failure: false, // one item's rejected tick must not stop the other loops
  },
  {
    id: 'wait-detached',
    phase: 'Drive',
    kind: 'script',
    run: 'conditional', // only after a tick returns action=detached
    needs: ['tick'],
    isolation: 'shared-checkout',
    command: 'waitDetached',
    output: 'SCRIPT_RESULT',
    effort: 'low',
    retry: 1,
  },
]
const STEP = (id) => STEPS.find((s) => s.id === id)

// ── The RELAY TABLE ─────────────────────────────────────────────────────────
// Every CLI invocation this plan issues is rendered from exactly one row here,
// so what the plan RUNS and what the contract test VALIDATES cannot drift.
// `flags` is the flag set that verb's argparse subparser ACTUALLY declares —
// `wait-detached`'s CliShape sets `experiment_dir_arg=False`, so it takes only
// `--spec`; appending `--experiment-dir` to it is rc=2, which is exactly how
// the shipped campaign-run plan parked every detached block as `wait_failed`.
//
// Strict JSON on purpose: tests/contracts/test_workflow_plan_commands.py
// json.loads this literal, materializes each row into an argv, and parses it
// with the REAL argparse tree (hpc_agent.cli.parser.build_parser) — an
// unsupported or missing flag fails the suite instead of shipping.
//
// There is NO drain verb and this plan does not invent one: `queue-status` is
// the ledger read, `block-drive` is the per-item tick (campaign-run's exemplar,
// unchanged), `wait-detached` is the pacing. Starting a QUEUED item is
// `queue-dispatch`'s job and is deliberately absent — this plan drives items
// that are already dispatched, which is what `drivable` asks.
const RELAYS = {
  "queueStatus": { "verb": "queue-status", "flags": ["spec", "experiment-dir"] },
  "blockDrive": { "verb": "block-drive", "flags": ["spec", "experiment-dir"] },
  "waitDetached": { "verb": "wait-detached", "flags": ["spec"] }
}

// One renderer per declared flag: the table names WHICH flags a verb takes,
// this names how each is spelled. A flag with no renderer is a plan bug and
// throws in validatePlan(), before anything dispatches.
const FLAG_RENDER = {
  spec: () => '--spec "$SPEC"',
  'experiment-dir': ({ repo }) => `--experiment-dir ${repo}`,
}

const relayCommand = (key, inputs, spec) => {
  const relay = RELAYS[key]
  const flags = relay.flags.map((flag) => FLAG_RENDER[flag](inputs)).join(' ')
  return `SPEC=$(mktemp) && printf '%s' '${JSON.stringify(spec)}' > "$SPEC" && hpc-agent ${relay.verb} ${flags}`
}

const COMMANDS = {
  // include_settled false + limit are the §7 relaunch-cheapness bound: a pass
  // that finds nothing drivable must cost ONE status relay and no loops, and an
  // unbounded projection over ledger HISTORY is precisely the cost that
  // forbids. The filters are relayed as SPEC fields so the kernel applies
  // them — a plan-side filter over items[] would be the plan deciding what is
  // in scope.
  queueStatus: (inputs) =>
    relayCommand('queueStatus', inputs, {
      include_settled: false,
      limit: inputs.statusLimit,
      ...(inputs.campaignBase ? { campaign_base: inputs.campaignBase } : {}),
      ...(inputs.cluster ? { cluster: inputs.cluster } : {}),
    }),
  blockDrive: (inputs) => relayCommand('blockDrive', inputs, { run_id: inputs.runId }),
  // timeout_sec 480 keeps each relayed wait comfortably under the harness's
  // ~10-min command bound (fable-sweep 2026-07-29: the CLI's 7200s default
  // would be KILLED by the harness before it could report its own timeout).
  // The chunk loop in the adapter re-arms until terminal or maxWaitChunks.
  waitDetached: (inputs) =>
    relayCommand(
      'waitDetached',
      inputs,
      inputs.block
        ? { run_id: inputs.runId, block: inputs.block, timeout_sec: 480 }
        : { run_id: inputs.runId, timeout_sec: 480 }
    ),
}

const PROMPTS = {
  scriptStep: ({ repo, command }) =>
    `Run EXACTLY this command in ${repo} and nothing else:
${command}
Return per the schema: exit_code (the command's real exit code), output
(stdout+stderr verbatim; if enormous, keep the first and last 100 lines).
Do not fix, retry, re-run, or interpret a failure — relaying it is the job.`,
}

// ============================================================================
// RUNTIME ADAPTER (Claude Workflow API)
// ----------------------------------------------------------------------------

const validatePlan = () => {
  const KNOWN = ['id', 'phase', 'kind', 'run', 'needs', 'isolation', 'prompt', 'command', 'output', 'effort', 'abort_on_failure', 'retry']
  const ids = new Set(STEPS.map((s) => s.id))
  const phaseTitles = new Set(meta.phases.map((p) => p.title))
  const problems = []
  for (const s of STEPS) {
    for (const k of Object.keys(s)) if (!KNOWN.includes(k)) problems.push(`${s.id}: unknown field '${k}'`)
    if (!phaseTitles.has(s.phase)) problems.push(`${s.id}: phase '${s.phase}' not in meta.phases`)
    if (!['agent', 'script'].includes(s.kind)) problems.push(`${s.id}: bad kind '${s.kind}'`)
    if (!['once', 'fan-out', 'conditional'].includes(s.run)) problems.push(`${s.id}: bad run '${s.run}'`)
    if (!['shared-checkout', 'fresh-worktree'].includes(s.isolation)) problems.push(`${s.id}: bad isolation '${s.isolation}'`)
    if (s.output !== null && !SCHEMAS[s.output]) problems.push(`${s.id}: output '${s.output}' not in SCHEMAS`)
    if (s.kind === 'agent' && typeof PROMPTS[s.prompt] !== 'function') problems.push(`${s.id}: prompt '${s.prompt}' not in PROMPTS`)
    if (s.kind === 'script' && typeof COMMANDS[s.command] !== 'function') problems.push(`${s.id}: command '${s.command}' not in COMMANDS`)
    for (const edge of s.needs) {
      const target = edge.split('@')[0]
      if (!ids.has(target)) problems.push(`${s.id}: needs unknown step '${target}'`)
    }
  }
  const state = {}
  const visit = (id) => {
    if (state[id] === 1) { problems.push(`cycle through '${id}'`); return }
    if (state[id] === 2) return
    state[id] = 1
    for (const edge of STEP(id).needs) {
      const target = edge.split('@')[0]
      if (ids.has(target)) visit(target)
    }
    state[id] = 2
  }
  for (const s of STEPS) visit(s.id)
  // The relay table is plan data too: a row with no verb, or a flag with no
  // renderer, must fail at load time rather than render a malformed command.
  for (const key of Object.keys(RELAYS)) {
    const relay = RELAYS[key]
    if (!relay || typeof relay.verb !== 'string' || !relay.verb) problems.push(`RELAYS.${key}: no verb`)
    if (!relay || !Array.isArray(relay.flags)) {
      problems.push(`RELAYS.${key}: flags must be an array`)
      continue
    }
    for (const flag of relay.flags) {
      if (typeof FLAG_RENDER[flag] !== 'function') problems.push(`RELAYS.${key}: flag '${flag}' has no FLAG_RENDER entry`)
    }
  }
  if (problems.length) throw new Error(`queue-drain plan invalid:\n${problems.join('\n')}`)
}

const runStep = (id, promptInputs, opts = {}) => {
  const step = STEP(id)
  const prompt =
    step.kind === 'script'
      ? PROMPTS.scriptStep({ repo: promptInputs.repo, command: COMMANDS[step.command](promptInputs) })
      : PROMPTS[step.prompt](promptInputs)
  return agent(prompt, {
    phase: step.phase,
    ...(step.output ? { schema: SCHEMAS[step.output] } : {}),
    ...(step.isolation === 'fresh-worktree' ? { isolation: 'worktree' } : {}),
    ...(step.effort ? { effort: step.effort } : {}),
    ...opts,
  })
}

const runWithRetry = async (id, promptInputs, opts) => {
  const attempts = 1 + (STEP(id).retry || 0)
  for (let i = 0; i < attempts; i++) {
    const r = await runStep(id, promptInputs, i ? { ...opts, label: `${opts.label}:retry` } : opts)
    if (r !== null && r !== undefined) return r
  }
  return null
}

// ── The ENVELOPE reader ─────────────────────────────────────────────────────
// The CLI's stdout is ONE envelope line, never a bare result
// (hpc_agent/cli/_helpers.py): `{"ok": true, "idempotent": ..., "data": {...}}`
// on success, `{"ok": false, "error_code": ..., "message": ...}` on refusal.
// The PAYLOAD is the envelope's `data` member — reading a result field off the
// envelope ROOT yields undefined for EVERY field, which is how campaign-run's
// park branches silently never fired before 2026-07-29. Every envelope in this
// plan is read through THIS function and nowhere else; the contract test pins
// that (one JSON.parse site, unwrapping `data`, branching on `ok`) against the
// envelope Python actually emits.
//
// Parsed defensively: the relay carries stdout+stderr verbatim, so the envelope
// is the last parseable JSON object line. Returns {ok, data, error} or null
// when no envelope line is present at all.
const parseEnvelope = (output) => {
  for (const line of String(output || '').split('\n').reverse()) {
    const t = line.trim()
    if (!t.startsWith('{') || !t.endsWith('}')) continue
    let env
    try {
      env = JSON.parse(t)
    } catch {
      continue // keep scanning — an earlier line may hold the envelope
    }
    if (env === null || typeof env !== 'object') continue
    if (env.ok === false) {
      return {
        ok: false,
        data: null,
        error: { error_code: env.error_code || null, message: env.message || null },
      }
    }
    if (env.ok === true && env.data !== null && typeof env.data === 'object') {
      return { ok: true, data: env.data, error: null }
    }
  }
  return null
}

// ── The drivable predicate: a MECHANICAL FIELD CHECK, not a judgment ────────
// Drivability is the CALLER's formula, computed from fields queue-status
// PUBLISHES and never a kernel verdict (ops/queue/status.py — the verb
// deliberately emits no `drivable` boolean, so there is exactly one definition
// and it is this one). Every field read here is a projection the kernel already
// computed and disclosed; the plan adds no inference:
//   dispatched          — a RunRecord exists, so there is a run to tick;
//   !terminal           — a settled run has nothing left to drive;
//   !parked             — a park is the HUMAN's turn, and this plan never
//                         answers one;
//   greenlight_unadvanced — except when the y is already committed and the run
//                         has not moved past the boundary yet: that item is
//                         drivable again the instant the y commits (R1 — the
//                         projection, not a copy of it).
// Plus the two stop conditions the four-field form cannot see, both published
// for exactly this caller:
//   !held               — an ESCALATION verdict (RunRecord.pending_verdict) is
//                         a human wait with NO boundary a greenlight could
//                         target, so it never clears by ticking. Testing
//                         `parked` alone would relay ticks at it forever;
//   !superseded_by      — supersession stamps BEFORE the status catches up, so
//                         for that window a superseded run still reads
//                         terminal=false while the occupancy predicate has
//                         already retired its slot.
// A row with no run_id cannot be ticked (block-drive's spec is keyed on it),
// so it is excluded here rather than relayed and refused.
const isDrivable = (item) =>
  Boolean(item) &&
  typeof item.run_id === 'string' &&
  item.run_id.length > 0 &&
  item.dispatched === true &&
  item.terminal !== true &&
  item.held !== true &&
  !item.superseded_by &&
  (item.parked !== true || item.greenlight_unadvanced === true)

// The per-item attempt counter. `drive_attempts` is the kernel's durable
// retryable(n) budget — consecutive agent-facing ticks that moved the run
// NOTHING, reset to 0 by any tick that advanced/reran/chained/detached/settled,
// stamped at block-drive's one write point and read back off the RunRecord. It
// is KERNEL state on purpose: the budget must survive the pass that spent it,
// so it cannot live in a plan variable (§7). The fallbacks and the 0 default
// keep this plan working against a kernel that has not published the field —
// a missing counter reads as attempts=0, which makes the policy purely
// additive. First numeric field wins.
const ATTEMPT_FIELDS = ['drive_attempts', 'attempts', 'attempt_count', 'dispatch_attempts']
const attemptsOf = (item) => {
  for (const field of ATTEMPT_FIELDS) {
    const value = item[field]
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0) return value
  }
  return 0
}

// ── The tick classifier: what the DRIVE LOOP does with a tick's action ──────
// BlockDriveResult.action is a closed 7-member literal set
// (_wire/workflows/block_drive.py). This maps each member to the loop's
// disposition, and it is a NAMED portable helper rather than a chain of `if`s
// inside driveItem precisely so the contract can EXECUTE it — the loop body
// itself needs the engine globals and cannot be run by a test.
//
//   park     — awaiting_decision: the HUMAN's turn. Recorded, loop ends.
//   skip     — the kernel says this tick moved NOTHING and named why: a block
//              that failed or returned no result (exit N), an R3 sha-pin
//              refusal, a position it cannot route, a workflow it cannot start.
//              Every one of those is a STABLE outcome — the next tick re-reads
//              the same durable position and returns the same skip — so ticking
//              again is a spin, not a retry. It burned the whole 25-tick budget,
//              stamped drive_attempts to 25 on disk (skip is a FUTILE action at
//              block-drive's one write point), and the item was then held
//              forever, because nothing this plan can do resets that counter:
//              only real progress through the agent-facing seat does. So a skip
//              ENDS the loop exactly like a park — one skip, one record, move to
//              the next item — and the next PASS retries it once, under the
//              maxAttempts ceiling that is now allowed to work.
//   terminal — end of chain. Recorded, loop ends.
//   wait     — detached: the chunked wait paces it, then the loop ticks again.
//   progress — advanced / reran / chained: the chain moved, tick again.
//   unknown  — an action outside the literal set (the kernel widened its
//              vocabulary). The loop ends and SAYS so rather than assuming
//              progress: assuming progress is the F3 spin with a new name.
const TICK_DISPOSITION = {
  awaiting_decision: 'park',
  skip: 'skip',
  terminal: 'terminal',
  detached: 'wait',
  advanced: 'progress',
  reran: 'progress',
  chained: 'progress',
}
const tickDisposition = (action) =>
  Object.prototype.hasOwnProperty.call(TICK_DISPOSITION, action)
    ? TICK_DISPOSITION[action]
    : 'unknown'

// ── The batch selector: one driver per RUN, not per item ────────────────────
// Two ledger items can resolve to the SAME run_id — identical resolved params
// and run_name compute one id — and queue-status PUBLISHES that as
// `collides_with` (§10.S2: it must be SAID, never silently collapsed). Both
// siblings are independently drivable by the field check, and driving them in
// the same parallel() batch would put TWO concurrent block-drive relays on ONE
// run: the futile-tick budget double-stamped, the human's greenlight consumed by
// one driver while the other takes the disclosed CAS loss, two watchdog markers
// racing. So the batch claims each run_id ONCE. The sibling is not dropped and
// not decided about — it is RECORDED with the collision named and left for a
// later pass, which picks it up naturally once the representative retires
// (nothing is remembered: the next pass re-reads collides_with off its own
// status relay).
//
// The `limit` clip is applied AFTER the collision check on purpose: an item past
// the bound whose run is already claimed is still a collision the human should
// see, while one merely past the bound is just next pass's work and needs no
// record.
const selectDrivableBatch = (drivable, limit) => {
  const batch = []
  const deferred = []
  const claimed = new Map() // run_id -> the item_id that claimed it this pass
  for (const item of drivable) {
    const runId = item.run_id
    const owner = claimed.get(runId)
    if (owner !== undefined) {
      deferred.push({
        item_id: item.item_id,
        run_id: runId,
        action: 'deferred_run_id_collision',
        driven_item_id: owner,
        collides_with: Array.isArray(item.collides_with) ? item.collides_with : [],
        reason:
          `run_id ${runId} is already being driven by item ${owner} in this pass ` +
          `(queue-status collides_with); driving both would put two concurrent ticks on ` +
          `one run. Deferred — a later pass drives it once the first retires.`,
      })
      continue
    }
    if (batch.length >= limit) continue // just past the bound: next pass's work
    claimed.set(runId, item.item_id)
    batch.push(item)
  }
  return { batch, deferred }
}

// ── The pass report ─────────────────────────────────────────────────────────
// One shape for every return site, with each outcome class under its OWN key and
// a count beside it. The counts are the point: a pass can accumulate many
// verbatim park briefs, and a human reading the result must be able to see
// "3 parked, 1 skipped, 2 deferred" without walking the records. Ordering the
// records is deliberately NOT done here — that is attention-queue's job (§13),
// and re-deriving a ranking in this plan would be a second, divergent one.
const BUCKET_KEYS = ['parked', 'skipped', 'deferred', 'held', 'settled', 'failed']
const passReport = (action, buckets, extra) => {
  const counts = {}
  for (const key of BUCKET_KEYS) counts[key] = buckets[key].length
  return { action, counts, ...buckets, ...(extra || {}) }
}

validatePlan()
if (!args || !args.repo) {
  throw new Error(`queue-drain needs args {repo, ...}: ${JSON.stringify(ARGS_CONTRACT)}`)
}
const { repo } = args
const maxLoops = args.maxLoops || 4
const maxPasses = args.maxPasses || 10
const maxTicks = args.maxTicks || 25
const maxWaitChunks = args.maxWaitChunks || 90
const maxAttempts = args.maxAttempts || 3
const statusLimit = args.statusLimit || 50
const statusInputs = {
  repo,
  statusLimit,
  campaignBase: args.campaignBase,
  cluster: args.cluster,
}

// Drive ONE item until it parks, settles, stalls, or exhausts its tick budget.
// Returns a record — never a decision, never an interpretation. This is
// campaign-run's Drive loop, scoped to one queue item and with the park turned
// from a RETURN into a RECORD: the human answers a park, so the pass keeps
// going with the other items instead of looping on this one.
const driveItem = async (pass, item) => {
  const runId = item.run_id
  const ticks = []
  for (let i = 0; i < maxTicks; i++) {
    const tag = `drain:p${pass}:${item.item_id}`
    const relay = await runWithRetry('tick', { repo, runId }, { label: `${tag}:tick:${i + 1}` })
    if (!relay || relay.exit_code !== 0) {
      return { item_id: item.item_id, run_id: runId, action: 'tick_failed', ticks: ticks.length, last_output: relay ? relay.output : '(tick relay agent died)' }
    }
    const parsed = parseEnvelope(relay.output)
    if (!parsed) {
      return { item_id: item.item_id, run_id: runId, action: 'tick_unparseable', ticks: ticks.length, last_output: relay.output }
    }
    if (!parsed.ok) {
      // An `ok:false` envelope is a REFUSAL the kernel stated, not a crash;
      // branched explicitly rather than falling through as an empty tick.
      return { item_id: item.item_id, run_id: runId, action: 'tick_refused', ticks: ticks.length, error_code: parsed.error.error_code, reason: parsed.error.message }
    }
    const tick = parsed.data
    ticks.push({ action: tick.action, verb: tick.current_verb || tick.next_verb || null })
    const disposition = tickDisposition(tick.action)

    if (disposition === 'park') {
      // THE PARK. The brief is code-digested and relay-bound: record it (plus
      // the pointer fields) untouched for the main session to relay verbatim
      // and for the human to answer inline. No agent in this plan sees the y,
      // and the pass does NOT come back to this item — the next pass will,
      // once the y has committed and queue-status projects it drivable again.
      return {
        item_id: item.item_id,
        run_id: runId,
        action: 'awaiting_decision',
        ticks: ticks.length,
        next_verb: tick.next_verb || null,
        brief: tick.brief || null,
        reason: tick.reason || null,
      }
    }
    if (disposition === 'skip') {
      // THE SKIP — a loop-ENDING record, for the same reason the park is one:
      // the kernel already decided nothing moved, and it will decide that again
      // on every retick. Disclose the kernel's own words (`reason` carries
      // "block <verb> failed or returned no result (exit N)", the R3 sha-pin
      // refusal, or the unroutable-position message) plus the position, so the
      // human sees WHY without a second relay. Reported under skipped[], not
      // parked[]: a park is a question waiting on a human, a skip is a stall
      // waiting on a fix, and collapsing them hides which one is which.
      return {
        item_id: item.item_id,
        run_id: runId,
        action: 'skip',
        ticks: ticks.length,
        current_verb: tick.current_verb || null,
        next_verb: tick.next_verb || null,
        reason: tick.reason || null,
      }
    }
    if (disposition === 'terminal') {
      return { item_id: item.item_id, run_id: runId, action: 'terminal', ticks: ticks.length, stage_reached: tick.stage_reached || null }
    }
    if (disposition === 'unknown') {
      return { item_id: item.item_id, run_id: runId, action: 'tick_unknown_action', ticks: ticks.length, tick_action: tick.action || null, reason: tick.reason || null }
    }
    if (disposition === 'wait') {
      const block = tick.current_verb || tick.next_verb || null
      log(`${tag}: detached at ${block || '?'} — waiting (chunked)`)
      // Chunked wait: each relay is one bounded wait-detached chunk
      // (timeout_sec 480 in the COMMANDS template, under the harness's ~10-min
      // command bound). The label carries BOTH the item id and the chunk index
      // — the index keeps every chunk a DISTINCT engine call (identical
      // (prompt, opts) would collide in the resume cache) and the item id keeps
      // two items' concurrent waits from colliding with each other, which is
      // also what makes the heartbeat log readable. A 'timeout' outcome logs a
      // heartbeat and re-arms; the other two outcomes wait-detached declares
      // ('worker_exited', 'no_live_worker') break to the next tick, which reads
      // the journal truth — the plan never interprets the wait, it only paces it.
      let resolved = false
      for (let chunk = 1; chunk <= maxWaitChunks; chunk++) {
        const wait = await runWithRetry('wait-detached', { repo, runId, block }, { label: `${tag}:wait:${i + 1}.${chunk}` })
        if (!wait || wait.exit_code !== 0) {
          return { item_id: item.item_id, run_id: runId, action: 'wait_failed', ticks: ticks.length, block, last_output: wait ? wait.output : '(wait relay agent died)' }
        }
        const waited = parseEnvelope(wait.output)
        if (waited && waited.ok && waited.data.outcome === 'timeout') {
          log(`${tag}: wait chunk ${chunk}/${maxWaitChunks} — still detached at ${block || '?'}`)
          continue
        }
        resolved = true
        break
      }
      if (!resolved) {
        return { item_id: item.item_id, run_id: runId, action: 'wait_stalled', ticks: ticks.length, block }
      }
      continue
    }
    // progress — advanced / reran / chained: the chain moved, so tick again.
    // `skip` is deliberately NOT here (see TICK_DISPOSITION): it is stable, so
    // ticking it again spends the whole budget re-reading the same answer.
    log(`drain:p${pass}:${item.item_id}: ${tick.action}${tick.current_verb ? ' @ ' + tick.current_verb : ''}`)
  }
  return { item_id: item.item_id, run_id: runId, action: 'tick_budget_exhausted', ticks: ticks.length }
}

// One bucket per outcome CLASS, reported under its own key with a count
// (passReport). Distinct keys are not cosmetics: parked[] is the human's queue,
// skipped[] is the "something is broken" queue, deferred[] is bookkeeping about
// this pass's own batching, and a reader who has to tell them apart by squinting
// at an `action` string inside one flat list pays that cost on every pass.
const buckets = { parked: [], skipped: [], deferred: [], held: [], settled: [], failed: [] }
let lastStatus = null
let passes = 0

for (let pass = 1; pass <= maxPasses; pass++) {
  passes = pass
  phase('Status')
  const relay = await runStep('status', statusInputs, { label: `drain:p${pass}:status` })
  if (!relay || relay.exit_code !== 0) {
    phase('Report')
    return passReport('status_failed', buckets, { passes, last_output: relay ? relay.output : '(status relay agent died)' })
  }
  const parsed = parseEnvelope(relay.output)
  if (!parsed) {
    phase('Report')
    return passReport('status_unparseable', buckets, { passes, last_output: relay.output })
  }
  if (!parsed.ok) {
    phase('Report')
    return passReport('status_refused', buckets, { passes, error_code: parsed.error.error_code, reason: parsed.error.message })
  }
  const status = parsed.data
  lastStatus = {
    computed_at: status.computed_at || null,
    counts: status.counts || null,
    occupancy: status.occupancy || null,
    total_items: typeof status.total_items === 'number' ? status.total_items : null,
    truncated: status.truncated === true,
    notes: status.notes || [],
  }

  const items = Array.isArray(status.items) ? status.items : []
  const drivableAll = items.filter(isDrivable)
  // Retry policy: an item at or over the ceiling is HELD, never driven, and
  // reported so the human sees it. Recomputed from THIS pass's rows, so a
  // counter the kernel resets is honoured immediately.
  const overAttempts = drivableAll.filter((it) => attemptsOf(it) >= maxAttempts)
  const drivable = drivableAll.filter((it) => attemptsOf(it) < maxAttempts)
  for (const it of overAttempts) {
    if (buckets.held.some((h) => h.item_id === it.item_id)) continue
    buckets.held.push({ item_id: it.item_id, run_id: it.run_id, attempts: attemptsOf(it), max_attempts: maxAttempts, reason: `drive_attempts ${attemptsOf(it)} >= maxAttempts ${maxAttempts}: ${attemptsOf(it)} consecutive tick(s) moved this run nothing; held, not driven` })
  }

  if (!drivable.length) {
    // The §7 relaunch-cheapness invariant, realized: a pass with nothing to do
    // cost exactly ONE status relay and spawned no loops.
    phase('Report')
    log(`pass ${pass}: nothing drivable (${items.length} item(s) in the status page)`)
    return passReport(passes === 1 ? 'nothing_drivable' : 'quiescent', buckets, { passes, status: lastStatus })
  }

  // THE LOOP BOUND, in full: min(drivable after run_id dedupe, maxLoops). There
  // is no third term and there must not be one that cannot bind. It used to
  // carry a ceiling read off the status digest — total_items, the count of
  // unsettled items this read MATCHED before the limit clip — and that term was
  // structural inertia rather than a guard: total_items >= items.length >=
  // drivable.length ALWAYS, so the min could never select it. queue-status
  // publishes no capacity field to re-point it at either, by design (§7 R3: the
  // scheduler IS the capacity queue and queue-advance shed the capacity
  // question) — occupancy is a per-campaign_id evidence map, not a pass ceiling.
  // So the bound is the caller's maxLoops, applied to the deduped set, and
  // nothing else.
  const { batch, deferred } = selectDrivableBatch(drivable, maxLoops)
  for (const record of deferred) {
    if (buckets.deferred.some((d) => d.item_id === record.item_id)) continue
    buckets.deferred.push(record)
  }
  phase('Drive')
  log(`pass ${pass}: ${drivable.length} drivable, driving ${batch.length} (maxLoops ${maxLoops}${deferred.length ? `, ${deferred.length} deferred on a run_id collision` : ''})`)
  const outcomes = await parallel(batch.map((item) => () => driveItem(pass, item)))

  for (const outcome of outcomes) {
    if (!outcome) continue
    if (outcome.action === 'awaiting_decision') buckets.parked.push(outcome)
    else if (outcome.action === 'skip') buckets.skipped.push(outcome)
    else if (outcome.action === 'terminal') buckets.settled.push(outcome)
    else buckets.failed.push(outcome)
  }

  // Re-status and repeat. Nothing from this pass is carried forward except the
  // RECORDS being reported; the next pass recomputes the drivable set from its
  // own relay, so a y journaled inline mid-pass is picked up with no
  // coordination and a relaunch here is indistinguishable from continuing.
}

phase('Report')
return passReport('pass_budget_exhausted', buckets, {
  passes,
  status: lastStatus,
  resume_hint:
    'the pass budget ran out with work still drivable — relaunch this workflow FRESH (same args, new run). ' +
    'NEVER resumeFromRunId: the engine replays cached calls verbatim, so a resumed pass returns these same ' +
    'records without one live relay. Kernel state is durable; a fresh pass re-reads the ledger.',
})
