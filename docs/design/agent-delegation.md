---
status: shipped
---
# Agent delegation — the recon-only boundary

**Status: SHIPPED (2026-07-27, landed with the code in one change; ordered
the same day — "reincorporate agents on some level, just not the same level
that we were doing before").** The prior level put an agent INSIDE the
execution path and was deleted with the §6 worker fence. This level puts
agents only BESIDE it: a subagent is a **context firewall for read-only
reconnaissance** — the verbose transcript (tool schemas, envelopes, render
bytes) lives and dies inside the subagent, and the main session receives a
compact advisory brief. Prereq reading: `docs/design/human-amplification-blocks.md`
(the §6 deletion list), `docs/design/philosophy-audit-2026-07.md` axis B5
(trusted display), `docs/internals/principles/lifecycle-verdicts.md` (the
relay-verbatim and sign-off-authorship enforcement maps).

## 1. Delegable: read-only reconnaissance

A step is delegable when its output is ADVISORY input to the main session's
own next step — a fact the session would otherwise gather by hand, whose
only consumer is the session's own planning. The delegable set is the
query/validate surface of the CLI, `_kernel/registry/primitive.py::VerbKind`
values `query` and `validate` resolved against the live registry rather than
any hand-maintained list. In practice, and named here so the skills can cite
one place:

`doctor`, `net-triage`, `poll-detached`, `read-decisions` (digest),
`attention-queue`, `evidence-brief`, `notebook-draft-context`,
`notebook-lint`, `dir-digest`, `worker-log-digest`, and kin.

What the delegation buys is footprint, not authority. The subagent pays the
tokens for envelopes and greps; the session pays only for the brief. Nothing
downstream is permitted to be *truer* because a subagent said it — see rule
3.

## 2. Locked: every read-and-sign and every act

A subagent NEVER:

- **calls a mutating, submit, scaffold, or workflow verb** — the
  `VerbKind` complement of rule 1. Acting is the main session's, in the
  session the human is watching;
- **touches `append-decision` or any `y`/nudge rendezvous.** The rendezvous
  points are where a human's typed words become evidence; a model standing
  between the prompt and the human is precisely the laundering the
  utterance-log gates exist to refuse;
- **relays — or summarizes, or paraphrases — content the doctrine marks
  relay-VERBATIM.** If an output would have to reach the human verbatim,
  no agent sits between the code render and the human. Full stop. This is
  axis B5 applied to delegation: a code-authored render is trusted display
  exactly because no model retyped it, and a subagent's retyping is a model
  retyping it.

For render-bearing verbs the subagent returns **POINTERS + COUNTS** — the
`render_path`, the shas (`view_sha`, `section_sha`, `cmd_sha`), the tallies —
and never a prose stand-in for the render. The main session then reads the
render itself, from disk, and relays it verbatim as it always did. A pointer
is safe to carry through a model because it is falsifiable at the
destination; a paraphrase is not.

## 3. Delegation never enters the trust chain

A subagent's report is model-carried text, and model-carried text is worth
exactly as much to the gates as any other model text: **nothing.** The gates
read journals, stores, and the utterance log — never a transcript. No gate
change is needed for this doctrine and none is made.

This is not a caveat; it is the reason the boundary is safe. Because a
delegated recon cannot launder anything the main session could not have
laundered itself, the delegation decision is a footprint decision and not a
security decision. A subagent that reports "the sign-off was given" changes
nothing: `append-decision`'s authorship gate still demands a bound or
forensic utterance record, and finds none. A subagent that reports "the
render is current" changes nothing: the T8 gate still recomputes the
section sha against the code-written render file. The delegable set of rule
1 is drawn where it is *because* the locked set of rule 2 has no gate
consequence to buy — not because the gates are watching for subagents.

## 4. The retired level, named

The `hpc-worker` spawn transport is the recorded anti-pattern this doctrine
replaces: a model-pinned subagent living inside the execution path, driving
workflow verbs through `claude -p` from rendered worker procedures. It was
physically deleted in the §6 worker fence (`docs/design/human-amplification-blocks.md`
DELETE list: `_kernel/lifecycle/invoke.py`, `_kernel/lifecycle/run.py`,
`_kernel/extension/spawn_prompt.py`, the worker prompts, the `run` verb, and
`agents/hpc-worker.md`) after a proving run showed the path was still
reachable and taken by default — "it cannot be a trap if it is gone."

Naming it here is load-bearing. The distinction between the two levels is
not the word "agent"; it is which side of the execution path the agent sits
on. `agent_assets.py`'s installer comment, which recorded "core ships none
since the §6 worker removal", now names `hpc-recon` as the deliberate
re-entry at the recon-only level — so the next reader does not have to
reconstruct whether the fence was breached or extended.

## Surfaces

- **`src/hpc_agent/slash_commands/agents/hpc-recon.md`** — the agent
  definition, the first core-shipped agent since the worker removal.
  Read-only by charter: its `tools:` grant excludes Write and Edit, its
  Bash grant is for `hpc-agent` query verbs only, and its body carries the
  rule-2 prohibitions including never paraphrasing a verbatim-relay render.
  Installed by the existing `agent_assets.py::_copy_asset_tree` `agents/`
  walk to `~/.claude/agents/` with zero machinery change.
- **Per-skill `## Delegation (hpc-recon)` sections** — `hpc-submit`,
  `hpc-status`, `hpc-aggregate`, `hpc-campaign`, `hpc-notebook-audit` each
  carry one, in a mechanizable shape: one `- delegable:` bullet per
  delegable step naming its verbs, one `- locked:` bullet restating the
  locked set, and `Task` in the frontmatter `allowed-tools`. Per the
  branchpoint rule, those sections stay short — the rationale is here and
  is cited, not restated there.
- **`tests/contracts/test_agent_delegation_guidance.py`** — the enforcement.
  No `- delegable:` line may name a locked verb (resolved against the live
  registry, not a hand list); every skill with a Delegation section carries
  the `- locked:` restatement and the `Task` grant; `hpc-recon.md` exists,
  excludes Write/Edit, and states the never-paraphrase rule; and the checker
  helper demonstrably refuses a synthetic skill declaring
  `- delegable: append-decision`.

## Drift log
