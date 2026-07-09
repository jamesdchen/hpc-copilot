`/notebook-audit-hpc` is the **human-interview wrapper** around the `hpc-notebook-audit` skill — the audit prelude + loop that turns the user's analysis code into an audited percent-format `.py` the submit pipeline will accept. The slash gathers intent (audit id, template, scope tags), invokes the skill, and relays each code-rendered view VERBATIM for the user's typed sign-offs. It never resolves a decision and never interprets the audit content.

## The flow

1. **Parse `$ARGUMENTS`** — optional `template` path, existing analysis `source` path, `audit_id`, `source_roots` / `input_roots`.
2. **Elicit as free text — never pre-filled options** (a click carries no authorship the sign-off gate will accept):
   - `audit_id` — a slug the user authors; NEVER agent-invented.
   - the intent — which analysis computes the citable numbers. Audit that script, not the whole repo.
   - scope tags — the user's own words for what this tests. An empty answer is recorded as no tags and disclosed; an agent-invented tag is index poisoning.
   - the template `.py` if not stated — its slugs are the required section inventory. An uncommitted template is an unsigned template; committing it IS the signature.
3. **Invoke the skill.** It runs `audit-preflight` first (GO/NO-GO brief, relayed verbatim), drafts the percent-format source from the user's existing analysis code (assisted refactoring — the user reviews, they never hand-write the format), then drives lint → auto-clear → view → sign-off → status until `passed`.
4. **Sign or nudge.** Each `notebook-audit-view` render is relayed VERBATIM; the user signs a section with a typed utterance (`sign <slug> <their words>`, multi-slug OK) journaled via `append-decision`, or nudges — a nudge re-drafts and re-runs the loop from lint. Edits auto-revoke stale sign-offs; only what changed needs re-signing.

## Invocation

Invoke the `hpc-notebook-audit` skill via the Skill tool (only the fields the user pinned):

```
Skill("hpc-notebook-audit", {
  experiment_dir: <required — absolute path to the experiment repo>,
  template: <the template .py whose slugs are the inventory>,
  audit_id: <the user-authored slug>,
  source_roots: <if the user declared import roots>,
  input_roots: <if the user declared data roots>
})
```

`experiment_dir` is required. The skill drafts the `source` during its prelude; pass one only when the user points at an existing percent-format draft to resume.

## Relaying

- **Every view and brief is a code render — relay VERBATIM.** No paraphrase, summary, or gloss of the diff/assertions/flags ever enters the audit path.
- **Typed refusals are working-as-designed:** `render-stale` / `view_sha-mismatch` / `diff-token-missing` → re-run `notebook-audit-view`, re-sign. Never improvise around a gate.
- **No sign-off verb exists by design** — a section is signed through `append-decision` or not at all; do not hunt for one.
- **The audit gates the FINAL canonical run:** an interview declaring `audited_source` refuses at graduation (`SourceUnaudited`) until the audit is current; interim hack-loop runs stay ungated.

## Notes

- **Auto-clear is machine attention, sign-off is human attention.** An `auto_cleared` section spent no human judgment; never present it as a human ack.
- **Assertion-bearing sections auto-clear only on a journaled receipt** (`notebook-record-receipt`, or the `hpc-agent-notebook-render` plugin's `--execute --record_receipts`). Without the plugin in the env, keep sections assertion-light — show computed values, don't assert.
- **Honest lint always** — relay `findings` and `unverifiable_paths` as they are; masking a finding to clear a section is the posture the gate exists to defeat.
