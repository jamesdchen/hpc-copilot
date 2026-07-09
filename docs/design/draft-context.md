---
status: plan
---
# The draft-context projection — mechanizing the drafting brief

**Status: PLANNED (2026-07-07, written during run #10's audit prelude — the
run IS the motivating evidence).** Registry: +1 (`notebook-draft-context`),
updating the cross-slate arithmetic in `slate-sequencing.md`. Subsumes the
V-LINK render item; carries V-ANSI as a sibling projection note.

## Motivation, measured live

Run #10's drafting prelude spent its wall-clock on discovery greps —
`PERIODS_PER_DAY`, engine signatures, the prod config's last-touch sha —
every one a fact derivable mechanically from the template + declared roots.
The relay session killed the latency by hand-writing a "drafting brief"
(engine call-sites with `path:line`, config identity, data quirks). This
plan mechanizes that brief, by the same move that made the submit side fast:
**lift the process out of the LLM** (#363; `design-cli-verbs-over-python`).
The drafting agent reads ONE deterministic artifact instead of N greps.

## The verb

`notebook-draft-context` — query, read-only, agent_facing.

**Spec**: `{template, source_roots, input_roots, inventory_roots?}` — all
roots caller-declared and OPAQUE (an `inventory_roots: ["configs"]` lists
files+shas under it; core never knows what a "config" is). Default roots
from the audit's recorded configuration when an `audit_id` is given
(one-declaration reuse, the data-manifest rule).

**Output** (deterministic markdown render + structured result):

1. **Template sections + guidance** — the parsed template's slugs and cell
   prose, verbatim.
2. **Resolved engines** — for each name the TEMPLATE imports: the resolving
   file under `source_roots` (extend `notebook-lint`'s `linked_sources`
   machinery — ONE resolution definition), the symbol's `path:lineno`,
   its signature (`ast.unparse` of the def's arguments), first docstring
   line, and `module_sha`. All AST; no imports executed.
3. **Name-match call sites** — for each engine symbol, `Call` nodes with a
   matching name across `source_roots`, as `path:lineno` (+count, capped,
   cap disclosed — the no-silent-caps rule). Labeled honestly:
   "name-match" (AST identity, not type resolution).
4. **Inventory listings** — files + sha12 + size under `input_roots` and
   each `inventory_roots` entry. When the data manifest (Phase 1a) exists,
   cite it rather than re-hashing — reuse seam.

**Cache**: content-keyed (describe-cache pattern) on the shas of every
input (template, resolved source files, listed roots) — recompute-on-read,
disposable index.

## The altitude boundary (what this verb refuses to know)

The projection LISTS; it never NOMINATES. "Which config is the deployed
baseline" is program-binding knowledge (the three-level rule vocabulary in
`data-manifest.md`); "which sections matter most" is pack guidance. The
hand-written run-#10 brief mixed mechanical facts with semantic nominations
— that mixture is exactly the two layers this design separates: core ships
the mechanical half; the pack later ships the semantic half as declarations
the same render can incorporate.

## Consumers

- **The drafting step of `hpc-notebook-audit`** — the skill's prelude reads
  the projection FIRST (one skill-prose edit), replacing discovery greps.
- **V-LINK (subsumed)** — the audit VIEW embeds the resolved-engines table
  (relpath + sha12, markdown-linked) using this verb's resolution machinery
  (one definition, two renders). NOTE: embedding into the canonical view
  changes computed view content → **a versioned view change (canon_version
  class), landed between campaigns, never mid-audit.** The draft-context
  verb itself is standalone and lands freely.
- **V-ANSI (sibling, recorded here)** — terminal harnesses need a
  code-rendered ANSI projection of view/draft-context renders (capability
  4's terminal binding). Same content, escape-coded by code, never styled
  by the LLM.

## Enforcement

- Toy-domain fixtures only (no quant vocabulary in tests).
- The projection's render is trusted-display class: LLM relays/points,
  never re-summarizes (the pinned 2026-07-07 doctrine).
- A contract test pins read-only-ness (no file writes; the cache lives in
  the standard cache home).

## Sequencing

Post-run-#10, freestanding (no hot-file collision with Phases 1–5 except
the `linked_sources` helper refactor inside `ops/notebook/lint.py` — a
pure extraction, serialize with any other lint edits). The V-LINK view
embed serializes with the next view-touching phase and its canon bump.
Registry: 148 → **149** expected post-slate.

## Drift log

- 2026-07-07: written mid-run-#10 (Fable, pre-deadline); user directive
  "the repo should be faster out of the box — no hand-rolled drafting
  brief".
