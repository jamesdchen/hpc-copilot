---
name: notebook-scaffold-template
verb: mutate
side_effects:
- file_write: <experiment>/<output_path> (new file; an existing one is refused)
idempotent: false
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent notebook-scaffold-template --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.notebook.scaffold_template_op.notebook_scaffold_template
---
# notebook-scaffold-template

Scaffold a **content-free notebook-audit template**: given an ordered list of
section slugs and an output path, write a jupytext percent-format `.py` with a
short format-only module docstring plus one `# %%` cell per slug — each cell's
first non-blank line is its `# hpc-audit-section: <slug>` marker, followed by a
one-line placeholder comment. Cell **bodies are caller-owned**; the verb emits
format machinery only, never content (the audit-template analog of
`build-template`'s shape-level scaffolding). The marker line and cell delimiter
come from the ONE percent-format grammar (`state/audit_source.py`) — the writer
can never emit a file the parser would not recognize.

**Round-trip verified.** After writing, the verb re-reads the file on disk and
parses it with the same `parse_percent_source` every audit consumer uses; the
parsed slugs must equal the requested slugs exactly. On any mismatch or parse
failure the partial file is **deleted** and the call refused — a scaffold that
does not survive its own parse is never left on disk.

## Inputs

A `NotebookScaffoldTemplateSpec`
(`hpc_agent._wire.actions.notebook_scaffold_template`):

- `slugs` (list of strings, required) — the ordered section inventory, one
  marker cell per slug. Slugs are OPAQUE identifiers (filesystem-safe shape
  `^[A-Za-z0-9._-]+$`); must be non-empty and duplicate-free.
- `output_path` (string, required) — where the scaffold `.py` is written.
  Relative paths resolve under the experiment dir; missing parent directories
  are created.

## Outputs

A `NotebookScaffoldTemplateResult`:

- `output_path` — the resolved absolute path written.
- `slugs` — the slugs as VERIFIED by the round-trip parse (equal to the
  requested slugs by construction).
- `module_sha` — `sha256_normalized` over the written module from that same
  parse (the fingerprint a later audit of the untouched scaffold reproduces).

## Errors

- `spec_invalid` — empty `slugs`; a duplicate slug (refused EARLY, offending
  slug named — a duplicate would fail the section parse anyway); a malformed
  slug (named, surfaced by the marker grammar itself, refused before any
  write); an `output_path` that already exists (**no force flag in v1** — the
  caller deletes first, never silently clobbered); or a written scaffold that
  fails its own round-trip parse (deleted before the raise).

## Idempotency

Not idempotent: a retry of a SUCCEEDED call finds the file it just wrote and
is refused (`already exists`). To re-scaffold, delete the output file first.

## Notes

- Format machinery only: the generated docstring and placeholder comments say
  what the file's structure is and who owns the bodies — zero domain
  vocabulary, per the substrate-not-semantics boundary.
- The scaffold parses with the docstring as PREAMBLE (belongs to no section,
  covered by `module_sha`), so an untouched scaffold's per-section hashes come
  entirely from the marker cells.
- Typical flow: scaffold the template, hand the file to the drafting LLM /
  human to fill each cell body, then run `notebook-lint` against it as the
  `template` input.
