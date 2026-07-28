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

// ---- args --------------------------------------------------------------------
// specsPath      absolute path to the package's unit-specs.json (required)
// memoPath       absolute path to ARCHITECT-MEMO.md (required — the memo WINS
//                over unit briefs on any conflict, per the template's authority
//                field)
// repo           absolute path of the checkout (required)
// branch         integration branch name (required)
// sessionTrailer optional extra commit-trailer line(s) (e.g. the Claude-Session
//                URL of the invoking session); Co-Authored-By is always added
if (!args || !args.specsPath || !args.memoPath || !args.repo || !args.branch) {
  throw new Error(
    'swarm-units needs args {specsPath, memoPath, repo, branch, sessionTrailer?}'
  )
}
const { specsPath, memoPath, repo, branch } = args
const trailer =
  'Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>' +
  (args.sessionTrailer ? '\n' + args.sessionTrailer : '')

// ---- Load --------------------------------------------------------------------
// Workflow scripts have no filesystem access; the loader agent is the bridge.
// It also re-runs the template's wave0_rule check (dirty files are claimed by
// in-flight work) so dispatch never races an uncommitted change.
phase('Load')
const LOAD_SCHEMA = {
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
}
const load = await agent(
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
  { label: 'load-specs', phase: 'Load', schema: LOAD_SCHEMA }
)
if (load.errors && load.errors.length) {
  return { aborted: 'unit-specs validation failed', errors: load.errors }
}
const waves = (load.wave_order && load.wave_order.length
  ? load.wave_order
  : [...new Set(load.units.map((u) => u.wave))]
).filter((w) => load.units.some((u) => u.wave === w))
log(`${load.units.length} units across ${waves.length} wave(s)`)

// ---- Build + Integrate, wave by wave ----------------------------------------
// The wave boundary is a REAL barrier (later waves may depend on earlier ones
// landing), so parallel() per wave + a sequential integrator is the correct
// shape here, not pipeline().
const UNIT_REPORT = {
  type: 'object',
  required: ['unit_id', 'branch', 'files_changed', 'tests', 'open_issues'],
  properties: {
    unit_id: { type: 'string' },
    branch: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    tests: { type: 'string' },
    open_issues: { type: 'array', items: { type: 'string' } },
  },
}
const INTEGRATE_REPORT = {
  type: 'object',
  required: ['merged', 'gauntlet', 'pushed', 'problems'],
  properties: {
    merged: { type: 'array', items: { type: 'string' } },
    gauntlet: { type: 'string' },
    pushed: { type: 'boolean' },
    problems: { type: 'array', items: { type: 'string' } },
  },
}
const buildReports = []
const integrateReports = []
for (const wave of waves) {
  const waveUnits = load.units.filter((u) => u.wave === wave)
  phase('Build')
  log(`wave ${wave}: building ${waveUnits.length} unit(s)`)
  const reports = (
    await parallel(
      waveUnits.map((u) => () =>
        agent(
          `You are one build unit of a swarm over ${repo} (you are in an ISOLATED
WORKTREE of it). Integration branch: ${branch}.

UNIT ${u.unit_id} — ${u.title}
YOUR EXCLUSIVE FILE CLAIM (never touch anything outside it):
${u.files.join('\n')}

UNIT SPEC (verbatim):
${u.brief}
${u.tests ? 'TEST OBLIGATIONS: ' + u.tests : ''}

ARCHITECT MEMO (BINDING — wins over the spec above on any conflict):
${load.memo_digest}

HOUSE RULES: comments state constraints, not narration; every new guard gets a
test that demonstrably fires; do NOT run regen scripts (the integrator owns
regen); run ONLY your own test files, with PYTHONPATH pointing at YOUR
worktree's src/ so you exercise your edits.

DELIVERY (mandatory): git checkout -b pkg/${u.unit_id}; add ONLY your claimed
files; ONE commit ("feat(...)/fix(...)/docs(...): <summary>") ending with
exactly:
${trailer}
Do NOT push. Return per the schema: unit_id, branch, files_changed, tests
(what you ran + results), open_issues (anything the integrator must know).`,
          {
            label: `build:${u.unit_id}`,
            phase: 'Build',
            schema: UNIT_REPORT,
            isolation: 'worktree',
          }
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
  const integ = await agent(
    `You are the integrator for wave ${wave} of a swarm on ${repo}, integration
branch ${branch}. Unit branches to merge IN THIS ORDER:
${reports.map((r) => `${r.branch} (${r.unit_id}: ${r.files_changed.length} files; open issues: ${r.open_issues.join('; ') || 'none'})`).join('\n')}

INTEGRATION PROTOCOL (from unit-specs.json, verbatim — follow it):
${load.integration_protocol}

Commits you author end with exactly:
${trailer}
Return per the schema: merged (branches merged), gauntlet (regen/lint/type/test
results — name what you ran), pushed, problems (conflicts you resolved, units
you had to amend or skip, anything needing human eyes).`,
    { label: `integrate:wave-${wave}`, phase: 'Integrate', schema: INTEGRATE_REPORT }
  )
  integrateReports.push({ wave, ...integ })
  if (integ.problems.length) log(`wave ${wave} integration problems: ${integ.problems.join(' | ')}`)
}

// ---- Review ------------------------------------------------------------------
phase('Review')
const FINDINGS = {
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
}
const lenses = await parallel([
  () =>
    agent(
      `Review the swarm's integrated diff on ${repo} branch ${branch} (diff against its merge-base with main) for CORRECTNESS: real bugs, guards that cannot fire, broken contracts. Adversarial posture — report only findings you verified against the code, with file + one-sentence summary + severity.`,
      { label: 'review:correctness', phase: 'Review', schema: FINDINGS }
    ),
  () =>
    agent(
      `Review the swarm's integrated diff on ${repo} branch ${branch} (diff against its merge-base with main) for HOUSE-RULES drift: narration comments, untested new branches, duplicated mechanism, regen artifacts committed by units, enforcement stated as prose instead of lint/test. Report file + one-sentence summary + severity.`,
      { label: 'review:house-rules', phase: 'Review', schema: FINDINGS }
    ),
])
const findings = lenses.filter(Boolean).flatMap((l) => l.findings)
let fixed = null
if (findings.length) {
  fixed = await agent(
    `On ${repo} branch ${branch}, address these review findings (fix what is
real; for any you reject, say why): ${JSON.stringify(findings)}.
Run the affected tests. Commit ("fix(review): ...") ending with exactly:
${trailer}
Push the branch when green. Return a plain-text report of fixes, rejections
(with reasons), and final test state.`,
    { label: 'review:fixer', phase: 'Review' }
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
