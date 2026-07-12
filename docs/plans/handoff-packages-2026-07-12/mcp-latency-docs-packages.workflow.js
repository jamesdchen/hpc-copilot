export const meta = {
  name: 'mcp-latency-docs-packages',
  description: 'Land the three greenlit packages: MCP completion, latency (engine-on/snapshot-cache/fast-path), doc-honesty pins — built in isolated worktrees, integrated after the async-refill series lands',
  phases: [
    { title: 'Build', detail: '8 file-disjoint units in isolated worktrees', model: 'opus' },
    { title: 'Integrate', detail: 'assemble pkg/* branches after async-refill lands; regen, test, push', model: 'opus' },
    { title: 'Review', detail: '2 lenses + fixer', model: 'opus' },
  ],
}

const REPO = '/home/user/hpc-copilot'
const BRANCH = 'claude/tool-latency-reduction-iqpgcr'

const CTX = `
REPO: a git worktree copy of ${REPO} (package hpc-agent, installed editable from the main checkout — run tests with PYTHONPATH pointing at YOUR worktree's src/ (e.g. PYTHONPATH=$PWD/src python -m pytest ...) so you exercise your edits, not the installed copy).
HOUSE RULES: dense module docstrings carrying design rationale with path::symbol cites; comments state constraints, not narration; guards must demonstrably fire (every new branch gets a test that exercises it); caches are "an optimisation, never a correctness gate" (fail-open, env bypass); enforcement is affordances/lints, never prose; do NOT run the regen scripts (integrator owns regen); do NOT edit files outside your declared ownership; run ONLY your own test files.
DELIVERY PROTOCOL (mandatory): you are in an isolated worktree. When done: git checkout -b pkg/<your-unit-key>; git add <your files>; ONE commit, message style "feat(...)/fix(...)/docs(...): <summary>", ending with exactly these trailer lines:
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01So7J6oM5V5gtDvifrm9JCT
Then report: branch name, files changed, test results, open issues for the integrator. Do NOT push.
BACKGROUND FACT: a separate async-refill build is landing on ${BRANCH} concurrently (new verb campaign-refill, watching_refill stage, campaign docs edits). Avoid its files: ops/campaign_refill.py, meta/campaign/*, infra/block_chain.py, docs/design/campaign-async-refill.md, docs/internals/campaign-lifecycle.md, CHANGELOG.md (integrator will handle changelog), the rulings ledger, hpc-campaign SKILL.
`

// Design phase pre-resolved: the architect memo below was authored by the
// session owner's Fable architect (2026-07-12) against the live tree.
// Where a unit brief conflicts with the memo, THE MEMO WINS.
const memo = `
# Architect memo — packages swarm (authored by Fable, 2026-07-12)

Settles the Design-phase questions against the tree so the build units are
pure execution. Cites are \`path::symbol\` verified today. Where this memo
contradicts a unit's original brief, THE MEMO WINS.

## 1. m-server — relay RECONCEIVED as envelope parity (do not build injection)

The two autofetch hooks are **Bash-transport compensations**, per their own
docstrings (\`_kernel/hooks/skill_return_autofetch.py\`,
\`decision_rendezvous_autofetch.py\`): pure, additive, fail-open observers that
re-inject content the agent already received on stdout but may not render.
Over MCP the envelope IS the structured tool result — nothing is lost in
transport. Therefore:

- Do NOT add server-side injection to \`mcp_server.py\`. That would duplicate
  mechanism (one-definition rule) to solve a transport problem MCP doesn't have.
- DO add parity tests (\`tests/test_mcp_server_envelope_parity.py\`): a parked
  \`block-drive\` \`tools/call\` result must contain every field
  \`decision_rendezvous_autofetch.build_hook_output\` would inject (the \`brief\`,
  \`next_block\` hint, awaiting marker). If a field is missing, enrich the
  **block-drive Result model**, never the server.
- DO document in \`docs/reference/mcp.md\`: autofetch hooks are unnecessary over
  MCP by construction; the **Stop-guard enforcement half has no MCP
  equivalent** — it maps to harness capability 2 (relay enforcement) in
  \`docs/internals/harness-contract.md\`; name the degradation honestly.

Curated additions (all as \`_CURATED_EXTRA_VERBS\` entries with house-style cited
rationale — the run-#8 unreachable-verb-gets-hand-rolled class):
\`read-decisions\`, \`verify-relay\` (named MCP-direct at hpc-submit/SKILL.md and
hpc-status/SKILL.md), \`attention-queue\` (hpc-status), \`revise-resolved\`
(**verified: \`ReviseResolvedResult\` declares NO \`next_block\`**
(\`_wire/workflows/revise_resolved.py::ReviseResolvedResult\`), so it does NOT
derive — despite hpc-submit instructing "MCP-direct"), and \`poll-detached\`
(built by m-poll; guard the pin test on registry presence).
Lint \`scripts/lint_skill_mcp_reachability.py\` as briefed, fire-path test
against a synthetic violating SKILL.

## 2. m-poll — poll-detached spec

Home \`ops/monitor/poll_detached.py\`; wire model \`_wire/queries/poll_detached.py\`.
Spec: \`run_id\` (required), \`block\` (verb string, e.g. "campaign-run"),
\`experiment_dir\`. Result: \`lease_present\`, \`pid\`, \`pid_alive\`,
\`journal_status\`, \`terminal_recorded\`, derived
\`state ∈ {running, exited_recorded, exited_unrecorded, no_lease}\`,
\`watch: "journal"\`. Sources — reuse, never reimplement:
\`_kernel/lifecycle/detached.py\` lease path/read helpers (lease JSON carries
\`pid\`; file \`<verb>-<run_id>.lease.json\` under the global \`_detached/\` home),
\`infra/proc.pid_alive\`, \`state/journal_poll.read_run_status\`,
\`state/block_terminal.read_terminal_with_fallback\`. Zero SSH; \`verb="query"\`,
\`idempotent=True\`, \`side_effects=[]\`. NOT detach-required over MCP.

## 3. l-engine — engine-on under mcp-serve

Verified: \`asyncssh>=2.23\` is a **core dependency** (pyproject, also mirrored
in the \`ssh\` extra); the engine imports it lazily and only when selected
(\`infra/ssh_engine.py\` header); ANY engine trouble raises \`EngineUnavailable\`
and \`infra/remote.py\` (\`except ssh_engine.EngineUnavailable:\` → one-shot path)
falls back automatically — fail-open holds by construction, including an
unimportable asyncssh (add a test mocking import failure regardless).
Implementation in \`cli/mcp.py::cmd_mcp_serve\` ONLY (no import-time side
effects): unless \`HPC_MCP_NO_SSH_ENGINE=1\`, do
\`os.environ.setdefault(ssh_engine.ENGINE_ENV, "asyncssh")\` (import the
constant, don't hardcode), before \`build_server\`. Extend the stderr ready line
with \`engine=on|off|user-set\`. Tests: default-when-unset; user-preset env
wins (setdefault semantics); opt-out wins; no effect outside mcp-serve.

## 4. l-snap — unchanged from brief, two precisions

Key \`(cluster_name, scheduler)\`; one JSON file per cluster under the journal
home \`_snapshot_cache/\` (mirror \`ops/preflight/probe_cache.py\` layout and its
flock/atomic/fail-open discipline exactly). TTL 60s default
(\`HPC_SNAPSHOT_CACHE_TTL_SEC\` override, \`HPC_NO_SNAPSHOT_CACHE=1\` bypass).
Skip breaker-invalidation in v1 (60s TTL bounds staleness); record SUCCESS
only. Wire in \`infra/inspect/__init__.py::inspect_cluster\`: read between the
in-process \`_CACHE\` miss and the backend fetch; write after a successful fetch.
Do not touch \`cluster_history/\` (provenance, not cache).

## 5. l-fastpath — blanket narrowing is UNSAFE; ship the capability-scoped fix

Verified: the guard CAN fire — "a plugin may override or extend a core verb
via \`register_cli\`, which only the full \`build_parser\` walk honours"
(\`cli/dispatch.py::_fast_dispatch_enabled\` docstring). So do NOT fast-dispatch
core verbs merely because they're in the map. Ship the conservative,
self-healing narrowing instead: \`_kernel/registry/plugins.py\` can see, per
entry point, WHICH hooks a plugin implements. A plugin that implements only
primitive-registration hooks (new verbs) cannot alter a core verb's CLI; only
\`register_cli\`-implementing plugins can. Change \`_fast_dispatch_enabled\` to:
plugins present → fast path allowed iff NO installed plugin implements a
CLI-shaping hook (determine hook names from \`plugins.py\`; verify the exact
hook vocabulary there before coding) — plugin-added verbs still miss
\`VERB_MODULE_MAP\` and fall through to the full walk naturally. Any metadata
error → full path (byte-identical fallback preserved). Docstring must record
this guard-can-fire analysis. Tests: primitives-only plugin → core verb fast,
plugin verb full; \`register_cli\` plugin → everything full; metadata error →
full.

## 6. d-pins — scope narrowed (many deletions are cited legitimately as history)

Pins live in \`tests/contracts/\`; scope = \`docs/internals/\` + \`docs/workflows/\`
ONLY (operational truth surfaces; \`docs/design/\` + \`docs/plans/\` narrate
history by design): (1) console-script names appearing in scope must exist in
pyproject \`[project.scripts]\`; (2) \`src/hpc_agent/...\` path-shaped references
in scope must exist on disk; both with a cited ALLOWLIST and fenced-block/
drift-log exclusion; (3) \`docs/design/*.md\` \`status:\` frontmatter vocabulary
{plan, shipped, superseded, partial} + a \`status: plan\` doc must not open with
an IMPLEMENTED/landed banner (regex-level; honest scope caveat in docstring).
Fire-path tests use synthetic fixture docs. Run against the tree AFTER the
async-refill series (its docs unit already corrected campaign-lifecycle.md and
the campaign README); report — don't edit — violations in files owned by
d-rewrite.

## 7. m-elicit, d-rewrite — unchanged from their briefs.

Sequencing: unchanged — m-server merges last (its curated pin references
poll-detached).

`

phase('Build')
const UNITS = [
  { key: 'm-server', iso: true, prompt: `UNIT m-server (you exclusively own src/hpc_agent/_kernel/extension/mcp_server.py and tests/test_mcp_server*.py plus any NEW scripts/lint file + its test). (1) Add read-decisions, verify-relay, attention-queue — and poll-detached (built by a sibling unit; add its entry now, tests tolerant of its absence via registry check or coordinate stage names from the memo) — to _CURATED_EXTRA_VERBS, each with a per-entry rationale comment in the established style (cite the run-#8 unreachable-verb class and the skill lines that name them MCP-direct); apply the memo's verdict on revise-resolved. Update the curated-catalog pin tests. (2) Implement the memo's server-side rendezvous/skill-return relay in the in-process runner with the memo's double-fire strategy; new tests proving: parked block-drive tool result carries the rendezvous digest; emit-skill-return result carries the return; no duplication when the hook path also runs. (3) NEW lint scripts/lint_skill_mcp_reachability.py: every verb a SKILL body names as MCP-direct must be curated-reachable (derived or extra) — with the standard fire-path test against a synthetic violation.` },
  { key: 'm-poll', iso: true, prompt: `UNIT m-poll (you own NEW files only: src/hpc_agent/ops/monitor/poll_detached.py, src/hpc_agent/_wire/queries/poll_detached.py (or the memo's chosen wire home), tests/ops/test_poll_detached.py). Implement the memo's poll-detached query primitive: instant, non-blocking snapshot of a detached worker — lease presence/pid-alive, journal run status, block-terminal presence, derived state (running/exited_recorded/exited_unrecorded/no_lease). @primitive verb="query", idempotent, CliShape mirroring sibling monitor queries. Do NOT hand-write schemas or touch mcp_server.py. Tests: all four derived states over synthetic lease/journal/terminal fixtures; never opens SSH (assert no transport import at runtime).` },
  { key: 'm-elicit', iso: true, prompt: `UNIT m-elicit (you own NEW test files only, plus minimal additions to tests/_mcp_harness.py if required — check it first). Prove capability-1-via-elicitation end to end (the AVL-C gap "no second client proves elicitation"): using tests/_mcp_harness.py::FakeMcpClient against a build_server(...) instance with elicitation declared, drive an authorship-gated append-decision flow where the human value arrives via the elicitation channel: assert (a) the server issues elicitation/create with correct id-namespace, (b) the typed response is filtered and lands via state/utterances.append_utterance with bound-capture set, (c) the authorship gate accepts the value and the dark-channel degradation triggers honestly on a timed-out elicitation (per-session _client_elicitation_dark). Read docs/design/mcp-elicitation.md + _kernel/extension/mcp_server.py elicitation legs first. New file tests/test_mcp_elicitation_client_proof.py.` },
  { key: 'l-engine', iso: true, prompt: `UNIT l-engine (you own src/hpc_agent/cli/mcp.py + NEW tests/cli/test_mcp_engine_default.py). Implement the memo's engine-on-under-mcp-serve default with the opt-out env and honest degradation (asyncssh unimportable / EngineUnavailable → one-shot path, already automatic — verify and cite). Docstring rationale: the engine's idle sweeper + slot-held-while-open invariant were hardened from mcp-serve incidents; the long-lived server is the one place a persistent connection pays. Tests: default set when unset; user env wins; opt-out wins; no effect on non-mcp-serve verbs.` },
  { key: 'l-snap', iso: true, prompt: `UNIT l-snap (you own NEW src/hpc_agent/state/snapshot_cache.py, src/hpc_agent/infra/inspect/__init__.py (wiring only), NEW tests/state/test_snapshot_cache.py). Disk-backed ClusterSnapshot cache copying ops/preflight/probe_cache.py idiom verbatim-in-style: keyed (cluster_name, scheduler), under the journal home, advisory_flock + atomic_write_json, DEFAULT_TTL_SEC=60 (match the in-process TTLCache budget), HPC_NO_SNAPSHOT_CACHE=1 bypass + HPC_SNAPSHOT_CACHE_TTL_SEC override, fail-open on every OSError, SUCCESS-only recording. Wire into inspect_cluster between the in-process _CACHE miss and the backend fetch (read), and after a successful fetch (write). Do NOT touch cluster_history persistence (provenance stays separate). Tests: cross-process hit within TTL (fresh module state), expiry, bypass env, corrupt file fail-open, write-after-fetch.` },
  { key: 'l-fastpath', iso: true, prompt: `UNIT l-fastpath (you own src/hpc_agent/cli/dispatch.py + NEW tests/cli/test_fast_path_plugins.py). Apply the memo's verdict on the plugin fast-path cliff. If the memo says core-verb-in-map dispatch is safe with plugins installed: narrow _fast_dispatch_enabled accordingly (fast path serves verbs present in VERB_MODULE_MAP; plugin verbs and anything else fall through to the full walk), preserving the byte-identical-fallback invariant, with a docstring recording WHY the wholesale disable was safe to narrow (the guard-can-fire analysis). If the memo says otherwise, implement its alternative or report drop-with-rationale. Tests: plugin-installed simulation (monkeypatch entry points) → core verb still fast-dispatches; plugin verb falls through; stale-map miss falls through.` },
  { key: 'd-rewrite', iso: true, prompt: `UNIT d-rewrite (you own docs/internals/submit-sequence.md + docs/workflows/code-driven-orchestration.md only). Rewrite both against the current tree (read _kernel/lifecycle/block_drive.py, drive.py, detached.py, ops/submit_blocks.py first): submit-sequence.md still narrates the DELETED claude-p bare worker reading worker_prompts/submit.md (steps 3.x) — rewrite the walkthrough against block-drive: slash → skill relay → block-drive tick → detached hpc-agent subprocess → journal. code-driven-orchestration.md cites DeterministicCampaignResolver and LlmJudgementResolver modules that do not exist — correct to the drive.py substrate + block-drive reality, and note campaign-refill (landing concurrently) as the mechanized refill actor. Per the drift-log doctrine: shrink present-tense claims to pointers at code seams (path::symbol, no line numbers); keep the valuable narrative structure.` },
  { key: 'd-pins', iso: true, prompt: `UNIT d-pins (you own NEW tests/contracts/test_doc_references.py and NEW tests/contracts/test_doc_status_headers.py, plus minimal fixes to docs OTHER than submit-sequence.md/code-driven-orchestration.md/the campaign docs — those are owned elsewhere; if a violation lives in an owned-elsewhere doc, record it in your report instead of editing). Two CI pins per the memo's drift examples: (1) doc-references: every console-script name and every src/hpc_agent module path referenced in docs/internals + docs/design (excluding history/ and explicit drift-log sections) must exist in pyproject [project.scripts] / the tree — allowlist file for legitimate historical mentions, fire-path test with a synthetic violation. (2) status-headers: every docs/design/*.md with a status: frontmatter must use a value from {plan, shipped, superseded, partial} and a doc marked plan must not also claim IMPLEMENTED/landed in its body header (regex-level, honest scope caveat in the docstring). Run both against the tree; fix or allowlist-with-citation what they catch (within your ownership).` },
]
const built = await parallel(UNITS.map(u => () =>
  agent(CTX + `\nARCHITECT MEMO:\n${memo}\n\n${u.prompt}`,
    { label: `build:${u.key}`, phase: 'Build', model: 'opus', effort: 'high', isolation: 'worktree' })
))

phase('Integrate')
const integration = await agent(
  `You are the integrator in the MAIN checkout ${REPO} (not a worktree). Context:\n${CTX}\nARCHITECT MEMO:\n${memo}\nBUILD REPORTS:\n${built.filter(Boolean).map((r, i) => `--- ${UNITS[i] ? UNITS[i].key : i} ---\n${r}`).join('\n')}\n\n` +
  `PRECONDITION GATE: a concurrent async-refill series must land on ${BRANCH} first. Check: git status --porcelain must be clean AND git log --oneline -12 must contain the campaign-refill/async-refill commits. If the tree is dirty or the series is absent, WAIT: re-check every ~3 minutes for up to 45 minutes (a bash wait loop is acceptable for this: e.g. 'for i in $(seq 1 15); do git status --porcelain | grep -q . || break; timeout 180 tail -f /dev/null; done' — do not busy-spin). If still blocked after the window, STOP and return exactly the word BLOCKED plus a one-line reason — do not merge into a dirty tree.\n` +
  `Then: (1) git checkout ${BRANCH}; git pull origin ${BRANCH} if behind. (2) Merge each pkg/* branch from the build reports (git merge --no-ff pkg/<key>, in this order: d-rewrite, d-pins, l-snap, l-fastpath, l-engine, m-poll, m-elicit, m-server — server last since it references poll-detached). Resolve conflicts favoring both-sides-composed; the units were file-disjoint so conflicts should be rare. (3) Run the regen scripts (scripts/: build_schemas.py, bake_operations_json.py, build_operations_index.py, build_primitive_index.py, build_primitive_frontmatter.py, build_verb_module_map.py; then check_no_pending_primitive_docs.py — poll-detached needs its primitive doc: author the body, don't leave 'Documentation pending'). (4) ruff check . && ruff format . && mypy src/hpc_agent. (5) pytest -q -m 'not slow' — fix all fallout including registry-count pins, operations.json conformance, the new lints' clean-tree runs. (6) Add a CHANGELOG entry covering the three packages. (7) Commit fixups (house style + the same two trailer lines used by the unit commits), git push -u origin ${BRANCH} with 4x backoff retry. (8) Delete merged pkg/* branches. Report: merge order results, regen/test summary, shas, push status.`,
  { label: 'integrate:packages', phase: 'Integrate', model: 'opus', effort: 'high' }
)

phase('Review')
const findings = await parallel([
  ['correctness', 'Hunt concrete bugs in the merged package diff: curated pin enumerations vs registry reality; the server relay double-fire logic under hooks-present and hooks-absent; poll-detached derived-state matrix vs lease/journal timing; snapshot-cache TTL/bypass/fail-open paths and its interaction with the in-process TTLCache; fast-path narrowing vs plugin registration order; engine default env precedence; doc-pin regexes (false positives on legitimate docs? vacuous passes?).'],
  ['doctrine', 'Audit the merged diff against repo doctrine: guards that can fire (every new branch tested), caches never correctness gates, no new judgment leaked into code or mechanism into prose, curated entries carry cited rationale in house style, lints have fire-path tests, docs cite path::symbol not line numbers, the engine default degrades honestly, no affordance removed that a workflow needs.'],
].map(([lens, brief]) => () =>
  agent(`Repo ${REPO} on ${BRANCH} (packages merged + pushed; review with git log/diff). ${CTX}\nINTEGRATION REPORT:\n${integration}\n\nLENS: ${lens}. ${brief}\nVerify each finding against the tree before reporting (path::symbol + failure scenario); report only findings surviving your own refutation attempt, severity-ranked; say so if none survive.`,
    { label: `review:${lens}`, phase: 'Review', model: 'opus', effort: 'high' })
))
const fixed = await agent(
  `${CTX}\nFindings:\n${findings.filter(Boolean).join('\n\n===\n\n')}\n\nFIXER in main checkout ${REPO} on ${BRANCH}: adjudicate each finding (fix or refute with cited evidence), re-run affected tests + pytest -q -m 'not slow' + ruff/mypy, regen if primitives/schemas changed, commit (same trailers) and push with retry. Report per-finding verdicts + final shas.`,
  { label: 'fix:packages', phase: 'Review', model: 'opus', effort: 'high' }
)
return { integration: String(integration).slice(0, 2500), findings: findings.filter(Boolean).map(f => String(f).slice(0, 700)), fix: String(fixed).slice(0, 2500) }