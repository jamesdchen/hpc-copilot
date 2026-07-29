---
status: shipped
---
# Agent delegation — the recon and plan-relay boundary

**Status: SHIPPED (2026-07-27; reworked 2026-07-28 by user order — "the
context firewall is good, but I feel like the dynamic workflow can do
more").** The original doctrine re-admitted agents at ONE level: a subagent
as a **context firewall for read-only reconnaissance**, beside the execution
path. The 2026-07-28 rework adds a SECOND level — **plan-driven relay**,
rule 5 — available only inside a validated workflow plan, where the model
composes nothing. What separates the levels from the retired anti-pattern
is unchanged and is the load-bearing distinction of this page: **who
composes the invocation** — authored plan data, or a model. Prereq reading:
`docs/design/human-amplification-blocks.md` (the §6 deletion list),
`docs/design/philosophy-audit-2026-07.md` axis B5 (trusted display),
`docs/internals/principles/lifecycle-verdicts.md` (the relay-verbatim and
sign-off-authorship enforcement maps).

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

This is the ONLY level available to a freeform subagent — an `hpc-recon`
Task spawn, or any agent whose instructions are composed per-occasion by a
model. Rule 5's wider grant belongs exclusively to validated plans.

## 2. Locked: every read-and-sign and every model-composed act

A subagent NEVER:

- **composes a mutating, submit, scaffold, or workflow invocation of its
  own devising** — the `VerbKind` complement of rule 1, whenever the model
  chooses the verb or shapes its arguments. Deciding to act is the main
  session's, in the session the human is watching. (A plan-fixed relay of
  such a verb is not the subagent's act — it is the plan's; see rule 5 for
  the conditions under which that is legal.);
- **touches `append-decision` or any `y`/nudge rendezvous.** The rendezvous
  points are where a human's typed words become evidence; a model standing
  between the prompt and the human is precisely the laundering the
  utterance-log gates exist to refuse. This lock has no plan-relay
  exception: no `COMMANDS` template may name it, under any tier;
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
section sha against the code-written render file. Rule 5 leans on this
same fact from the other side: a plan-relayed `block-drive` tick that
reaches a greenlight gate is refused by `assert_greenlit_target` exactly
as an inline one would be — the gates do not know or care that a workflow
invoked the verb, which is why widening the relay surface required no gate
change either.

## 4. The retired level, named

The `hpc-worker` spawn transport is the recorded anti-pattern this doctrine
replaces: a model-pinned subagent living inside the execution path, driving
workflow verbs through `claude -p` from rendered worker procedures. It was
physically deleted in the §6 worker fence (`docs/design/human-amplification-blocks.md`
DELETE list: `_kernel/lifecycle/invoke.py`, `_kernel/lifecycle/run.py`,
`_kernel/extension/spawn_prompt.py`, the worker prompts, the `run` verb, and
`agents/hpc-worker.md`) after a proving run showed the path was still
reachable and taken by default — "it cannot be a trap if it is gone."

Naming it here is load-bearing. The distinction is not the word "agent",
and — since the rule-5 rework — it is not even which side of the execution
path the agent sits on. It is **who composes the invocation**: the worker
transport had a model reading rendered procedures and deciding what
workflow verbs to run, per-occasion, unreviewable before the fact. A rule-5
plan relay is the opposite on every axis: the command is an authored
`(namedInputs) => string` template in a reviewed file, validated before the
first agent dispatches, and the relaying agent is instructed to run exactly
it and nothing else. `agent_assets.py`'s installer comment, which recorded
"core ships none since the §6 worker removal", names `hpc-recon` as the
deliberate re-entry at the recon-only level.

## 5. Plan-driven relay — what a validated workflow plan may additionally do

A `kind: 'script'` step inside a validated workflow plan (shipped in
`src/hpc_agent/slash_commands/workflows/`, installed to
`<claude_dir>/workflows/`) may relay ANY
registry verb except the rule-2 locks — including `workflow`-kind verbs like
`block-drive`, `aggregate-check`, and `aggregate-run` — because in a plan
the model composes nothing: the command is a pure authored template, the
step's failure policy and schema are declared data, and `validatePlan()`
rejects the plan before anything dispatches. The determinism-boundary
principle applied one level up: an LLM relays a mechanized invocation, it
never derives one.

The conditions, all mechanized
(`tests/contracts/test_workflow_plan_delegation.py`):

- **`COMMANDS` templates only.** A mutating/workflow verb may appear as an
  `hpc-agent` invocation only inside a plan's `COMMANDS` section. `PROMPTS`
  templates — the text a model acts on freely — stay recon-only: any
  `hpc-agent <verb>` they name must be `query`/`validate`.
- **`append-decision` appears in no plan, ever.** Tier-3 lock, no
  exception; the checker refuses the plan.
- **Gates park, never pass.** A plan that drives `block-drive` stops when a
  tick returns `awaiting_decision` and RETURNS the brief pointer to the
  main session; the human's `y` is journaled inline as always, and the
  workflow resumes afterward. This is not a plan-author courtesy — the
  runtime enforces it (`assert_greenlit_target` refuses an ungreenlit
  block regardless of caller), so the plan rule merely makes the honest
  shape the declared one.

What rule 5 buys is the same thing rule 1 buys — footprint, not authority.
A campaign driven through a plan keeps every tick invocation, every
detached wait, and every probe out of the main session's context; the
session sees briefs at the gates and a structured result at the end,
exactly the surface the human needed anyway.

## Surfaces

- **`src/hpc_agent/slash_commands/agents/hpc-recon.md`** — the agent
  definition, the first core-shipped agent since the worker removal.
  Read-only by charter: its `tools:` grant excludes Write and Edit, its
  Bash grant is for `hpc-agent` query verbs only, and its body carries the
  rule-2 prohibitions including never paraphrasing a verbatim-relay render.
  Installed by the existing `agent_assets.py::_copy_asset_tree` `agents/`
  walk to `~/.claude/agents/` with zero machinery change. Freeform spawns
  stay at rule-1 scope; rule 5 does not apply to them.
- **`src/hpc_agent/slash_commands/workflows/`** — the validated plans
  (shipped in the wheel, installed by `agent_assets`; portable-plan/adapter
  contract in that directory's README). `campaign-recon.js` operates at
  rule-1 scope (every command query/validate); `campaign-run.js` and
  `queue-drain.js` exercise rule 5 (plan-relayed `block-drive` ticks that
  park at every gate — one run, and the whole ledger, respectively). Every
  plan declares its commands as a `RELAYS` table and renders them from it;
  `tests/contracts/test_workflow_plan_commands.py` materializes each row
  and PARSES it with the real argparse tree, and pins the `{ok, …, data}`
  envelope unwrapping — the execution-level half of the boundary the
  regex sweep below cannot see.
- **Per-skill `## Delegation (hpc-recon)` sections** — `hpc-submit`,
  `hpc-status`, `hpc-aggregate`, `hpc-campaign`, `hpc-notebook-audit` each
  carry one, in a mechanizable shape: one `- delegable:` bullet per
  delegable step naming its verbs, one `- locked:` bullet restating the
  locked set, and `Task` in the frontmatter `allowed-tools`. Per the
  branchpoint rule, those sections stay short — the rationale is here and
  is cited, not restated there.
- **`tests/contracts/test_agent_delegation_guidance.py`** — enforcement of
  the rule-1 surface: no `- delegable:` line may name a locked verb
  (resolved against the live registry, not a hand list); every skill with a
  Delegation section carries the `- locked:` restatement and the `Task`
  grant; `hpc-recon.md` exists, excludes Write/Edit, and states the
  never-paraphrase rule; and the checker helper demonstrably refuses a
  synthetic skill declaring `- delegable: append-decision`.
- **`tests/contracts/test_workflow_plan_delegation.py`** — enforcement of
  the rule-5 surface: every `hpc-agent <verb>` occurrence in a plan's
  `PROMPTS` resolves to `query`/`validate`; `append-decision` is refused
  anywhere in a plan; fire paths prove both refusals actually fire.

## Drift log

**2026-07-28 — rule 5 added (user-ordered rework: "the context firewall is
good, but I feel like the dynamic workflow can do more").** Rule 2's first
bullet re-scoped from "calls a mutating verb" to "composes a mutating
invocation of its own devising" — the distinction §4 always turned on, now
stated as the rule rather than an implication. Plan-relayed execution
(rule 5) is the new second level: `COMMANDS`-template invocations of any
verb except the rendezvous locks, gates parking to the main session,
enforcement in `tests/contracts/test_workflow_plan_delegation.py`. The
freeform-agent surface (`hpc-recon`, skill delegation sections) is
unchanged and stays at rule-1 scope; `tests/contracts/`
`test_agent_delegation_guidance.py` needed no edit, which is the evidence
the rework widened a different surface rather than weakening the old one.
