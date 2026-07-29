export const meta = {
  name: 'campaign-recon',
  description:
    'Read-only recon sweep over a campaign: fan out the delegable query verbs (doctor, attention-queue, log/dir digests, detached polls, evidence brief) in parallel subagents and return one advisory result of exit codes, bounded outputs, and pointers — the mechanized fan-out form of the hpc-recon context firewall',
  whenToUse:
    'When the main session wants campaign reconnaissance without paying the context for it — before composing a status relay, after a failure signature, ahead of an aggregate decision. Pass args: {repo, detached?, logPaths?, digestPaths?, decisionsScope?, triageHost?, evidenceTags?}. Recon only: every command is a query/validate verb (enforced by tests/contracts/test_workflow_plan_delegation.py); the execution path — block-drive ticks, submit/aggregate blocks, append-decision — is LOCKED out per docs/design/agent-delegation.md rule 2 and stays with the main session.',
  phases: [
    { title: 'Sweep', detail: 'fan out the delegable query verbs; every probe relayed verbatim (exit code + bounded output); a failed probe is a finding, never a retry-and-fix' },
    { title: 'Diagnose', detail: 'only when a probe is anomalous: one read-only agent folds the anomalous outputs into findings + read-next pointers' },
  ],
}

// ============================================================================
// PORTABLE PLAN
// ----------------------------------------------------------------------------
// Everything down to RUNTIME ADAPTER is engine-neutral: the argument contract,
// the step graph (STEPS), the structured-output schemas (SCHEMAS, plain JSON
// Schema), the prompt templates (PROMPTS) and script commands (COMMANDS) —
// all pure data / pure string-building functions with named inputs. Porting
// this workflow to another orchestrator means translating THIS section; only
// the adapter below speaks the Claude Workflow API. The mapping contract lives
// in the README.md beside this file.
//
// This plan operates at the recon-only delegation level
// (docs/design/agent-delegation.md): every COMMANDS entry invokes a
// query/validate verb and nothing else — subagents sit BESIDE the execution
// path as a context firewall, never inside it. The main session keeps every
// act: block-drive ticks, the submit/aggregate blocks, append-decision, and
// every verbatim relay. A render-bearing verb's output travels as POINTERS +
// COUNTS (render paths, shas, tallies); no agent paraphrases a render the
// human must see verbatim.

const ARGS_CONTRACT = {
  repo: 'absolute path of the experiment checkout (required; every verb runs with --experiment-dir)',
  detached: 'optional array of {run_id, block} detached launches to poll (poll-detached)',
  logPaths: 'optional array of worker log paths to digest (worker-log-digest)',
  digestPaths: 'optional array of directories to summarize (dir-digest)',
  decisionsScope: 'optional {scope_kind, scope_id} — runs read-decisions in digest mode when given',
  triageHost: 'optional cluster host — runs net-triage when given',
  evidenceTags: 'optional array of tags to scope evidence-brief',
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
  DIAGNOSIS: {
    type: 'object',
    required: ['findings', 'read_next'],
    properties: {
      findings: {
        type: 'array',
        items: {
          type: 'object',
          required: ['source', 'summary', 'severity'],
          properties: {
            source: { type: 'string' },
            summary: { type: 'string' },
            severity: { type: 'string' },
          },
        },
      },
      read_next: { type: 'array', items: { type: 'string' } },
    },
  },
}

// Step graph. Same vocabulary as every plan in this directory:
//   kind:      'agent' (LLM judgment) | 'script' (rule-fixed command; the
//              deterministic work an LLM must relay, never re-derive — the
//              determinism-boundary principle applied to orchestration)
//   run:       'once' | 'fan-out' | 'conditional'
//   needs:     DAG edges, as step ids
//   isolation: 'shared-checkout' | 'fresh-worktree'
//   prompt:    PROMPTS key (agent steps) — validated present
//   command:   COMMANDS key (script steps) — validated present
//   output:    SCHEMAS key, or null for free text
//   effort:    optional reasoning-effort override
//   retry:     re-dispatches for a dead instance before giving up on it
//
// Recon never aborts (contrast a build plan's abort_on_failure): a red probe
// is exactly the data the sweep exists to surface, so every step logs and
// continues, and a nonzero exit feeds the Diagnose condition.
const STEPS = [
  {
    id: 'doctor',
    phase: 'Sweep',
    kind: 'script',
    run: 'once',
    needs: [],
    isolation: 'shared-checkout',
    command: 'doctor',
    output: 'SCRIPT_RESULT',
    effort: 'low',
  },
  {
    id: 'attention-queue',
    phase: 'Sweep',
    kind: 'script',
    run: 'once',
    needs: [],
    isolation: 'shared-checkout',
    command: 'attentionQueue',
    output: 'SCRIPT_RESULT',
    effort: 'low',
  },
  {
    id: 'evidence-brief',
    phase: 'Sweep',
    kind: 'script',
    run: 'once',
    needs: [],
    isolation: 'shared-checkout',
    command: 'evidenceBrief',
    output: 'SCRIPT_RESULT',
    effort: 'low',
  },
  {
    id: 'poll-detached',
    phase: 'Sweep',
    kind: 'script',
    run: 'fan-out', // one instance per {run_id, block} in args.detached
    needs: [],
    isolation: 'shared-checkout',
    command: 'pollDetached',
    output: 'SCRIPT_RESULT',
    effort: 'low',
    retry: 1,
  },
  {
    id: 'worker-log-digest',
    phase: 'Sweep',
    kind: 'script',
    run: 'fan-out', // one instance per path in args.logPaths
    needs: [],
    isolation: 'shared-checkout',
    command: 'workerLogDigest',
    output: 'SCRIPT_RESULT',
    effort: 'low',
    retry: 1,
  },
  {
    id: 'dir-digest',
    phase: 'Sweep',
    kind: 'script',
    run: 'fan-out', // one instance per path in args.digestPaths
    needs: [],
    isolation: 'shared-checkout',
    command: 'dirDigest',
    output: 'SCRIPT_RESULT',
    effort: 'low',
    retry: 1,
  },
  {
    id: 'read-decisions',
    phase: 'Sweep',
    kind: 'script',
    run: 'conditional', // only when args.decisionsScope is given
    needs: [],
    isolation: 'shared-checkout',
    command: 'readDecisions',
    output: 'SCRIPT_RESULT',
    effort: 'low',
  },
  {
    id: 'net-triage',
    phase: 'Sweep',
    kind: 'script',
    run: 'conditional', // only when args.triageHost is given
    needs: [],
    isolation: 'shared-checkout',
    command: 'netTriage',
    output: 'SCRIPT_RESULT',
    effort: 'low',
  },
  {
    id: 'diagnose',
    phase: 'Diagnose',
    kind: 'agent',
    run: 'conditional', // only when a sweep probe is anomalous (nonzero exit / dead agent)
    needs: [
      'doctor',
      'attention-queue',
      'evidence-brief',
      'poll-detached',
      'worker-log-digest',
      'dir-digest',
      'read-decisions',
      'net-triage',
    ],
    isolation: 'shared-checkout',
    prompt: 'diagnose',
    output: 'DIAGNOSIS',
    effort: 'high',
  },
]
const STEP = (id) => STEPS.find((s) => s.id === id)

// Rule-fixed commands for kind: 'script' steps. Every entry is a
// query/validate verb — the contract test resolves each `hpc-agent <verb>`
// against the live registry and refuses anything else, so a mutating verb
// cannot land here by drift. Specs are heredoc'd to mktemp files because the
// verbs take --spec JSON; the template stays a pure (namedInputs) => string.
const specRun = (repo, verb, spec) =>
  `SPEC=$(mktemp) && printf '%s' '${JSON.stringify(spec)}' > "$SPEC" && hpc-agent ${verb} --spec "$SPEC" --experiment-dir ${repo}`

const COMMANDS = {
  doctor: ({ repo }) => specRun(repo, 'doctor', {}),
  attentionQueue: ({ repo }) => specRun(repo, 'attention-queue', {}),
  evidenceBrief: ({ repo, evidenceTags }) =>
    specRun(repo, 'evidence-brief', evidenceTags && evidenceTags.length ? { tags: evidenceTags } : {}),
  pollDetached: ({ repo, item }) => specRun(repo, 'poll-detached', { run_id: item.run_id, block: item.block }),
  workerLogDigest: ({ repo, logPath }) => specRun(repo, 'worker-log-digest', { log_path: logPath }),
  dirDigest: ({ repo, path }) => specRun(repo, 'dir-digest', { path }),
  readDecisions: ({ repo, decisionsScope }) =>
    specRun(repo, 'read-decisions', { scope_kind: decisionsScope.scope_kind, scope_id: decisionsScope.scope_id, digest: true }),
  netTriage: ({ repo, triageHost }) => specRun(repo, 'net-triage', { host: triageHost }),
}

const PROMPTS = {
  scriptStep: ({ repo, command }) =>
    `Run EXACTLY this command in ${repo} and nothing else:
${command}
Return per the schema: exit_code (the command's real exit code), output
(stdout+stderr verbatim; if enormous, keep the first and last 100 lines).
Do not fix, retry, re-run, or interpret a failure — relaying it is the job.`,

  diagnose: ({ repo, anomalies }) =>
    `You are a READ-ONLY reconnaissance agent for the hpc-agent campaign in
${repo}. These recon probes came back anomalous (nonzero exit, or the probe
agent died) — their relayed outputs, verbatim:
${JSON.stringify(anomalies, null, 2)}

Fold them into findings. You may read files and run hpc-agent QUERY verbs to
confirm what you report; you must NOT run any mutating, submit, scaffold, or
workflow verb, must NOT touch append-decision or any sign-off, and must NOT
write or edit any file (docs/design/agent-delegation.md rule 2). If a probe's
output points at a code-rendered file the human must see, return its PATH in
read_next — never a paraphrase of its contents.

Return per the schema:
- findings: source (the probe step id), one-sentence summary, severity.
- read_next: file paths / render paths the main session should read itself,
  in priority order. Empty if the anomalies need no follow-up reading.`,
}

// ============================================================================
// RUNTIME ADAPTER (Claude Workflow API)
// ----------------------------------------------------------------------------
// The only section that calls agent()/parallel()/phase()/log(). It walks the
// plan above: step order + barriers implement STEPS[].needs, and each dispatch
// takes its kind/isolation/schema/effort from its STEPS entry.

// Reject a malformed plan before the first agent is dispatched (the
// load-time-validation posture of declarative DAG engines: typos must not
// silently take effect).
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
  // cycle check over base-id edges
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
  if (problems.length) throw new Error(`campaign-recon plan invalid:\n${problems.join('\n')}`)
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

// retry-once semantics for instances whose agent died (returned null)
const runWithRetry = async (id, promptInputs, opts) => {
  const attempts = 1 + (STEP(id).retry || 0)
  for (let i = 0; i < attempts; i++) {
    const r = await runStep(id, promptInputs, i ? { ...opts, label: `${opts.label}:retry` } : opts)
    if (r !== null && r !== undefined) return r
  }
  return null
}

validatePlan()
if (!args || !args.repo) {
  throw new Error(`campaign-recon needs args {repo, ...}: ${JSON.stringify(ARGS_CONTRACT)}`)
}
const { repo } = args
const detached = args.detached || []
const logPaths = args.logPaths || []
const digestPaths = args.digestPaths || []

// The whole sweep is one parallel() barrier ON PURPOSE: Diagnose's condition
// (any anomalous probe) and the returned advisory result both need every
// probe's outcome — genuine cross-item dependence, the case a barrier is for.
phase('Sweep')
const probes = [
  { step: 'doctor', target: null, inputs: { repo } },
  { step: 'attention-queue', target: null, inputs: { repo } },
  { step: 'evidence-brief', target: null, inputs: { repo, evidenceTags: args.evidenceTags } },
  ...detached.map((item) => ({ step: 'poll-detached', target: `${item.run_id}/${item.block}`, inputs: { repo, item } })),
  ...logPaths.map((logPath) => ({ step: 'worker-log-digest', target: logPath, inputs: { repo, logPath } })),
  ...digestPaths.map((path) => ({ step: 'dir-digest', target: path, inputs: { repo, path } })),
  ...(args.decisionsScope ? [{ step: 'read-decisions', target: `${args.decisionsScope.scope_kind}/${args.decisionsScope.scope_id}`, inputs: { repo, decisionsScope: args.decisionsScope } }] : []),
  ...(args.triageHost ? [{ step: 'net-triage', target: args.triageHost, inputs: { repo, triageHost: args.triageHost } }] : []),
]
log(`sweeping ${probes.length} probe(s)`)
const results = await parallel(
  probes.map((p) => () =>
    runWithRetry(p.step, p.inputs, { label: `recon:${p.step}${p.target ? ':' + p.target : ''}` })
  )
)

const swept = probes.map((p, i) => ({
  step: p.step,
  target: p.target,
  exit_code: results[i] ? results[i].exit_code : null,
  output: results[i] ? results[i].output : '(recon agent died after retry)',
}))
const anomalies = swept.filter((s) => s.exit_code !== 0)

let diagnosis = null
if (anomalies.length) {
  phase('Diagnose')
  log(`${anomalies.length} anomalous probe(s) — diagnosing`)
  diagnosis = await runStep('diagnose', { repo, anomalies }, { label: 'diagnose' })
} else {
  log('all probes clean — no diagnosis needed')
}

// Advisory only: this result is model-carried text and is worth exactly
// nothing to the gates (docs/design/agent-delegation.md rule 3). The main
// session reads render files itself for anything relay-verbatim.
return {
  swept,
  anomaly_count: anomalies.length,
  diagnosis,
}
