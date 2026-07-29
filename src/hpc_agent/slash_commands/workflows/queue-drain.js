export const meta = {
  name: 'queue-drain',
  description:
    'Drain the run queue: relay one queue-status, compute the drivable set from its items[] fields in plan code, drive each drivable item with plan-relayed block-drive ticks (chunked detached waits), record every park and move on, then re-status and repeat until nothing is drivable — a PURE relay, the sibling of campaign-run at the ledger level (docs/plans/run-queue-placement-2026-07-28.md §5/§7, rule 5 of docs/design/agent-delegation.md)',
  whenToUse:
    "When the run queue has work and the tick/wait relay traffic should stay out of the main session's context. Pass args: {repo, maxLoops?, maxPasses?, maxTicks?, maxWaitChunks?, maxAttempts?, campaignBase?, cluster?}. INTAKE FIRST (README, 'Intake'): resolve every ARGS_CONTRACT field BEFORE launching — repo from cwd, the bounds from their defaults — and propose the WHOLE arg set in ONE confirm/correct exchange. RELAUNCH IS THE REFLEX: a pass is stateless and reads everything it needs from its OWN first queue-status relay, so relaunching FRESH at any time, for any reason, is always correct and always cheap — a pass that finds nothing drivable costs exactly one status relay and returns (the §7 relaunch-cheapness invariant). NEVER relaunch with resumeFromRunId: the engine replays cached calls verbatim, so a resumed pass returns the recorded parks forever without one live tick. The workflow NEVER passes a gate and never decides anything: admission, placement and sequencing are kernel code (queue-advance / queue-dispatch / block-drive), the drivable set is a mechanical field check over relayed items[], and every awaiting_decision is RECORDED and left for the human — this plan does not loop on a parked item, it moves to the next one. AUTO-RESUME: the moment a y is journaled inline, relaunch this workflow FRESH in the same breath; the freshly-drivable item is picked up by the next pass.",
  phases: [
    { title: 'Status', detail: 'one queue-status relay per pass; the drivable set is computed from its items[] fields in plan code, never remembered across passes' },
    { title: 'Drive', detail: 'one block-drive loop per drivable item, chunked wait-detached for detached blocks; a park is recorded and the loop ends' },
    { title: 'Report', detail: 'return the merged park queue + the last status snapshot when nothing is drivable' },
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
// tick. Those two tickers can overlap by design and the race is safe because
// the CLAIMS make it safe: run ids are COMPUTED, so both derive the same id and
// the shipped single-lease guard (_kernel/lifecycle/detached.py) arbitrates —
// the loser gets a held claim, never a double-drive. This plan therefore never
// coordinates with the main session, and must never grow a "wait for the
// inline tick" step.
//
// S5 (fresh relaunch) — every value this plan uses comes from ARGS or from the
// CURRENT pass's queue-status relay. Nothing is carried across passes, nothing
// is resumed, nothing is cached. A relaunch from scratch at any instant is
// equivalent to the pass that would have run next.

const ARGS_CONTRACT = {
  repo: 'absolute path of the experiment checkout (required; verbs run with --experiment-dir)',
  maxLoops:
    'optional bound on drivable items driven per pass (default 4); the pass drives min(drivable, maxLoops, the status read\'s own item ceiling)',
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

// The pass's ledger-derived ceiling, READ from this pass's own status relay and
// never remembered. queue-status exposes no capacity FIELD by design (§7 R3:
// the scheduler IS the capacity queue and queue-advance shed the capacity
// question), so the ceiling is the one bound the digest does carry —
// total_items, the count of unsettled items this read matched. It can only
// narrow the pass, never widen it.
const statusCeiling = (data, fallback) =>
  typeof data.total_items === 'number' && data.total_items >= 0 ? data.total_items : fallback

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

    if (tick.action === 'awaiting_decision') {
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
    if (tick.action === 'terminal') {
      return { item_id: item.item_id, run_id: runId, action: 'terminal', ticks: ticks.length, stage_reached: tick.stage_reached || null }
    }
    if (tick.action === 'detached') {
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
    // advanced / reran / chained / skip (BlockDriveResult.action's remaining
    // members): tick again
    log(`drain:p${pass}:${item.item_id}: ${tick.action}${tick.current_verb ? ' @ ' + tick.current_verb : ''}`)
  }
  return { item_id: item.item_id, run_id: runId, action: 'tick_budget_exhausted', ticks: ticks.length }
}

const parked = []
const held = []
const settled = []
const failed = []
let lastStatus = null
let passes = 0

for (let pass = 1; pass <= maxPasses; pass++) {
  passes = pass
  phase('Status')
  const relay = await runStep('status', statusInputs, { label: `drain:p${pass}:status` })
  if (!relay || relay.exit_code !== 0) {
    phase('Report')
    return { action: 'status_failed', passes, parked, held, settled, failed, last_output: relay ? relay.output : '(status relay agent died)' }
  }
  const parsed = parseEnvelope(relay.output)
  if (!parsed) {
    phase('Report')
    return { action: 'status_unparseable', passes, parked, held, settled, failed, last_output: relay.output }
  }
  if (!parsed.ok) {
    phase('Report')
    return { action: 'status_refused', passes, parked, held, settled, failed, error_code: parsed.error.error_code, reason: parsed.error.message }
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
    if (held.some((h) => h.item_id === it.item_id)) continue
    held.push({ item_id: it.item_id, run_id: it.run_id, attempts: attemptsOf(it), max_attempts: maxAttempts, reason: `drive_attempts ${attemptsOf(it)} >= maxAttempts ${maxAttempts}: ${attemptsOf(it)} consecutive tick(s) moved this run nothing; held, not driven` })
  }

  if (!drivable.length) {
    // The §7 relaunch-cheapness invariant, realized: a pass with nothing to do
    // cost exactly ONE status relay and spawned no loops.
    phase('Report')
    log(`pass ${pass}: nothing drivable (${items.length} item(s) in the status page)`)
    return { action: passes === 1 ? 'nothing_drivable' : 'quiescent', passes, parked, held, settled, failed, status: lastStatus }
  }

  // The pass's loop count, derived from THIS pass's relayed data every time —
  // never remembered, never accumulated (§5: state in variables + journals,
  // and the variables are rebuilt from the relay each pass).
  const loops = Math.min(drivable.length, maxLoops, statusCeiling(status, drivable.length))
  const batch = drivable.slice(0, loops)
  phase('Drive')
  log(`pass ${pass}: ${drivable.length} drivable, driving ${batch.length} (maxLoops ${maxLoops}, status ceiling ${statusCeiling(status, drivable.length)})`)
  const outcomes = await parallel(batch.map((item) => () => driveItem(pass, item)))

  for (const outcome of outcomes) {
    if (!outcome) continue
    if (outcome.action === 'awaiting_decision') parked.push(outcome)
    else if (outcome.action === 'terminal') settled.push(outcome)
    else failed.push(outcome)
  }

  // Re-status and repeat. Nothing from this pass is carried forward except the
  // RECORDS being reported; the next pass recomputes the drivable set from its
  // own relay, so a y journaled inline mid-pass is picked up with no
  // coordination and a relaunch here is indistinguishable from continuing.
}

phase('Report')
return {
  action: 'pass_budget_exhausted',
  passes,
  parked,
  held,
  settled,
  failed,
  status: lastStatus,
  resume_hint:
    'the pass budget ran out with work still drivable — relaunch this workflow FRESH (same args, new run). ' +
    'NEVER resumeFromRunId: the engine replays cached calls verbatim, so a resumed pass returns these same ' +
    'records without one live relay. Kernel state is durable; a fresh pass re-reads the ledger.',
}
