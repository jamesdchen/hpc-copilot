export const meta = {
  name: 'swarm-units',
  description:
    'Mechanized handoff-package swarm: build file-disjoint units from a unit-specs.json in parallel worktrees, integrate per wave (one regen + lint gauntlet), then review lenses + fixer',
  whenToUse:
    'When a handoff package (docs/plans/<pkg>/ with ARCHITECT-MEMO.md + unit-specs.json, per docs/plans/_TEMPLATE-handoff/) is greenlit for implementation. Pass args: {specsPath, memoPath, repo, branch, sessionTrailer?, pushPolicy?}. This is the mechanized form of the swarm-dispatch protocol the plans previously described in prose (e.g. docs/plans/handoff-packages-2026-07-12/mcp-latency-docs-packages.workflow.js, the one-off ancestor of this script).',
  phases: [
    { title: 'Load', detail: 'disjointness gate (script) + spec/memo load, in parallel; abort before any build on failure' },
    { title: 'Build', detail: 'one worktree agent per unit, waves in order, file-disjoint within a wave; dead units retried once' },
    { title: 'Integrate', detail: 'per wave: merge pkg/* in order, regen ONCE, lint gauntlet, targeted tests; a failed wave aborts the run' },
    { title: 'Review', detail: 'correctness + house-rules lenses, then a fixer for confirmed findings' },
  ],
}

// ============================================================================
// PORTABLE PLAN
// ----------------------------------------------------------------------------
// Everything down to RUNTIME ADAPTER is engine-neutral: the argument contract,
// the step graph (STEPS), the structured-output schemas (SCHEMAS, plain JSON
// Schema), the prompt templates (PROMPTS) and script commands (COMMANDS) —
// all pure data / pure string-building functions with named inputs. Porting
// this workflow to another orchestrator — e.g. a declarative step/DAG format
// like awman's workflow.toml — means translating THIS section; only the
// adapter below speaks the Claude Workflow API. The mapping contract lives in
// .claude/workflows/README.md.
//
// STEPS is consumed by the adapter (kind, isolation, output schema, phase,
// effort, failure policy), so the operational fields cannot drift from what
// actually runs; `needs` records the DAG edges the adapter's control flow
// implements, and validatePlan() rejects a malformed plan before the first
// agent is dispatched.

const ARGS_CONTRACT = {
  specsPath: 'absolute path to the package unit-specs.json (required)',
  memoPath:
    'absolute path to ARCHITECT-MEMO.md (required — the memo WINS over unit briefs on any conflict, per the template authority field)',
  repo: 'absolute path of the checkout (required)',
  branch: 'integration branch name (required)',
  sessionTrailer:
    'optional extra commit-trailer line(s); Co-Authored-By is always added',
  pushPolicy:
    "optional: 'push' (default) or 'hold' — hold means every step commits but nothing is pushed, leaving the integrated branch for human inspection",
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
  LOAD: {
    type: 'object',
    required: ['units', 'integration_protocol', 'memo_digest'],
    properties: {
      units: {
        type: 'array',
        items: {
          type: 'object',
          required: ['unit_id', 'wave', 'title', 'files'],
          properties: {
            unit_id: { type: 'string' },
            wave: { type: 'string' },
            title: { type: 'string' },
            files: { type: 'array', items: { type: 'string' } },
          },
        },
      },
      integration_protocol: { type: 'string' },
      wave_order: { type: 'array', items: { type: 'string' } },
      memo_digest: { type: 'string' },
      errors: { type: 'array', items: { type: 'string' } },
    },
  },
  UNIT_REPORT: {
    type: 'object',
    required: ['unit_id', 'branch', 'files_changed', 'tests', 'open_issues'],
    properties: {
      unit_id: { type: 'string' },
      branch: { type: 'string' },
      files_changed: { type: 'array', items: { type: 'string' } },
      tests: { type: 'string' },
      open_issues: { type: 'array', items: { type: 'string' } },
    },
  },
  INTEGRATE_REPORT: {
    type: 'object',
    required: ['ok', 'merged', 'gauntlet', 'pushed', 'problems'],
    properties: {
      ok: { type: 'boolean' },
      merged: { type: 'array', items: { type: 'string' } },
      gauntlet: { type: 'string' },
      pushed: { type: 'boolean' },
      problems: { type: 'array', items: { type: 'string' } },
    },
  },
  FINDINGS: {
    type: 'object',
    required: ['findings'],
    properties: {
      findings: {
        type: 'array',
        items: {
          type: 'object',
          required: ['file', 'summary', 'severity'],
          properties: {
            file: { type: 'string' },
            summary: { type: 'string' },
            severity: { type: 'string' },
          },
        },
      },
    },
  },
}

// Step graph.
//   kind:      'agent' (LLM judgment) | 'script' (rule-fixed command; the
//              deterministic work an LLM must relay, never re-derive — the
//              determinism-boundary principle applied to orchestration)
//   run:       'once' | 'fan-out' | 'conditional'
//   needs:     DAG edges, as step ids; an '@qualifier' suffix scopes the edge
//              ('@previous-wave', '@all-in-wave', '@final') without breaking
//              id resolution
//   isolation: 'shared-checkout' | 'fresh-worktree' (required wherever
//              fan-out mutates files)
//   prompt:    PROMPTS key (agent steps) — validated present
//   command:   COMMANDS key (script steps) — validated present
//   output:    SCHEMAS key, or null for free text
//   effort:    optional reasoning-effort override ('low' for mechanical
//              relay steps, 'high' for adversarial review)
//   abort_on_failure: a failed/dead instance aborts the whole run (vs the
//              default: log, skip, continue)
//   retry:     re-dispatches for a dead instance before giving up on it
const STEPS = [
  {
    id: 'check-disjointness',
    phase: 'Load',
    kind: 'script',
    run: 'once',
    needs: [],
    isolation: 'shared-checkout',
    command: 'checkDisjointness',
    output: 'SCRIPT_RESULT',
    effort: 'low',
    abort_on_failure: true,
  },
  {
    id: 'load-specs',
    phase: 'Load',
    kind: 'agent',
    run: 'once',
    needs: [],
    isolation: 'shared-checkout',
    prompt: 'loadSpecs',
    output: 'LOAD',
    abort_on_failure: true,
  },
  {
    id: 'build-unit',
    phase: 'Build',
    kind: 'agent',
    run: 'fan-out', // one instance per unit, within a wave; waves are sequential
    needs: ['check-disjointness', 'load-specs', 'integrate-wave@previous-wave'],
    isolation: 'fresh-worktree',
    prompt: 'buildUnit',
    output: 'UNIT_REPORT',
    retry: 1,
  },
  {
    id: 'integrate-wave',
    phase: 'Integrate',
    kind: 'agent',
    run: 'once', // per wave, a barrier after that wave's build-unit fan-out
    needs: ['build-unit@all-in-wave'],
    isolation: 'shared-checkout',
    prompt: 'integrateWave',
    output: 'INTEGRATE_REPORT',
    abort_on_failure: true, // a red gauntlet must not become the next wave's base
  },
  {
    id: 'review-correctness',
    phase: 'Review',
    kind: 'agent',
    run: 'once',
    needs: ['integrate-wave@final'],
    isolation: 'shared-checkout',
    prompt: 'reviewCorrectness',
    output: 'FINDINGS',
    effort: 'high',
  },
  {
    id: 'review-house-rules',
    phase: 'Review',
    kind: 'agent',
    run: 'once',
    needs: ['integrate-wave@final'],
    isolation: 'shared-checkout',
    prompt: 'reviewHouseRules',
    output: 'FINDINGS',
    effort: 'high',
  },
  {
    id: 'fix-findings',
    phase: 'Review',
    kind: 'agent',
    run: 'conditional', // only if the review lenses returned findings
    needs: ['review-correctness', 'review-house-rules'],
    isolation: 'shared-checkout',
    prompt: 'fixFindings',
    output: null, // plain-text report
  },
]
const STEP = (id) => STEPS.find((s) => s.id === id)

// Rule-fixed commands for kind: 'script' steps. The disjointness gate is the
// mechanized checker (same-wave overlap, claim typos, dirty-worktree
// intersection — fire paths in tests/scripts/test_check_handoff_disjointness.py);
// the loader must NOT re-derive those checks by eyeball.
const COMMANDS = {
  checkDisjointness: ({ specsPath, repo }) =>
    `cd ${repo} && python scripts/check_handoff_disjointness.py ${specsPath} --against-worktree`,
}

const PROMPTS = {
  scriptStep: ({ repo, command }) =>
    `Run EXACTLY this command in ${repo} and nothing else:
${command}
Return per the schema: exit_code (the command's real exit code), output
(stdout+stderr verbatim; if enormous, keep the first and last 100 lines).
Do not fix, retry, re-run, or interpret a failure — relaying it is the job.`,

  loadSpecs: ({ specsPath, memoPath }) =>
    `Read ${specsPath} (a unit-specs.json per docs/plans/_TEMPLATE-handoff/unit-specs.template.json)
and ${memoPath}. File-claim safety (overlaps, typos, dirty worktree) is
checked by a separate mechanized gate — do NOT re-derive it. Return per the
schema:
- units: every unit's unit_id, wave (stringified), title, files (its
  EXCLUSIVE claim, verbatim from the specs).
- wave_order: waves in dispatch order (the specs' order of first appearance
  unless the file states an explicit order).
- integration_protocol: the specs' integration_protocol string verbatim.
- memo_digest: the memo's binding decisions compressed to what a build agent
  must not violate (keep path::symbol cites; the memo WINS over unit briefs).
- errors: units missing required spec fields (unit_id, wave, files, brief).
  Empty if clean.`,

  buildUnit: ({ repo, branch, unit, specsRel, memoDigest, trailer }) =>
    `You are one build unit of a swarm over ${repo} (you are in an ISOLATED
WORKTREE of it). Integration branch: ${branch}.

UNIT ${unit.unit_id} — ${unit.title}
YOUR EXCLUSIVE FILE CLAIM (never touch anything outside it):
${unit.files.join('\n')}

YOUR FULL UNIT SPEC lives in YOUR worktree at ${specsRel} — read your own
entry (unit_id ${unit.unit_id}) from that file FIRST and treat it as the
brief: design_constraints, acceptance_tests, test_batteries_to_run,
forbidden_files all bind you. Reading it from disk (not from this prompt)
is deliberate: the spec text must reach you byte-exact.

ARCHITECT MEMO (BINDING — wins over the spec on any conflict):
${memoDigest}

HOUSE RULES: comments state constraints, not narration; every new guard gets a
test that demonstrably fires; do NOT run regen scripts (the integrator owns
regen); run ONLY your own test files, with PYTHONPATH pointing at YOUR
worktree's src/ so you exercise your edits.

DELIVERY (mandatory): git checkout -b pkg/${unit.unit_id}; add ONLY your claimed
files; ONE commit ("feat(...)/fix(...)/docs(...): <summary>") ending with
exactly:
${trailer}
Do NOT push. Return per the schema: unit_id, branch, files_changed, tests
(what you ran + results), open_issues (anything the integrator must know).`,

  integrateWave: ({ repo, branch, wave, reports, protocol, trailer, pushLine }) =>
    `You are the integrator for wave ${wave} of a swarm on ${repo}, integration
branch ${branch}. Unit branches to merge IN THIS ORDER:
${reports.map((r) => `${r.branch} (${r.unit_id}: ${r.files_changed.length} files; open issues: ${r.open_issues.join('; ') || 'none'})`).join('\n')}

INTEGRATION PROTOCOL (from unit-specs.json, verbatim — follow it):
${protocol}
${pushLine}
Commits you author end with exactly:
${trailer}
Return per the schema: ok (true ONLY if every branch merged and the full
gauntlet — regen, lint, type, targeted tests — is green; a red gauntlet or an
unresolved merge is ok=false, and the run will abort on it rather than build
the next wave on a broken base), merged (branches merged), gauntlet (what you
ran + results), pushed, problems (conflicts you resolved, units you had to
amend or skip, anything needing human eyes).`,

  reviewCorrectness: ({ repo, branch }) =>
    `Review the swarm's integrated diff on ${repo} branch ${branch} (diff against its merge-base with main) for CORRECTNESS: real bugs, guards that cannot fire, broken contracts. Adversarial posture — report only findings you verified against the code, with file + one-sentence summary + severity.`,

  reviewHouseRules: ({ repo, branch }) =>
    `Review the swarm's integrated diff on ${repo} branch ${branch} (diff against its merge-base with main) for HOUSE-RULES drift: narration comments, untested new branches, duplicated mechanism, regen artifacts committed by units, enforcement stated as prose instead of lint/test. Report file + one-sentence summary + severity.`,

  fixFindings: ({ repo, branch, findings, trailer, pushLine }) =>
    `On ${repo} branch ${branch}, address these review findings (fix what is
real; for any you reject, say why): ${JSON.stringify(findings)}.
Run the affected tests. Commit ("fix(review): ...") ending with exactly:
${trailer}
${pushLine}
Return a plain-text report of fixes, rejections (with reasons), and final
test state.`,
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
  // cycle check over base-id edges; '@previous-wave' edges point at the PRIOR
  // fan-out instance, so they cross the wave axis rather than closing a cycle
  const state = {}
  const visit = (id) => {
    if (state[id] === 1) { problems.push(`cycle through '${id}'`); return }
    if (state[id] === 2) return
    state[id] = 1
    for (const edge of STEP(id).needs) {
      const [target, qualifier] = edge.split('@')
      if (qualifier === 'previous-wave') continue
      if (ids.has(target)) visit(target)
    }
    state[id] = 2
  }
  for (const s of STEPS) visit(s.id)
  if (problems.length) throw new Error(`swarm-units plan invalid:\n${problems.join('\n')}`)
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

// retry-once semantics for fan-out instances whose agent died (returned null)
const runWithRetry = async (id, promptInputs, opts) => {
  const attempts = 1 + (STEP(id).retry || 0)
  for (let i = 0; i < attempts; i++) {
    const r = await runStep(id, promptInputs, i ? { ...opts, label: `${opts.label}:retry` } : opts)
    if (r !== null && r !== undefined) return r
  }
  return null
}

validatePlan()
if (!args || !args.specsPath || !args.memoPath || !args.repo || !args.branch) {
  throw new Error(
    `swarm-units needs args {specsPath, memoPath, repo, branch, sessionTrailer?, pushPolicy?}: ${JSON.stringify(ARGS_CONTRACT)}`
  )
}
const { specsPath, memoPath, repo, branch } = args
const trailer =
  'Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>' +
  (args.sessionTrailer ? '\n' + args.sessionTrailer : '')
const hold = args.pushPolicy === 'hold'
const pushLine = hold
  ? 'PUSH POLICY: HOLD — commit everything, push NOTHING; a human inspects the branch before it leaves this machine.'
  : 'Push the branch when green (git push -u, retry with backoff on network failure).'
const specsRel = specsPath.startsWith(repo + '/') ? specsPath.slice(repo.length + 1) : specsPath

// Workflow scripts have no filesystem access; the Load steps are the bridge.
// The mechanized disjointness gate and the LLM spec/memo load are independent,
// so they run in parallel; both are abort_on_failure — nothing builds on a
// failed gate or an unloadable package.
phase('Load')
const [gate, load] = await parallel([
  () => runStep('check-disjointness', { specsPath, repo }, { label: 'gate:disjointness' }),
  () => runStep('load-specs', { specsPath, memoPath }, { label: 'load-specs' }),
])
if (!gate || gate.exit_code !== 0) {
  return { aborted: 'disjointness gate failed', gate_output: gate ? gate.output : '(gate agent died)' }
}
if (!load || (load.errors && load.errors.length)) {
  return { aborted: 'unit-specs load failed', errors: load ? load.errors : ['(loader agent died)'] }
}
const waves = (load.wave_order && load.wave_order.length
  ? load.wave_order
  : [...new Set(load.units.map((u) => u.wave))]
).filter((w) => load.units.some((u) => u.wave === w))
log(`${load.units.length} units across ${waves.length} wave(s); gate clean`)

// The wave boundary is a REAL barrier (later waves may depend on earlier ones
// landing), so parallel() per wave + a sequential integrator is the correct
// shape here, not pipeline().
const buildReports = []
const integrateReports = []
for (const wave of waves) {
  const waveUnits = load.units.filter((u) => u.wave === wave)
  phase('Build')
  log(`wave ${wave}: building ${waveUnits.length} unit(s)`)
  const reports = (
    await parallel(
      waveUnits.map((u) => () =>
        runWithRetry(
          'build-unit',
          { repo, branch, unit: u, specsRel, memoDigest: load.memo_digest, trailer },
          { label: `build:${u.unit_id}` }
        )
      )
    )
  ).filter(Boolean)
  buildReports.push(...reports)
  if (reports.length < waveUnits.length) {
    log(
      `wave ${wave}: ${waveUnits.length - reports.length} unit(s) dead after retry — integrator told to skip their branches`
    )
  }

  phase('Integrate')
  const integ = await runStep(
    'integrate-wave',
    { repo, branch, wave, reports, protocol: load.integration_protocol, trailer, pushLine },
    { label: `integrate:wave-${wave}` }
  )
  integrateReports.push({ wave, ...(integ || { ok: false, merged: [], gauntlet: '(integrator died)', pushed: false, problems: ['integrator agent died'] }) })
  if (!integ || !integ.ok) {
    log(`wave ${wave} integration FAILED — aborting before the next wave builds on a broken base`)
    return {
      aborted: `wave ${wave} integration failed`,
      units_built: buildReports,
      waves_integrated: integrateReports,
    }
  }
  if (integ.problems.length) log(`wave ${wave} integration problems: ${integ.problems.join(' | ')}`)
}

phase('Review')
const lenses = await parallel([
  () => runStep('review-correctness', { repo, branch }, { label: 'review:correctness' }),
  () => runStep('review-house-rules', { repo, branch }, { label: 'review:house-rules' }),
])
const findings = lenses.filter(Boolean).flatMap((l) => l.findings)
let fixed = null
if (findings.length) {
  fixed = await runStep(
    'fix-findings',
    { repo, branch, findings, trailer, pushLine },
    { label: 'review:fixer' }
  )
} else {
  log('review: no findings survived')
}

return {
  units_built: buildReports,
  waves_integrated: integrateReports,
  review_findings: findings,
  fixer_report: fixed,
  push_policy: hold ? 'hold (nothing pushed)' : 'push',
}
