# The attention queue — status-snapshot v2, design + implementation plan

**Status: IMPLEMENTED (2026-07-07).** Landed across Wave A (promotions), Wave B
(the collectors + wire + render), and Wave C (the verb + snapshot embed + skill
+ enforcement), plus the **D2 REVISED — leverage-primary ordering** (user,
2026-07-08). The module path deviated from the plan (top-level `ops/attention_*`
files, not an `ops/attention/` package) — see the **drift log** at the foot of
this document. Cite `path::symbol`, never line numbers. Sibling projection built
concurrently: `docs/design/run-story.md` (the per-run timeline; this document is
the fleet-wide ordering, that one is the single-run narrative — both pure
ordering/identity projections, the "Related, planned separately" pair from
`docs/design/notebook-audit.md`).

## Product intent

The human-amplification thesis made concrete: **the system routes the scarce
resource — human attention — instead of dumping state.** Today the overnight
return-to-desk read is `status-snapshot` (what is running where, what changed
since I last looked) plus `doctor` (stalled drivers, dead workers) plus
whatever the human remembers to ask about campaigns, audits, and scopes. Each
surface is honest but none *orders* the morning: the human triages by
scrolling.

The attention queue is the fleet-wide digest ordered by
**needs-your-verdict-first**: every place in the system where a human action
is the blocking edge — pending greenlights, committed-but-unadvanced
decisions, anomaly briefs, campaign completion briefs, unsigned/stale
notebook-audit sections, dead detached workers — collected across every run,
campaign, and audit, sorted by a deterministic code-computed rule, rendered
as a deterministic markdown digest. Pure ordering/identity projection
(`ops/relay_render.py` posture): **code computes the queue; no LLM
prioritization prose anywhere in the path.**

What it is NOT: a dashboard of everything (the snapshot already digests
state), a scheduler (it never advances anything), or a store (the queue is
recomputed on every read, never persisted).

## Architecture decisions (settled)

### D1 — the item model: identity + class + evidence pointer, never a score

A queue item is:

```
{
  kind:        <opaque string — "greenlight-unadvanced", "run-parked",
                "run-stalled", "run-anomaly", "dead-worker",
                "campaign-pending", "audit-section-unsigned",
                "audit-section-stale", "alert", "ssh-circuit-open">,
  class:       "blocked" | "verdict" | "informational"   (D2),
  subject:     {scope_kind: run|campaign|scope|notebook, scope_id, block?},
  experiment_dir: <str — which experiment this item belongs to (fleet mode)>,
  cluster:     <str | null — where, when the subject has one>,
  since:       <ISO ts — when this item's condition began, from the SOURCE
                record (awaiting_since, last_tick_at, decision ts, alert ts);
                null when the source carries no timestamp>,
  action:      <the source predicate's OWN drafted proposal/note string
                (doctor's StalledRunProposal.proposal, ParkedRunNote.note,
                the anomaly recommendation DATA) — the queue NEVER authors
                one; absent when the source drafts none>,
  evidence:    <the source's own structured dict, passed through opaque>,
}
```

Rationale, recorded: the subject vocabulary is exactly the decision-journal
scope kinds (`state/decision_journal.py::SCOPE_KINDS`) — the queue introduces
ZERO new domain vocabulary. There is **no urgency score field**: a numeric
urgency without a defined ordering rule is the fabrication class (a number
the code cannot justify invites the LLM to re-rank by it). Priority is
expressed only as the D2 class plus position in the D2 total order — both
recomputable from the record, neither asserted.

### D2 — the ordering rule (the load-bearing decision)

Deterministic, code-computed, a **total order** so the render is
byte-reproducible for a given fleet state:

1. **Class `blocked` first** — the human's action (or a trivial re-arm) is
   the only thing between the fleet and forward progress, and *no judgment
   is pending*: a committed `y` the driver never consumed
   (`greenlight-unadvanced`), a stalled driver (`run-stalled`), a dead
   detached worker with no recorded terminal (`dead-worker`). Rationale:
   these are pure wall-clock waste — the decision was already made or none
   is needed; every minute unread is compute or harvest lost. They also have
   the cheapest unit cost (a one-line re-arm), so front-loading them clears
   the board before the judgment work starts.
2. **Class `verdict` second** — the system is healthy and parked, waiting on
   genuine human judgment: a run parked at a y/nudge boundary
   (`run-parked`), a failed/abandoned run with its recommendation DATA
   (`run-anomaly`), a campaign whose latest journaled touchpoint awaits a
   response (`campaign-pending` — covers both the completion brief and an
   anomaly brief), an unsigned notebook-audit section blocking graduation
   (`audit-section-unsigned`). Rationale: these are the queue's namesake —
   verdicts only the human can give — but nothing rots while they wait the
   way a dead harvest worker's window does.
3. **Class `informational` last** — no verdict required; awareness only: a
   stale human sign-off (`audit-section-stale` — the T6 vocabulary's
   informational state; it also appears as `audit-section-unsigned`'s cause
   when a gate is actually blocked, so it never masquerades as actionable),
   unacknowledged watchdog alerts (`alert`), open SSH circuits
   (`ssh-circuit-open`).

Within a class: **oldest `since` first** (the longest-waiting item has
absorbed the most staleness risk; `null` `since` sorts last within its
class). Final tiebreak for a total order: `(kind, subject.scope_id)`
lexicographic. No timestamps compared across sources are ever *interpreted*
— age is ordering input only, never rendered as a judgment ("URGENT").

**Caller-overridable, the T12 `attention_order` precedent**
(`ops/notebook/audit_view.py::build_audit_view`'s `attention_order`
semantics, reused verbatim): the spec takes `class_order: list[str] | None`
— listed classes first in the given order, unknown names ignored, unlisted
classes keep the default order after them. Default = the built-in
`blocked, verdict, informational`. The override is the CLASS sequence only;
the within-class rule is fixed (a caller re-ranking individual items is a
caller doing prioritization prose — the affordance is deliberately absent).
Rationale for overridability at the class grain: the default encodes a
policy judgment ("waste before judgment") that a real morning may invert
("verdicts first, I'll re-arm after coffee"); making it data keeps that
disagreement out of code forks, exactly as T12 did for section order.

**D2 REVISED (user, 2026-07-08) — leverage-primary ordering.** The queue is
less an overnight digest than a STANDING TODO spanning weeks while the
human bootstraps trustworthy blocks one on top of another. The primary
sort key is therefore **LEVERAGE = computed unblock fan-out**: for each
item, the count of pending downstream subjects that become actionable when
this one verdict clears, walked over dependency edges the journals ALREADY
encode — a committed-unadvanced greenlight blocks its whole run; an
unsigned section blocks its module's `passed`, which blocks the graduation
gate, which blocks every **pending** (non-terminal, non-superseded)
run/campaign whose sidecar `audited_source` echo names that audit (F4 —
already-graduated/terminal runs are historical usage, not pending fan-out); a
campaign-pending verdict blocks the campaign's remaining runs. This stays inside the no-fabrication boundary because
fan-out is COUNTED from record structure, never scored: where no encoded
edge exists the fan-out is 0 and the item falls through to the class
order. Full order: **fan-out descending → class (blocked, verdict,
informational) → oldest `since` → (kind, scope_id)**. The class-grain
`class_order` override survives as the tiebreak-level override; a caller
override of the fan-out computation itself is deliberately absent (that
would be prioritization prose). The registration kernel (planned
separately) makes fan-out truer over time as prerequisite chains become
explicit attestations — the walk gains edges, never opinions.

**Delivery de-scoped (same user decision):** the queue is a PULL surface
(the verb + the snapshot embed) read repeatedly over days/weeks; an item
persists — recomputed, with its age — until the human clears its subject.
Cron/notification delivery is a later convenience, not architecture, and
is deliberately out of scope for this design. The no-mutable-state flag
does the load-bearing work here: nothing is marked read, so a standing
TODO cannot silently rot behind a watermark.

### D3 — discovery scope: experiment-first, fleet opt-in via the journal home

Default scope = **one `experiment_dir`** (every existing block's contract).
`fleet: true` widens to the machine: enumerate the journal home
(`state/run_record.py::_current_homedir`, default `~/.claude/hpc/`), and for
each `<repo_hash>/` namespace read its `repo.json` — which
`state/run_record.py::journal_dir` already stamps with the resolved
`experiment_dir` — to recover the experiment root, then run the identical
per-experiment collection there. The namespace boundary is therefore the
journal home: **fleet = every experiment this machine has ever journaled**,
which is what "overnight digest" means for a one-laptop operator.

Non-creating read discipline (the `relay_audit_stop` posture, and the
doctor's fail-open scan): the fleet walk **globs** `*/repo.json` — it never
calls `journal_dir()` (which mkdirs) and never scaffolds a namespace. A
`repo.json` whose `experiment_dir` no longer exists on disk, is unreadable,
or is torn is **skipped silently and counted** (`skipped: [{repo_hash,
reason}]` in the result) — a wiped demo repo must never crash the morning
read. Per-experiment stores under `<experiment_dir>/.hpc/` (campaign,
scope, notebook journals) are discovered by glob too
(`.hpc/campaigns/*/decisions.jsonl`, `.hpc/notebooks/*.decisions.jsonl`),
same fail-open rule.

### D4 — a sibling QUERY verb `attention-queue`; the snapshot evolves by embedding it

Two candidate shapes were weighed:

* *Evolve `status-snapshot` in place* (literally "v2 of the verb"). Rejected
  as the primary surface: `status-snapshot` is a **chain block** — it has
  `next_block` semantics in `infra/block_chain.py::SUCCESSORS`, a
  `needs_decision` contract the driver parks on, a `mark_seen` watermark
  side effect, and skill/relay contracts (`hpc-status` SKILL, `render_relay`)
  built on its brief shape. The queue is none of those things: it *reports*
  decision points, it is not one (`needs_decision` on the queue itself is a
  category error); it is fleet/cross-experiment, which breaks the block's
  one-`experiment_dir` contract; and it must be watermark-neutral (D6).
* *A new sibling verb.* **Chosen**: `attention-queue`, a read-only
  `verb="query"` primitive (the `doctor` posture: no SSH, no side effects,
  `idempotent=True`), agent-facing, MCP-exposed.

"Status-snapshot v2" is then delivered as the **snapshot embedding the
projection**: `status-snapshot`'s brief gains an additive `attention` field
— the single-experiment queue items in D2 order — so the in-flow morning
read is ordered by the same rule without any output-shape break (readers
tolerate additive fields; no `next_block`/chain change; the skill prose
gains one paragraph). One ordering definition serves both surfaces because
the snapshot calls the same `collect + order` functions the verb does
(enforcement row below).

### D5 — one-definition seams: the queue AGGREGATES predicates, never re-implements them

Each item kind names the single existing predicate it routes through. The
queue adds selection and ordering ONLY — any recomputation of a verdict,
staleness, or liveness inside `ops/attention/` is a defect by definition.

| Item kind | Source predicate (the one definition) |
|---|---|
| `greenlight-unadvanced` | `state/index.py::find_parked_runs` split by `state/decision_journal.py::is_latest_committed_greenlight` — the SAME pair `ops/recover/doctor.py::doctor` and the Stop guard (`_kernel/hooks/decision_rendezvous_stop_guard.py::find_committed_unadvanced`) key on. The queue becomes the THIRD surface that must agree; it calls the same symbols, so it cannot disagree. |
| `run-parked` | `state/index.py::find_parked_runs` where the latest decision is NOT a committed `y` (the complementary branch of the same split). |
| `run-stalled` | `state/index.py::find_stalled_runs` (parked ≠ stalled already encoded there). |
| `dead-worker` | `ops/recover/doctor.py::_scan_dead_detached_workers` — **promoted to a public name** (T5 below) and imported; the lease-walk + `_pid_alive` + `state/block_terminal.py::read_terminal` logic is never copied. |
| `run-anomaly` | `ops/status_blocks.py`'s anomaly reduction: `_ANOMALY_STATUSES`, `_recommendation_for`, `_digest_run` — promoted to importable names (T6a) with the supersession exclusion (`is_superseded` never an anomaly) intact. |
| `campaign-pending` | `state/decision_journal.py::latest_decision` + `is_latest_committed_greenlight` over `scope_kind="campaign"` per discovered campaign dir: a campaign whose newest journaled touchpoint is not a committed `y` is awaiting a verdict (completion brief or anomaly brief — the record's `block` distinguishes them in `evidence`). No new campaign-state predicate is invented. |
| `audit-section-unsigned` / `audit-section-stale` | `state/notebook_audit.py::audit_module` — the T6 reduction (which itself routes drift through `state/attestation.py::reduce`). The queue maps `UNSIGNED` → unsigned item, `SIGNED_STALE` → stale item, and never touches a sha. Required slugs come from the audit's template via the same parse the gate uses (`state/audit_source.py`); an experiment with no `audited_source` opt-in contributes nothing (D7 fail-safe posture). |
| `alert` | `ops/recover/notify.py::read_unacknowledged_alerts` — peek-only (D6). |
| `ssh-circuit-open` | `ops/recover/net_triage.py::open_circuit_lines`. |

Scope-unlock requests — named in the product sketch — are **deliberately NOT
a v1 item kind, with a recorded reason**: there is no durable "unlock
requested" record anywhere in the tree. A scope-gate refusal
(`ScopeLocked`) raises and vanishes; the no-unlock-verb doctrine
(`docs/design/rigor-primitives.md`) means no pending-unlock state exists by
design. Fabricating an item from a raised-and-gone exception is the
fabrication class. The item joins the queue the day the scope gate journals
its refusals (a design change owned by the rigor layer, not smuggled in
here); until then a *locked scope* is visible only through its consequences
(a parked run whose brief names the refusal).

### D6 — render + freshness: recomputed, watermark-neutral, self-dating

The digest is a code-rendered markdown projection (`ops/attention/render.py`
— pure string work, no I/O, no `_wire` import; the `ops/relay_render.py`
posture, and a natural sibling of the run-story renderer). Layout: a header
line, then one section per D2 class in order, one line per item —
`<age> · <kind> · <scope_id>[ on <cluster>] — <action or evidence one-liner>`
— wording composed from the item's own fields, never free prose.

Freshness, decided: the result carries ONE `computed_at` stamp (top-level
and in the render header: `"attention queue · computed 2026-07-08T06:12Z ·
re-run for current state"`), and every item carries its source-side `since`.
Ages are rendered as durations relative to `computed_at` — so an overnight
digest read at noon is *visibly* a 6am projection, and the remedy is stated
in the header: re-run the verb (cheap, journal-first, no SSH). There is
deliberately **no digest file, no cache, no served page**: a persisted
digest is a second source of truth that drifts from the journal
(reconcile-is-truth, `docs/design/proving-run-2-hardening.md` Move 4). The
queue is recomputed on every read.

Watermark neutrality, decided: the queue moves **no state** — not
`last_seen_by_human_at` (`state/journal.py::mark_seen_by_human`), not the
alert acknowledgment watermark (`ops/recover/notify.py::acknowledge_alerts`).
Both stay `status-snapshot`'s job under its `mark_seen` gate. Rationale: a
read that silently marks things seen makes the snapshot's changed-since
delta lie ("nothing changed since you looked" when the human never read the
queue's render); and an out-of-session scheduled queue run (the doctor's
cron pattern) must never consume the human's attention markers. The spec has
NO `mark_seen` field at all — absence of affordance, not a default.

### D7 — spec surface

`_wire/queries/attention_queue.py::AttentionQueueSpec`:
`{fleet: bool = False, class_order: list[str] | None = None,
now: str | None = None}` plus the standard `experiment_dir` CLI arg. `now`
is the deterministic-testing override (the `doctor` spec precedent), never
an agent-facing knob for reshaping ages. Result model
`AttentionQueueResult`: `{computed_at, items: [...], counts: {class: n},
skipped: [...], render: <markdown>}` — `render` rides the result the way
`relay` rides `StatusBlockResult`, so the agent relays it verbatim.

### D8 — demand-driven routing + the decision-ready bar (user-ruled 2026-07-07)

Tiering alone relocates fatigue; the channel dies the week the human
learns that opening an item costs more than ignoring it. Two restrictions,
binding on EVERY alarm/verdict source that feeds the queue (fingerprint
`needs_verdict`, manifest drift, claim-check findings, conformance
verdicts — and all future sources):

1. **Route only what blocks.** An item may INTERRUPT (surface in a brief)
   only when its unblocks fan-out > 0 or a consumer is demanding it (a
   gate or verdict something is actually waiting on). Leverage-zero items
   PARK — pull-only, aging in the standing queue, never pushed. The
   fingerprint's `needs_verdict` specifically: a thin-envelope sample does
   NOT route at creation; it routes when registration/graduation/verify
   blocks on the verdict — **verdict-on-demand** (fingerprint doc,
   Amendment 2).
2. **The decision-ready bar.** A routed item must carry ALL FOUR or it
   may not route: (a) what it blocks, named; (b) ONE code-rendered
   evidence block sized for the brief (trusted-display class — the LLM
   points, never composes); (c) a PRE-DRAFTED resolution the human can
   accept with `y` or redirect with a nudge (the decision-brief shape,
   generalized to alarms); (d) delivery at an existing decision moment
   (greenlight, harvest, the morning batch) — never its own session.

Repeats fold (first occurrence per (subject, class) may route; recurrence
updates the aging standing item — the manifest attention contract's rule,
promoted to queue-wide). Ignore-rate/ack-latency data is INPUT TO A HUMAN
RE-RULING of a class's tier, never adaptive self-quieting.

## Task waves (file-disjoint for parallel Opus dispatch)

Every task lands with tests that both FIRE on a synthetic violation and PASS
on the happy path, per `docs/internals/adding-a-primitive.md`.

**Wave A — promotions (parallel; each touches one existing hot file):**

* **T5** `ops/recover/doctor.py` — rename `_scan_dead_detached_workers` →
  public `scan_dead_detached_workers` (module `__all__`; `doctor()` re-points;
  behavior byte-identical). Test: existing doctor dead-worker tests move to
  the public name; add an `inspect.getsource`-style assertion is NOT needed
  here (this IS the definition).
* **T6a** `ops/status_blocks.py` — promote `_ANOMALY_STATUSES`,
  `_recommendation_for`, `_digest_run` to public names (drop the underscore,
  keep aliases if churn is large); no behavior change. Tests: existing
  snapshot tests unchanged; add a public-name import test.

**Wave B — the new package (parallel after Wave A; all files new):**

* **T1** `ops/attention/queue.py` — the item model (a frozen dataclass or
  TypedDict mirroring D1), per-kind collectors (one small function per row
  of the D5 table, each CALLING the named source symbol), `order_items`
  (D2: class order incl. `class_order` override, `since` oldest-first,
  `(kind, scope_id)` tiebreak — property-test the total order), and
  `collect_queue(experiment_dir, now)` composing them. Tests: synthetic
  journals per kind; a route-through `inspect.getsource` assertion per
  collector (the `test_layers_share_one_drift_predicate` precedent) pinning
  that no collector re-inlines its predicate.
* **T2** `_wire/queries/attention_queue.py` — `AttentionQueueSpec` /
  `AttentionQueueResult` (D7). Tests: spec validation (unknown class names
  in `class_order` are ignored, not refused — the T12 semantics).
* **T3** `ops/attention/render.py` — deterministic markdown (D6). Tests:
  golden render over a crafted item list; byte-stability under dict-order
  shuffling; the `computed_at` header line present.

**Wave C — wiring (sequential; hot files):**

* **T4** `ops/attention/queue_op.py` — the `attention-queue` `@primitive`
  (`verb="query"`, `side_effects=[]`, `idempotent=True`,
  `agent_facing=True`, `requires_ssh=False`), single-experiment and
  `fleet=True` paths (D3 glob discovery, `skipped` accounting,
  non-creating: test asserts NO directory is created under a fresh journal
  home during a fleet scan). Registry count +1 (134 at plan time — verify
  against `hpc-agent capabilities` at implementation).
* **T6b** `ops/status_blocks.py` — snapshot v2: `brief["attention"]` =
  `collect_queue(...)` items for this experiment in D2 order (additive
  field; `render_relay` untouched in v1 — the snapshot's relay line already
  summarizes; the queue's own render is the digest surface). Test: the
  brief carries the field; ordering matches `order_items` byte-for-byte
  (the one-definition seat).
* **T7** `src/slash_commands/skills/hpc-status/SKILL.md` — one added
  paragraph: the morning read is `attention-queue` (MCP, read-only, direct
  — no spec-file round-trip), relay the returned `render` VERBATIM; the
  snapshot's `attention` field is the same projection in-flow. **Skill-prose
  constraints:** no shell-pipe characters anywhere in prose (even backticked
  — `scripts/lint_no_blocklisted_commands.py`), no raw ssh
  (`scripts/lint_no_raw_ssh.py`), steps end in a verb or an enumerated
  choice (`scripts/lint_skills.py` prose rules). Run all three lints.
* **T8** regen + inventory tails (after T4): ALL SIX regen scripts —
  `scripts/bake_operations_json.py --write`,
  `scripts/build_operations_index.py`, `scripts/build_schemas.py`,
  `scripts/build_verb_module_map.py`, `scripts/build_primitive_index.py`,
  `scripts/build_primitive_frontmatter.py` (the dev_regen_list lesson: a
  missed bake costs test failures). Inventory tails: `_SPEC_VERBS` in
  `tests/contracts/test_schema_roundtrip.py` and
  `tests/contracts/test_primitive_remediation.py`; the primitive doc page
  `docs/primitives/attention-queue.md`
  (`scripts/check_no_pending_primitive_docs.py`); the MCP curated-catalog
  prose in the server instructions if it enumerates query verbs; skill
  lints per T7.
* **T9** `docs/internals/engineering-principles.md` — enforcement rows (see
  below) + this doc's status flip and drift log at implementation time.

Enforcement rows T9 adds (the D5 rule mechanized):

| Rule | Enforced by | Fires when |
|---|---|---|
| Every attention-queue collector routes through its named source predicate (`find_parked_runs`/`is_latest_committed_greenlight`, `find_stalled_runs`, `scan_dead_detached_workers`, `audit_module`, `latest_decision`, `read_unacknowledged_alerts`, `open_circuit_lines`) — the queue aggregates, never re-implements | `tests/ops/attention/test_queue.py` route-through assertions (one per collector, `inspect.getsource`) | a collector re-inlines a liveness/staleness/greenlight comparison instead of calling the source symbol |
| The queue is watermark-neutral and store-free: no `mark_seen_by_human`, no `acknowledge_alerts`, no file writes anywhere under `ops/attention/` | `tests/ops/attention/test_queue.py::test_read_only` (source scan + a tmp-journal write-probe) | any mutation call or write path appears in the attention package |
| Snapshot v2 and the verb share ONE ordering definition | `tests/ops/status/test_snapshot_attention.py` | `status_blocks` re-sorts or re-collects instead of calling `ops/attention/queue.py::collect_queue`/`order_items` |

## Boundary-drift flags (watch list)

* **No LLM prioritization, ever.** The `action` string is the source
  predicate's drafted proposal or absent; the render is code-composed. Any
  future "let the model summarize the queue" rides the rule-10 verify-relay
  machinery, never this path.
* **No fabricated urgency.** No score field, no "critical/high/low"
  vocabulary, no cross-source age interpretation. Priority = D2 class +
  position, both recomputable. Pressure to add a score is pressure to let
  the ordering rule go undefined — refuse it there.
* **No domain metric interpretation.** Items carry identity, counts, and
  opaque evidence (the scope look-ledger's identity-only discipline,
  `state/scopes.py::record_look`). A queue item never reads what a run
  *found*.
* **No mutable queue state.** Recomputed every read; no digest file, no
  acknowledgment, no per-item "dismissed" flag (dismissal is the snapshot's
  `mark_seen` watermark or the underlying condition resolving — the queue
  reflects, it never remembers).
* **Kinds stay opaque strings; subjects stay `SCOPE_KINDS`.** A new item
  kind must name its one-definition source predicate first (the D5 table
  grows a row before `ops/attention/` grows a collector) — the scope-unlock
  entry documents the refusal pattern.
* **The queue never becomes a chain block.** No `next_block`, no
  `needs_decision`, no park. If a workflow wants "queue then act", the
  acting verb is the block; the queue is its evidence.

## Open questions (for the implementing wave, each needs a recorded answer)

* Whether `campaign-pending`'s first-touchpoint case (a campaign journal
  whose ONLY record is the start greenlight `y`) correctly yields no item —
  expected yes by D5's predicate, but pin it with a test.
* Whether the fleet scan should cap enumerated namespaces (a machine with
  hundreds of stale `repo_hash` dirs) — lean no cap + `skipped` accounting,
  but measure on the real journal home before deciding.
* Whether `audit-section-*` items need the audit's template to resolve
  `required_slugs` when `interview.json` carries no `audited_source`
  (un-opted-in audits under `.hpc/notebooks/`) — lean: journal-discovered
  audits with no resolvable source contribute nothing (fail-safe), recorded
  in `skipped`.

Each open question was answered in the implementation and pinned: the
first-touchpoint campaign case (`test_campaign_pending_fires_but_start_only_
greenlight_yields_nothing`), the un-opted-in audit skip
(`test_audit_with_no_opt_in_is_skipped_not_crashed`,
`test_collect_items_carries_audit_skips`). The fleet-namespace cap stays
un-capped with `skipped` accounting (no cap measured as needed).

## Drift log (implementation deviations, recorded)

* **Module path — top-level `ops/attention_*`, not an `ops/attention/`
  package.** The plan named `ops/attention/queue.py` / `render.py` /
  `queue_op.py`. Wave A/B landed them as `ops/attention_queue.py`,
  `ops/attention_render.py`, and Wave C as `ops/attention_op.py` — the `ops/`
  role-root convention (siblings `notebook_status.py`, `export_dossier.py`,
  `run_story.py`), so the subject-imports lint short-circuits the cross-subject
  reads by construction. All D5/D6 seams and enforcement rows are unchanged; only
  the dotted paths differ.
* **`AttentionItem` is a frozen dataclass with a FLAT subject** (`scope_kind` /
  `scope_id` / `block`), re-nested into the D1 wire shape by `as_dict()`. The
  wire model (`AttentionItemModel`) carries the nested `subject` + the `class`
  alias. A `scope_kind` of `null` is a first-class subject state for the two
  fleet-level infra signals (`alert`, `ssh-circuit-open`), which carry no
  run/campaign/scope/notebook subject.
* **Run-anomaly enumeration.** No fleet `find_failed_runs` predicate exists (the
  in-flight scans exclude terminal runs), so `collect_anomalies` enumerates run
  records non-creatingly (`_all_run_records`, a direct `runs/*.json` glob) and
  applies the promoted `status_blocks` reduction (`digest_run` +
  `ANOMALY_STATUSES` + `recommendation_for`); the anomaly VERDICT and the
  supersession exclusion are never re-inlined.
* **Audit source resolver reuse.** The audit collector resolves
  `(parsed_source, required_slugs)` through
  `ops/decision/verify_relay.py::_nb_resolve_sources` (itself the T8 gate's
  `_read_interview_audited_source` + `state/audit_source.py`) — the same seam the
  sign-off gate uses; the queue never re-parses the opt-in.
* **D2 REVISED — leverage-primary ordering (user, 2026-07-08).** The primary sort
  key became LEVERAGE = the unblock **fan-out** counted over encoded edges, above
  the class order. An `unblocks: int` field lands on `AttentionItem` (default 0),
  its wire model, `as_dict`, and the render (`unblocks N`, only when `> 0`, an
  honest count — never urgency prose). The edges COUNTED (`_apply_fanout` in
  `ops/attention_queue.py`, applied in `collect_items` so both the verb and the
  snapshot embed inherit it): a `greenlight-unadvanced` → its run (1); an
  `audit-section-unsigned`/`-stale` → the module's `passed` gate → every
  **pending** (non-terminal, non-superseded) run whose sidecar `audited_source`
  echo names the audit (a non-creating `.hpc/runs/*.json` glob joined to the run's
  journal record for its status — adversarial review F4: the echo is stamped
  *after* graduation, so counting *every* echoing run measured historical usage
  and inflated the leverage forever instead of the pending fan-out; the filter
  now mirrors the `campaign-pending` edge's `TERMINAL_STATUSES` posture); a
  `campaign-pending` → the campaign's remaining (non-terminal) runs (via
  `find_runs_by_campaign` + `TERMINAL_STATUSES`). Every other kind has no encoded
  edge → 0 and falls through to the class order byte-identically with the
  pre-revision rule (the old Wave-B ordering tests still pass unchanged). The
  `class_order` override survives at the tiebreak level; a caller override of the
  fan-out computation itself is deliberately absent (that would be prioritization
  prose). Delivery (cron/notification) was de-scoped in the same decision — the
  queue is a PULL surface; nothing is marked read, so a standing TODO cannot rot
  behind a watermark.
