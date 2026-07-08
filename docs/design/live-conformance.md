# Live conformance — the registration watchdog

**Status: PLANNED (2026-07-07), not yet implemented — sequenced after the
slate (`docs/design/slate-sequencing.md`) and its registration/fingerprint
dependencies.** This document is the durable hand-off (the
`docs/design/notebook-audit.md` pattern): settled decisions with recorded
rationale, file-disjoint task waves for parallel Opus dispatch, enforcement
rows, and boundary-drift flags. Cite `path::symbol`, never line numbers.
Record implementation drift in the drift log at the foot of this document.

## Product intent (user-approved framing, 2026-07-07)

**A registration is a hypothesis; production is the experiment that never
stops.** The registration kernel (`docs/design/registration-kernel.md`) makes
the promotion boundary rigorous — a sealed dossier, a verified prerequisite
chain, the maximal human sign-off — and then the system goes blind at the
exact moment consequences start. The registered evidence is a point-in-time
claim ("this behaved like THIS, measured over THAT window"); the live world
immediately begins accumulating evidence for or against it, and today nothing
reads that evidence, judges it against the registration, or routes the
verdict.

Live conformance extends the attestation substrate into operation time:

- **Live outcomes stream in as code attestations** — journaled receipts at
  production cadence, sha-linked to the registration they test.
- **A conformance comparator judges them against the REGISTERED evidence** —
  the fingerprint's envelope machinery
  (`docs/design/determinism-fingerprint.md`) pointed at production.
- **Drift routes ATTENTION — never action.** A nonconforming window is a
  FINDING in the attention queue; the human's verdict is a dated conclusion;
  the remedies are the registration kernel's own (re-register, revoke), each
  a human act.

## The canonical lineage: SPC rebuilt on attestations (settled)

The design center is **statistical process control** — Shewhart's control
chart, rebuilt on this repo's primitives. The lineage is load-bearing because
it settles the two boundaries this feature lives or dies on:

1. **The chart judges; the operator adjusts.** Shewhart's chart never touched
   the machine. The substrate OBSERVES, JUDGES, and ROUTES; every actuation —
   halting a strategy, recalibrating an instrument, placing or cancelling an
   order — is the operator's act, outside this system entirely.
2. **Control LIMITS vs control RULES.** The LIMITS derive from measured,
   registered evidence — the order-statistics envelope machinery
   (`state/determinism.py`, fingerprint T1) computed over the sealed baseline
   the registration itself carries. The RULES ("act on 8 consecutive points
   above the centerline") are domain policy: **pack/caller territory,
   forever.** Core ships the per-point and per-window comparison arithmetic;
   any sequential run-rule, alarm policy, or action threshold lives
   caller-side over the ledger core exposes.

## The agency boundary (the sharpened scope doctrine — user-defended, settled)

The substrate **observes, judges, routes — never actuates beyond the
experiment sandbox.** It never places orders, never recalibrates an
instrument, never connects to a brokerage, an exchange, or a lab device. Even
OBSERVATION is arms-length: the EMITTER — the caller-side code that knows
what a fill, a chromatogram, or a sensor reading is — lives outside core and
journals receipts (the D9 execution-contract pattern,
`docs/design/notebook-audit.md`; the `notebook-record-receipt` precedent for
sha-bound recording, `state/notebook_audit.py::record_render_receipt`). Core
receives an already-reduced opaque payload and binds it; it never fetches,
polls, or holds a credential to any external system.

Precedents this pins itself to, verbatim:

- the attention queue **"never advances"** anything
  (`docs/design/attention-queue.md` — "it never becomes a chain block");
- the doctor **"drafts, never re-spawns"** (`ops/recover/doctor.py` posture);
- the evidence-memory **never-blocking priors pin**
  (`docs/design/evidence-memory.md` T-NB — surfacing never gates).

The enforcement rows below pin this the same way: **no actuation affordance
exists; drift routes, never acts** — the no-actuation pin is the FIRST-CLASS
row of this plan's enforcement suite.

## The settled design center (user-ruled 2026-07-07 — DECIDED)

Departures during implementation are drift to be logged, not re-litigated.

### C1 — live outcomes are RECEIPTS at production cadence

Each live observation is one journaled **code attestation** sha-linked to the
registration it tests: `attestor="code"`, bound via
`state/attestation.py::bind` at append (the recompute lock — the payload sha
is recomputed server-side, so an observation cannot be asserted at a sha it
does not carry), carrying the registration's identity so the ledger reads as
"evidence FOR/AGAINST registration R". Truthfulness of the observation itself
is CALLER-ATTESTED — the F8 honesty from the notebook receipt applies
verbatim: the bind vouches for the exact recorded bytes, the trust boundary
is the emitter, and consumers WEIGH the caller-attested outcome rather than
re-deriving it. This is the same class of trust as a conforming harness's
out-of-band writes, honestly named.

### C2 — the comparator judges against REGISTERED evidence, with tiered verdicts

One comparator definition; three tiers — the D-attention pattern, exactly the
fingerprint's classifier (`docs/design/determinism-fingerprint.md` decision 3):

- **`conforming`** — mechanized, zero human attention: every declared key's
  live window sits inside a WELL-EVIDENCED registered envelope and the window
  meets the caller-declared evidence floor. A derived verdict, recomputed on
  read — never stored state (D-envelope's "the envelope is DERIVED" posture).
- **`needs_verdict`** — routed to the human WITH calibrated, range-phrased
  evidence: *"realized key `yield_pct` window min 0.914 vs registered
  envelope [0.941, 0.973] (window n=40 obs since 2026-05-01; baseline n=126,
  sealed 2026-03-02)"*. Triggers are all mechanical: thin registered evidence
  (baseline n below the well-evidenced bar), an insufficient live window
  (n below the caller-declared floor — **a rolling window too short to
  compare reads `needs_verdict/insufficient_window`, never a fabricated
  verdict**), key novelty (a live key the baseline never carried, or
  vice versa), incomparable values (NaN / type-changed), or a label novelty
  the receipts disclose.
- **`nonconforming`** — a window outside a WELL-EVIDENCED registered envelope.
  A **FINDING** — surfaced, dated, evidence-cited; it never mutates the
  registration's status, never revokes, never halts anything. Discovered
  drift is the feature working (the `ops/verify_reproduction.py` module-
  docstring posture, at the operation boundary).

### C3 — the drift verdict is a DATED CONCLUSION

A human's resolution of a `needs_verdict`/`nonconforming` item is an ordinary
`append-decision` record (block `"conformance-verdict"`, no verdict verb —
the no-unlock-verb doctrine, `docs/design/rigor-primitives.md`), citing the
offending receipts by sha. The verdict is DATED EVIDENCE feeding evidence
memory (`docs/design/evidence-memory.md`): the no-kill-ledger semantics
extend — **"died in regime X, 2026" is a prior, never a tombstone.** If the
human judges the drift real, the remedies are the registration kernel's own
R7 remedies (revoke with reason; re-register on fresh evidence) — separate
human records; the verdict itself never revokes.

### C4 — the review-horizon hook: time-based staleness

The registration MAY carry a `review_horizon` — a caller-computed ISO
timestamp. A registration whose horizon lapses without re-verdict reads
**`stale`** mechanically: time-based staleness joins edit-based drift in the
ONE registration reduction (mechanism settled in C-horizon below — the
reduce layer consults the field). Durations and cadences ("review every 90
days") are domain policy: the caller computes the date; core compares
timestamps, never names periods (the evidence-memory `as_of` posture).

### C5 — the honest comparison semantics (the judgment core — settled in C-compare)

Point-in-time registered evidence (an envelope over a fixed, sealed window)
versus a ROLLING live window (different n, different regime, autocorrelated
samples) is an apples-to-oranges comparison that core must not paper over
with invented statistics. The fingerprint's posture applies unchanged: order
statistics + evidence labels + thin-evidence-routes-to-human. Full semantics
in C-compare.

### C6 — second-consumer discipline: the INSTRUMENT-QC toy

Test fixtures use the instrument-QC toy case — a fake sensor's registered
calibration envelope judged against live readings — **NEVER trading
vocabulary** (the toy-domain rule: real domain words in fixtures smuggle a
vocabulary into the tree that greps and future maintainers mistake for core
knowledge).

## Decisions settled in THIS document

### C-store — where live receipts live: a registration-scoped ledger

**Experiment-local, append-only:
`<experiment>/_aggregated/_conformance/<registration_id>.jsonl`** — one
ledger per registration, each line one observation receipt. Weighed against
the alternatives, per the no-new-store posture:

- **Not the registration's decision journal**
  (`.hpc/registrations/<registration_id>.decisions.jsonl`): the fingerprint
  D-store already settled this exact question
  (`docs/design/determinism-fingerprint.md`) — the decision journal is
  wipeable CONTROL state; a measured record of an experiment's live behavior
  is a durable SCIENTIFIC record that must survive a journal wipe. Production
  cadence also means potentially thousands of records; the journal's
  reductions must never wade through them.
- **Not a new store CLASS**: the `_aggregated/_fingerprints/` ledger idiom is
  reused verbatim — same append mechanics (advisory flock + fsync, one JSON
  line, no dedup, via the shared append helper fingerprint T3 extracts from
  `ops/verify_reproduction.py::_append_receipt`; pre-implementation
  verification 2026-07-07: `infra/io.py::append_jsonl_line` ALREADY exists as
  the package's one JSONL-append definition with exactly this discipline —
  the "extraction" is re-pointing `_append_receipt` at it, never minting a
  third helper), same tolerant read, same
  travel-with-the-experiment rationale. This is the second consumer of an
  existing idiom, not a third storage invention.
- **Keyed on `registration_id`**, not `cmd_sha`: the subject under test is
  the REGISTRATION (the sealed hypothesis), not a code identity — one
  registration may cover a lineage; its live evidence accrues to it.

**Record shape** (schema_version 1; bump on shape change — the
`RECEIPT_SCHEMA_VERSION` convention):

```json
{"ts": "...", "schema_version": 1,
 "attestor": "code", "subject_kind": "conformance-observation",
 "subject_id": "<registration_id>",
 "content_sha": "<canonical-JSON sha over {payload, labels, observed_at}>",
 "registration": {"registration_id": "...",
                  "dossier_sha": "<the registration's content_sha>",
                  "status_at_record": "current|stale|revoked|superseded"},
 "observed_at": "<ISO ts the caller says the observation occurred>",
 "labels": {"<opaque caller label>": "<opaque value>"},
 "payload": {"<metric key>": 0.947, "...": "..."},
 "emitter": "<opaque caller-declared emitter id>"}
```

- `content_sha` uses the harness sha canonicalization
  (`docs/internals/harness-contract.md` §"The sha canonicalization" —
  `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`,
  SHA-256 lowercase hex), recomputed SERVER-SIDE at append and bound via
  `state/attestation.py::bind` — the sha cannot be asserted into existence.
  Truthfulness of `payload`/`observed_at` is caller-attested (C1's F8
  honesty).
- `payload` values are opaque scalars: identity-compared, range-compared,
  counted — never read for meaning. Keys are caller vocabulary.
- `labels` are opaque caller data (a cluster, a venue tag, a batch id —
  core never learns which). Label NOVELTY relative to the window is
  disclosed evidence, never interpreted.
- **Recording is fail-open for evidence**: an observation against a
  registration that currently reduces `stale`/`revoked`/`superseded` is
  RECORDED (production is the experiment that never stops; refusing evidence
  is the one thing an evidence system must not do) with the reduced status
  stamped `status_at_record` — disclosed, never silently mixed. An
  observation naming a registration that is ABSENT is refused loudly
  (`errors.SpecInvalid` — there is no hypothesis to test; the fabrication
  class).

### C-declare — the conformance declaration: caller config, structure-only

Conformance is **opt-in per registration** (the D7 fail-safe posture: absent
declaration → no conformance machinery runs, byte-identical). The
registration's `resolved` gains ONE optional structured block, `conformance`,
validated structure-only by `state/registration.py` (an additive amendment to
the R6 lock-2 field list, sequenced after registration T7; recorded rationale:
the declaration must be SEALED INTO the registration record so the limits'
provenance is the registration itself — a side-channel config file would let
the limits drift from the hypothesis they bound):

```json
{"baseline": {"path": "<relpath inside the sealed dossier>",
              "sha256": "<that entry's manifest sha>"},
 "keys": ["<metric key>", "..."],
 "min_window_n": 20,
 "review_horizon": "<ISO ts>"}
```

- **`baseline`** names the sealed artifact carrying the registered samples —
  rows of `{key: value}` observations (a backtest's per-period metrics; a
  calibration run's readings). The append gate verifies the `(path, sha256)`
  pair is a MEMBER of the dossier's manifest entries (identity against the
  entries list the dry re-gather returns — registration T3's
  `ops/export_dossier.py::compute_dossier_signature` seam), so **the control
  limits derive from evidence inside the sealed dossier by construction** —
  never from a file the caller can swap after sign-off.
- **`keys`** — the caller-declared key set the comparator judges (opaque
  slugs; core never learns what `yield_pct` means). Empty/absent → every key
  present in the baseline (disclosed).
- **`min_window_n`** — the caller-declared live-window evidence floor.
  COUNTING against a caller number (the R3 `scope-budget` pattern): core
  compares `window_n >= min_window_n`, never picks the number.
- **`review_horizon`** — C4's timestamp. Optional.
- Unknown keys in the block are a LOUD `errors.SpecInvalid` (the R4
  dangling-reference posture: an opted-in requirement core cannot check must
  never silently pass).
- **Window LENGTHS and cadences are caller-declared opaque config,
  forever.** `min_window_n` and the query-time window selection (C-compare)
  are the entire core surface; "40 trading days", "6 sensor cycles",
  "post-restart" are caller words core never sees.

### C-compare — the comparator: one definition, honest arithmetic

`state/conformance.py::judge_window(baseline_rows, receipts, declaration,
*, now)` — pure, no I/O, the ONE definition every surface calls (the query
verb, the verify-registration finding, the queue collector — the
attention-queue one-definition enforcement pattern).

**The registered side.** The baseline envelope per key is the
**order-statistics envelope** — observed `[min, max]` plus derived relative
spread, labeled with its evidence `{n, sealed_at}` — computed by the ONE
envelope reduction the fingerprint owns (`state/determinism.py`, fingerprint
T1; T1a below factors the per-key order-statistics helper so both consumers
route through it — never a second envelope definition). The baseline is
**point-in-time and SEALED**: it is read from the dossier-bound artifact and
never grows. No fitted distribution at any n — no mean±kσ, no interval
estimate (the D-envelope rejection, verbatim).

**The live side.** The window is an explicit selection over the ledger —
`{since, until?}` timestamps or `last_n` — supplied by the caller at query
time. Core selects by timestamp/count arithmetic only; it never picks,
defaults, or "recommends" a window. Per declared key, the window reduces to
its own order statistics `[min, max]` + `n` + the distinct label sets
observed.

**The honest comparison (the settled judgment core).** The two sides differ
in n, in regime, and in sampling structure (a rolling production window is
autocorrelated; a sealed backtest window was fixed). Core therefore:

1. **Does ONLY comparison arithmetic**: per key, is the window's observed
   range inside the baseline envelope (per-point verdicts are the same
   arithmetic applied to a single receipt — the Shewhart per-point read);
   counting: `window_n >= min_window_n`, `baseline_n >= 3` (the fingerprint's
   well-evidenced bar, reused — the one mechanized evidence threshold, an
   existing vocabulary, not a new invention).
2. **Labels every verdict with both sides' evidence, verbatim**: the brief
   states window n + span, baseline n + sealed date, and the label sets —
   range-phrased, never sigma-phrased ("window min 0.914 vs registered
   [0.941, 0.973]; window n=40 since 2026-05-01; baseline n=126 sealed
   2026-03-02"). A σ is a fitted parameter core refuses to fabricate.
3. **Routes thinness to the human, both directions**: `window_n <
   min_window_n` → `needs_verdict/insufficient_window` (never a verdict
   fabricated from too little evidence — inside OR outside the envelope);
   `baseline_n < 3` or key/label novelty → `needs_verdict` with the novelty
   named. The classifier's triggers are evidence-thinness, novelty, and
   incomparability — all mechanical properties of the records, never a
   closeness judgment (the rejected near-boundary heuristic stays rejected).
4. **Makes NO exchangeability claim.** Autocorrelation, regime shift, and
   changed sampling are disclosed as structure (dates, ns, labels), not
   corrected for. The D-envelope closing posture applies verbatim:
   statistics are not banned — fabricating their parameters in core is. The
   ledger preserves raw receipts so a human, a pack, or a caller can run a
   real test (effective-n, changepoint, equivalence) on top with an alpha
   someone OWNS; the order-statistics comparison is the floor requiring no
   fabricated parameter.
5. **Ships NO control rules.** No consecutive-point counters, no trend
   detectors, no Western-Electric rules — sequential RULES are domain policy,
   caller-side over the per-point verdict stream this comparator exposes
   (enforcement row).

**The fold** (mirroring D-verdict-wire): any declared key whose
sufficiently-evidenced window range exits a well-evidenced baseline envelope
→ `nonconforming`; else any thin/novel/incomparable key → `needs_verdict`;
else `conforming`. Per-key `tier_reason` vocabulary:
`"within_envelope" | "outside_envelope" | "insufficient_window" |
"thin_baseline" | "key_novelty" | "label_novelty" | "incomparable"`.

**No admission — re-baselining IS re-registration (settled, the SPC close).**
Live observations NEVER widen the registered envelope. The fingerprint's
D-consume admission rule exists because its envelope accumulates; here the
baseline is a sealed point-in-time claim and must stay one — admitting live
samples would launder production drift into the limits that exist to catch it
(the self-laundering class, at the operation boundary). The only re-baseline
is a NEW registration over a new sealed dossier (whose baseline may, caller's
choice, incorporate the live record via the dossier export — C-dossier), 
facing the full R6 human bar. Control limits move only by a deliberate,
owned, signed act — Shewhart's recalculation discipline, rebuilt on
attestations.

### C-emitter — the emitter contract (~30 lines, caller-side forever)

Documented for callers (the D9 "~30 lines of caller-side convention"
precedent, promoted to a written contract in this doc + the verb's doc page):

1. The emitter lives in the CALLER's environment and owns all domain I/O —
   it is the only thing that ever touches a broker, instrument, or data feed.
   Core never gains a connector, a credential field, or a polling loop.
2. It reduces each observation to the flat opaque payload
   `{key: scalar}` using the SAME keys the registered baseline carries
   (the caller's mapping — core never learns what a fill is).
3. It records via **`conformance-record`** (C-verbs) with
   `registration_id`, `payload`, `observed_at`, optional `labels`, and its
   `emitter` id — one CLI call per observation or batch.
4. Its truthfulness is its own (C1): the verb binds the sha, not the world.
   An emitter that lies is the same trust class as a harness that edits its
   own config (`docs/internals/harness-contract.md`, "The honest trust
   limit") — out of scope, honestly named.
5. Cadence, batching, retention of raw domain data: caller policy, never
   core's.

### C-verbs — the two verbs, and what each may touch

- **`conformance-record`** — the mutate verb (the `notebook-record-receipt`
  sibling): validates the registration exists, recomputes + binds the payload
  sha, stamps `status_at_record`, appends one ledger line. Its ONLY side
  effect is that append. **`agent_facing=False`** (a human/cron-invoked CLI
  verb, never an agent tool — the F1 ingest-verb posture, recorded rationale:
  an agent authoring the outcome stream that judges its own registration is
  the receipt-laundering class at the operation boundary; the emitter is
  caller machinery, not the driving agent).
- **`conformance-status`** — read-only `verb="query"`, `side_effects=[]`,
  `idempotent=True`, agent-facing, MCP-exposed: spec
  `{registration_id, since?, until?, last_n?}` → loads the ledger + the
  registration + the sealed baseline, calls `judge_window` (the one
  definition), returns per-key verdicts + the overall tier + a deterministic
  code-rendered markdown brief (`ops/conformance_render.py` — the
  `ops/relay_render.py` posture: pure string work, wording composed from
  record fields, no urgency or recommendation prose). Verdicts are DERIVED on
  every read — no verdict store, no watermark, nothing marked seen (the
  attention-queue D6 recompute posture).
- **No third verb.** No `conformance-resolve` (the human verdict is
  `append-decision` — C-verdict), no `conformance-halt`, no
  `conformance-baseline` (re-baselining is re-registration). Registry +2
  (cross-slate expected sum moves accordingly — note the CONCURRENT sibling
  plans also move it: evidence-memory +2 → 148, challenge-attestation +1,
  multi-human +1, in whatever post-slate order lands; verify against
  `hpc-agent capabilities` at implementation, never against a doc's frozen
  number). Naming note (pre-implementation verification 2026-07-07): the
  word "conformance" is already claimed by the HARNESS conformance kit
  (`docs/design/conformance-kit.md`, package `src/hpc_agent/conformance/`);
  this plan's modules (`state/conformance*.py`, `ops/conformance/`) are a
  DIFFERENT subject — registration conformance. The paths are disjoint and
  importable side by side; the collision is cognitive, recorded here so a
  grepping implementer does not conflate the two.

### C-horizon — the staleness mechanism, exactly

**The reduce layer consults the horizon field.** The registration reduction
(`state/registration.py`, registration T1) gains a `now` parameter (the
`doctor` spec's deterministic-testing precedent): the newest current
registration record whose `conformance.review_horizon` is non-null and
`< now` reduces **`stale`** with cause `horizon-lapsed` — time-based
staleness joining edit-based drift in the ONE reduction, so
`verify-registration`, the deployment refusal, and the queue all inherit it
with zero new consumers.

**The re-verdict that cures a lapse without re-registration:** a
`"registration-review"` record on the registration's journal —
human-authored, facing the R6-form bar (non-bare, names the
`registration_id` token-exact + the dossier sha by an 8+-hex prefix), with
`resolved={registration_id, dossier_sha, review_horizon: <new ISO ts>}`. The
gate RECOMPUTES the live dossier signature (the R2 dry re-gather) — **you
cannot re-affirm a drifted registration**; if the stores moved, the review is
refused and the remedy is re-registration. The reduction takes the newest
horizon among the registration record and any subsequent current review
records. Recorded rationale for the cheaper tier: when nothing has drifted,
the lapse is asking "does a human still stand behind this, today?" — a dated
re-affirmation answers exactly that; forcing a full re-registration for an
unchanged dossier would train horizon inflation (the rubber-stamp-fatigue
class, inverted).

### C-verdict — the human flow, and the conclusion join

- The verdict record: block `"conformance-verdict"` on scope kind
  `"registration"` (**no NEW scope kind** — recorded rationale, the R9
  test applied: a conformance verdict is ABOUT one registration and does not
  outlive it; its home is the registration's journal, exactly as the
  reproduction verdict rides the run scope. Ordinals are nominal per the
  slate standing rule — the cross-plan count when this lands depends on
  whether `"conclusion"`/`"challenge"` have landed; the claim here is only
  "adds none"). `resolved =
  {registration_id, cites: [<receipt content_sha>, ...], note: <free text,
  opaque>}` — `cites` non-empty, each resolved against the ledger at append
  (the E-shape citation posture: recompute leg, refuse a sha the ledger does
  not carry).
- **The authorship gate** —
  `ops/decision/journal.py::_assert_conformance_verdict_authorship`, the
  `_assert_registration_authorship` sibling: no affordance (append-decision
  only), server-side citation resolution, bare acks refused
  (`ops/decision/journal.py::_is_bare_ack`), the response names the
  `registration_id` token-exact AND at least one cited receipt sha by an
  8+-hex prefix (the R6/E1 bar, reused verbatim; full-strength under harness
  capability 1, journal-response friction tier honestly named when absent).
- **Resolution is mechanical, never semantic:** the queue item clears when
  the newest committed `conformance-verdict` post-dates the newest receipt in
  the offending window (the fingerprint-T7 answered-verdict pattern). Core
  never parses `note` for meaning.
- **The evidence-memory join:** a drift verdict SHOULD be followed by an
  evidence-memory conclusion (`docs/design/evidence-memory.md` E1) citing the
  same evidence — "no alpha in regime X, 2026" as a dated prior. Encouraged
  in skill prose, REQUIRED nowhere (the E1 never-mandated posture). Near-term
  a conclusion cites the re-registration dossier (which embeds the ledger via
  C-dossier); a first-class `conformance` member of
  `CITATION_KINDS`/`PREREQUISITE_KINDS` is **reserved as a future reviewed
  vocabulary change** (the E6 form), not added here — both sets are closed.

### C-queue — the collectors, and the honest fan-out answer

`ops/attention_queue.py` gains two kinds (D5 discipline: each names its one
source predicate — here `state/conformance.py::judge_window` over the ledger
+ the registration journal's newest verdict; route-through
`inspect.getsource` pin, the module's standing rule):

- **`conformance-needs-verdict`** — class `verdict` in `KIND_CLASS`: a
  registration whose declared default window (the ledger's trailing
  `min_window_n` receipts — the ONE mechanical default selection, taken from
  the caller's own declared floor, never a core-invented span) judges
  `needs_verdict` with no newer committed verdict. Evidence = the calibrated
  brief fields verbatim.
- **`conformance-nonconforming`** — class `verdict`: same predicate, the
  `nonconforming` fold. A FINDING awaiting human judgment.
- **Horizon lapse needs NO new kind**: it reduces the registration `stale`
  (C-horizon), so the registration kernel's R8 stale-registration item
  already carries it, with `horizon-lapsed` among the named causes — reuse,
  not a sibling.

**Fan-out: 0 — decided honestly against capital-shaping.** The temptation is
real: a drifting registration has the highest leverage in the system —
capital, not compute. But the D2-revised rule is that fan-out is COUNTED from
edges the records ENCODE, never scored; no journal encodes what a
registration's deployment is worth or what depends on it downstream
(the registry instance and the deploy boundary are caller-side by R8).
A capital weight would be a number core cannot justify — the fabrication
class, and the exact "no urgency score" line the queue's D1 drew. The class
ordering (`verdict`) plus the registration-kernel's existing prerequisite
fan-out carry the priority; if a future caller-side registry ever journals
explicit dependency edges, the walk gains them then — edges, never opinions.

### C-dossier — disclosure at re-registration

`ops/export_dossier.py` gains a **`live-conformance`** source noun: the
registration's conformance ledger exports into the dossier, so a
RE-registration's sealed evidence carries the live record that motivated it
— the anti-gaming-by-disclosure pattern (fingerprint decision 4) at the
operation boundary. "Ran nonconforming for 3 windows before re-registration"
is printed where reviewers look, never summarized. Same-commit pair-edit of
`tests/contracts/test_dossier_boundary.py::_EXPECTED_SOURCES` (the
deliberate-friction pin).

## Task waves (file-disjoint, Opus-sized — sequenced AFTER the slate)

Hard dependencies inherited: registration T1/T3/T6/T7 (`state/registration.py`,
the dossier signature seam, the scope kind, the authorship gate) and
fingerprint T1/T3 (`state/determinism.py` envelope math,
`state/fingerprint_store.py` + the shared append helper) land first (slate
Phases 2–3); registration T8 + fingerprint T7 edit `ops/attention_queue.py`
before our queue task; evidence-memory (also post-slate) is independent
except the shared hot files — serialize behind whichever lands first.
The two CONCURRENT sibling plans collide on the same hot files
(`docs/design/challenge-attestation.md` T5/T7 and
`docs/design/multi-human.md` MT7 both edit `ops/decision/journal.py`;
challenge T7 edits `ops/attention_queue.py`): no mutual order is recorded
anywhere yet — `docs/design/slate-sequencing.md` ends at the slate and must
gain a post-slate phase ordering before any two of the four post-slate
plans dispatch concurrently. Until it does, treat journal.py /
attention_queue.py / decision_journal.py edits across these plans as
strictly serial in whatever order executes.
Standing rules: regen commits strictly serial; enforcement-map edits
append-only and serialized; every wave ends regen → full suite → commit →
push → CI green. Every task lands with a fires+passes test pair.

**Wave A (parallel — new or upstream-refactor files):**

- **T1** `state/conformance.py` (new) — the pure kernel: the observation
  record model + validation (projecting to `state/attestation.py::validate`
  records), the canonical payload sha (harness-contract form), the
  declaration validator (structure-only; unknown keys refused), baseline-row
  loading shape, window selection arithmetic (`since/until/last_n`), and
  `judge_window` (per-key + fold + `tier_reason` vocabulary). No I/O.
  Tests: envelope route-through assertion (T1a's helper, `inspect.getsource`),
  insufficient-window routes to `needs_verdict` in BOTH directions, novelty
  and incomparability routing, no numeric threshold literal beyond the reused
  n>=3 bar.
- **T1a** `state/determinism.py` — factor the per-key order-statistics
  envelope helper to a shared function both the fingerprint reduction and
  `judge_window` import (one envelope definition — enforcement row). Pure
  refactor + byte-equality test against the fingerprint's existing reduction.
  (Serialized with any in-flight fingerprint work on that file.)
- **T2** `_wire/actions/conformance.py` + `_wire/queries/conformance.py`
  (new) — `ConformanceRecordSpec {registration_id, payload, observed_at,
  labels?, emitter?}` and `ConformanceStatusSpec {registration_id, since?,
  until?, last_n?}` + results (verdicts, evidence blocks, `render`). No
  domain vocabulary in field names (the `_FORBIDDEN_FIELD_NAMES` walk,
  mirrored). Schema regen tail.
- **T3** `state/conformance_store.py` (new) — the ledger: path derivation
  (`_aggregated/_conformance/<registration_id>.jsonl`), append routed through
  the shared flock+fsync helper (fingerprint T3's extraction) +
  `attestation.bind`, tolerant read, window selection hook for T1.

**Wave B (after A, parallel — one file each):**

- **T4** `ops/conformance/record_op.py` (new) — the `conformance-record`
  mutate verb (`agent_facing=False`): registration-exists check (absent →
  loud refusal), server-side sha bind, `status_at_record` stamp, fail-open
  recording against stale/revoked registrations (disclosed). Fire tests:
  asserted-sha mismatch refused; absent registration refused; stale
  registration recorded-and-stamped, never refused.
- **T5** `ops/conformance/status_op.py` + `ops/conformance_render.py` (new) —
  the query verb + the deterministic brief (range-phrased, both sides'
  evidence labels verbatim, no urgency/recommendation vocabulary — token pin
  as in evidence-memory T4). Write-probe test: the query creates and mutates
  nothing.
- **T6** `state/registration.py` — the `conformance` declaration block
  (STRUCTURE-ONLY validation; unknown keys refused) + the horizon consult
  in the reduction (`now` param, additive with a safe default so existing
  callers are untouched; `horizon-lapsed` cause) + the
  `registration-review` record's reduction (newest current horizon wins).
  Serialized behind all registration-kernel work on the file.
  **Pre-implementation verification (2026-07-07): the baseline-membership
  check does NOT live here.** The state substrate never imports `ops`
  (zero such imports exist tree-wide — evidence-memory's "dispatch
  placement" correction records the same split), and
  `compute_dossier_signature` is an `ops/export_dossier.py` seam; the
  membership check is the append GATE's recompute leg (C-declare already
  says "the append gate verifies") and lands in T7, composed at the ops
  caller exactly as evidence-memory composes its `dossier` resolver.

**Wave C (sequential — hot files, one at a time):**

- **T7** `ops/decision/journal.py` —
  `_assert_conformance_verdict_authorship` + the `registration-review` floor
  (C-horizon's recompute: live dossier signature re-gather), wired beside
  `_assert_registration_authorship`; PLUS (moved from T6, pre-implementation
  verification 2026-07-07) the `conformance` declaration's
  baseline-membership recompute leg at the registration append — the
  `(path, sha256)` pair checked against `compute_dossier_signature`'s
  entries, an extension of the R6 lock-2 legs. Touchpoint to check at
  implementation: if registration T7 landed a block-family convention for
  the `"registration"` scope (R6's "block convention, both directions"),
  its allowed-block set gains `"conformance-verdict"` and
  `"registration-review"` in this same commit — otherwise the new blocks
  are refused by the sibling's mirror. Fire tests per lock: fabricated
  receipt sha, empty cites, bare ack, missing sha prefix,
  review-of-a-drifted-dossier refused, baseline path/sha not in the
  manifest refused at registration.
- **T8** `ops/attention_queue.py` — the two kinds (C-queue): `KIND_CLASS`
  entries, collectors routing through `judge_window` (route-through pin),
  fan-out 0 (assert the fanout walk gains no conformance edge), D5-table
  rows. Serialized behind registration T8 / fingerprint T7 / evidence-memory
  T10.
- **T9** `ops/export_dossier.py` — the `live-conformance` source noun +
  the `_EXPECTED_SOURCES` same-commit pair-edit (C-dossier).
- **T10** `tests/contracts/test_conformance_boundary.py` (new) — the
  enforcement suite (rows below), **the no-actuation pin first**, + TOY
  fixtures under `tests/fixtures/toy_conformance/` — the instrument-QC case:
  a fake sensor (`sensor-7`), a sealed calibration-readings baseline, a live
  readings stream, a drift scenario driving: register with declaration →
  record conforming stream → `conformance-status` conforming → drifted
  readings → nonconforming FINDING (registration status BYTE-UNCHANGED) →
  human verdict via append-decision → queue item clears → horizon lapses →
  registration reads `stale (horizon-lapsed)` → `registration-review` cures
  it → re-registration embeds the ledger. Never trading vocabulary.
- **T11** skill prose — the verdict-relay + conclusion-encouragement steps
  (whichever skill owns the morning read; the evidence-memory T12 form);
  skill lints.
- **T12** this doc — status flip + drift log.

Regen tails: ALL SIX regen scripts after T4/T5 (registry +2); schema regen
for T2; `_SPEC_VERBS` inventory tails; primitive doc pages
(`docs/primitives/conformance-record.md`, `conformance-status.md`).

## Enforcement rows (accrue to `docs/internals/engineering-principles.md`)

| Rule | Enforced by | Fires when |
|---|---|---|
| **NO ACTUATION AFFORDANCE (first-class — the agency boundary mechanized):** no verb, chain, next_block, or skill in the conformance surface mutates anything beyond the one ledger append; no core code path reaches a broker/instrument/external system (no network client, no credential field in any conformance module); a `nonconforming` verdict changes NO registration status, revokes nothing, halts nothing — drift routes, never acts | `tests/contracts/test_conformance_boundary.py` (registry scan: no mutate verb beyond `conformance-record`, whose sole side effect is the append; import/AST scan over `state/conformance*` + `ops/conformance*` for network/subprocess reach; behavioral: registration status byte-identical before/after a nonconforming window) | a halt/pause/recalibrate/deploy affordance lands, or a verdict grows a side effect — the substrate reaching past the chart into the machine |
| Observation receipts route through the ONE attestation kernel — append binds via `state/attestation.py::bind`; the payload sha is server-recomputed, never trusted from the wire | T4 fire tests (asserted-sha mismatch refused) + route-through assertions | a receipt path bypasses `bind`, or the record verb trusts a caller sha |
| ONE envelope definition: the baseline envelope routes through the shared order-statistics helper in `state/determinism.py`; `judge_window` never re-inlines min/max/spread math | T1/T1a `inspect.getsource` route-through pins + the T1a byte-equality test | a second envelope reduction appears — the limits forking from the fingerprint's |
| The baseline is SEALED and FIXED: it resolves only against the dossier-membership-checked artifact; live observations never enter it; no admission path exists | T10 fire tests (a baseline path/sha not in the manifest refused at registration; recording N live receipts changes no baseline envelope byte) | a "learning limits" or rolling-baseline branch lands — production drift laundered into the limits that exist to catch it |
| No invented window, cadence, or threshold: window selection is caller `{since/until/last_n}` + the declared `min_window_n`; the only mechanized evidence bar is the fingerprint's existing n>=3; no other numeric literal in the classifier | AST pin over `state/conformance.py` (no tolerance/threshold literal) + behavior tests | a "reasonable default" window or closeness heuristic lands anywhere in core |
| **No control RULES in core**: no consecutive-point, trend, or run-rule logic — per-point and per-window arithmetic only; sequential policy is caller/pack territory | source scan (no sequence-pattern branch over verdict streams in `state/conformance.py` / `ops/conformance*`) + a behavior test (8 consecutive near-limit points inside the envelope stay `conforming`) | a Western-Electric-style rule is "helpfully" mechanized — domain policy calcifying into core |
| A thin window or thin baseline never auto-verdicts, in either direction: insufficient/novel/incomparable → `needs_verdict`, never `conforming` or `nonconforming` | fire tests: deviation outside the envelope at `window_n < min_window_n` → `needs_verdict`; inside likewise | the classifier fabricates a verdict from evidence it disclosed as insufficient |
| Verdicts are DERIVED, never stored; the query is watermark-neutral and store-free | T5 write-probe + a recompute-equality test (two reads, no state) | a verdict cache/store becomes load-bearing, or a read marks anything seen |
| No verdict verb; the human resolution is `append-decision` (block `"conformance-verdict"`) facing the sha-prefix bar; the record verb is `agent_facing=False` | the no-unlock-verb registry pin form + T7 authorship fire tests + an `agent_facing` pin on `conformance-record` | a resolve/accept verb appears, or the record verb becomes an agent tool — the agent authoring the evidence that judges its own registration |
| No market vocabulary anywhere: wire schemas pass the `_FORBIDDEN_FIELD_NAMES` walk; fixtures are instrument-QC toy only (token denylist over `tests/fixtures/toy_conformance/` + the conformance test files) | the dossier-suite walk mirrored + the toy-domain denylist scan | a fill/order/position/pnl-shaped name lands in core or fixtures |
| The horizon is a timestamp comparison in the ONE registration reduction; core never names a period or computes a cadence | T6 tests (lapse → `stale (horizon-lapsed)`; review record cures; drifted-dossier review refused) + a no-duration-vocabulary pin | a "review every N days" field or named-regime sugar lands core-side |

## Boundary-drift flags (the Q1 watch list — written before implementation)

- **No actuation affordance, ever — drift routes, never acts.** Pressure to
  make core "just pause the strategy" or "flag the instrument out of service"
  is the feature working: the operator adjusts, the chart never touches the
  machine. Any actuation is a caller-side consumer of `conformance-status`,
  exactly as the deployment refusal is a caller-side consumer of
  `verify-registration` (R8).
- **Control RULES stay caller-side forever.** The moment core counts
  consecutive points, detects a trend, or ships an alarm policy, domain
  judgment has calcified into mechanism. Core exposes the verdict stream;
  rules live above it with owned authorship.
- **Windows are caller config.** No default window, no "recommended" span,
  no adaptive sizing. `min_window_n` and `{since/until/last_n}` are the whole
  surface; anything smarter is a caller's owned statistics.
- **The emitter is never core.** No connector, no feed adapter, no
  "convenience" polling loop, no credential field. The day core imports a
  brokerage or instrument SDK, the arms-length boundary is gone.
- **The baseline never self-updates.** No rolling re-baseline, no
  "incorporate accepted observations" path — re-baselining is
  re-registration, the full human bar, forever.
- **`nonconforming` never rots into auto-revocation.** A finding informs the
  human and the queue; the registration's status moves only by drift
  recompute, horizon lapse, supersession, or an explicit human revoke (R7).
  Wiring the verdict to the status is actuation through the side door.
- **No market vocabulary in core or fixtures.** Keys, labels, emitter ids,
  notes: opaque. The instrument-QC toy is the only fixture domain; the moment
  a core branch or a fixture knows what a fill is, quant assumptions have
  calcified.
- **No capital-shaped fan-out.** Leverage is counted from encoded edges or
  it is zero; a "this one is worth more" weight is the urgency-score
  fabrication class. The walk gains edges when journals encode them, never
  opinions.
- **The evidence brief stays range-phrased and dual-labeled.** No σ, no
  p-value, no confidence interval composed core-side; both sides' n and dates
  on every line. Sharper statistics layer above with disclosed authorship.

## Related docs

- `docs/design/registration-kernel.md` — the subject under test: R2
  (dossier-sha binding), R6 (the authorship bar reused), R7 (the remedies —
  this plan adds no new ones), R8 (the stale-registration queue item the
  horizon cause rides), the R9 scope-kind test C-verdict applies.
- `docs/design/determinism-fingerprint.md` — the envelope machinery pointed
  at production: the order-statistics-only posture, the tiered classifier,
  the D-consume admission rule this plan deliberately does NOT extend (the
  sealed-baseline divergence, recorded in C-compare).
- `docs/design/evidence-memory.md` — where drift verdicts compound into
  dated priors; the E1 conclusion the verdict flow encourages.
- `docs/design/attention-queue.md` — the collector rules (D5 route-through,
  no urgency, fan-out from encoded edges only) C-queue satisfies.
- `docs/design/notebook-audit.md` — D9 (the emitter/execution-contract
  pattern), the F8 caller-attested-truthfulness honesty, the hand-off form
  this document follows.
- `docs/design/slate-sequencing.md` — this plan slots AFTER the slate's
  registration (Phase 2) and fingerprint (Phase 3) phases; the hot-file
  serialization it inherits.
- `docs/internals/harness-contract.md` — the sha canonicalization every
  `content_sha` here uses; the capability-1 authorship tiers the verdict gate
  inherits.
- `docs/internals/engineering-principles.md` — the Q1 boundary the flags
  patrol; the enforcement maps the rows accrue to.

## Implementation drift log

- **Fifth-pass adversarial verification 2026-07-08 (independent Opus sweep;
  no code had landed) — GO.** Anchors verified present: `evidence_meets` and
  its `{min_n, scales, clusters}` demand vocabulary, `detect_code_drift`,
  `count_prior_looks`, and the conformance-record status-stamp reduction
  path. Phase 7 lands after the registration/fingerprint machinery it amends,
  so its dependencies are warm. Hardest attacks that FAILED to find a defect:
  (a) "sealed baselines admit NOTHING contradicts the fingerprint admission
  rule" — refuted; the divergence is deliberate and recorded, and the
  admission rule is explicitly scoped to the fingerprint envelope only;
  (b) "the shared order-statistics envelope helper forks" — refuted; it is
  one definition reused from fingerprint T1a per the reuse ledger. No defect
  surfaced.

(Populate per deviation, each with its recorded reason, when
implementation lands. The `docs/design/notebook-audit.md` drift log is the
form to follow.)
