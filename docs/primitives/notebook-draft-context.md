---
name: notebook-draft-context
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent notebook-draft-context --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.notebook.draft_context_op.notebook_draft_context
---
The drafting projection for a notebook-audit template. It mechanizes the
hand-written "drafting brief" run #10 built by hand (engine signatures, call
sites, config identities) into ONE deterministic artifact the drafting agent
reads instead of running N discovery greps. Everything is derived by AST and file
stat; no import is ever executed. The projection LISTS, never NOMINATES — it
ranks no section, names no "baseline" config, and attaches no meaning to a root
(roots are opaque; core never learns what a "config" is). The `markdown` render is
trusted-display class: the LLM relays or points at it, never re-summarizes it.

## Inputs

- template (string) — experiment-relative path to the template `.py` (jupytext
  percent format). Its slugs and cell prose are echoed verbatim, and its imports
  are the declared engines the projection resolves.
- source_roots (list of string, optional) — opaque import roots the engines
  resolve under. `null` defaults from the audit's recorded config when `audit_id`
  is given, else empty.
- input_roots (list of string, optional) — opaque data roots whose files are
  listed in the inventory. `null` defaults from the audit's recorded config when
  `audit_id` is given, else empty.
- inventory_roots (list of string) — additional opaque roots to list in the
  inventory (e.g. a configs dir). Never defaulted from the audit.
- audit_id (string, optional) — when given, absent `source_roots` / `input_roots`
  default from that audit's recorded configuration (interview.json
  `audited_source`, else the journaled `notebook-record-config` record).

## Outputs

`{template_sections, resolved_engines, call_sites, inventory, source_roots,
input_roots, markdown}`.

- template_sections — `{slug, source}` per template section, verbatim.
- resolved_engines — `{name, module, symbol, resolved, file, symbol_lineno,
  signature, doc, module_sha}` per name the template imports. `resolved` is false
  for stdlib / site-packages / external imports (listed honestly, never dropped).
  `signature` is `ast.unparse` of the def's arguments; `doc` is the first
  docstring line; `module_sha` is the shared normalized hash `notebook-lint` uses.
- call_sites — `{name, sites, count, cap, truncated}` per engine. `sites` are
  `path:lineno` name-matches (AST call-name identity, not type resolution) across
  `source_roots`, capped at `cap` with `truncated` disclosing when more existed.
- inventory — `{root, kind, entries, manifest_cited}` per declared root; each
  entry is `{relpath, sha12, size, cited}`. `cited` (and `manifest_cited`) is true
  when the sha/size came from `.hpc/data_manifest.json` instead of re-hashing.
- markdown — the deterministic, code-authored render (same inputs => identical
  bytes) the drafting agent reads and the skill relays verbatim.

## Errors

- spec_invalid — the template file is missing / unreadable, or is a malformed
  percent-format module (bad, duplicate, or misplaced section marker).

## Idempotency

Pure read; recomputed from the `.py` and declared roots on every call. The result
is memoized in a content-keyed, disposable cache in the standard cache home
(`~/.claude/hpc/draft_context_cache/`); a stat change to any input misses and
recomputes (recompute-on-read). `HPC_NO_DRAFT_CONTEXT_CACHE=1` bypasses the cache.

## Notes

- Read-only: the only write is the disposable cache in the cache home; nothing
  under the experiment dir is ever written.
- The engine resolution is the SAME machinery `notebook-lint` uses
  (`ops/notebook/linked_sources.py`) — one resolution definition, two verbs.
- The data-manifest citation is read defensively by the documented record shape
  (`{relpath: {sha256, size, built_by?}}`), so it works whether or not the
  parallel Phase-1a `data-manifest` verb has landed; absent, it falls back to
  hashing.
