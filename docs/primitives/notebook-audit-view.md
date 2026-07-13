---
name: notebook-audit-view
verb: query
side_effects:
- file_write: <experiment>/.hpc/renders/<audit_id>/<slug>.<view_sha12>.md
- file_write: <experiment>/.hpc/notebooks/<audit_id>.decisions.jsonl (render relay-due
    marker, CANONICAL human-required sections only)
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent notebook-audit-view --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.notebook.view_op.notebook_audit_view
---
# notebook-audit-view

Render the deterministic **per-section audit view** of a notebook-audit source
`.py` against its template — the D6 *interface* over the audited source. A pure
read: it parses the source + template (jupytext percent format), projects every
**source** section (classification-by-hash, diff-from-template, static assertion
table, opaque lint flags, D-attention tier), and returns the canonical
per-section + module-level projection **plus its code-rendered `markdown`**.

This is the interface surface of the notebook-audit substrate
(`docs/design/notebook-audit.md`, Wave B / T5). The `markdown` is the
**verbatim-relay** projection: the audit skill hands it to the human unchanged —
no LLM-freeform prose ever enters the audit path (D6). A per-section `view_sha`
is exactly what a sign-off binds (D5): `append-decision`
(`block=notebook-sign-off`, `resolved={…, view_sha}`) records *what the human
saw*, and the section hash moving revokes that sign-off by construction.

## Seam

`notebook-audit-view` does **not** recompute lint findings. The `notebook-lint`
primitive exposes no reusable non-primitive function (its rule orchestration
lives inside the primitive body), so findings are **chained in opaquely** via the
`lint_findings` spec field — the caller runs `notebook-lint` first and passes its
`findings` list through. A finding is attributed to a section by a slug-naming
key it carries (`slug` / `section` / `section_slug`); a finding with no such key
is module-scoped and flips no section's tier.

## Inputs

A `NotebookAuditViewSpec` (`hpc_agent._wire.queries.notebook_audit_view`):

- `source` (string, required) — experiment-relative path to the audited source
  `.py` (jupytext percent format). Per-section shas are recomputed **fresh on
  every call**, so an edit revokes stale trust by construction.
- `template` (string, required) — experiment-relative path to the template `.py`.
  Each source section is classified and diffed against the template section that
  shares its slug.
- `lint_findings` (list of objects, default `[]`) — **opaque** findings chained
  in from `notebook-lint` (its `findings` list). Embedded verbatim under the
  section each names; never parsed or interpreted here.
- `receipt` (object, optional) — **opaque INLINE** execution receipt
  `{slug: {output_sha, error}}`, for **preview only** (this read-only verb
  journals nothing). `error is False` marks that section's declared assertions
  **green**; absent a receipt, a section *with* assertions is not green
  (unverified is not green). An inline entry carries no `section_sha`, so it is
  not sha-freshness-gated here — the mutate `notebook-auto-clear` path instead
  reads **journaled**, sha-bound receipts (`notebook-record-receipt`), which drift
  stale by construction.
- `attention_order` (list of strings, optional) — caller-supplied section-slug
  ordering for the presented sections + `markdown` (T12). Default (absent) is
  source order. Listed slugs are shown **first** in the given order; unknown slugs
  are ignored; source slugs the order omits keep source order after the listed
  ones. It changes what the human saw, so it participates in the module
  `view_sha`; per-section `view_sha`s are unaffected.

## Outputs

`data` is a `NotebookAuditViewResult`:

```
{
  "sections": [
    {
      "slug": "<section slug>",
      "classification": "inherited | added | modified",
      "tier": "auto_cleared | human_required",
      "section_sha": "<64-hex>",
      "template_section_sha": "<64-hex, or null when added>",
      "diff": ["<unified diff line>", ...],
      "assertions": [{"test": "<expr>", "lineno": <int>, "msg": "<str or null>"}],
      "lint_flags": [ <opaque finding>, ... ],
      "view_sha": "<64-hex — what a sign-off binds>"
    }
  ],
  "dropped_template_slugs": ["<slug>", ...],
  "source_module_sha": "<64-hex>",
  "template_module_sha": "<64-hex>",
  "view_sha": "<64-hex roll-up>",
  "markdown": "<code-rendered projection, for verbatim relay>"
}
```

- **classification** (D6, by source-hash): `inherited` (slug in template, shas
  equal — empty diff), `added` (slug absent from template), `modified` (slug in
  template, shas differ).
- **tier** (D-attention): `auto_cleared` iff the section is `inherited`, has zero
  lint flags, AND its declared assertions are green (zero assertions is green
  statically; with assertions, green requires a receipt). Everything else →
  `human_required`.
- **dropped_template_slugs** — template slugs absent from the source (the draft
  dropped a section the template declared). Surfaced, never hidden; the
  graduation gate refuses on these, the view only shows them.
- **view_sha** — a deterministic roll-up over the section shas + the two module
  fingerprints; any section OR preamble edit moves it.

## Errors

- `spec_invalid` — an unreadable `source`/`template` path (naming which), or a
  malformed percent-format module (a bad, duplicate, or misplaced
  `# hpc-audit-section:` marker — the parser's boundary guards). Not retry-safe;
  fix the path or the source.

## Idempotency

A pure query with no side effects and no natural identity key. Derived state:
recomputed from the `.py` on disk on every call, so identical inputs yield an
identical `view_sha` (and byte-identical `markdown`) on every platform.

## Usage

```
hpc-agent notebook-audit-view --spec spec.json --experiment-dir .
```

where `spec.json` is `{"source": "<py relpath>", "template": "<py relpath>",
"lint_findings": [...], "receipt": {...}}`. The skill relays `markdown` verbatim;
the human signs a section via `append-decision` (`block=notebook-sign-off`,
binding the section's `view_sha`) — there is deliberately no sign-off verb here
(the no-unlock-verb doctrine). This verb only reads.
