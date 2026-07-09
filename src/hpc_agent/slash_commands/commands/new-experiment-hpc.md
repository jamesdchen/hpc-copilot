`/new-experiment-hpc` is the **idea→computation on-ramp**: the user arrives with the beginnings of an idea; this slash gathers what only they can supply, invokes the `hpc-notebook-audit` skill to draft and audit the analysis source, and on `passed` hands off to `/submit-hpc` or `/campaign-hpc` for the cluster compute. The slash elicits and relays; the skill owns the loop mechanics; it never resolves a decision.

## The flow

1. **Parse `$ARGUMENTS`** — the idea seed (free prose), plus any of: experiment repo path, `template`, existing `source`, `audit_id`, `source_roots` / `input_roots`.
2. **Elicit as free text — never pre-filled options** (a click carries no authorship):
   - what the experiment computes and **which numbers will be citable** — that analysis script is what gets audited, not the whole repo;
   - the experiment repo (`experiment_dir` — required, absolute path);
   - `audit_id` — a slug the user authors;
   - scope tags — the user's own words for what this tests (empty = recorded as no tags);
   - the template `.py`, if one exists;
   - **the compute shape**, if cluster fan-out is intended — what varies across tasks (the task axes, e.g. bucket × chunk) and roughly how many of each. One question now; it becomes the `task_generator` at handoff.
3. **Invoke the skill** with the resolved fields. It runs the preflight, drafts, and drives the audit loop; relay each of its code renders VERBATIM and translate the user's `y` / `sign <slug> ...` / nudge. Refusal remedies, auto-clear, and receipts are the skill's business — do not re-derive them here.
4. **Hand off to compute — pass the resolved spec, re-elicit nothing.** On `passed`, this flow already holds everything the submit interview needs: the `goal` (the idea seed), the entry point (the audited source — this flow authored it), the `task_generator` (the compute shape from step 2 — the draft wrote that loop), `audited_source` (the audit_id just marked passed), and `summary_artifact` (the exact file the source writes). Invoke the `hpc-wrap-entry-point` skill with that fully-resolved spec — the interview verb still runs and still materializes `tasks.py` + `interview.json` (the provenance seat; NEVER hand-edit tasks.py to "add" the new tasks), it just has nothing left to ask. Then `/submit-hpc` (single run) or `/campaign-hpc` (sweep); graduation gates on the current audit; interim hack-loop runs stay ungated by design.

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
