---
status: thesis
audience: public — seeds the README hero, the talk abstract, the paper's contribution statement
---
# The reproducibility thesis — a clean reproduction, extracted mechanically from a messy process

**Status: THESIS (2026-07-17).** The public-facing intellectual core: what this
repo's distinctive contribution to the reproducibility crisis *is*, stated to the
standard of a contribution statement. Every capability claim is grounded in a cited
`path::symbol` and the commit that landed it; anything not yet built is flagged
**FRONTIER** and named honestly. Where this document and the code disagree, the code
and its enforcement-mapped tests win — the map (`docs/plans/`, the capability audit)
is downstream of the tree.

> **A clean reproduction is a mechanical *extraction* from the messy process of
> experimentation — not a diligence artifact reconstructed from memory.**

---

## 1. The crisis, precisely

"Science isn't reproducible" is too coarse to build against. The specific mechanism
this repo attacks is narrower and sharper:

> **Between the messy exploration and the clean citable number sits a gap that today
> is bridged only by human diligence and memory — and diligence fails at scale.**

A real experiment is not a recipe; it is a *mess*. Dead ends. Retargets to a
different cluster mid-run. Parameter drift across a dozen submissions. A canary that
blocked, a run that was superseded, an operator who reduced the table by hand at 2am
because the pipeline was wedged. Hand-typed numbers. When the paper is written, one
table survives — and the chain from *that table* back to *which code, which data,
which environment, which runs, reduced by which command* produced *that exact digit*
exists nowhere durable. It lives in the scientist's head and a scroll of shell
history.

So reproduction becomes **archaeology**: reconstruct, from memory and vibes, what
probably happened. Archaeology is a *narrative* activity, and narrative
reconstruction is precisely where both humans and LLMs confabulate — the honest "I
think we used the v3 data" that turns out wrong, the caption number that was copied
from the wrong run. The field's response has been to ask for *more diligence*:
pre-registration, lab notebooks, "just be more careful." But diligence is a per-human,
per-run tax that scales linearly with the number of runs and inversely with how tired
the scientist is at midnight. **A discipline that depends on everyone being careful
every time, at scale, does not have a reproducibility problem it can solve by trying
harder.** The gap is structural: the clean result and the messy process are two
different objects, and nothing mechanically links them.

Git closed exactly this gap on exactly one axis. "What changed in the code since
last-known-good?" stopped being archaeology — reconstruct-from-memory — and became a
`diff`: mechanical, total, trustworthy. Science got that for free on *code* and has
been trying to hand-roll the equivalent for data, environment, and results ever
since. It hasn't, so those axes are still archaeology.

## 2. The thesis

The repo's distinctive move is to make the clean reproduction a **mechanical
extraction from the messy process**, not a diligence artifact laid down alongside it.
Concretely, it is the product one-liner
(`docs/design/onboarding-map.md`) applied at *publication time*:

> **"What changed since last-known-good" — answered mechanically instead of by
> archaeology.**

It records a last-known-good on all five axes git only ever gave science on one, and
makes the diff — and the extraction — mechanical:

| Axis | Last-known-good record | The mechanism |
|---|---|---|
| code | git (the axis science got for free) | `cmd_sha` + `tasks_py_sha` on every run |
| data | the content-sha manifest | `state/data_manifest.py::data_identity` |
| behavior | the determinism fingerprint's envelope | the double canary + order statistics |
| beliefs | registrations with review horizons | evidence-memory conclusions |
| decisions | the journal itself | the greenlight / settle ledger |

Every link in the chain from *input → code → environment → execution → reduction →
selection → analysis → the paper's number* is one of three things, and the design's
whole discipline is that it is **never a fourth thing (a story)**:

- **MECHANICAL** — code computes and enforces it; a stranger gets it for free.
- **DISCLOSED** — code captures it and surfaces drift as a named, counted fact, but
  never blocks (the amplification posture; §4). A sub-case, **DILIGENCE**, is a
  MECHANICAL capture whose *precondition* is opt-in — it works once the human
  declared inputs, and is honestly silent-absent by default.
- **ABSENT** — no capture exists yet, and the document says so out loud.

The point of the trichotomy is that a stranger — given the experiment repo, the
journal, and the cluster artifacts — can **re-derive the citable table and every
number in it with no archaeology and without asking the original human.** Where a link
is not yet mechanical, the tool does not paper over it; it *discloses the gap as a
first-class fact*.

**How this differs from what the field already has.** Every existing tool captures a
*layer* and leaves the *extraction* to diligence:

| Tool class | What it captures | Where it leaves the diligence gap open |
|---|---|---|
| **Notebooks** (Jupyter) | code + output, co-located | Output can drift from the code that "produced" it (a stale cell); no link from *this figure* to *which run*, *which data sha*. Re-running is manual. |
| **Workflow managers** (Snakemake, Nextflow) | the DAG of steps, re-executable | Captures the *plan*, not the *history* — the dead ends, retargets, and the human's selection of which run is "the result" live outside the DAG. The messy exploration is not the workflow. |
| **Containers** (Docker/Singularity) | the environment, frozen | Freezes *an* environment; does not tell you *which* frozen environment produced *which cited number*, nor detect when the two diverged. Provenance of the digit is still manual. |
| **Provenance/pre-registration** (RO-Crate, OSF) | metadata + intent, human-authored | Faithfully records *what the human wrote down* — which is exactly the diligence artifact whose failure mode is the crisis. |

Each of these is a real, useful last-known-good record on *one* layer. None of them
answers the extraction question — *from this messy campaign of runs, which minimal
set of them, at which shas, reduced by which command, produced this citable table, and
prove it* — mechanically. That question is the thesis, and answering it as
IDENTITY + ORDERING + COUNTING over opaque records (never as a story) is the
contribution.

## 3. The mechanism, link by link

The chain walked concretely, each link classed and cited. Grounded against the
canonical `src/hpc_agent/` tree at `main @ 62cb0a5a`.

### The summary

| # | Link | Class | Load-bearing evidence |
|---|---|---|---|
| 1 | Input data | **DILIGENCE** | `state/data_manifest.py::data_identity`; `data_sha` — captured by content, drift disclosed, opt-in by default |
| 2 | Code | **MECHANICAL** | `cmd_sha`/`tasks_py_sha`; `reproduce_run.py::_assert_no_drift` REFUSES on code/param drift |
| 3 | Environment | **ABSENT / weak** | `env_hash` captured (`state/run_sha.py::compute_env_hash`) but never compared; full package env uncaptured |
| 4 | Execution | **MECHANICAL** | the double canary → `state/determinism.py`; harvest receipts; the un-fakeable bind-lock |
| 5 | Reduction | **MECHANICAL** | reduce-time `contributing_run_ids` (`ops/aggregate_flow.py`); the reducer, never the LLM, computes numbers |
| 6 | Selection / curation | **MECHANICAL** | `ops/extract_recipe.py::extract_recipe` — the messy→clean walk |
| 7 | Analysis / the number → the paper | **MECHANICAL to the dossier; ABSENT into the manuscript** | `verify-relay`; the sealed dossier; then a human types the digit |

The strongest claim the tree supports: **the two links that used to be the weakest —
reduction and selection — are now the strongest, and the two that remain the classic
reproducibility-crisis failures — data and environment — are exactly where the chain
is still DILIGENCE-or-ABSENT.** That is the honest shape of the frontier (§4).

### The walk

**1. Input data — DILIGENCE.** Two content fingerprints ride every run sidecar:
`data_sha` (hashes the declared `input_datasets` — DVC `outs[0].md5`, else raw
bytes) and `data_manifest_sha` (`state/data_manifest.py::data_identity` →
`manifest_doc_sha`, a canonical sha over `{relpath: {sha256, size}}` of every file
under the declared `input_roots`). When inputs are declared, drift is *caught and
disclosed* — `reproduce_run.py::_data_drift_disclosure` yields `match`/`drifted`/
`unknown` on the greenlight brief, and `verify_reproduction` folds it into the
fingerprint envelope, **never blocking.** The gap: capture is opt-in. A run that
declares neither writes a byte-identical sidecar with both data fields `null` and is
silently invisible to all data-drift attribution. This is the quiet-corruption class
the manifest was built for (`docs/design/data-manifest.md`) — mechanical once
declared, silent by default.

**2. Code — MECHANICAL.** `cmd_sha` (parameter identity) and `tasks_py_sha` (code
bytes) are stamped on every run; the per-task `.hpc_cmd_sha` markers carry staleness.
`reproduce-run` *refuses*: `ops/reproduce_run.py::_assert_no_drift` recomputes
`cmd_sha` and routes `state/code_drift.py::detect_code_drift` over the executor and
`tasks_py_sha` — either mismatch raises `SpecInvalid` naming the first differing task.
This is the git axis, extended to parameters and the executor. (One disclosed
DILIGENCE hole: a legacy sidecar predating the executor stamping passes the code-drift
leg vacuously — a known, cheap gap.)

**3. Environment — ABSENT / weak, and never compared.** The wheel version
(`hpc_agent_version`) is stamped on every sidecar (`ops/write_run_sidecar.py` L193)
and `env_hash` (`state/run_sha.py::compute_env_hash`) is computed — but `env_hash`
hashes only the *activation directive* (module-load names + the conda-env *name* +
the source-script path), so two materially different environments sharing one
env-name collide. No `pip freeze` / conda-export / lockfile is captured; the
interpreter path and `sys.version` have zero hits in `src/`. **Decisively: the
`env_hash` we do capture is never compared in any gate** — `reproduce_run` checks
param + code only, and the fingerprint identity filter
(`state/determinism.py::IDENTITY_FIELDS`) excludes it. Environment drift between an
original and its reproduction is the *opposite* posture to data (disclosed) and code
(refused): it is **invisible.** This is the single largest silent hole in the chain.

**4. Execution — MECHANICAL, with the un-fakeable fingerprint.** Behavior is
captured, not asserted. `submit-s2` fires a **double canary** — `<run>-canary` and
`<run>-canary2` concurrently (`ops/submit_and_verify.py::_mint_double_canary_sample`
L415) — verifies both, diffs their task-0 metrics, and mints an n=2 determinism-
fingerprint sample; a failed second canary blocks the main array exactly like a
failed first. The sample has **one definition**
(`state/determinism.py::build_sample_record`, `IDENTITY_FIELDS = (cmd_sha,
tasks_py_sha, executor)`), accretes append-only to
`_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl` keyed on cmd_sha, and the envelope
reduces admitted samples to **order statistics only** — `lo = min`, `hi = max`,
`rel_spread` (`state/determinism.py` L704–718): no mean, no stddev, no invented
epsilon. The un-fakeable leg: `append_sample` bind-locks each sample's `content_sha`
against the two on-disk canary payloads via `state/attestation.py::bind` — **a spread
cannot be asserted over payloads that were never on disk.** (DILIGENCE residue: the
double canary is best-effort and disable-able via `HPC_NO_DOUBLE_CANARY`, so the
fingerprint can silently not grow.)

**5. Reduction — MECHANICAL. The reducer, never the LLM, computes every citable
number.** The deterministic combine+reduce (`ops/aggregate_flow.py`) is the *only*
thing that produces an aggregate number; the LLM never computes one. And the table is
now *self-describing*: `_reduce_input_provenance` (L1229, commit `f6d9959e`) stamps
`contributing_run_ids` + `piece_cmd_shas` + `hpc_agent_version` into the reduced
table's provenance, sourced from the reduce's own `_combiner/wave_*.json` partials and
the `.hpc_cmd_sha` fingerprints — **the table→run-set link is first-class.** The
run-13 stale-cache class is closed: the per-task mirror cache is now fingerprint-
checked and evicts a task dir whose sha moved (the `.hpc_cmd_sha` that "existed on disk
but was never compared" is now compared). And the reducer is checked *before* the
array launches: `ops/submit_and_verify.py::_check_reducer_on_canary` (L113, commit
`2f6fd10d`) executes the declared `aggregate_cmd` against the verified canary's real
row, asserting contract shape, disclosing any error verbatim, never refusing.
(Residual ABSENT: the cluster combiner footer does not yet mirror the reduce-time
provenance, so the `HPC_CLUSTER_FINAL_REDUCE=1` path degrades link 6 to the lineage
fallback — a disclosed gap, not a silent one.)

**6. Selection / curation — MECHANICAL. This is the messy→clean step, and it is now a
walk, not a memory act.** `ops/extract_recipe.py::extract_recipe` (commit `8fff2a2c`)
is a read-only query that takes a citable artifact (`run_id` / `campaign_id` /
`aggregate_path`) and walks *back* to the **minimal contributing run-set**, applying
three exclusions **in order, each disclosed and counted** (L264–279):

1. **canary** — `-canary` / `-canary2` family siblings (`canary_parent_of`);
2. **superseded** — non-head members of a `lineage_chain` (keep the newest);
3. **dead-end** — runs with no harvest receipt (`harvest_receipt_exists`) — the runs
   that were explored and abandoned, never reduced into the table.

It then emits each kept run's full provenance fingerprint — `cmd_sha`, `tasks_py_sha`,
`data_sha`, `data_manifest_sha`, `env_hash`, **`hpc_agent_version` (the wheel)**,
`cluster`, `profile` (`_FINGERPRINT_FIELDS` L69) — a `recipe_signature` over **only
the minimal set** (`manifest_signature(recipe_body)`, L412), the runnable
re-derivation steps, and the receipts chain with every gap disclosed. **It never names
a metric and never picks a "best" run.** The extraction the user asked for — *these
runs, at these shas, reduced by this command, produced this table; here is what is
missing* — is IDENTITY + ORDERING + COUNTING over opaque records, and nothing else.
And for the ugliest case — an operator who reduced a table *outside* the flow —
`ops/settle_aggregate.py::settle_aggregate` (commit `8fff2a2c`) gives the bypass table
a provenance home: it sha256's the artifact at record time, validates the named runs
exist, refuses a synthesized utterance, and journals `source: "operator-settled,
provenance human-asserted"` — **it records, it never blesses the numbers.**

> **The wheel-sha is signed end-to-end (landed 2026-07-17, R3 ruled do-now).** The
> wheel identity is captured on every sidecar, carried in the dossier's identity
> projection (`ops/export_dossier.py`), signed inside `extract-recipe`'s
> `recipe_signature` (it is in `_FINGERPRINT_FIELDS`), **and** — as of commit
> `008198ee` — a signed field of the standalone `provenance-manifest` verb
> (`_RUN_PROVENANCE_FIELDS` includes `hpc_agent_version`;
> `PROVENANCE_MANIFEST_SCHEMA_VERSION = 2`, tamper-pinned, absent-version signs an
> explicit null, v1 manifests still verify). extract-recipe prefers the signed
> value and discloses its source (`hpc_agent_version_source`).

**7. Analysis → the paper — MECHANICAL to the sealed dossier, then ABSENT.** The
pointing doctrine holds the whole way: the reducer computes, the LLM only *points* the
human at a code-written render, the human *concludes*, and any number the LLM relays
is audited — `ops/decision/journal/verify_relay.py::verify_relay` checks every relayed
figure against the run's own corpus (journal + sidecar + record + briefs + the
aggregate/`wave_*`/`*.csv` bytes) and flags the nearest source value on a mismatch.
The recompute-lock guards everything trusted:
`state/attestation.py::bind` recomputes-and-refuses ("a hash cannot be asserted into
existence") and `reduce` is drift-revocation (`CURRENT`/`STALE`/`ABSENT`, newest-wins —
an edit revokes stale trust). The terminal bound artifact is the sealed dossier —
sidecar + journal + briefs + `_aggregated` bytes + the fingerprint ledger, copied
verbatim, **never parsed**, integrity-manifested. **And there the mechanical chain
stops:** a repo-wide search for LaTeX / `\cite` / manuscript emission returns no code.
The last step — a human reads the reducer's number off the sealed dossier and types it
into the figure caption — is purely human and entirely unbound. This is the last
unprotected link, named as ABSENT.

**A note on the reproduction act itself.** A stranger *can* check a claim:
`verify-reproduction` in external-baseline / claim-check mode
(`ops/verify_reproduction.py::_run_claim_check`) needs only a fresh observed run and a
human-authored claim — no recorded original. But it refuses, *by construction*, to
launder that into a "reproduction":
`_assert_receipt_kind_matches_baseline` (L778) raises because "an external claim was
never observed," and the n=2 fingerprint samples come only from the fresh double
canary, never from the claim. A stranger can check a claim against their own fresh
run; they cannot fabricate a reproduction of a run they never observed.

## 4. What is honestly not solved yet — and why disclosure, not gate, is right

Two links remain the classic reproducibility-crisis failures, and they are the
frontier — named, not hidden:

- **Input-data capture is opt-in (§3.1).** The default is silent-null; an undeclared
  run is invisible to data-drift attribution with no warning. *The build direction:*
  capture-by-default plus a never-blocking S1 disclosure ("data identity uncaptured —
  this run is invisible to data-drift attribution").
- **Environment drift is invisible (§3.3).** Full package env, interpreter, and
  hardware are ABSENT; `env_hash` is captured but never compared. *The build
  direction:* have the canary — which already runs in the run's environment — emit a
  resolved-environment lockfile snapshot, fold its sha into an additive `env_lock_sha`,
  and add an env-drift *disclosure* leg mirroring the data leg. This rides the existing
  canary at ~0 wall-clock.
- **The number → paper transcription is unbound (§3.7).** *The build direction
  (being built):* a **`cite-check`** surface — the human pastes their table row, code
  confirms it matches the sealed number or names the nearest source value, using the
  `verify-relay` corpus machinery. It cannot follow a number into LaTeX, but it can
  bring the audit to the manuscript's door. **Aspirational** until it lands.

**Why the posture is disclose-not-gate — and why that is a design commitment, not a
limitation.** The tool amplifies a human's rigor when they have the energy; it does
not coerce it. Nothing refuses a bare `y`. Environment drift, data drift, a dirty
worktree — each becomes a *named, counted, disclosed fact* on a brief, never a block.
This is deliberate:

- **Refusing on env or data would break the legitimate case.** Reproducing a result
  under an upgraded environment is a *valid* reproduction whose moved dimension must be
  *named*, not forbidden. A gate that refuses it forces the scientist to lie to the
  tool (disable the check) to do real work — which destroys the record the gate
  existed to protect.
- **A gate a tired human routes around at midnight protects nothing.** Disclosure that
  survives into the durable record — the sidecar, the brief, the recipe's disclosed-gap
  list — is worth more than a block that gets disabled. The amplification doctrine is
  that the tool makes the *next rung of evidence cheap to accrue*, not that it holds
  entry hostage.

And crucially, **more consent ceremony would change reproducibility not at all.**
Provenance lives entirely in the code-minted, bind-locked shas — `cmd_sha`,
`tasks_py_sha`, `data_sha`, the fingerprint `content_sha` — all minted with
`attestor: "code"` and no human in the loop; `_is_admitted` admits every double-canary
sample with zero human involvement. A human `y` is **authorization lineage** (whose
decision, at which boundary, why the run took its shape — a genuine and useful axis,
the *decisions* axis of the five), categorically **not provenance**. So the answer to
"are the current `y`'s enough?" is: the `y`'s are exactly enough for what they do; the
reproducibility gaps are in machine data/env capture, not in consent. Effort belongs
in the frontier, not in more ceremony.

## 5. The claim to make publicly

**What this repo can claim today, honestly:**

> Every citable number is reducer-computed and byte-sealed in an integrity-manifested
> dossier — never computed, and never silently altered, by a language model. The
> minimal run-set that produced a table is a first-class, signature-verified,
> gap-disclosing recipe (`extract-recipe`): dead ends, canary siblings, superseded
> lineage, and even operator-bypass tables are mechanically excluded-and-disclosed,
> never named or judged. Determinism is fingerprinted under an un-fakeable
> recompute-lock — a hash cannot be asserted into existence. And the reproduction act
> refuses, by construction, to launder an unobserved claim into the trust chain. The
> chain is complete for **code**, and for **data and environment when the scientist
> opted in.** The one unbound step is a human typing the sealed number into the paper.

**The honest caveats, stated in the same breath:** input-data capture is opt-in
(silent by default); full-environment identity is not yet captured and `env_hash` is
not yet compared; the number→paper transcription is unaudited (`cite-check` is being
built); the standalone `provenance-manifest` signature does not yet include the wheel
sha (the recipe signature does). These are the frontier, and the document ranks them
by damage × frequency rather than hiding them.

**What it should claim once the top gaps (data + environment capture) close:**

> **A stranger, given the experiment repo + the journal + the cluster artifacts,
> mechanically re-derives the citable table and every number in it — with code, data,
> AND environment identity fingerprinted and drift-attributed by default — no
> archaeology, and no asking the original human.**

That is the sentence that turns "the strongest reproduction tooling for the one axis
science already had (code)" into **"the first tool that makes data and environment
reproducible by default, mechanically, at publication time"** — which is the crisis's
actual center of gravity. The distinctive contribution, in one line for the README
hero and the talk abstract:

> **A clean reproduction, extracted mechanically from the mess — because the record is
> code-minted and the extraction is a walk, not a memory.**

---

## Drift log

- **2026-07-17 — created (thesis).** Cites the user directive (2026-07-17, verbatim
  intent): "how the repo contributes to solving the reproducibility crisis in modern
  science … how do we capture a clean reproduction from the messy process of
  experimentation … are most current `y`'s enough?" Expands section (d) of the
  capability map (`docs/plans/reproducibility-program-2026-07-17.md`, commit
  `7e01eb94` on a divergent branch) into a full public-facing document, grounded
  against the canonical `src/hpc_agent/` tree at `main @ 62cb0a5a`. Inputs: the product
  one-liner + five axes (`docs/design/onboarding-map.md`), the extract-recipe framing
  (`docs/plans/clean-reproduction-extraction-2026-07-17.md`), and a direct read of the
  shipped machinery — `extract-recipe` + `settle-aggregate` (commit `8fff2a2c`),
  reduce-time `contributing_run_ids` provenance (`f6d9959e`), the canary reducer-check
  (`2f6fd10d`), the double-canary fingerprint (`state/determinism.py`,
  `ops/submit_and_verify.py`), the attestation recompute-lock (`state/attestation.py`),
  `verify-reproduction`'s anti-laundering split, and `verify-relay`.
- **Draft-time correction, since resolved at integration.** The draft (written on a
  worktree predating `008198ee`) flagged the capability map's "signed wheel-sha
  (manifest schema v2)" as not-landed on its tree. R3 landed at `008198ee`
  (`PROVENANCE_MANIFEST_SCHEMA_VERSION = 2`, `hpc_agent_version` signed, v1
  read-compat); §3.6 was updated by the integrator to the landed truth. The
  method stands: where doc and code disagree, the code wins.
- **Classification vocabulary.** Uses MECHANICAL / DISCLOSED / ABSENT as the task
  framed it, with DILIGENCE named as the honest sub-case of a MECHANICAL capture whose
  precondition is opt-in (the capability map's third label) — so the trichotomy the
  directive asked for and the audit's four-way class reconcile without either being
  contradicted.
