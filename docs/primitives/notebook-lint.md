---
name: notebook-lint
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent notebook-lint --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.notebook.lint.notebook_lint
---
# notebook-lint

Run four read-only structural checks over a notebook-audit **source** `.py`
(jupytext percent format, parsed by `state/audit_source.py`) against its
**template** and caller-declared, **opaque** path/import roots. The lint
**reports** findings; it never refuses — the graduation gate (T9) is what
refuses. A section with zero findings is one auto-clear precondition for the
tier computation (T5).

The four rules:

1. **structural completeness** — the template's marker slugs must appear in the
   source's slugs as an **order-preserving subsequence**. Missing and reordered
   template slugs are reported. Slugs are opaque identifiers — no content meaning
   is inspected.
2. **executes-live** — path-shaped **string literals** (a `str` constant that
   carries a path separator, or that resolves under a declared `input_root`) are
   checked to exist under `input_roots`; a missing one is a finding. A **computed**
   path expression (an f-string or a `+`-concatenation carrying a separator)
   cannot be verified and is recorded in `unverifiable_paths` — an honest gap,
   never silently skipped. No reader-function vocabulary (`read_csv`, …) is ever
   consulted.
3. **linked_sources** — `import` / `from … import …` statements that resolve to a
   file under a declared `source_root` are reported as `{module, file,
   module_sha}`, judging import **origin identity** only. Imports that don't
   resolve under a root (stdlib, site-packages) are simply not linked — never
   findings.
4. **template_import_shadowed** — a source **section** that defines (`def` /
   `async def` / `class`) or rebinds (a top-level assignment, or an import with
   a **different** origin) a name the **template** imports anywhere is reported,
   with the shadowed `name` and the `template_slug` that imports it
   (`module-preamble` when the import sits before the first section marker).
   The shadow list is derived **only** from the template's own import
   statements — no name lists, no configuration knob, no domain vocabulary: the
   template's imports are the caller's declared engines, and the finding NAMES
   a re-derivation hazard at sign-off. An identical verbatim re-import is clean;
   a name bound inside a function body or an attribute/subscript assignment is
   never flagged. Findings are sorted by `(slug, name)` (deterministic
   view_sha downstream).

## Inputs

- `source` (string, required) — Relpath (under the experiment dir) or absolute
  path to the audit source `.py`.
- `template` (string, required) — Relpath or absolute path to the template `.py`.
- `input_roots` (list of strings, default `[]`) — Opaque data-path roots the
  executes-live rule tests path literals against. Relative roots resolve under
  the experiment dir.
- `source_roots` (list of strings, default `[]`) — Opaque import roots the
  linked-sources rule resolves imports under.

## Outputs

A `NotebookLintResult` object with:

- `findings` (list of `NotebookLintFinding`) — Empty list = clean. Each finding has:
  - `rule` — `"structural_completeness"`, `"executes_live"`, `"linked_sources"`,
    or `"template_import_shadowed"`.
  - `section` — the section slug the finding is about, or `null` (module-level).
  - `detail` — human-readable description.
  - `evidence` — opaque structured payload (slug, path literal, line number, …).
- `unverifiable_paths` (list of strings) — Computed path expressions that could
  not be checked (the honest executes-live gap).
- `linked_sources` (list of `LinkedSource`) — Each with `module`, `file` (relpath
  under the experiment dir when possible, else absolute), and `module_sha`
  (`sha256_normalized` over the file text — the value T9 drift-checks at sign-off).

## Errors

Only a **malformed input** raises; every rule VIOLATION is a reported finding.

- `spec_invalid` (user) — the `source`/`template` file is missing, the source is
  not parseable Python, or a marker/slug in either is malformed (surfaced by
  `parse_percent_source`).

## Idempotency

Pure read over the source, template, and declared roots — calling twice with the
same files and spec produces the same report. No side effects.

## Notes

- **Boundary discipline (Q1):** slugs stay opaque (no content-meaning check);
  path detection is purely syntactic (no `read_csv`-style reader vocabulary);
  linked-sources judges import origin identity only, never import content.
- Path separators are matched as both `/` and `\` so a source hashes and lints
  identically across POSIX and Windows.
- A path literal is resolved permissively (root-relative and as a leaf under each
  declared root) so a false-positive "missing" finding is avoided.
- Relative imports (`from . import x`) are not cross-root links and are skipped by
  the linked-sources rule.

**Schemas:** [`notebook_lint.input.json`](../../src/hpc_agent/schemas/notebook_lint.input.json), [`notebook_lint.output.json`](../../src/hpc_agent/schemas/notebook_lint.output.json).
