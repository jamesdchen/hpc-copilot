export const meta = {
  name: 'swarm-units',
  description:
    'Mechanized handoff-package swarm: build file-disjoint units from a unit-specs.json in parallel worktrees, integrate per wave (one regen + lint gauntlet), then review lenses + fixer',
  whenToUse:
    'When a handoff package (docs/plans/<pkg>/ with ARCHITECT-MEMO.md + unit-specs.json, per docs/plans/_TEMPLATE-handoff/) is greenlit for implementation. Pass args: {specsPath, memoPath, repo, branch, sessionTrailer?}. This is the mechanized form of the swarm-dispatch protocol the plans previously described in prose (e.g. docs/plans/handoff-packages-2026-07-12/mcp-latency-docs-packages.workflow.js, the one-off ancestor of this script).',
  phases: [
    { title: 'Load', detail: 'read + validate unit-specs.json and the architect memo' },
    { title: 'Build', detail: 'one worktree agent per unit, waves in order, file-disjoint within a wave' },
    { title: 'Integrate', detail: 'per wave: merge pkg/* in order, regen ONCE, lint gauntlet, targeted tests' },
    { title: 'Review', detail: 'correctness + house-rules lenses, then a fixer for confirmed findings' },
  ],
}

// ============================================================================
// PORTABLE PLAN
// ----------------------------------------------------------------------------
// Everything down to RUNTIME ADAPTER is engine-neutral: the argument contract,
// the step graph (STEPS), the structured-output schemas (SCHEMAS, plain JSON
// Schema), and the prompt templates (PROMPTS, pure string-building functions
// with named inputs). Porting this workflow to another orchestrator — e.g. a
// declarative step/DAG format like awman's workflow.toml — means translating
// THIS section; only the adapter below speaks the Claude Workflow API.
// The mapping contract lives in .claude/workflows/README.md.
//
// STEPS is consumed by the adapter (isolation, output schema, phase), so the
// operational fields cannot drift from what actually runs; `needs` records the
// DAG edges the adapter's control flow implements.

const ARGS_CONTRACT = {
  specsPath: 'absolute path to the package unit-specs.json (required)',
  memoPath:
    'absolute path to ARCHITECT-MEMO.md (required — the memo WINS over unit briefs on any conflict, per the template authority field)',
  repo: 'absolute path of the checkout (required)',
  branch: 'integration branch name (required)',
  sessionTrailer:
    'optional extra commit-trailer line(s); Co-Authored-By is always added',
}

const SCHEMAS = {
  LOAD: {
    type: 'object',
    required: ['units', 'integration_protocol', 'memo_digest', 'dirty_files'],
    properties: {
      units: {
        type: 'array',
        items: {
          type: 'object',
          required: ['unit_id', 'wave', 'title', 'files', 'brief'],
          properties: {
            unit_id: { type: 'string' },
            wave: { type: 'string' },
            title: { type: 'string' },
            files: { type: 'array', items: { type: 'string' } },
            brief: { type: 'string' },
            tests: { type: 'string' },
          },
        },
      },
      integration_protocol: { type: 'string' },
      wave_order: { type: 'array', items: { type: 'string' } },
      memo_digest: { type: 'string' },
      dirty_files: { type: 'array', items: { type: 'string' } },
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
    required: ['merged', 'gauntlet', 'pushed', 'problems'],
    properties: {
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

// Step graph. run: 'once' | 'fan-out' | 'conditional'; isolation:
// 'shared-checkout' (agent works in the real repo) | 'fresh-worktree'
// (agent gets an isolated copy; required wherever fan-out mutates files).
const STEPS = [
  {
    id: 'load-specs',
    phase: 'Load',
    run: 'once',
    needs: [],
    isolation: 'shared-checkout',
    output: 'LOAD',
  },
  {
    id: 'build-unit',
    phase: 'Build',
    run: 'fan-out', // one instance per unit, within a wave; waves are sequential
    needs: ['load-specs', 'integrate-wave (previous wave, if any)'],
    isolation: 'fresh-worktree',
    output: 'UNIT_REPORT',
  },
  {
    id: 'integrate-wave',
    phase: 'Integrate',
    run: 'once', // per wave, a barrier after that wave's build-unit fan-out
    needs: ['build-unit (all in wave)'],
    isolation: 'shared-checkout',
    output: 'INTEGRATE_REPORT',
  },
  {
    id: 'review-correctness',
    phase: 'Review',
    run: 'once',
    needs: ['integrate-wave (final)'],
    isolation: 'shared-checkout',
    output: 'FINDINGS',
  },
  {
    id: 'review-house-rules',
    phase: 'Review',
    run: 'once',
    needs: ['integrate-wave (final)'],
    isolation: 'shared-checkout',
    output: 'FINDINGS',
  },
  {
    id: 'fix-findings',
    phase: 'Review',
    run: 'conditional', // only if the review lenses returned findings
    needs: ['review-correctness', 'review-house-rules'],
    isolation: 'shared-checkout',
    output: null, // plain-text report
  },
]
const STEP = (id) => STEPS.find((s) => s.id === id)

const PROMPTS = {
  loadSpecs: ({ specsPath, memoPath, repo }) =>
    `Read ${specsPath} (a unit-specs.json per docs/plans/_TEMPLATE-handoff/unit-specs.template.json)
and ${memoPath}. Also run \`git -C ${repo} status --porcelain\` for the wave0_rule.
Return per the schema:
- units: every unit with unit_id, wave (stringified), title, files (its EXCLUSIVE
  claim), brief (the unit's full spec text verbatim — do not summarize away
  constraints), tests (its test obligations if stated).
- wave_order: waves in dispatch order (the specs' order of first appearance
  unless the file states an explicit order).
- integration_protocol: the specs' integration_protocol string verbatim.
- memo_digest: the memo's binding decisions compressed to what a build agent
  must not violate (keep path::symbol cites; the memo WINS over unit briefs).
- dirty_files: files reported dirty by git status.
- errors: schema violations you found — same-wave units sharing a file entry,
  units claiming a dirty file, units missing required fields. Empty if clean.`,

  buildUnit: ({ repo, branch, unit, memoDigest, trailer }) =>
    `You are one build unit of a swarm over ${repo} (you are in an ISOLATED
WORKTREE of it). Integration branch: ${branch}.

UNIT ${unit.unit_id} — ${unit.title}
YOUR EXCLUSIVE FILE CLAIM (never touch anything outside it):
${unit.files.join('\n')}

UNIT SPEC (verbatim):
${unit.brief}
${unit.tests ? 'TEST OBLIGATIONS: ' + unit.tests : ''}

ARCHITECT MEMO (BINDING — wins over the spec above on any conflict):
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

  integrateWave: ({ repo, branch, wave, reports, protocol, trailer }) =>
    `You are the integrator for wave ${wave} of a swarm on ${repo}, integration
branch ${branch}. Unit branches to merge IN THIS ORDER:
${reports.map((r) => `${r.branch} (${r.unit_id}: ${r.files_changed.length} files; open issues: ${r.open_issues.join('; ') || 'none'})`).join('\n')}

INTEGRATION PROTOCOL (from unit-specs.json, verbatim — follow it):
${protocol}

Commits you author end with exactly:
${trailer}
Return per the schema: merged (branches merged), gauntlet (regen/lint/type/test
results — name what you ran), pushed, problems (conflicts you resolved, units
you had to amend or skip, anything needing human eyes).`,

  reviewCorrectness: ({ repo, branch }) =>
    `Review the swarm's integrated diff on ${repo} branch ${branch} (diff against its merge-base with main) for CORRECTNESS: real bugs, guards that cannot fire, broken contracts. Adversarial posture — report only findings you verified against the code, with file + one-sentence summary + severity.`,

  reviewHouseRules: ({ repo, branch }) =>
    `Review the swarm's integrated diff on ${repo} branch ${branch} (diff against its merge-base with main) for HOUSE-RULES drift: narration comments, untested new branches, duplicated mechanism, regen artifacts committed by units, enforcement stated as prose instead of lint/test. Report file + one-sentence summary + severity.`,

  fixFindings: ({ repo, branch, findings, trailer }) =>
    `On ${repo} branch ${branch}, address these review findings (fix what is
real; for any you reject, say why): ${JSON.stringify(findings)}.
Run the affected tests. Commit ("fix(review): ...") ending with exactly:
${trailer}
Push the branch when green. Return a plain-text report of fixes, rejections
(with reasons), and final test state.`,
}

// ============================================================================
// RUNTIME ADAPTER (Claude Workflow API)
// ----------------------------------------------------------------------------
// The only section that calls agent()/parallel()/phase()/log(). It walks the
// plan above: step order + barriers implement STEPS[].needs, and each agent()
// call takes its isolation and output schema from its STEPS entry.

const runStep = (id, prompt, opts = {}) => {
  const step = STEP(id)
  return agent(prompt, {
    phase: step.phase,
    ...(step.output ? { schema: SCHEMAS[step.output] } : {}),
    ...(step.isolation === 'fresh-worktree' ? { isolation: 'worktree' } : {}),
    ...opts,
  })
}

if (!args || !args.specsPath || !args.memoPath || !args.repo || !args.branch) {
  throw new Error(
    `swarm-units needs args {specsPath, memoPath, repo, branch, sessionTrailer?}: ${JSON.stringify(ARGS_CONTRACT)}`
  )
}
const { specsPath, memoPath, repo, branch } = args
const trailer =
  'Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>' +
  (args.sessionTrailer ? '\n' + args.sessionTrailer : '')

// Workflow scripts have no filesystem access; the loader agent is the bridge.
// It also re-runs the template's wave0_rule check (dirty files are claimed by
// in-flight work) so dispatch never races an uncommitted change.
phase('Load')
const load = await runStep(
  'load-specs',
  PROMPTS.loadSpecs({ specsPath, memoPath, repo }),
  { label: 'load-specs' }
)
if (load.errors && load.errors.length) {
  return { aborted: 'unit-specs validation failed', errors: load.errors }
}
const waves = (load.wave_order && load.wave_order.length
  ? load.wave_order
  : [...new Set(load.units.map((u) => u.wave))]
).filter((w) => load.units.some((u) => u.wave === w))
log(`${load.units.length} units across ${waves.length} wave(s)`)

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
        runStep(
          'build-unit',
          PROMPTS.buildUnit({
            repo,
            branch,
            unit: u,
            memoDigest: load.memo_digest,
            trailer,
          }),
          { label: `build:${u.unit_id}` }
        )
      )
    )
  ).filter(Boolean)
  buildReports.push(...reports)
  if (reports.length < waveUnits.length) {
    log(
      `wave ${wave}: ${waveUnits.length - reports.length} unit(s) returned nothing — integrator told to skip their branches`
    )
  }

  phase('Integrate')
  const integ = await runStep(
    'integrate-wave',
    PROMPTS.integrateWave({
      repo,
      branch,
      wave,
      reports,
      protocol: load.integration_protocol,
      trailer,
    }),
    { label: `integrate:wave-${wave}` }
  )
  integrateReports.push({ wave, ...integ })
  if (integ.problems.length) log(`wave ${wave} integration problems: ${integ.problems.join(' | ')}`)
}

phase('Review')
const lenses = await parallel([
  () =>
    runStep('review-correctness', PROMPTS.reviewCorrectness({ repo, branch }), {
      label: 'review:correctness',
    }),
  () =>
    runStep('review-house-rules', PROMPTS.reviewHouseRules({ repo, branch }), {
      label: 'review:house-rules',
    }),
])
const findings = lenses.filter(Boolean).flatMap((l) => l.findings)
let fixed = null
if (findings.length) {
  fixed = await runStep(
    'fix-findings',
    PROMPTS.fixFindings({ repo, branch, findings, trailer }),
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
}
