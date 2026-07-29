# Branch: the audit will hand off to a submit interview

Read when this audit is the `/new-experiment-hpc` on-ramp — the drafted
source is headed for a submit interview after the audit passes. Skip
entirely for a standalone audit.

**Journal the audit-OPEN intent on the config seat.** Record the `goal` (the
free-text campaign goal the human typed) and `task_axes` (their free-text
names for what varies across tasks, e.g. `bucket`, `chunk`) via
`notebook-record` (`kind: "config"`) alongside the roots. This is the ONE durable seat
`audit-handoff` reads to draft the interview, so the intent stops living only
in chat.

- Record VERBATIM; an omitted goal or axis stays omitted — `audit-handoff`
  emits a placeholder, never an invented value.
- The `goal` / `task_axes` are authorship-locked fields elicited as free
  text the human types (the SKILL's authorship doctrine) — never composed,
  never a pre-filled option.

At audit close, `audit-handoff` drafts the interview from this seat; the
handoff brief is relayed like any other code render.
