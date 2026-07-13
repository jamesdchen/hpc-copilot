# Architect memo — packages swarm (authored by Fable, 2026-07-12)

Settles the Design-phase questions against the tree so the build units are
pure execution. Cites are `path::symbol` verified today. Where this memo
contradicts a unit's original brief, THE MEMO WINS.

## 1. m-server — relay RECONCEIVED as envelope parity (do not build injection)

The two autofetch hooks are **Bash-transport compensations**, per their own
docstrings (`_kernel/hooks/skill_return_autofetch.py`,
`decision_rendezvous_autofetch.py`): pure, additive, fail-open observers that
re-inject content the agent already received on stdout but may not render.
Over MCP the envelope IS the structured tool result — nothing is lost in
transport. Therefore:

- Do NOT add server-side injection to `mcp_server.py`. That would duplicate
  mechanism (one-definition rule) to solve a transport problem MCP doesn't have.
- DO add parity tests (`tests/test_mcp_server_envelope_parity.py`): a parked
  `block-drive` `tools/call` result must contain every field
  `decision_rendezvous_autofetch.build_hook_output` would inject (the `brief`,
  `next_block` hint, awaiting marker). If a field is missing, enrich the
  **block-drive Result model**, never the server.
- DO document in `docs/reference/mcp.md`: autofetch hooks are unnecessary over
  MCP by construction; the **Stop-guard enforcement half has no MCP
  equivalent** — it maps to harness capability 2 (relay enforcement) in
  `docs/internals/harness-contract.md`; name the degradation honestly.

Curated additions (all as `_CURATED_EXTRA_VERBS` entries with house-style cited
rationale — the run-#8 unreachable-verb-gets-hand-rolled class):
`read-decisions`, `verify-relay` (named MCP-direct at hpc-submit/SKILL.md and
hpc-status/SKILL.md), `attention-queue` (hpc-status), `revise-resolved`
(**verified: `ReviseResolvedResult` declares NO `next_block`**
(`_wire/workflows/revise_resolved.py::ReviseResolvedResult`), so it does NOT
derive — despite hpc-submit instructing "MCP-direct"), and `poll-detached`
(built by m-poll; guard the pin test on registry presence).
Lint `scripts/lint_skill_mcp_reachability.py` as briefed, fire-path test
against a synthetic violating SKILL.

## 2. m-poll — poll-detached spec

Home `ops/monitor/poll_detached.py`; wire model `_wire/queries/poll_detached.py`.
Spec: `run_id` (required), `block` (verb string, e.g. "campaign-run"),
`experiment_dir`. Result: `lease_present`, `pid`, `pid_alive`,
`journal_status`, `terminal_recorded`, derived
`state ∈ {running, exited_recorded, exited_unrecorded, no_lease}`,
`watch: "journal"`. Sources — reuse, never reimplement:
`_kernel/lifecycle/detached.py` lease path/read helpers (lease JSON carries
`pid`; file `<verb>-<run_id>.lease.json` under the global `_detached/` home),
`infra/proc.pid_alive`, `state/journal_poll.read_run_status`,
`state/block_terminal.read_terminal_with_fallback`. Zero SSH; `verb="query"`,
`idempotent=True`, `side_effects=[]`. NOT detach-required over MCP.

## 3. l-engine — engine-on under mcp-serve

Verified: `asyncssh>=2.23` is a **core dependency** (pyproject, also mirrored
in the `ssh` extra); the engine imports it lazily and only when selected
(`infra/ssh_engine.py` header); ANY engine trouble raises `EngineUnavailable`
and `infra/remote.py` (`except ssh_engine.EngineUnavailable:` → one-shot path)
falls back automatically — fail-open holds by construction, including an
unimportable asyncssh (add a test mocking import failure regardless).
Implementation in `cli/mcp.py::cmd_mcp_serve` ONLY (no import-time side
effects): unless `HPC_MCP_NO_SSH_ENGINE=1`, do
`os.environ.setdefault(ssh_engine.ENGINE_ENV, "asyncssh")` (import the
constant, don't hardcode), before `build_server`. Extend the stderr ready line
with `engine=on|off|user-set`. Tests: default-when-unset; user-preset env
wins (setdefault semantics); opt-out wins; no effect outside mcp-serve.

## 4. l-snap — unchanged from brief, two precisions

Key `(cluster_name, scheduler)`; one JSON file per cluster under the journal
home `_snapshot_cache/` (mirror `ops/preflight/probe_cache.py` layout and its
flock/atomic/fail-open discipline exactly). TTL 60s default
(`HPC_SNAPSHOT_CACHE_TTL_SEC` override, `HPC_NO_SNAPSHOT_CACHE=1` bypass).
Skip breaker-invalidation in v1 (60s TTL bounds staleness); record SUCCESS
only. Wire in `infra/inspect/__init__.py::inspect_cluster`: read between the
in-process `_CACHE` miss and the backend fetch; write after a successful fetch.
Do not touch `cluster_history/` (provenance, not cache).

## 5. l-fastpath — blanket narrowing is UNSAFE; ship the capability-scoped fix

Verified: the guard CAN fire — "a plugin may override or extend a core verb
via `register_cli`, which only the full `build_parser` walk honours"
(`cli/dispatch.py::_fast_dispatch_enabled` docstring). So do NOT fast-dispatch
core verbs merely because they're in the map. Ship the conservative,
self-healing narrowing instead: `_kernel/registry/plugins.py` can see, per
entry point, WHICH hooks a plugin implements. A plugin that implements only
primitive-registration hooks (new verbs) cannot alter a core verb's CLI; only
`register_cli`-implementing plugins can. Change `_fast_dispatch_enabled` to:
plugins present → fast path allowed iff NO installed plugin implements a
CLI-shaping hook (determine hook names from `plugins.py`; verify the exact
hook vocabulary there before coding) — plugin-added verbs still miss
`VERB_MODULE_MAP` and fall through to the full walk naturally. Any metadata
error → full path (byte-identical fallback preserved). Docstring must record
this guard-can-fire analysis. Tests: primitives-only plugin → core verb fast,
plugin verb full; `register_cli` plugin → everything full; metadata error →
full.

## 6. d-pins — scope narrowed (many deletions are cited legitimately as history)

Pins live in `tests/contracts/`; scope = `docs/internals/` + `docs/workflows/`
ONLY (operational truth surfaces; `docs/design/` + `docs/plans/` narrate
history by design): (1) console-script names appearing in scope must exist in
pyproject `[project.scripts]`; (2) `src/hpc_agent/...` path-shaped references
in scope must exist on disk; both with a cited ALLOWLIST and fenced-block/
drift-log exclusion; (3) `docs/design/*.md` `status:` frontmatter vocabulary
{plan, shipped, superseded, partial} + a `status: plan` doc must not open with
an IMPLEMENTED/landed banner (regex-level; honest scope caveat in docstring).
Fire-path tests use synthetic fixture docs. Run against the tree AFTER the
async-refill series (its docs unit already corrected campaign-lifecycle.md and
the campaign README); report — don't edit — violations in files owned by
d-rewrite.

## 7. m-elicit, d-rewrite — unchanged from their briefs.

Sequencing: unchanged — m-server merges last (its curated pin references
poll-detached).
