# Plugin → core API contract (v1)

## Purpose

This document is the single source of truth for the **import surface** the
shipped example plugin
[`examples/plugins/hpc-agent-notebook-render/`](../../examples/plugins/hpc-agent-notebook-render/)
is permitted to reach into `hpc_agent` core. The notebook-render plugin is
CI-gated (the `plugins` job runs its suite), yet it imports ~18 core module
paths — most of them *past* any previously documented seam. Nothing froze
that surface, so a core reorg that renamed or moved one of those modules would
silently break a plugin the project ships and tests.

The allowlist below is frozen and enforced in **both directions** by
[`scripts/lint_plugin_api_surface.py`](../../scripts/lint_plugin_api_surface.py):

- **Stay-inside** — every `hpc_agent.*` import the plugin makes (top-level,
  `TYPE_CHECKING`-guarded, or function-local — the scan `ast.walk`s the whole
  tree) must appear in `ALLOWED_PLUGIN_IMPORTS`, down to the symbol.
- **Anti-drift** — every allowlisted module/symbol must still resolve in the
  installed core. A reorg that moves or renames an entry fails the lint loudly
  and must consciously update this contract, rather than breaking the plugin.

A third leg, `unused_allowlist_entries`, forbids a *dead* row (an allowlist
entry the plugin no longer imports), so the surface stays exactly the real
surface. The `--fire-path` leg (CI `plugins` job only) additionally proves the
plugin registers `notebook-render` / `notebook-ingest-signoffs` as real CLI
verbs, each carrying a `CliShape`.

**Contract version: `1`.** The version is bumped only on a **narrowing** (a
removed module or symbol); a widening is backward-compatible for the plugin and
needs no bump.

## The allowed surface

Each row is a core module path, the exact symbols the plugin may import from
it, and a one-line rationale. `*` means the whole module is imported wholesale
(`from hpc_agent import errors` binds the `errors` submodule).

### Sanctioned seams

These are the intended, documented seams — a plugin is *expected* to use them.

| Module | Symbols | Rationale |
|---|---|---|
| `hpc_agent.errors` | `*` | The public error taxonomy every verb raises through; the plugin maps its own failures onto core `HpcError` codes. |
| `hpc_agent.infra.io` | `append_jsonl_line` | The atomic append-one-JSONL-line helper — the plugin's between-cell observer writes trace records through it, not a hand-rolled open/write. |
| `hpc_agent._wire.plugin_manifest` | `PluginManifest` | The declaration surface every plugin builds its `MANIFEST` from (reconciled by `lint_plugin_manifests.py`). |

### Primitive registry

| Module | Symbols | Rationale |
|---|---|---|
| `hpc_agent._kernel.registry.primitive` | `primitive`, `SideEffect` | The `@primitive` decorator + declared-side-effect type; how the plugin's two verbs register with zero host edits. Also root-public (see note below). |

### CLI shape

| Module | Symbols | Rationale |
|---|---|---|
| `hpc_agent.cli._dispatch` | `CliShape` | The declarative CLI shape a `@primitive` carries so the host's registry walk builds its argparse subcommand. |

### Wire action models

| Module | Symbols | Rationale |
|---|---|---|
| `hpc_agent._wire.actions.decision_journal` | `AppendDecisionInput` | The typed input the plugin passes to `append_decision` when a human sign-off is ingested. |
| `hpc_agent._wire.actions.notebook_record_receipt` | `NotebookReceiptEntry`, `NotebookRecordReceiptSpec` | The typed receipt models the plugin assembles before calling the core record-receipt op. |

### Ops verb entrypoints

| Module | Symbols | Rationale |
|---|---|---|
| `hpc_agent.ops.decision.journal` | `append_decision` | Core append-decision op — the plugin routes a rendered sign-off through it for bind/gate parity. |
| `hpc_agent.ops.notebook.audit_view` | `HUMAN_REQUIRED`, `SectionView` | The tier constant + per-section view type the plugin renders audit cells from. |
| `hpc_agent.ops.notebook.canonical` | `AuditConfig`, `build_canonical_view`, `read_recorded_config` | Builds the canonical audit view over sealed records — the projection the notebook renders. |
| `hpc_agent.ops.notebook.record_receipt_op` | `notebook_record_receipt` | Core op that binds each receipt to the freshly-parsed section sha server-side. |
| `hpc_agent.ops.notebook.render_store` | `write_render` | Core store for a rendered notebook artifact (atomic write). |

### State APIs

| Module | Symbols | Rationale |
|---|---|---|
| `hpc_agent.state.audit_source` | `parse_percent_source` | Parses the audited `# %%` percent-format source into cells. |
| `hpc_agent.state.data_trace` | `ingest_trace`, `make_record`, `stdlib_measure` | The runner-tier trace record builder + stdlib fallback measurer the between-cell observer uses. |
| `hpc_agent.state.decision_journal` | `read_decisions` | Read-only decision-journal reader (renders recorded sign-offs into audit cells). |
| `hpc_agent.state.notebook_audit` | `audit_section` | Per-section audit read used to compute the rendered status/tier/shas. |
| `hpc_agent.state.utterances` | `append_utterance`, `is_harness_injected` | The documented utterance-log write API — the plugin writes human-typed sign-off text through it (the "second conforming harness" path). |

### Mapreduce trace constants

| Module | Symbols | Rationale |
|---|---|---|
| `hpc_agent.execution.mapreduce.data_trace_contract` | `TRACE_SOURCE_RUNNER`, `TRACE_TRANSPORT_FILENAME` | The `source="runner"` tag + transport filename the observer stamps onto each trace record so it ingests into the audit scope. |

### Note on root-public names

`primitive` and `SideEffect` are also **root-public** — `from hpc_agent import
primitive` works via the package root re-export. The plugin currently imports
them from their canonical home
(`hpc_agent._kernel.registry.primitive`), which is what the allowlist pins. If
a future release narrows the plugin surface to the root re-export only, this
entry MAY be narrowed to `hpc_agent` (a `CONTRACT_VERSION` bump — a narrowing).

## How to extend

The surface and this doc are kept in lockstep by the lint; drift fails CI.

- **Widening** (the plugin needs a new core module/symbol): add the entry to
  `ALLOWED_PLUGIN_IMPORTS` in `scripts/lint_plugin_api_surface.py`, add the
  matching row to the table above, in the **same PR** as the plugin change. No
  version bump — a widening is backward-compatible.
- **Narrowing** (a module/symbol is removed from the surface): delete the
  allowlist entry, delete the row here, and **bump `CONTRACT_VERSION`** — all
  in one PR.
- **A core reorg moved an entry**: the anti-drift leg fails loudly
  (`check_allowlist_resolves`) — that is by design. Point the allowlist +
  this doc at the new location in the reorg PR; do not delete the pin.

The `test_allowlist_covers_the_real_plugin_exactly` test additionally forbids a
dead row: if the plugin stops importing something, its allowlist entry must go
too. So the table above is always *exactly* the plugin's real core surface,
never a superset.
