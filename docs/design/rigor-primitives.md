---
status: shipped
---
# Design: rigor primitives — locked evidence scopes + a look ledger

Status: **SHIPPED** (rigor-primitives wave, tasks T1–T6). The substrate is
`state/scopes.py`; the agent surface is the `scope-lock` / `scope-status` verbs
(`ops/decision/scope_lock.py`); the reduction gate is `ops/scope_gate.py`, wired
synchronously at the S4 pre-detach seam (`ops/submit_blocks.py`) and inside the
one reduction seam (`ops/aggregate_flow.py`); the unlock is a human act behind
the authorship gate (`ops/decision/journal.py::_assert_unlock_authorship`). This
document is the decision record — WHY the shape is what it is, and the
alternatives rejected. Facts (symbol names, defaults) cite `path::symbol`; where
this doc and the code disagree, the code and its enforcement-mapped tests win.

## Problem

A caller wants to *reserve* a body of evidence — a held-out split, an embargoed
test set, a pre-registered comparison — so the framework refuses to reduce over
it until the human deliberately re-opens it. The classic failure this closes is
silent multiplicity: a held-out set looked at "just once more" every iteration
until the number that clears a bar is noise. The framework cannot police the
statistics (it never learns what a metric *means*), but it CAN make the act of
looking **countable** and make a reserved look **refuse loudly** — mechanism
where mechanism is possible, judgment left to the human above.

The tension: the framework must do this WITHOUT acquiring any vocabulary about
what the caller's evidence means. It is experiment-agnostic by construction
(see `docs/internals/engineering-principles.md`, "substrate, not semantics").
A "holdout" primitive that knew what a holdout *was* would be exactly the
semantics leak the boundary test forbids.

## The boundary the feature crystallized

Designing this forced a sharp formulation of what core's agnostic surface
actually is. Core's operations over caller content are exactly four:

- **IDENTITY** — is this the same run / lineage / command? (`run_id`, `cmd_sha`,
  `lineage_root`)
- **ORDERING** — which record is newest? (the newest-first lock/unlock scan)
- **COMPARISON** — is this tag equal to that tag? is this the same `(scope,
  run_id)` pair?
- **COUNTING** — how many looks, across how many distinct lineages?

Anything decomposable into IDENTITY, ORDERING, COMPARISON, and COUNTING over
**opaque caller content** is core. Anything that requires core to NAME what the
content means — "this scope is a holdout", "N looks is too many", "apply a
Bonferroni correction" — is a **domain pack** that sits above core. The scope
substrate stays strictly on the core side of that line: it counts looks, it
never reads what a look found; it compares tags, it never interprets one. The
domain semantics (what a holdout is, the statistics of multiplicity, the
correction to apply) live above, where a human or a domain pack owns them.

## Decisions

### Lock state is an append-only journal, not a state file

A scope's lock state lives as ordinary decision records under a THIRD
`scope_kind` — `"scope"` — in the decision journal (`state/scopes.py`
`record_lock`, routed through `decision_journal.append_decision` with
`scope_kind="scope"`; the sidecar decision journals are `run` and `campaign`).
The current state is a **reduction over the log**: `is_scope_locked` scans the
records newest→oldest and the first one whose `resolved.scope_action` is `lock`
or `unlock` decides (the same newest-first precedence idiom
`ops/block_gate.assert_greenlit_target` uses).

WHY a journal, not a `locked: true` state file: **auditability, and unlock never
erases history.** A boolean file overwritten on unlock loses the fact that the
scope was ever locked, when, and why. The append-only log means `lock` then
`unlock` reads unlocked while BOTH records remain on disk — the full history of
every reservation and every deliberate re-opening survives, which is precisely
the record a "did you peek at the holdout?" audit needs. `scope-status` surfaces
the `lock_history_len` (`ops/decision/scope_lock.py::_lock_history_len`) so the
count of state transitions is itself visible.

### The sidecar is the tag attachment point

A run carries its scopes on the per-run sidecar's `scopes` field
(`state/runs.py` — `list[str] | None`, in the v2 field set beside `data_sha` /
`env_hash`, written verbatim, defaulted `None`). The gate reads them locally
off the sidecar (`ops/scope_gate.py::assert_scopes_unlocked` →
`read_run_sidecar`), never over SSH. WHY the sidecar: it is the run's existing
identity record, already local, already the place `cmd_sha` / `lineage`
tooling reads; attaching the tag there means the gate is a pure local read that
can run before ANY network work, and a scope-less run's sidecar stays
byte-identical (the field is only written when non-`None`, like every other v2
field).

### Tags are opaque — the NO-VOCABULARY rule

A tag is validated for **slug shape only** (`state/scopes.py::validate_tag`,
reusing `state.runs._RUN_ID_RE == ^[A-Za-z0-9._\-]+$` — the same filesystem-safe
class `RunIdStrict` / `CampaignId` pin, so a tag is a safe path segment under
`.hpc/scopes/`). Shape is the ONLY constraint. The framework NEVER:

- names or defaults a tag — there is no built-in `"holdout"` / `"test"` /
  `"embargo"`; core supplies no vocabulary a caller must match against,
- checks a tag against a role vocabulary,
- makes a scope `AUTO_RESOLVABLE` — a scope has no safe default the framework
  could invent.

The last point is the load-bearing one. **An invented tag is the fabrication
class.** It is the exact sibling of the fabricated-`task_generator` failure
(engineering-principles, "a guard the LLM itself satisfies is not a guard"): if
core defaulted or auto-resolved a scope, the agent could conjure a scope the
human never reserved — or, worse, quietly *not* reserve one the human meant to.
The tag must originate with the caller, always. Core records it and counts over
it; it never authors one.

### Looks are counted BEFORE the reduction's own look, and deduped

A *look* is a run whose results were reduced against a scope. On a successful
reduction, `aggregate_flow._record_scope_looks` does two steps per tag, in this
order:

1. **snapshot** `count_prior_looks` — the counts returned are PRIOR **by
   construction**, because this run's own look is not yet on the ledger;
2. **append** `record_look` — deduped on `(scope, run_id)`
   (`state/scopes.py::record_look` read-before-append: a second look at the same
   run under the same scope is a no-op returning `None`).

WHY prior-by-construction: the number a caller sees for THIS reduction must be
"how many times was this scope looked at BEFORE now" — counting the current look
into its own report is an off-by-one that misstates the multiplicity. Snapshot
then append makes prior-ness structural, not a subtraction that could rot.

WHY `(scope, run_id)`-deduped: aggregate-flow is idempotent and re-runnable (a
harvest retry, a replay). Without dedup, every replay of the same run would
inflate the look count — a bookkeeping artifact reading as real multiplicity.
Dedup makes a replay re-report the SAME prior counts and never double-count.

The ledger stores **IDENTITY only** — `run_id`, `cmd_sha`, `lineage_root`,
`reducer_block` — **NEVER a metric value** (`state/scopes.py::record_look`).
WHY: a metric in the ledger would tempt interpretation, and interpretation is
the domain-pack's job, not core's. Identity is all a caller needs to COUNT.
`count_prior_looks` returns two plain integers — `prior_looks` (total records)
and `distinct_lineages` (distinct `lineage_root` values, so several
supersession-chained reruns of the SAME experiment collapse to one lineage).
`lineage_root` walks the run's `supersedes` back-links to the chain root
(cycle-guarded), so "N looks across M genuinely-distinct experiments" is
answerable without core consulting a single metric.

### The unlock is a human act with NO agent affordance

Locking is the SAFE direction — it only ever restricts — so `scope-lock`
(`ops/decision/scope_lock.py`) carries no authorship bar and routes straight
through `record_lock`. Unlocking RELAXES the restriction, re-opening the scope
for another look, so it faces the **human-authorship bar**
(`ops/decision/journal.py::_assert_unlock_authorship`), tiered exactly like the
fabricated-`task_generator` gate:

- a **bare ack cannot unlock** — a `y` / click (`_is_bare_ack`) carries no
  authored rationale; relaxing a scope must be a deliberate human statement;
- with the harness utterance log present (the shared lock tier,
  `_harness_human_texts`), the rationale's word tokens must derive from a logged
  human utterance, not the agent-relayed `response`;
- the unlock is journaled permanently (`block="scope-unlock"`,
  `resolved.scope_action="unlock"`), and the block convention is enforced both
  directions (a `scope-unlock` block is scope-only; a scope unlock MUST use that
  block — a laundered unlock cannot hide under the lock block).

Crucially, there is **NO unlock verb** — the scope module ships `scope-lock` and
`scope-status` only. The unlock is deliberately NOT in any chain, `next_block`,
or skill sequence. WHY: an unlock that appeared in a driver chain could be
reached by a bare greenlight, which is exactly the laundering the authorship
bar exists to refuse. Removing the affordance (engineering-principles, "the
enforcement is removing the affordance, not adding prose") is the lock; the
authorship gate is the backstop for the one path that remains
(`append-decision`).

### The reduction gate: one definition, two synchronous call sites

`ops/scope_gate.py::assert_scopes_unlocked` is the single gate. It is called
from TWO seats:

- **synchronously, before the S4 detach** (`ops/submit_blocks.py`, before
  `aggregate.run_id` is handed to the detached harvest worker), and
- **inside the one reduction seam** (`ops/aggregate_flow.py`, before any
  combine / pure-API / SSH work).

WHY both: the S4 seat makes the refusal fire SYNCHRONOUSLY in the parent, where
a "you spent a reserved look" error is visible at the human boundary — never
buried in a detached child's log where nobody looks. The aggregate-flow seat is
the true precondition on reduction itself: it catches a scope locked in the
window between the parent check and the child's reduce, AND covers every OTHER
caller of aggregate-flow (campaign loops, direct invocation) that never passes
through S4. ONE definition, TWO call sites — defense in depth, same gate→detach
ordering discipline the greenlight gate keeps. Fail-safe by construction: a
missing sidecar or a scope-less run PASSES silently (`assert_scopes_unlocked`
returns early), so the gate can never false-trip a run that carries no scopes,
and a scope-less run reduces byte-identically to before the feature.

### Harvest-guard treats a locked scope as a clean skip, not a fault

The guaranteed terminal harvest (`ops/monitor/harvest_guard.py`) catches
`errors.ScopeLocked` distinctly from every other exception: it records a CLEAN
SKIP (`harvest_skipped_reason="scope_locked"`) — never `harvest_ok:false`, never
an anomaly, and it skips the error sweep (nothing was harvested to sweep). WHY:
a lock is **deliberate human state, not a harvest failure**. The scope gate
refused the reduction on purpose. Painting a human's reservation red forever —
or firing an anomaly that pages someone — would train the human to treat their
own deliberate lock as a bug. The skip is the correct, quiet outcome.

### Domain semantics live above core

What a scope MEANS (a holdout, an embargo), the STATISTICS of looking (is N
looks too many?), and the CORRECTIONS to apply (Bonferroni, pre-registration
checks) are all **above** core. Core surfaces the raw material — the lock state
and two plain look counts (`scope-status`, and `aggregate_flow`'s
`scope_looks` copied verbatim into the S4 brief) — and stops. A human or a
domain pack reads those counts and decides what they mean. This is the boundary
formulation above, applied: core does IDENTITY / ORDERING / COMPARISON /
COUNTING; naming and judging the content is someone else's layer.

## Alternatives rejected

- **A `locked: true` state file instead of a journal.** Loses the audit trail —
  unlock would erase the fact of the lock, its timestamp, and its reason. The
  append-only log keeps every reservation and re-opening on disk; the `did you
  peek?` audit needs exactly that history. Rejected.
- **A named `holdout` / `test` scope vocabulary.** A framework that knows what a
  holdout IS has crossed the substrate/semantics line (engineering-principles
  Q1). It would also invite core to invent or default a tag — the fabrication
  class. Rejected: tags are opaque, caller-authored, slug-shape-only.
- **Storing the metric in the look ledger.** A metric on the ledger tempts
  interpretation; core would be one `if look.value > threshold` away from a
  domain judgment it must not make. Identity is sufficient to COUNT. Rejected.
- **Counting the current look into its own report** (append then snapshot).
  Off-by-one that overstates multiplicity. Snapshot-then-append makes prior-ness
  structural. Rejected.
- **An `unlock` verb / an unlock step in a driver chain.** Any reachable-by-chain
  unlock can be reached by a bare greenlight — the laundering the authorship bar
  refuses. No affordance is the lock; the authorship gate backstops the one
  remaining path. Rejected.
- **Scopes as `AUTO_RESOLVABLE` with a safe default.** A scope has no safe
  default core could invent without fabricating a reservation the human never
  made (or silently dropping one they did). Required-caller, always. Rejected.
- **Gating only inside aggregate-flow (single seat).** The refusal would surface
  only in the detached harvest child's log, invisible at the human boundary.
  The S4 synchronous seat plus the aggregate-flow seat is the defense-in-depth
  answer. Rejected the single-seat version.

## Enforcement

The two rows this feature adds to the newest enforcement-map table in
`docs/internals/engineering-principles.md` pin: (A) the gate fires at both seats,
scope-less runs reduce byte-identically, and the look counts stay prior +
deduped; (B) the unlock faces the human-authorship bar, is journaled
permanently, and has no agent affordance. The cited tests are the normative copy
— this doc is the WHY.
