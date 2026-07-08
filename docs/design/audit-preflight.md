# audit-preflight — the GO/NO-GO brief for the audit loop

**Status: PLANNED, USER-RULED into the slate (2026-07-07, during run #10 —
Phase 1b).** Registry +1 (`audit-preflight`). Precedent: `submit-preflight`.
The R2 item from the run-#10 mechanization sweep, promoted.

## Motivation, measured live

Run #10's demo session spent its opening turns hand-verifying prereqs
(template committed? version skew? MCP fresh?) and was misdirected by a
kickoff-prose defect ("sign it first" — it went hunting for a signing verb
that deliberately does not exist). Every prereq is mechanical; prose
instructions rot, code checks don't. With this verb the kickoff collapses
to: **"run audit-preflight; if GO, begin"** — a sentence that cannot rot
because it delegates to code.

## The verb

`audit-preflight` — query, read-only, agent_facing.

**Spec**: `{template, source_roots?, input_roots?, audit_id?}` (roots
default from the audit's recorded configuration when `audit_id` names an
existing audit — the one-declaration rule).

**Checks (ALL compositions of existing machinery — no new detection):**

1. **Template present + adopted**: file parses (`parse_percent_source`),
   and is git-committed clean at the declared path (the same
   git-awareness registration's `template_sha` needs; an uncommitted or
   dirty template = NO-GO "unsigned template", remedy: commit = the
   signature).
2. **Version skew**: doctor's existing skew detection between the CLI,
   the MCP server (via `harness-capabilities`), and the recorded env.
3. **Roots validity**: declared `input_roots`/`source_roots` exist and are
   non-empty; when the data manifest (Phase 1a) exists, its drift counts
   ride along as DISCLOSURE (never a blocker — the attention contract).
4. **Prior audit state**: whether `audit_id` already has a journal
   (resuming vs fresh — tonight's exit-2 probe, mechanized).

**Output**: a D8 decision-ready brief — GO, or NO-GO with each blocker
named and its remedy pre-drafted ("template not committed → commit it;
that commit IS the signature"). Code-rendered, relayed verbatim, LLM
points only.

## Boundary

Composes existing checks; detects nothing new. Never blocks anything
itself (it is a query — the gates it predicts remain the enforcement).
No proving-run-specific knowledge: run-#10-style prereqs that are
process-not-substrate (e.g. "envs refreshed tonight") stay in kickoff
prose; only substrate prereqs (parse/commit/skew/roots) live here.

## Sequencing

**Phase 1b** — beside the data-manifest verb (Phase 1a); both are small,
self-contained, registry +1 each. The manifest-drift disclosure line lands
only after 1a. Skill edit: `hpc-notebook-audit`'s prelude opens with the
preflight.

## Drift log

- 2026-07-07: written mid-run-#10 (Fable, pre-deadline); promoted from
  memory item R2 by user directive.
