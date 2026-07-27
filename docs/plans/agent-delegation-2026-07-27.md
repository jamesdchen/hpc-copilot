# Agent reincorporation — read-only recon delegation (plan)

**Status: PLAN (user-ordered 2026-07-27: "reincorporate agents on some level,
just not the same level that we were doing before"; implementation dispatched
as an agent swarm the same day).** The prior level — the §6 haiku-pinned
`hpc-worker` spawn transport, removed with the worker fence — put an agent
INSIDE the execution path. The new level puts agents only BESIDE it: a
subagent is a **context firewall for read-only reconnaissance** — the verbose
transcript (tool schemas, envelopes, render bytes) lives and dies in the
subagent; the main session receives a compact advisory brief. Cite
`path::symbol`. Drift log at foot.

## Settled decisions

### D1 — The delegation boundary (doctrine, one page)

New design doc `docs/design/agent-delegation.md` (frontmatter
`status: shipped` — it lands with the code in one change). The rules:

1. **Delegable: read-only reconnaissance.** Query verbs whose output the main
   session treats as ADVISORY input to its own next step: `doctor`,
   `net-triage`, `status-snapshot`, `read-decisions` (digest),
   `attention-queue`, `evidence-brief`, `notebook-draft-context`,
   `notebook-lint`, `dir-digest`, `worker-log-digest` and kin.
2. **Locked: every read-and-sign and every act.** A subagent NEVER: calls a
   mutating/submit/workflow verb; touches `append-decision` or any
   `y`/nudge rendezvous; relays (or summarizes) content the doctrine marks
   relay-VERBATIM — if the output would need to be relayed verbatim to the
   human, no agent sits between the code render and the human. For
   render-bearing verbs the subagent returns POINTERS + COUNTS (the
   `render_path`, the shas, the tallies), never a paraphrase of the render.
3. **Delegation never enters the trust chain.** A subagent's report is
   model-carried text — worth exactly as much to the gates as any other
   model text: nothing. The gates read journals, stores, and the utterance
   log; no gate change is needed or made. This is why the boundary is safe:
   a delegated recon cannot launder anything the main session couldn't.
4. **The retired level, named.** The `hpc-worker` spawn transport (an agent
   inside the execution path, haiku-pinned) is the recorded anti-pattern this
   doctrine replaces; `agent_assets.py`'s "core ships none since the §6
   worker removal" comment updates to name `hpc-recon` as the deliberate
   re-entry at the recon-only level.

### D2 — The `hpc-recon` agent definition

`src/hpc_agent/slash_commands/agents/hpc-recon.md` — the first core-shipped
agent since the worker removal. Frontmatter: `name: hpc-recon`,
`description:` (when to use: read-only hpc-agent reconnaissance),
`tools: Bash, Read, Grep, Glob` (NO Write/Edit — read-only by charter;
Bash is for `hpc-agent` query verbs only, stated in the body),
`model: sonnet` (recon is reading + brief-composing; revisit if briefs
degrade). Body charter: run only query/validate verbs; return a compact
brief of counts, states, shas, and `render_path`/brief-file POINTERS; never
paraphrase a verbatim-relay render; never call a mutating verb or
`append-decision`; never write files. The existing
`agent_assets._copy_asset_tree` `agents/` walk installs it to
`~/.claude/agents/` with zero machinery change; `pyproject.toml` package-data
already ships `slash_commands/agents/*.md`.

### D3 — Per-skill delegation sections

Each workflow skill (`hpc-submit`, `hpc-status`, `hpc-aggregate`,
`hpc-campaign`, `hpc-notebook-audit`) gains a short `## Delegation
(hpc-recon)` section in a MECHANIZABLE shape — one `- delegable:` bullet
per delegable step naming its verbs, one `- locked:` bullet restating the
locked set — plus `Task` added to the frontmatter `allowed-tools`. Delegable
content per skill follows D1 rule 1 (the parallel-prep back-half preflight,
status recon, draft-context digestion); the locked bullet always names
`append-decision`, the `y`/nudge rendezvous, sign-off/consent, and
verbatim relays. The branchpoint rule from the context-footprint plan
applies: the section is short; rationale lives in the doctrine doc, cited.

### D4 — Enforcement

`tests/contracts/test_agent_delegation_guidance.py`:

- every skill's `- delegable:` lines never name a LOCKED verb
  (`append-decision`, `scope-lock`/`scope-unlock`, the submit/aggregate/
  campaign mutating blocks, `notebook-auto-clear`, any `verb!=query/validate`
  primitive — resolved against the live registry, not a hand list);
- every skill with a Delegation section has the `- locked:` restatement and
  `Task` in `allowed-tools`;
- `hpc-recon.md` exists, its `tools:` exclude Write/Edit, and its body names
  the never-paraphrase-a-verbatim-render rule;
- fire-path: a synthetic skill with `- delegable: append-decision` is
  refused by the checker helper.

### D5 — Records

CHANGELOG entry under Unreleased; `agent_assets.py` comment update (D1.4);
no wire/schema change, no regen beyond none-expected (verify with
`regen_all --check`).

## Swarm dispatch (user-authorized; file-disjoint)

Four implementers in parallel — (A) doctrine doc; (B) agent definition +
`agent_assets.py` comment; (C) the five SKILL.md sections + frontmatter;
(D) contract test + CHANGELOG — then central verification (ruff, lint
gauntlet incl. the skills lints, regen check, full pytest) by the
orchestrating session, which owns the commit.

## Drift log

(open)
