---
name: suggest-prelude-action
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent suggest-prelude-action [--experiment-dir <dir>]
  python: hpc_agent.cli.prelude_actions.suggest_prelude_action
exit_codes:
- 0: ok
- 1: user-error
---
# suggest-prelude-action

Answer "what is the next prelude step" **mechanically**, once, from the durable
substrates — instead of the agent walking it as prose every session ("is there an
audit? has it passed? is there an interview.json? is the pack bound *and* opted
in?"). The prelude (idea → S1) has exactly four genuine judgment points
(`docs/plans/prelude-chain-2026-07-30.md`): draft authoring, the typed sign-off,
the axis LLM tree behind `classify-axis-auto`'s escalation, and the human-owned
intent fields. Everything between them is deterministic, so it belongs in a verb.

This is the `suggest-setup-action` treatment applied to that chain: one **total**
priority ladder, expressed as ordered rules over the decision kernel
(`hpc_agent._kernel.decision.decide`) with a catch-all default, so it resolves
`decided_by="code"` and never escalates.

## The five substrates it reads

1. the notebook decision journal (`.hpc/notebooks/<audit_id>.decisions.jsonl`)
   plus `notebook-status`'s `passed` predicate;
2. the notebook-audit-config seat — either `interview.json`'s `audited_source`
   block or a journaled `notebook-audit-config` record;
3. the pack journal + `interview.json`'s `packs` opt-in, **and the integrity of
   that pair** (see below);
4. `.hpc/axes.yaml` presence + staleness (the stored `run_signature_sha` against
   the entry point's current one);
5. `interview.json` presence / `_materialized`.

## The ladder

First match wins. Every state maps to exactly one `action`.

| Rung | State | `action` |
|---|---|---|
| 0 | a substrate could not be read or parsed | `doctor` |
| 1 | the pack bind / opt-in pair disagrees | `pack-optin-repair` |
| 2 | an audit journal exists with no config seat | `notebook-record-config` |
| 3 | no durable seat names the audit's source/template | `notebook-status` |
| 4 | `passed` is false — N sections await sign-off | `notebook-audit-view` |
| 5 | the audit passed and no interview intent is recorded | `audit-handoff` |
| 6 | `interview.json` exists but carries no `_materialized` | `interview` |
| 7 | `axes.yaml` absent, missing the run's entry, or stale | `classify-axis` |
| 8 | nothing exists at all — cold start | `notebook-scaffold-template` |
| 9 | every substrate is settled | `submit-s1` |

Rung 2 precedes the sign-off rungs deliberately: the config seat is
immutable-per-audit and recording it late moves every `view_sha`, so a missing
seat must be fixed *before* more human attention is spent signing.

Returns `{rung, action, why, scaffold, findings, disclosures, substrates}`. The
agent branches on `action`, relays `why`, and invokes `scaffold`.

## The pack integrity pair — the named remedy

A pack bind and a `packs` opt-in are two independent records and nothing
reconciled them. `pack-status` iterates the **opt-in list**, so a pack that is
`bound` but absent from `interview.json`'s `packs` block is invisible to it: every
pack gate reads the pack as absent and silently passes. That is the 2026-07-30
live fumble, and it is now a named remedy —

> `pack rv bound but not opted in — add the packs entry`

— carrying the `packs` fragment to paste, with the `manifest` relpath **derived**:
every conventionally-named manifest under the experiment dir is parsed through
`state.pack.load_manifest` and accepted when its declared `name` matches *and*,
when the bind record carries a `manifest_sha`, its recomputed raw-bytes sha
matches. That makes the match an identity rather than a name coincidence (a
lab-vs-upstream copy shares the name but not the sha). An ambiguity the sha cannot
break is **disclosed**, never resolved by picking — a wrong relpath binds the
wrong pack root. The reverse direction (opted in, never bound) scaffolds
`pack-bind`.

## Two conditions this verb deliberately does NOT test

Applying the repo's "verify a guard can actually fire" rule
(`docs/internals/engineering-principles.md`) while building it moved two rungs:

* **Rung 5 is not "audit passed, no interview.json".** The only durable seat
  naming an audit's source/template is `interview.json`'s `audited_source` block,
  so a passed audit is never observable *here* without an interview.json (rung 3
  catches the seatless case first). The gap that wording meant is **intent**, so
  the rung fires on "passed + no `goal` + not materialized".
* **An audit is enumerated from either seat** — a journal file *or* an
  `audited_source` declaration. Journals-only silently missed the
  freshly-opened audit whose sections are all awaiting sign-off, which is exactly
  rung 4's case.

## Boundary posture

* **One definition.** `passed` is recomputed through the same reduction
  `notebook-status` uses (`state.notebook_audit.audit_module` + `PASSING_STATUSES`),
  never a journal-only proxy. When the source/template are not resolvable the verb
  says so and hands the predicate back to `notebook-status` rather than
  approximating it.
* **Never crashes.** Every substrate read is tolerant. An invalid `axes.yaml`, an
  unparseable `interview.json`, a journal of nothing but corrupt lines, a
  malformed `packs` block *or entry*, an `audited_source` naming an unparseable
  `.py` — each becomes a disclosed rung-0 finding. A malformed `packs` entry is
  treated as corrupt rather than skipped precisely because skipping it would
  recreate the fumble class above.
* **Rung 0 out-ranks rung 1.** Deciding over unknown state always loses to
  reporting that the state is unknown; the precedence is pinned by
  `tests/cli/test_prelude_actions.py::test_doctor_pre_empts_the_pack_repair`.
* **Advisory.** It suggests. It never journals, gates, consents, or mutates.

## Compose with

- **Predecessors**: none — this is the entrypoint primitive for the prelude, the
  sibling of `suggest-setup-action` at the submit boundary.
- **Successors**: whichever verb the chosen rung names — `doctor`, `pack-bind`,
  `notebook-record`, `notebook-status`, `notebook-audit-view`, `audit-handoff`,
  `scaffold-spec --verb interview`, `classify-axis`,
  `notebook-scaffold-template`, or `block-drive` for the settled case.
