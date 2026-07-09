---
status: plan
---
# Design: the determinism fingerprint — measure, don't ask

Status: **PLANNED (2026-07-07), not yet implemented.** User-co-designed
2026-07-07; the decision center below is SETTLED — treat departures as drift
to be logged, not re-litigated. This document is the durable hand-off (the
`docs/design/notebook-audit.md` pattern): settled decisions + rationale,
file-disjoint Opus task waves, enforcement rows, boundary-drift flags. Facts
cite `path::symbol`; where this doc and shipped code disagree, the code and
its enforcement-mapped tests win.

## Problem

Reproduction tolerances are today CALLER CONFIG: `verify-reproduction`
compares two runs' reduced metrics under a caller-owned
`ReproTolerance` (`_wire/queries/verify_reproduction.py::ReproTolerance`,
consumed by `ops/verify_reproduction.py::_resolve_key_tol`), and with no
tolerance supplied it compares floats EXACTLY. That posture is honest — core
never invents what a metric means — but it pushes a question onto the human
that the machinery can *measure*: **how much does this experiment's output
actually move when you run it twice, unchanged?** The human is asked to
divine a tolerance a priori; the framework already runs the code (the submit
canary) and could observe the answer.

The fix is the **determinism fingerprint**: a measured, accumulating,
confidence-labeled record of an experiment's observed run-to-run spread,
minted by the machinery and consumed by a tiered verdict classifier — so the
simple mass of reproduction verdicts is mechanized (the auto-mode /
D-attention pattern), the genuinely ambiguous residue routes to the human
WITH calibrated evidence, and a mismatch stays a FINDING exactly as today.

The boundary is unchanged: core measures, classifies by structure, and
compares — it still never names a metric, never privileges one, and never
*invents* a tolerance. Every number in the envelope is an OBSERVATION.

## The settled design center (DECIDED, 2026-07-07)

### 1. Measure, don't ask — the fingerprint, two tiers

Tolerances stop being primary caller config. The machinery mints a
DETERMINISM FINGERPRINT per experiment identity:

- **STATIC tier** — outputs classified by structure/type from the reduced
  metrics artifact (`_aggregated/<run_id>/metrics_aggregate.json`, the
  L2-closed comparator input every reduce path now persists via
  `ops/aggregate_flow.py::_persist_local_aggregate`): key sets and shapes
  are ALWAYS exact; ints / strings / bools compare exact; floats are the
  only *tolerance-class-eligible* leaves. Free, domain-agnostic, computable
  from a single sample. The static tier assigns a float NO tolerance — it
  only marks which keys an empirical envelope may ever apply to. A float
  with no empirical evidence still compares EXACTLY (the no-invented-
  tolerance rule).
- **EMPIRICAL tier — the DOUBLE CANARY.** The submit flow's existing canary
  (`ops/submit_flow.py::_should_run_canary` → the S2 detached worker →
  `ops/verify_canary.py::verify_canary`) runs TWICE; the per-metric diff of
  the two executions is the first fingerprint sample. Byte-identical →
  per-key class `exact`; float jitter → class `stochastic` with the
  observed spread as the envelope. Cost: exactly one extra canary
  execution, only on submits where a canary would run anyway.

The fingerprint is a **CODE ATTESTATION** (the render-receipt pattern,
`state/notebook_audit.py::record_render_receipt`): each sample is journaled
append-only, bound via `state/attestation.py::bind` (the recompute lock — a
sample's `content_sha` is recomputed from the on-disk metrics artifacts at
append time, so a spread cannot be asserted into existence), and
drift-revoking via the kernel's staleness POSTURE — a code-identity change
reads prior samples STALE, implemented by T1's CURRENT-identity filter over
the ledger (see D-consume).

Rejected: **asking the caller for a tolerance as the primary source.** The
caller-owned `ReproTolerance` DEMOTES to an explicit override — still
accepted, but recorded verbatim in the receipt as `tolerance_spec` (it
already is) and labeled `caller_override` in the verdict, never silently
blended with measured evidence.

### 2. Accumulating, confidence-labeled — never a one-shot truth

The n=2 honesty (user-refined): the double canary's n=2 at submit is a
**labeled PRIOR**, not a truth.

- Byte-identical-twice honestly supports the `exact` class (identity
  observed is identity, whatever n).
- A stochastic envelope at n=2 is WEAK and says so: the envelope record
  carries its evidence `{n, scales, clusters}` and every consumer reads
  the label before trusting the width.
- **Every subsequent reproduction APPENDS a sample**: a full
  `verify-reproduction`, a partial reproduction, a cross-cluster
  reproduction — each comparison's per-key observed values become one more
  sample in the ledger. The fingerprint converges by being used.

Recorded n=2 failure modes (carry these verbatim into the module docstring
and the evidence brief renderer — they are WHY the envelope must stay
labeled):

1. **Rare-event nondeterminism** — a race or rare branch that fires once in
   many runs looks `exact` at n=2 and is not.
2. **Canary-scale ≠ main-scale regimes** — BLAS/GPU libraries select
   algorithms by problem size; a 1-task canary's spread can differ in kind
   from the main array's. Samples record a `scale` label; an envelope with
   only canary-scale evidence is THIN for a main-scale verdict.
3. **Same-node correlated samples** — the double canary's two executions
   may land on the same node/SKU; the n=2 prior records
   `same_submission: true` so the classifier treats it as one environment
   observed twice, not two.

Rejected: **a fitted distribution at small n.** No stddev, no Gaussian, no
confidence interval computed from 2 points. The envelope at any n is the
OBSERVED RANGE (per-key min/max, plus the derived relative spread) with the
labeled n — an honest description of what was seen, never an extrapolation.
(Enforcement row below pins that the envelope reducer computes order
statistics only.)

### 3. The tiered verdict classifier (the D-attention pattern)

User directive: **mechanize the simple mass to kill decision fatigue —
NEVER total mechanization.** `verify-reproduction`'s verdict becomes
three-tiered, mirroring `docs/design/notebook-audit.md` D-attention
(`auto_cleared` / human-required) and the auto-mode classifier:

- **`auto_cleared`** — a code attestation, zero human attention:
  (a) exact match on every key (the identity fold — today's `match`), or
  (b) every float deviation comfortably inside a **well-evidenced**
  envelope. *Well-evidenced* is mechanized, never judged: `n >= 3` AND the
  compared run's `scale` label appears in the envelope's evidence scales
  AND its cluster appears in the evidence clusters. The auto-clear is
  journaled through `state/attestation.py::bind` exactly like a notebook
  auto-clear — mechanical, hash-bound, never claiming human review.
- **`needs_verdict`** — routed to the human WITH the calibrated evidence
  brief (code-rendered, the `ops/relay_render.py` posture): *"0.4%
  deviation vs ±0.3% envelope at n=2 canary-scale"*. Triggers, all
  mechanical:
  - any deviation (inside OR outside) against a THIN envelope (n < 3, or
    scale novelty, or cluster novelty) — **a thin envelope produces
    needs_verdict items, never wrong auto-verdicts** in either direction;
  - `incomparable` keys (one-sided / NaN / type-changed), today's
    incomparable fold;
  - a cross-cluster deviation — labeled **environment-sensitivity
    FINDING** in the brief, not a failure (decision 5).
  The human's verdict lands as an ordinary `append-decision` record on the
  run scope (block `reproduction-verdict`) — **no verdict verb exists**
  (the no-unlock-verb doctrine, `docs/design/rigor-primitives.md`), and the
  item surfaces in the attention queue as a VERDICT-class kind (integration
  point: `ops/attention_queue.py` collectors — see the task list).
- **`mismatch`** — a deviation outside a WELL-EVIDENCED envelope, or an
  exact-class key that moved. A FINDING, `needs_decision=True`, exit-0 —
  the existing posture (`ops/verify_reproduction.py` module docstring:
  "discovered nondeterminism is the feature working"), byte-unchanged.

The exact-match fast path costs the human nothing; the honest middle is
concentrated where judgment actually happened — rarity buys seriousness
(the D-attention rationale, verbatim).

Rejected: **a fractional "near-boundary" heuristic** (e.g. flag deviations
> 80% of the envelope). That is an invented threshold — a tolerance core
made up. The ONLY needs_verdict triggers are evidence-thinness, novelty,
and incomparability: all mechanical properties of the record, never a
judgment about closeness.

### 4. Anti-gaming by disclosure

A deliberately-wide envelope is self-defeating: the envelope — width, n,
scales, clusters — is **DISCLOSED verbatim at graduation / registration**
as evidence. "Reproduces only within ±30% (n=4, canary-scale, 1 cluster)"
is a statement about the experiment, printed where reviewers look (the
dossier: `ops/export_dossier.py` gains a `determinism-fingerprint` source
noun; the closed-set `_EXPECTED_SOURCES` pin in
`tests/contracts/test_dossier_boundary.py` updated in the same commit).

Cross-reference: **`docs/design/registration-kernel.md`** (concurrent). A
registration can DEMAND evidence tiers: e.g. `fingerprint at main-scale
n>=3`. The fingerprint module exposes ONE pure predicate for it —
`evidence_meets(samples, demand) -> (bool, shortfall)` — so the
registration kernel consumes evidence without re-implementing the envelope
reduction (the one-definition rule). The demand vocabulary is
caller-authored `{min_n, min_n_full?, scales, clusters}` (plural `scales` /
`clusters`); `min_n` counts n_full + n_partial samples both, and the
optional `min_n_full` demands scale-quality — full (non-partial) samples —
separately, over the `n_full` leg the evidence block already isolates. Core
matches by identity and counts, never interprets.

**S5 demotion (note, required by this design):** the tolerance-defaults
seam **S5 in `docs/design/domain-packs.md`** is DEMOTED by this design from
primary source to fallback — domain-packs.md already anticipates this
("Related, planned separately": *"The determinism fingerprint — may demote
the tolerance-defaults seam (S5) from primary source to fallback; the S5
resolver is designed to be removable"*). Resolution order after this
feature (settled 2026-07-07, coherence review — one order across this doc,
matching D-consume): **caller explicit override (labeled + disclosed) >
measured envelope (well-evidenced) > pack S5 default > exact.** An explicit
owned override outranks a measurement because a HUMAN owns it — disclosure
(the verdict's `caller_override` label, printed verbatim) is what keeps it
honest; the measured envelope outranks everything UNOWNED, so a pack default
NEVER outranks a measurement.

### 5. Derived subsets — partial reproduction folds in (the old T8)

Partial-reproduction subset selection derives MECHANICALLY from the axes
(`state/axes.py` + the DataAxis machinery / `compute_wave_map`'s row-major
task-id encoding): the canary task (task 0) plus a deterministic stride per
axis value — for each axis, one task per distinct axis value at a fixed,
reproducible stride over that axis's coordinate range. Caller-specified
subsets (`task_sample` on the reproduce spec) are allowed and win. **Core
NEVER invents a "representative" heuristic** (boundary question Q1): no
importance sampling, no metric-aware selection — the subset is a pure
function of the axis structure or the caller's explicit list.

Comparison stays PER-TASK, never pooled-vs-subset — the
`docs/design/reproduction-receipt.md` decision ("Partial reproduction
compares PER-TASK") is inherited unchanged; this design supplies the
subset-derivation mechanism that doc deferred.

**Receipts record partiality LOUDLY** (no-silent-caps): `partial: true`,
the exact task indices compared, and what was NOT compared (the uncompared
key/task counts) on every partial receipt. A partial sample appended to the
fingerprint carries the same `partial` label; the envelope evidence counts
partial and full samples separately (`n_full`, `n_partial`).

**The fingerprint records the measuring cluster** per sample. Cross-cluster
spread is an **environment-sensitivity FINDING**, never a reproduction
failure: a deviation whose only novelty is the cluster routes to
needs_verdict with the env-sensitivity label; the human's accepted verdict
records the cluster split, and the envelope evidence thereafter carries
per-cluster membership so the classifier can tell "this experiment is
cluster-sensitive" from "this run broke".

## Decisions settled in this document

### D-store — where the fingerprint lives

**Experiment-local, append-only, beside the metrics it describes** —
`<experiment>/_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl`, one ledger
per experiment identity, each line one sample record. The
one-store-no-migration posture, applied:

- The fingerprint is a **durable scientific record**, exactly the class the
  reproduction receipt settled journal-vs-local for
  (`docs/design/reproduction-receipt.md`, "The receipt lives
  experiment-local"): the decision journal is wipeable control state; the
  measured determinism of an experiment must survive a journal wipe.
- The subject is the experiment **identity**, not one run: samples from the
  original's double canary and from every later reproduction accumulate to
  the SAME ledger, so it cannot key on `run_id`. It keys on `cmd_sha`
  (param identity, `state/run_sha.py`) with the full identity fields
  (`cmd_sha`, `tasks_py_sha`, `executor`) inside every record — the same
  verbatim-lift discipline as `_IDENTITY_FIELDS` in
  `ops/verify_reproduction.py`.
- Append mechanics reuse the receipt idiom verbatim: advisory flock +
  fsync, one JSON line, no dedup. *(Pre-implementation verification
  2026-07-07: the shared helper already exists —
  `infra/io.py::append_jsonl_line`, landed with the harvest-marker fix; T3
  is a RE-POINT of `ops/verify_reproduction.py::_append_receipt` and the
  new ledger onto it, never a third definition.)*
- **No new decision-journal scope kind.** The human's needs_verdict
  resolution rides the EXISTING run scope via `append-decision` (block
  `reproduction-verdict`) — the journal keeps control decisions, the ledger
  keeps measurements. One store each, no migration anywhere.

Rejected: a notebook-style journal scope for samples (wipeable; wrong
store for scientific record), and a machine-global home under the journal
homedir (the fingerprint is per-experiment evidence and must travel with
the experiment's `_aggregated/` results).

**Sample record shape** (schema_version 1, append-only ledger — bump on
shape change, the `RECEIPT_SCHEMA_VERSION` convention):

```json
{"ts": "...", "schema_version": 1,
 "attestor": "code", "subject_kind": "determinism-fingerprint",
 "subject_id": "<cmd_sha>", "content_sha": "<sha over the compared artifacts, canonical form>",
 "identity": {"cmd_sha": "...", "tasks_py_sha": "...", "executor": "..."},
 "source": "double-canary" | "verify-reproduction",
 "run_ids": ["<a>", "<b>"],
 "cluster": "<measuring cluster>", "scale": "canary" | "main",
 "verdict": "auto_cleared" | "needs_verdict" | "mismatch",
 "same_submission": true,
 "partial": false, "task_indices": null,
 "per_key": [{"key": "...", "a": 1.0, "b": 1.0002,
              "abs_diff": 0.0002, "rel_diff": 0.0002,
              "static_class": "float" | "int" | "str" | "bool" | "shape"}]}
```

The attestation fields make each line a valid
`state/attestation.py::validate` record; the append routes through
`bind` with `recompute` = the canonical sha over the two COMPARED on-disk
payloads (the harness sha canonicalization,
`docs/internals/harness-contract.md` §"The sha canonicalization" —
`json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)`,
SHA-256 lowercase hex). Which artifacts, per source (pre-implementation
verification 2026-07-07 — the old text said `metrics_aggregate.json` for
both, but a canary has no aggregate): for `verify-reproduction` samples,
each side's artifact-ladder payload; for `double-canary` samples, the two
locally-FETCHED canary task-0 `metrics.json` payloads (D-double-canary's
pull). Scale labels are assigned mechanically, never judged: `scale:
"canary"` for `double-canary` samples, `scale: "main"` for
`verify-reproduction` samples (a partial reproduction is main-scale with
`partial: true` — partiality and scale are separate axes). `verdict` is
the comparison's verdict AT APPEND (D-consume clause 1 — judgment always
precedes append): `auto_cleared` for a double-canary pair whose both
executions verified ok (the passing code verdict; a failed second canary
blocks the submit and appends nothing). A sample cannot be recorded for
artifacts that are not on disk saying what the sample claims.

### D-envelope — the math at small n (honest choice, recorded)

The envelope for a key is the **observed range**: `lo = min(observed)`,
`hi = max(observed)` over all CURRENT-identity samples, plus the derived
max relative spread — order statistics ONLY, computed fresh at every read
(the envelope is DERIVED, never stored state; no staleness, no migration).
Labeled with its evidence `{n, n_full, n_partial, scales: [...],
clusters: [...], same_submission_only: bool}`. **Never a fitted
distribution**: no mean±kσ, no interval estimate, at any n — a range plus
its n is exactly what was observed and nothing more; the classifier's
well-evidenced bar (n>=3 + scale + cluster coverage), not a wider
synthetic envelope, is what guards the weak-n case. A per-key class:

- `exact` — every sample pair identical on this key (and the static class
  is not float, or is float with zero observed spread).
- `stochastic` — any nonzero float spread observed; the range is the
  envelope.

**Resolution disclosure — the error-direction asymmetry (added 2026-07-07,
user review).** There is NO near-boundary proximity trigger (rejected above
as an invented tolerance); a value is inside the range or outside it. The
two error directions are treated asymmetrically, deliberately:

- **False negative** (legitimate sample outside the observed range): under
  exchangeable sampling a fresh legitimate sample exceeds the observed
  min/max of n priors with probability ≈ 2/(n+1) — HIGH at small n, and
  that is fine because the cost is a HUMAN REVIEW with the evidence brief
  (a finding), never a silent kill; the human's acceptance admits the
  sample (D-consume admission rule) so the instrument self-corrects and
  the rate falls as n grows.
- **False positive** (real drift landing inside the range): a drift
  smaller than the spread of previously-accepted behavior is BELOW THE
  RESOLUTION OF THE EVIDENCE — undetectable by any method without
  assumptions. The guarantee is therefore not "no false positives" but
  **no undisclosed resolution**: every verdict's receipt records
  `envelope_applied` (the exact range + evidence weight that judged it),
  so a consumer needing finer resolution refuses the envelope rather than
  trusting it (`evidence_meets` at registration). Because judgment always
  precedes append and the ledger is append-only, the envelope at any past
  verdict is reconstructible — a too-wide envelope discovered later
  identifies every auto-clear that relied on it (retrospective
  revocability, which no point-in-time interval estimate provides).

Statistics are not banned — FABRICATING their parameters in core is: the
ledger preserves raw samples so a human, a pack, or a registration
requirement can run a real equivalence test on top with an alpha someone
OWNS. The order-statistics envelope is the floor requiring no fabricated
parameter; anything sharper layers above it with disclosed authorship.

### D-double-canary — integration with the existing canary flow

- **Where — pinned to the real code structure (pre-implementation
  verification 2026-07-07)**: the S2 detached worker runs
  `ops/submit_and_verify.py::submit_and_verify(stop_after_canary=True)`;
  the canary QSUB lives in `ops/submit_flow.py` (the `_should_run_canary`
  branch) but the VERIFY — and therefore the "after the first canary
  verifies `ok=True`" seam — lives in `submit_and_verify`, between
  `_mark_canary_terminal(..., status="complete")` and the
  `stop_after_canary` return. The second execution CANNOT be a naive
  re-call of `submit_flow` (its canary leg hardcodes `<run_id>-canary` and
  the existing-canary replay branch would reuse the just-completed first
  canary instead of firing a second): extract/parametrize the canary
  submission leg of `submit_flow` (the `_mirror_canary_sidecar` +
  `_make_single_array_submission` + `submit_and_record` sequence) so
  `submit_and_verify` can fire run_id `<main_run_id>-canary2` (mirror its
  sidecar exactly as `-canary`'s), verify it with the same
  `ops/verify_canary.py::verify_canary`, and close its RunRecord via the
  `_mark_canary_terminal` sibling. **`expect_output` / `fingerprint`
  trap:** `verify_canary` REFUSES an `expect_output` that does not contain
  the canary run_id, and a path built for `-canary` does not contain
  `-canary2` — the second verify must substitute the `-canary2` run_id
  into those paths (or omit them; the completion count still verifies
  output). **The metrics artifacts are REMOTE**: a canary writes its
  task-0 `metrics.json` under the cluster `results/<canary_run_id>/…`
  tree, and `verify_canary` only sha-fingerprints it over SSH — so the
  worker must FETCH both canaries' task-0 `metrics.json` payloads locally
  (the `rsync_pull`/`ssh_run` idiom `_per_task_metrics_reduce` already
  uses), landing them under `_aggregated/_fingerprints/_pulls/<run_id>/`
  so the sample's `bind` recompute has on-disk artifacts to re-hash. Then
  per-metric diff the two local payloads (T1) and append the n=2 prior
  sample (T3). A failed FIRST canary short-circuits as today (no
  fingerprint, no main array); a failed SECOND canary is itself a loud
  finding (the same code passed then failed — nondeterminism observed the
  hard way) and blocks exactly like a failed first canary. (The second
  verify's success re-records the same canary-cache key
  `record_canary_validated` already wrote — idempotent, harmless.)
- **Cost accounting**: one extra 1-task canary execution per fingerprint-
  minting submit. It runs CONCURRENTLY with nothing (sequential after the
  first verifies) — a submit that canaries pays roughly 2× canary
  wall-clock once per `cmd_sha`; the canary cache amortizes it exactly as
  it amortizes the first.
- **Canary-cache interaction** (`state/canary_cache.py`): a validated-fresh
  skip (`is_canary_validated_fresh`) skips BOTH executions and mints NO
  sample — the fingerprint simply doesn't grow on that submit. The cache
  key is untouched. `force_canary` / `HPC_AGENT_ALWAYS_CANARY` re-run both.
- **Opt-out**: yes, operator-grade — `HPC_NO_DOUBLE_CANARY=1` (the
  `HPC_NO_CANARY_SKIP` idiom) reverts to the single canary; no
  agent-reachable spec field disables it (the #283 posture: operator env
  wins, the agent gets no lever to skip evidence collection).
- **Canary exclusion**: the aggregate's canary-exclusion machinery must
  ALSO exclude `-canary2`, or the second canary's row lands in the main
  run's future mean (the run-#6 11-row-mean class, re-opened). **The ONE
  suffix definition is `ops/monitor/reconcile.py::_sibling_run_ids`** —
  `ops/aggregate_flow.py::_per_task_metrics_reduce` deliberately routes
  through it ("never a second hardcoded `-canary`"), so the
  generalization lands THERE (return every `-canary`-family sibling), and
  every `(canary_id,) = _sibling_run_ids(...)` single-unpack call site
  must be updated in the same commit or it CRASHES on the widened return:
  `ops/aggregate_flow.py`, `ops/supersession.py` (two sites),
  `ops/resolve_submit_inputs.py`, plus the loop consumers in
  `ops/monitor/reconcile.py` and `ops/status_blocks.py`. Side benefit,
  REQUIRED: reconcile's sibling-settling loop then settles the `-canary2`
  journal entry too — otherwise an unsettled `-canary2` record blocks the
  next submit (the run-#7 stall class). Fire test: a planted `-canary2`
  row must not enter the main reduce. This is a REQUIRED same-commit
  change of T4.

### D-consume — verify reads ALL samples, every time

`verify-reproduction` loads the whole ledger for the pair's identity,
filters to CURRENT-identity samples (identity fields equal to the pair
being compared — a `tasks_py_sha` drift reads prior samples STALE via the
kernel's posture: stale samples are excluded from the envelope, retained in
the ledger as history), reduces the envelope fresh, classifies, verdicts,
appends the receipt (schema_version 2 — the receipt gains
`envelope_applied` per key and the tier verdict), and **appends this
comparison as a new sample**. All-samples, not newest-reduction: the
fingerprint is EVIDENCE, not state — every honest observation counts, and
the newest sample has no special authority over the envelope.

**D-consume ADMISSION RULE (added 2026-07-07 — the self-laundering close,
surfaced by user review).** Two clauses, both load-bearing:

1. **Judge BEFORE append.** A comparison is always classified against the
   PRIOR evidence only; its own sample never participates in the envelope
   that judges it. (Ordering, not just bookkeeping — an envelope that
   includes the sample under judgment is self-justifying.)
2. **ONE admission rule: a sample joins the envelope iff its comparison
   received a PASSING verdict** — code's (`auto_cleared`) or an explicit
   human acceptance recorded via append-decision (which faces the
   authorship gate — deliberately effortful, the D-attention bet that
   rarity plus typing cost buys seriousness). Consequences, spelled out:
   an unresolved `needs_verdict` sample is recorded-but-inadmissible; a
   `mismatch` sample is inadmissible UNLESS a human explicitly accepts it
   (e.g. judged an environment-sensitivity finding, not a drift) — the
   same ticket, no special case; nothing is ever admitted silently.
   Follow the naive all-samples rule adversarially to see why: enough
   noisy reproductions and every mismatch widens the envelope until it
   swallows the drift it exists to catch — laundering through
   accumulation. Inadmissible samples remain in the ledger as disclosed
   findings — visible in the envelope's evidence block as
   `excluded_unadmitted: n` (the no-silent-caps posture) — informing the
   human, never the auto path. CRUCIALLY this creates NO new decision
   point: admission always rides a verdict the tiering already routed
   (code's, or the human resolution the human was making anyway); the
   needs_verdict rate is front-loaded and decays as admitted evidence
   accumulates. Enforcement row: the admission rule has one definition,
   with fire tests that an unadmitted sample does not change the reduced
   envelope and that a human-accepted one does.

**Admission MECHANICS, pinned (pre-implementation verification 2026-07-07
— the rule above was implementable only with a join the doc never
specified).** Admission is computable from two reads, joined on the
sample's `content_sha`:

- A sample whose `verdict` field (D-store) is `auto_cleared` is admitted at
  append — code's passing verdict, no join needed. `double-canary` samples
  are `auto_cleared` by construction when both executions verified ok
  (without this clause the n=2 prior could never enter any envelope and the
  submit-time tier would be dead weight — the internal contradiction this
  pin closes).
- A `needs_verdict`/`mismatch` sample is admitted iff the REPRODUCTION
  run's decision journal (the run scope of the sample's second `run_ids`
  member — the run whose receipts ledger holds the comparison) carries a
  `reproduction-verdict` record whose `resolved` names the sample's
  `content_sha` TOKEN-EXACT and `accept: true`. The content_sha is the join
  key precisely because it is bind-locked — an acceptance cannot name
  evidence that was never on disk.
- **The join lives in the STORE layer (T3), never in T1**: T1's envelope
  reduction and `evidence_meets` are pure over
  `(samples, admitted_flags)`-shaped input; T3 computes the flags by
  reading the ledger + the named run journals. `evidence_meets` counts
  ADMITTED, CURRENT-identity samples ONLY (its `n`/`n_full`/`scales`/
  `clusters` are the admitted evidence — an unadmitted sample can never
  satisfy a registration demand).
- **The acceptance record is GATED (new task T12).** Verified against
  `ops/decision/journal.py`: arbitrary blocks face NO authorship gate today
  (`_assert_human_authorship` gates REQUIRED_CALLER greenlight fields;
  `_assert_unlock_authorship` / `_assert_signoff_authorship` gate their own
  blocks) — so without a gate an AGENT could append the acceptance and
  launder a mismatch into the envelope, the exact accumulation attack this
  section exists to close. T12 adds `_assert_reproduction_verdict_authorship`
  beside its siblings, same structure: block `reproduction-verdict` refused
  for any `scope_kind` other than `"run"` (and vice-versa nothing else
  claims the block); bare acks refused (`_is_bare_ack`); the response must
  name the accepted sample's `content_sha` by an 8+ hex prefix (the
  registration-kernel R6 form — a token that can only derive from the
  presented evidence brief); the named sample must EXIST in the ledger
  (recompute leg: the gate re-reads the ledger and refuses a prefix that
  matches nothing).

Precedence per key: caller `tolerance_spec` override (labeled
`caller_override`, disclosed) > well-evidenced envelope (auto path) > thin
envelope (needs_verdict path) > exact.

### D-verdict-wire — the exact vocabulary

- Per-key `verdict`: `match` | `mismatch` | `incomparable` (unchanged) plus
  `envelope_applied: {class, lo, hi, rel_spread, evidence: {n, n_full,
  n_partial, scales, clusters, same_submission_only}} | null` and
  `tier_reason: "exact" | "within_evidenced_envelope" |
  "within_thin_envelope" | "outside_thin_envelope" |
  "outside_evidenced_envelope" | "caller_override" | null`.
- Overall `stage_reached`: `auto_cleared` | `needs_verdict` | `mismatch` |
  `incomparable` (missing artifacts, unchanged). `needs_decision =
  stage_reached != "auto_cleared"`. The fold: any
  outside-evidenced-envelope or exact-class-moved key → `mismatch`; else
  any thin/novel/incomparable key → `needs_verdict`; else `auto_cleared`.
- The `auto_cleared` receipt is itself the code attestation (bound sha,
  journal-free, ledger-resident); the needs_verdict brief is code-rendered
  from the receipt (never LLM-authored numbers — the D6 archive/interface
  split).

## Task waves (file-disjoint, Opus-sized)

**Cross-slate order (`docs/design/slate-sequencing.md`, the master): this
whole plan is Phase 3, AFTER the registration kernel** — T8 builds on
registration T3's `compute_dossier_signature` refactor of
`ops/export_dossier.py`, and T7 serializes behind registration T8's fan-out
edges in `ops/attention_queue.py`. T4 must land before packs T9 touches
`ops/submit_flow.py`.

Wave A (parallel):

- **T1** `state/determinism.py` (new) — the pure kernel: sample record
  model + validation (projecting to `state/attestation.py::validate`
  records), the canonical content-sha over two metrics payloads
  (harness-contract canonical form), the STATIC classifier
  (`static_class` per flattened key — reuse
  `ops/verify_reproduction.py::_flatten_metrics`'s conventions, do not
  duplicate them: EXTRACT into `state/` and re-point
  `ops/verify_reproduction.py` at the extraction in T5's commit — `state`
  never imports `ops` (the subject-imports layering; "import" is not an
  option here)), the all-samples envelope reduction
  (order statistics + evidence labels; CURRENT-identity filter), the
  tiered classifier (pure: samples + per-key diffs → per-key tiers +
  overall), and `evidence_meets(samples, demand)`. No I/O beyond none —
  pure like the attestation kernel. Tests: envelope honesty (range-only),
  thin-vs-evidenced routing, identity-drift staleness.
- **T2** `_wire/queries/determinism.py` (new) + schema — the wire shapes
  for the envelope, evidence, demand, and receipt-v2 extensions. Regen
  tail: schema bake.
- **T3** `state/fingerprint_store.py` (new) — the ledger: path derivation,
  append routed through `attestation.bind` then
  `infra/io.py::append_jsonl_line` (**the shared flock+fsync helper already
  EXISTS** — pre-implementation verification 2026-07-07; do not extract a
  second one. T5's commit re-points
  `ops/verify_reproduction.py::_append_receipt` at it — one definition),
  tolerant read, the CURRENT-identity filter hook for T1, and the ADMISSION
  join (D-consume mechanics: ledger `verdict` field + the
  `reproduction-verdict` records of the samples' repro-run journals, joined
  on `content_sha`) so T1 stays pure.

Wave B (after A, parallel — file-disjoint):

- **T4** `ops/submit_and_verify.py` + `ops/submit_flow.py` — the double
  canary per D-double-canary's pinned seam (the verify — and the
  after-first-canary-verifies point — lives in `submit_and_verify`, NOT in
  `submit_flow`; the canary submission leg of `submit_flow` is
  parametrized so `-canary2` can fire without tripping the `-canary`
  replay branch): second execution `-canary2`, both task-0 `metrics.json`
  payloads FETCHED locally, per-metric diff via T1, n=2 prior append via
  T3 with `same_submission: true` and `verdict: "auto_cleared"`,
  `HPC_NO_DOUBLE_CANARY` opt-out, cache non-interaction. **Same commit**:
  generalize `ops/monitor/reconcile.py::_sibling_run_ids` (the ONE suffix
  definition) to the `-canary` suffix family, update every
  `(canary_id,) =` unpack call site (D-double-canary names them), + fire
  test (foreign-row contamination guard: a planted `-canary2` row must not
  enter the main reduce).
- **T5** `ops/verify_reproduction.py` — consume the fingerprint (D-consume),
  tiered verdict + receipt schema_version 2, sample append-back, partiality
  fields (`partial`, `task_indices`, uncompared accounting) on the receipt,
  caller-override labeling. **Partial pairs compare PER-TASK, which the
  artifact ladder does not serve** (it loads reduced aggregates): the
  partial path loads each side's per-task `metrics.json` for the compared
  indices — locally when present, else the
  `_per_task_metrics_reduce`-style filtered pull — a NEW load path, named
  here so it isn't improvised. The mismatch-is-a-FINDING posture and the
  no-metric-vocabulary comparator are byte-preserved.
- **T6** `ops/reproduce_run.py` + `_wire/workflows/reproduce_run.py` (the
  spec field + schema regen) + `execution/mapreduce/dispatch.py` — derived
  subsets. **Pre-implementation verification 2026-07-07 — the constraints a
  literal implementation hits, pinned:** `task_sample` does NOT exist yet
  (reproduction-receipt.md deferred it; this task ADDS it), and a subset
  MUST preserve param identity — rebuilding a smaller `trial_params` would
  move `cmd_sha` and be REFUSED by `reproduce_run`'s own param-drift guard
  (and would orphan the fingerprint ledger key). So: the reproduction keeps
  the SAME task shape / `trial_params` / `cmd_sha`, and the subset
  restricts EXECUTION — an include-list env (`HPC_TASK_INCLUDE`, the
  selected indices) threaded through the job env; the dispatcher exits 0
  immediately for a non-selected index (the `metrics.json`
  idempotency-skip's sibling seam in `execution/mapreduce/dispatch.py`), so
  scheduler slots for skipped tasks cost milliseconds. `task_sample`
  accepts a caller list OR the derived mode (canary task + deterministic
  per-axis stride via `state/axes.py`); the indices are recorded on the
  reproduction sidecar so T5 can compare per-task honestly. No
  representative heuristic — the derivation is a pure function of the axes.
  Sequences AFTER T4 (both touch the submit path). Severable: if this task
  slips, T5's partial fields still land (a caller-run subset via
  resubmitted indices remains possible) — do not half-ship the derivation.

Wave C (after B, parallel):

- **T7** `ops/attention_queue.py` — new kind `reproduction-needs-verdict`
  (class VERDICT in `KIND_CLASS`): collector routes through the ONE
  reduction (T1's classifier over T3's ledger + the run journal's latest
  `reproduction-verdict` decision — a receipt whose needs_verdict is
  already answered by a committed verdict record yields no item). Evidence
  = the calibrated brief fields verbatim. Fan-out 0 (no encoded edge yet).
  D5-table row + route-through `inspect.getsource` pin, per the module's
  standing rule.
- **T8** `ops/export_dossier.py` — the `determinism-fingerprint` source
  noun (disclosure at graduation) +
  `tests/contracts/test_dossier_boundary.py::_EXPECTED_SOURCES` updated in
  the same commit. **Seal the LEDGER FILE's RAW BYTES** (the run's
  `cmd_sha`-addressed `.jsonl`, resolved from the sidecar's `cmd_sha` the
  gather already reads) — NEVER a rendered envelope: the envelope is
  DERIVED (D-envelope) and `export_dossier` is boundary-pinned to never
  `json`-parse what it seals (its module docstring + the no-parse contract
  test), so "export the envelope verbatim" is unimplementable there. The
  human-readable envelope disclosure lives in the code-rendered briefs
  (the verify-registration brief, the repo-side renderer over the sealed
  ledger) — the D6 archive/interface split. Disclosed consequence: every
  appended sample moves the sealed bytes, so a registration's dossier leg
  reads stale after new evidence accrues — re-export + re-register is the
  remedy (registration-kernel R7's posture, deliberately).
- **T9** the registration seam — export `evidence_meets` for
  `docs/design/registration-kernel.md` (concurrent doc; build only the
  predicate, reserve the seam, instantiate nothing).
- **T10** enforcement suite `tests/contracts/test_determinism_boundary.py`
  (rows below) + TOY fixtures — toy-domain vocabulary only (widget
  metrics), never harxhar's (the domain-packs toy-fixture rule: real domain
  words in fixtures smuggle a vocabulary into the tree).
- **T11** this doc — status flip + drift log entries.
- **T12** `ops/decision/journal.py` —
  `_assert_reproduction_verdict_authorship` (the D-consume admission gate:
  block↔scope-kind convention both directions, bare-ack refusal, the
  sample `content_sha` 8+ hex prefix named in the response, the
  prefix-resolves-in-ledger recompute leg), wired beside
  `_assert_signoff_authorship`. Fire tests: agent-authored bare acceptance
  refused; acceptance naming no sample refused; a prefix matching nothing
  in the ledger refused. **HOT FILE** — serializes with registration T7
  per `docs/design/slate-sequencing.md` (registration lands first, so this
  gate lands after it).

Regen tails: `bake_operations_json.py --write` after any `@primitive`
change (T5/T6 touch primitive-decorated verbs); schema regen for T2; the
dossier `_EXPECTED_SOURCES` pin (T8) is deliberately a same-commit manual
edit — that friction is the pin working.

## Enforcement rows (accrue to `docs/internals/engineering-principles.md` maps)

| Rule | Enforced by | Fires when |
|---|---|---|
| Fingerprint samples and auto-clears route through the ONE attestation kernel — append binds via `state/attestation.py::bind`; identity-staleness via the kernel's posture; never a re-inlined recompute-or-newest-first | `tests/state/test_determinism.py` route-through (`inspect.getsource`) assertions, the accruing-member rule on the existing attestation row | a sample append or envelope filter bypasses `bind`/`reduce` |
| **No invented tolerance**: absent a measured envelope and a caller override, every comparison is EXACT; core carries no default float tolerance, per-metric or global | `tests/contracts/test_determinism_boundary.py` (behavior: two floats differing in the last ulp with an empty ledger and no spec tolerance → not `match`) + an AST pin over `state/determinism.py` (no numeric tolerance literal in the classifier) | a "reasonable default" epsilon lands anywhere in core |
| The envelope is order statistics only — observed min/max + labeled n, never a fitted distribution | same suite (n=2 samples → envelope equals the two points exactly; no `statistics`/variance import in the envelope path) | someone "improves" the envelope with mean±kσ at small n |
| A thin envelope never auto-clears and never auto-mismatches: n<3 or scale/cluster novelty → `needs_verdict`, both directions | fire tests: deviation inside an n=2 envelope → `needs_verdict`; deviation outside an n=2 envelope → `needs_verdict`, not `mismatch` | the classifier's well-evidenced bar is weakened or the thin branch collapses into auto |
| **The admission rule has ONE definition, SCOPED TO THE FINGERPRINT ENVELOPE**: a sample joins iff its comparison got a passing verdict (code's `auto_cleared` or a gated human acceptance); judge-before-append. Explicitly does NOT govern other ledgers — live-conformance's sealed baselines deliberately admit NOTHING (its recorded divergence) | fire tests: an unadmitted sample does not change the reduced envelope; a human-accepted one does; the double-canary prior is admitted by construction *(row added pre-implementation verification 2026-07-07 — D-consume promised it; the table lacked it)* | a second admission predicate appears, an unadmitted sample moves an envelope, or the rule leaks onto a ledger that opted out |
| **No-silent-caps on partiality**: every partial comparison's receipt and sample carry `partial: true`, the task indices, and the uncompared accounting | fire test: a subset receipt missing any partiality field is refused at append | a partial verdict prints like a full one |
| The double canary's rows never contaminate aggregates: the `-canary` suffix-family exclusion covers `-canary2`, defined ONCE in `ops/monitor/reconcile.py::_sibling_run_ids` | fire test in the aggregate suite (a planted `-canary2` row must not enter the main reduce) + a route-through pin that the aggregate exclusion still imports `_sibling_run_ids` | the exclusion predicate stays literal `-canary` while a second canary ships, or a second suffix definition appears |
| No verdict verb: the needs_verdict resolution is `append-decision` (block `reproduction-verdict`) or nothing; no chain/next_block/skill affordance writes it | the operations-registry contract test (the no-unlock-verb pin form) | a `resolve-reproduction` verb or auto-resolving skill appears |
| The acceptance that ADMITS a sample is gated human evidence: block `reproduction-verdict` faces `_assert_reproduction_verdict_authorship` (T12) — bare acks refused, the sample's `content_sha` prefix named, the prefix resolving in the ledger; and `evidence_meets` counts ADMITTED CURRENT-identity samples only | T12 fire tests + a T1 test that an unadmitted sample never satisfies a demand | an agent-authored ack admits a sample (laundering through accumulation), or a demand is satisfied by unadmitted evidence |
| A CODE attestation never satisfies a human tier: `auto_cleared` receipts appear in no human-authorship path; a needs_verdict item clears only via the human record | the existing `_assert_signoff_authorship` fire-test family + a no-affordance pin | a fingerprint receipt is accepted where a human verdict is demanded (e.g. by a registration tier) |
| Caller override WINS but is disclosed; the measured envelope outranks everything unowned: precedence caller (labeled) > measured > S5 pack default > exact | precedence table test + receipt-field pin (`tier_reason="caller_override"` present whenever a spec tolerance decided a key) | a caller tolerance wins UNDISCLOSED or unlabeled, or S5 silently outranks a well-evidenced measurement |

## Boundary-drift flags (the Q1 watch list)

- **Core never invents what a metric means.** The fingerprint classifies by
  STRUCTURE and measures by OBSERVATION; the moment a branch keys on a
  metric NAME ("loss can jitter, accuracy can't"), the line is crossed.
- **Core never invents a representative subset.** Derived subsets are pure
  functions of axis structure; any importance/novelty heuristic is the
  fabrication class.
- **The envelope never becomes a model.** Range + n, forever, until a
  recorded decision says otherwise; a fitted distribution at small n is a
  lie with error bars.
- **needs_verdict never rots into auto.** Pressure to auto-clear
  "obviously fine" thin-envelope deviations is rubber-stamp fatigue
  returning through the back door — widen the AUTO tier only by
  accumulating evidence (n grows), never by weakening the bar.
- **Disclosure is not judgment.** The dossier/registration print the
  envelope; they never grade it. "±30% is bad" is the reviewer's sentence,
  not core's.
- **One extra canary, never more.** If the empirical tier grows an n=5
  submit-time sampling loop, cost discipline broke; accumulation happens
  through reproductions, not submit-time repetition.

## Related docs

- `docs/design/reproduction-receipt.md` — the substrate this extends; its
  per-task-partial deferral folds in here (decision 5).
- `docs/design/notebook-audit.md` — D-attention (the tier pattern), the
  attestation kernel origin, the render-receipt template.
- `docs/design/attention-queue.md` — the D5 one-definition collector rules
  T7 must satisfy.
- `docs/design/domain-packs.md` — S5 demotion (anticipated there).
- `docs/design/registration-kernel.md` — concurrent; consumes
  `evidence_meets` and the disclosure surface.
- `docs/internals/harness-contract.md` — the normative sha canonicalization
  every `content_sha` here uses.

## Implementation drift log

- **Pre-implementation verification 2026-07-07 (adversarial plan review; no
  code had landed):**
  1. *D-double-canary / T4 — seam corrected to the real code structure.*
     The canary VERIFY (and so the after-first-verifies point) lives in
     `ops/submit_and_verify.py::submit_and_verify`, not `ops/submit_flow.py`;
     a naive second `submit_flow` call replays the completed `-canary`
     record instead of firing `-canary2`; `verify_canary` refuses an
     `expect_output` not containing the canary run_id (a `-canary` path
     fails the `-canary2` verify); and canary metrics are cluster-side only
     (`verify_canary` fetches a sha, never content) — the payloads must be
     pulled locally for the diff and the `bind` recompute. All pinned.
  2. *T4 same-commit exclusion relocated.* The `-canary` exclusion routes
     through the ONE suffix definition
     `ops/monitor/reconcile.py::_sibling_run_ids`; generalizing in
     `aggregate_flow` alone would fork the definition, and widening the
     return CRASHES the `(canary_id,) =` single-unpack call sites
     (aggregate_flow, supersession ×2, resolve_submit_inputs) — all named
     now. Reconcile settling the `-canary2` journal entry is a REQUIRED
     consequence (the run-#7 unsettled-sibling stall class).
  3. *D-store / D-consume — admission mechanics pinned.* Added the sample
     `verdict` field, the `content_sha` join to the repro run's
     `reproduction-verdict` records, the store-layer (T3) join so T1 stays
     pure, `evidence_meets` counting admitted samples only, and the
     double-canary sample's admitted-by-construction clause (without it the
     n=2 prior could never enter an envelope — internal contradiction).
  4. *T12 added — the acceptance gate.* Verified that arbitrary
     `append-decision` blocks face NO authorship gate in
     `ops/decision/journal.py`, so the human acceptance that admits a
     sample was agent-fabricable (laundering through accumulation).
     Enforcement row added.
  5. *T8 corrected against the dossier's no-parse boundary.* "Export the
     envelope verbatim" would force `export_dossier` to json-parse a store —
     pinned to sealing the ledger's raw bytes; rendered disclosure moves to
     the code-rendered briefs. Staleness-on-append consequence disclosed.
  6. *T1 "import or extract" → EXTRACT* (`state` never imports `ops`);
     *T3's shared append helper already exists* (`infra/io.py::
     append_jsonl_line`) — route through it instead of extracting a second.
  7. *T6 rewritten with the identity constraint.* `task_sample` does not
     exist yet; a rebuilt-subset reproduction would move `cmd_sha` and be
     refused by `reproduce_run`'s own param-drift guard — pinned to
     same-`trial_params` + execution restriction via a dispatcher
     include-list; file list widened (wire spec + schema regen +
     `dispatch.py`); T5's partial path needs a per-task load the artifact
     ladder does not provide (named).
  8. *Scale labels pinned* (canary vs main vs partial-as-main) — previously
     only implied; *cross-slate sequencing note added* (Phase 3 after the
     registration kernel, per `docs/design/slate-sequencing.md`).

(Populate further per deviation, each with its recorded reason, when
implementation lands. The `docs/design/notebook-audit.md` drift log is the
form to follow.)

## Amendment (2026-07-07, user-ruled 0b): the data-identity dimension

The staleness/admission model above is code-identity only (`tasks_py_sha`
family) — a rebuilt input file would read as nondeterminism, poisoning the
envelope with a false mismatch. Ruled amendment (full design:
`docs/design/data-manifest.md`; lands INSIDE Phase 3 per hot-file
serialization): (1) submit echoes the data-manifest shas of files under
the declared input roots into the sidecar; (2) samples are comparable
ONLY within the same data identity — a different-data sample is disclosed
as data drift, never admitted as nondeterminism evidence; (3)
`reproduce-run`'s drift guard grows to three dimensions (code, env,
data), and verify verdicts name the moved dimension, or "data identity
unknown (no manifest at record time)" — disclosed, never fabricated.

**Amendment 2 (2026-07-07, user-ruled): `needs_verdict` routes on demand,
not on creation.** A thin-envelope/near-boundary sample parks as a
leverage-zero standing item; it surfaces as a verdict brief only when a
consumer blocks on it (registration demand, graduation, an explicit
verify) — and then decision-ready per attention-queue D8 (named blocker,
one code-rendered evidence block, pre-drafted y/nudge resolution, at an
existing decision moment). Restriction rationale: unrestricted routing of
low-tier verdicts teaches the human to ignore the channel wholesale.
