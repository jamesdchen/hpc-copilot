`/new-experiment-hpc` is the **idea→computation on-ramp**: the user arrives with the beginnings of an idea; this slash gathers what only they can supply, invokes the `hpc-notebook-audit` skill to draft and audit the analysis source, and on `passed` hands off to `/submit-hpc` or `/campaign-hpc` for the cluster compute. The slash elicits and relays; the skill owns the loop mechanics; it never resolves a decision.

## The flow

1. **Parse `$ARGUMENTS`** — the idea seed (free prose), plus any of: experiment repo path, `template`, existing `source`, `audit_id`, `source_roots` / `input_roots`.
2. **Elicit as free text — never pre-filled options** (a click carries no authorship):
   - what the experiment computes and **which numbers will be citable** — that analysis script is what gets audited, not the whole repo;
   - the experiment repo (`experiment_dir`) — **COMPOSE, don't ask** (run-#12 finding 2): when the invoking session's cwd git root carries experiment markers (an `interview.json` or a `.hpc/` tree), that root IS the experiment dir — use it and DISCLOSE the composed value in your restatement ("experiment repo = this repo"). Ask the open absolute-path question ONLY when the cwd carries no experiment markers at all (a genuinely fresh/foreign location), or the user's seed names a different path;
   - `audit_id` — a slug the user authors;
   - scope tags — the user's own words for what this tests (empty = recorded as no tags);
   - the template `.py` — but **when a domain pack is bound, do NOT ask for the template AT ALL** (2026-07-10 ruling, CONVERSION 2 — "prose cannot be load-bearing"; run-#12 finding 1: an open question invites the wrong answer — a legacy `specs/…run10.py`, an unsigned spec — when the ACTIVE audit template is already prepared). The `interview` verb COMPOSES the default from the bound pack's `audit_template` seam IN CODE (preferring the program pack over the domain skeleton) and DISCLOSES it in the persisted record (`interview.json._materialized.composed_defaults`) — silently, never brought to human attention. You neither ask nor hand-derive the path. Only ask the open "template `.py`, if one exists" question when NO pack is bound;
   - **the compute shape**, if cluster fan-out is intended — what varies across tasks (the task axes, e.g. bucket × chunk) and roughly how many of each. One question now; it becomes the `task_generator` at handoff.
3. **Invoke the skill** with the resolved fields. It runs the preflight, drafts, and drives the audit loop; relay each of its code renders VERBATIM and translate the user's `y` / `sign <slug> ...` / nudge. Refusal remedies, auto-clear, and receipts are the skill's business — do not re-derive them here.
4. **Hand off to compute.** On `passed`, run `audit-handoff`, confirm its draft, and pass it to the interview (via the `hpc-wrap-entry-point` skill). `audit-handoff` projects the durable audit records — the journaled goal + task-axes intent, the config, and an AST scan of the source (entry point, `$HPC_RESULT_DIR` writes) — into a DRAFT `InterviewSpec` with explicit placeholders for anything it will not guess; you fill the placeholders and confirm, never re-derive the mapping by hand. The interview verb materializes `tasks.py` + `interview.json` — NEVER hand-edit tasks.py. Then `/submit-hpc` (single run) or `/campaign-hpc` (sweep).

## Invocation

Invoke the `hpc-notebook-audit` skill via the Skill tool (only the fields the user pinned):

```
Skill("hpc-notebook-audit", {
  experiment_dir: <required — absolute path>,
  template: <if one exists>,
  audit_id: <the user-authored slug>,
  source_roots: <if declared>,
  input_roots: <if declared>
})
```

The skill drafts the `source` during its prelude; pass one only to resume an existing draft.

## Notes

- **The pipeline is the plan — do not enter plan mode or hand-explore the repo.** The verbs do discovery; freestyled exploration is the improvisation class this surface exists to kill.
- An un-onboarded repo is onboarded by the submit interview at handoff — do not detour into onboarding before the audit.
