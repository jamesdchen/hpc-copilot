export const meta = {
  name: 'campaign-run',
  description:
    'Drive one campaign through the block chain by plan-relayed block-drive ticks: advance in code, wait out detached blocks in bounded chunks, PARK at every typed-y gate returning the brief, relaunch FRESH after the human greenlights inline (kernel state is durable; never resume-from-cache across a park) — rule 5 of docs/design/agent-delegation.md',
  whenToUse:
    "When a campaign should progress without its tick/wait relay traffic entering the main session's context. Pass args: {repo, runId, workflow?, maxTicks?}. INTAKE FIRST (README, 'Intake'): resolve every ARGS_CONTRACT field BEFORE launching — fill what you can yourself (repo from cwd, runId from the runs store or the user's words, prior-run args), then propose the WHOLE arg set to the user in ONE confirm/correct exchange of diffs; never launch half-resolved, never interrogate cold. For unattended driving, the same intake exchange offers the overnight STANDING CONSENT (typed inline, hard caps + armed wake — README 'Upfront the y's'): block-drive then auto-advances consented boundaries in code and this plan parks only at unconsented ones. The workflow NEVER passes a gate: a tick that returns awaiting_decision ends the run with the brief pointer; the human journals the y inline (append-decision stays with the main session, always). AUTO-RESUME: the moment the y is journaled, relaunch this workflow FRESH (a new run, same args) immediately and without asking — a park is a question, not a stop, and the human should never have to say 'continue'. Never relaunch with resumeFromRunId across a park: the cache replays the recorded awaiting_decision tick verbatim without a live call, so a resumed run returns the SAME park forever. Fresh is cheap by construction — block-drive's state is durable, so a fresh pass simply ticks from the current journal. block-drive keeps owning the sequencing; this plan only relays its rule-fixed invocations (the runtime refuses ungreenlit blocks regardless of caller, so parking is the enforced shape, not a courtesy).",
  phases: [
    { title: 'Drive', detail: 'tick block-drive; advance/skip/rerun loop in plan code; a detached block gets a wait-detached relay before the next tick' },
    { title: 'Park', detail: 'awaiting_decision or terminal: return action + brief pointer to the main session; no agent touches the y' },
  ],
}

// ============================================================================
// PORTABLE PLAN
// ----------------------------------------------------------------------------
// Engine-neutral section; the adapter below is the only part that speaks the
// Claude Workflow API. Seam contract: README.md beside this file.
//
// This plan exercises the plan-relay level (docs/design/agent-delegation.md
// rule 5): its COMMANDS invoke workflow-kind verbs (block-drive,
// wait-detached), which is legal ONLY here — the commands are authored
// templates, the model composes nothing, and every typed-y gate parks back
// to the main session. append-decision appears in no plan, ever (rule 2;
// enforced by tests/contracts/test_workflow_plan_delegation.py).

const ARGS_CONTRACT = {
  repo: 'absolute path of the experiment checkout (required; verbs run with --experiment-dir)',
  runId: 'the campaign run id to drive (required; block-drive spec run_id)',
  workflow: "optional block-drive workflow name (e.g. 'submit'); omitted = block-drive default for the run",
  maxTicks: 'optional safety bound on ticks per invocation (default 25); hitting it parks with action "tick_budget_exhausted"',
  maxWaitChunks:
    'optional bound on wait-detached chunks per detached block (default 90; ~8 min per chunk ≈ 12h); hitting it parks with action "wait_stalled"',
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

// Step vocabulary as in every plan here (see README). Both steps are
// kind: 'script' — there is deliberately NO agent-judgment step in this
// plan: driving is code (block-drive) plus rule-fixed relays, and anything
// needing judgment (a red tick, a stuck wait) parks to the main session
// with the relayed output rather than being interpreted in-flight.
const STEPS = [
  {
    id: 'tick',
    phase: 'Drive',
    kind: 'script',
    run: 'fan-out', // one instance per loop iteration, sequential by needs
    needs: [],
    isolation: 'shared-checkout',
    command: 'blockDrive',
    output: 'SCRIPT_RESULT',
    effort: 'low',
    abort_on_failure: true, // a tick the CLI itself rejects must not be re-ticked blind
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

// Rule-fixed commands. block-drive and wait-detached are workflow/query
// verbs relayed under rule 5: pure authored templates, specs heredoc'd to
// mktemp files, nothing model-composed.
const specRun = (repo, verb, spec) =>
  `SPEC=$(mktemp) && printf '%s' '${JSON.stringify(spec)}' > "$SPEC" && hpc-agent ${verb} --spec "$SPEC" --experiment-dir ${repo}`

const COMMANDS = {
  blockDrive: ({ repo, runId, workflow }) =>
    specRun(repo, 'block-drive', workflow ? { run_id: runId, workflow } : { run_id: runId }),
  // timeout_sec 480 keeps each relayed wait comfortably under the harness's
  // ~10-min command bound (fable-sweep 2026-07-29: the CLI's 7200s default
  // would be KILLED by the harness before it could even report its own
  // timeout, parking every real detached block as wait_failed). The chunk
  // loop in the adapter re-arms until terminal or maxWaitChunks.
  waitDetached: ({ repo, runId, block }) =>
    specRun(
      repo,
      'wait-detached',
      block ? { run_id: runId, block, timeout_sec: 480 } : { run_id: runId, timeout_sec: 480 }
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
  if (problems.length) throw new Error(`campaign-run plan invalid:\n${problems.join('\n')}`)
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

// The tick's JSON payload, parsed defensively: the relay carries
// stdout+stderr verbatim, so the payload is the last JSON object line.
const parseTick = (output) => {
  for (const line of output.split('\n').reverse()) {
    const t = line.trim()
    if (t.startsWith('{') && t.endsWith('}')) {
      try {
        return JSON.parse(t)
      } catch {
        // keep scanning — an earlier line may hold the payload
      }
    }
  }
  return null
}

validatePlan()
if (!args || !args.repo || !args.runId) {
  throw new Error(`campaign-run needs args {repo, runId, ...}: ${JSON.stringify(ARGS_CONTRACT)}`)
}
const { repo, runId } = args
const maxTicks = args.maxTicks || 25
const maxWaitChunks = args.maxWaitChunks || 90

phase('Drive')
const ticks = []
for (let i = 0; i < maxTicks; i++) {
  const relay = await runStep('tick', { repo, runId, workflow: args.workflow }, { label: `tick:${i + 1}` })
  if (!relay || relay.exit_code !== 0) {
    // abort_on_failure: a rejected tick parks with the relayed output — the
    // main session decides; re-ticking blind could mask a real refusal.
    phase('Park')
    return {
      action: 'tick_failed',
      ticks: ticks.length,
      last_output: relay ? relay.output : '(tick relay agent died)',
    }
  }
  const tick = parseTick(relay.output)
  if (!tick) {
    phase('Park')
    return { action: 'tick_unparseable', ticks: ticks.length, last_output: relay.output }
  }
  ticks.push({ action: tick.action, verb: tick.current_verb || tick.next_verb || null })

  if (tick.action === 'awaiting_decision') {
    // THE PARK. The brief is code-digested and relay-bound: return it (plus
    // the pointer fields) untouched for the main session to relay verbatim
    // and for the human to answer inline. No agent in this plan sees the y.
    phase('Park')
    return {
      action: 'awaiting_decision',
      ticks: ticks.length,
      next_verb: tick.next_verb || null,
      brief: tick.brief || null,
      reason: tick.reason || null,
      resume_hint:
        'after the y is journaled inline, relaunch this workflow FRESH (same args, new run) — ' +
        'NOT resumeFromRunId: the cache would replay this parked tick verbatim and return the ' +
        'same park without a live call. Kernel state is durable; fresh ticks from the journal.',
    }
  }
  if (tick.action === 'terminal') {
    phase('Park')
    return { action: 'terminal', ticks: ticks.length, stage_reached: tick.stage_reached || null, tick_log: ticks }
  }
  if (tick.action === 'detached') {
    const block = tick.current_verb || tick.next_verb || null
    log(`tick ${i + 1}: detached at ${block || '?'} — waiting (chunked)`)
    // Chunked wait (fable-sweep 2026-07-29): each relay is one bounded
    // wait-detached chunk (timeout_sec 480 in the COMMANDS template, under
    // the harness's ~10-min command bound). The chunk index in the label
    // keeps every chunk a DISTINCT engine call — identical (prompt, opts)
    // would collide in the resume cache. A 'timeout' outcome logs a
    // heartbeat and re-arms; any other outcome (terminal, worker exit,
    // no-live-worker) breaks to the next tick, which reads the journal
    // truth — the plan never interprets the wait, it only paces it.
    let resolved = false
    for (let chunk = 1; chunk <= maxWaitChunks; chunk++) {
      const wait = await runWithRetry(
        'wait-detached',
        { repo, runId, block },
        { label: `wait:${i + 1}.${chunk}` }
      )
      if (!wait || wait.exit_code !== 0) {
        phase('Park')
        return {
          action: 'wait_failed',
          ticks: ticks.length,
          last_output: wait ? wait.output : '(wait relay agent died)',
        }
      }
      const payload = parseTick(wait.output)
      if (payload && payload.outcome === 'timeout') {
        log(`tick ${i + 1}: wait chunk ${chunk}/${maxWaitChunks} — still detached at ${block || '?'}`)
        continue
      }
      resolved = true
      break
    }
    if (!resolved) {
      phase('Park')
      return { action: 'wait_stalled', ticks: ticks.length, block, tick_log: ticks }
    }
    continue
  }
  // advance / advance_carrying / fresh / rerun / skip: tick again
  log(`tick ${i + 1}: ${tick.action}${tick.current_verb ? ' @ ' + tick.current_verb : ''}`)
}

phase('Park')
return { action: 'tick_budget_exhausted', ticks: ticks.length, tick_log: ticks }
