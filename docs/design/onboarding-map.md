# The onboarding map — where the copilot meets scientists, and with what trust

**Status: DOCTRINE + MAP (2026-07-07).** Written at Fable quality before the
model deadline; the judgment here is the deliverable — every build item
either rides an existing phase of `slate-sequencing.md` or is explicitly
parked as NEEDS RULING. Nothing in this doc adds core scope by itself.

## The organizing principle

The copilot takes responsibility for the **transitions into the trust
chain** — attaching evidence to research artifacts at every maturity rung —
never for the work at the rung itself. Every on-ramp obeys one posture,
already ruled seam-by-seam across the codebase and named here as a single
rule:

> **Accept with disclosure, refuse nothing, offer the upgrade path.**

An artifact enters with exactly the trust it has earned (unaudited,
untagged, fingerprint n=0 — stated plainly), and the machinery's job is to
make the next rung's evidence cheap to accrue, not to gate entry. The
existing instances of this rule: the T9 `audited_source` opt-in (absent =
byte-identical behavior), untagged-scope disclosure at greenlight
("invisible to memory", never refused), the fingerprint's confidence-labeled
envelope, evidence-memory's enforcement-pinned never-blocking surfacing.

**The rot test that guards the on-ramps** (found 2026-07-07, the
build-template notebook lane): a surface that *steers new work* toward a
retired shape is rot and gets fixed; a surface that *accepts existing
artifacts* in an old shape is an on-ramp and gets kept. Steering surfaces
must always point at the audit-able/current format; accepting surfaces are
the product's welcome mat. (`export_notebook` — the ipynb→`.py` lifter —
is the type specimen of a *converter*: an acceptor that moves the artifact
toward the doctrine, more valuable after the doctrine shift, not less.)

## The ladder and the trust model are the same object

Each rung up attaches more evidence: audited source → tagged scopes →
fingerprint samples → sealed dossier → registered claim → live conformance.
Entering mid-ladder means entering with less attached evidence, disclosed.
"More automation" and "more trust" are not two axes; the ladder is one.

## The map

| # | Stage transition | On-ramp | Status | Trust posture on entry |
|---|---|---|---|---|
| 0 | Raw data → trusted inputs | content-sha data manifest + provenance record | **GAP — rides Phase 4** (bind-as-data makes it explicit) | provenance absent = disclosed, never blocking |
| 1 | Idea → audited script | the notebook-audit prelude (drafting + tiered sign-off) | BUILT (v1–v1.6) | highest — born inside the audit loop |
| 2 | Scribbles / messy `.ipynb` → draft | acceptors: `experiment_kit/notebook.py::export_notebook`, jupytext→percent, the interview's ipynb candidates | BUILT | executable; unaudited, disclosed |
| 3 | `.py` script / repo → scaled experiment | interview, `hpc-wrap-entry-point`, `decorate-entry-point`, `discover` | BUILT (the original product) | onboarded; untagged runs disclosed at greenlight |
| 4 | Scale → verdict | campaign/aggregate blocks, briefs, look ledger | BUILT | — |
| 5 | Existing *results* → evidence | import prior findings as dated conclusion attestations (the retro-indexing mechanism) | rides Phase 6 (evidence memory) | prior evidence, dated, cited by file sha; never receipts for runs the copilot didn't observe |
| 6 | External claim → reproducible claim | **onboard-by-reproduction** — first act on arrival is reproducing the claimed result under observation; the run mints the identity AND the first fingerprint sample | **NEEDS RULING** (below) | claim + n=1 fingerprint, honestly labeled |
| 7 | Claim → registered/defended | registration kernel, dossier export, DSSE | PLANNED (Phases 2, 5) | the top rung |
| 8 | Solo → team | multi-human (per-actor logs, authorship) | PLANNED (Phase 9) | — |

Between rungs 1–4 coverage is complete: the acceptors fill the gaps and the
2026-07-07 lane fix makes the greenfield path audit-native (`build-template
--shape notebook` emits percent-format `.py`).

## The parked ruling: onboard-by-reproduction (rung 6)

The most common real arrival mid-career is *script + claimed result*. The
strongest first interaction the copilot can offer is: "let's see if your
result reproduces under observation" — it converts the skeptic's "I don't
need workflow tooling" into "your result now has a reproduction receipt",
and the claim's evidence history begins at the front door.

Why it is NOT buildable-as-is: `reproduce-run`/`verify-reproduction` require
a copilot-recorded identity; here the first run IS the identity-minting act
(reproduction as *entry*, not follow-up — an inversion of the recorded
posture). That makes it new scope outside the ten-doc jurisdiction map, so
it awaits an explicit human ruling. Recommendation on file: YES, but
sequence it AFTER Phase 3 (the determinism fingerprint) exists to receive
the samples — it is a new front door to rooms already planned, and building
the door before the rooms inverts the dependency.

## What the copilot explicitly does NOT onboard (the refusal list)

Named so the boundary stays real: research-program formation (what to work
on), literature review, data acquisition itself (we manifest what arrives;
we never fetch), publication writing (we export the dossier; the paper is
theirs), and stage-level judgment anywhere (the human signs; we route).
Each is either *meaning* (caller-side forever, per the scope doctrine) or
*actuation* (observe/judge/route, never actuate).

## The user-facing artifact this doc becomes

"Arrive with X, start here" — a one-page entry guide derivable today only
by knowing all 142 verbs. Home: the conformance-kit era (when outside users
are real), augmented per discipline by packs (the pack is the
discipline-specific on-ramp kit: core owns the ladder, packs own the
hand-holds). Until then, THIS doc is the internal source of truth for
on-ramp decisions.

## Drift log

- 2026-07-07: written (Fable, pre-deadline). Inputs: the four-layer
  hierarchy ruling (core / quant pack / idea→trade / target program /
  attempt), the steering-vs-accepting rot classification from the
  build-template investigation, the tier-0 `endbartime` merge failure in
  harxhar-clean as the live evidence for rung 0, and the run-#10
  automatability reframing.
