# ============================================================================
# QUANT PACK — the DOMAIN layer's S4 audit_template SKELETON.
#
# LAYER (four-layer hierarchy, user-ruled): this is the reusable quant DOMAIN
# layer — "only the specificity necessary to generalize a quant workflow." It is
# METHODOLOGY SCAFFOLDING, not research content: it must be reusable by ANY quant
# lab and contains ZERO references to any one lab's symbols, docs, target, or
# results. The concrete PROGRAM template lives one layer down, in a consuming
# lab's program pack (that seat, not this one, is what audits reference). See
# README.md for the layer table and portability test.
#
# WHAT EACH SECTION CARRIES: the CONTRACT as comment prose, never a code body.
# The five sections are the kernel of one quant research iteration —
#   data-selection -> target-construction -> feature-construction -> baseline -> metrics
# — and the pinned prose states the invariant each must satisfy. A lab drafting
# from this skeleton REPLACES the prose with its own concrete cells; the drift
# tiers then route each drafted section to the appropriate sign-off.
#
# PROVENANCE (does NOT violate the no-inventing rule): the section contracts
# below are lifted verbatim-in-spirit from the header and pinned/variable-cell
# comments of a SIGNED, human-approved audit-template precedent in a consuming
# lab — the working precedent. Only the DISCIPLINE is lifted;
# every lab-specific symbol, path, transform, and unit is stripped out, leaving
# the reusable contract. This file is authored, not derived from research.
#
# Core hashes this file and tracks section drift; it never runs it.
#
# GROWTH (see README.md): the 5-slug inventory below is a kernel. When a
# consuming lab's program template graduates to a fuller section inventory, this
# domain skeleton grows to the same superset (and check/check_quant.py's
# _EXPECTED_SECTIONS grows to match); the two move together and the pack rebuilds.
# ============================================================================
"""Quant DOMAIN audit skeleton — five section contracts, no research content.

THE INTERFACE CONTRACT (the one seam that lets a metrics cell stay generic):
by the end of `baseline`, a lab's filled draft must have defined three aligned
1-D arrays on the target's RAW (untransformed, as-forecast) scale —

    pred_raw           the candidate model's forecasts
    true_raw           the realized target values
    baseline_pred_raw  the deployed-baseline forecasts, same rows

NAMING JUDGMENT (recorded): the signed precedent called these "raw-variance"
arrays because its target is realized variance. That word is target-specific and
does NOT belong in the domain layer. The VARIABLE NAMES (`pred_raw` / `true_raw`
/ `baseline_pred_raw`) are already domain-neutral — "raw" means the target's own
untransformed scale, which exists for any quant target (returns, volatility,
variance, price, spread). So the names are kept verbatim and only the PROSE is
generalized from "raw-variance" to "raw (untransformed target) scale." A lab
whose native scale needs a different word may rename in its own program template
(a program pack whose target's raw scale is variance does not); the contract is the
three-aligned-arrays shape, not the noun.

THE DRIFT-TIER SPLIT this skeleton is built for: a drafted section auto-clears
only when it is byte-identical to the template cell, lint-clean, and assert-free;
otherwise it diffs and routes to human sign-off. The pinned prose here is NOT
runnable code, so EVERY section a lab fills necessarily diffs and routes to a
human — the domain layer deliberately grants no auto-clear. Auto-clear is a
program-layer affordance (a concrete pinned cell in the program pack), never a domain
one: methodology cannot pre-approve a lab's actual analysis.

VERDICT BOUNDARY: sections show evidence; no section states a conclusion.
"""

# %%
# Path bootstrap (PREAMBLE — outside every audit section by design; kept as a
# generic, lab-agnostic repo-root discovery so `src.*`-style imports resolve when
# a lab fills the sections). Walks up from the file (or cwd when __file__ is
# absent, e.g. an interactive cell) to the first directory containing src/.
import sys
from pathlib import Path

import os

_START = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
_ROOT = _START
while not (_ROOT / "src").is_dir() and _ROOT != _ROOT.parent:
    _ROOT = _ROOT.parent
if (_ROOT / "src").is_dir():  # only normalize when a repo root was found
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    os.chdir(_ROOT)

# %%
# hpc-audit-section: data-selection
# [CONTRACT — replace with your lab's cell.] Load the raw inputs through the
# lab's PINNED loader (a named reader callable, declared in the program pack's
# reader_calls vocabulary), from a path literal UNDER the audit's input_roots.
# Never re-implement loading inline. Evidence: show the loaded shape (rows/cols);
# state no conclusion.

# %%
# hpc-audit-section: target-construction
# [CONTRACT — replace with your lab's cell.] Construct the prediction target by
# CALLING the production target transform — the exact invariant transform used in
# deployment, never re-derived here. If the transform also yields a reconstruction
# baseline (e.g. a scale factor to return to raw units), keep it for the
# raw-scale reconstruction the interface contract needs. Evidence: show the
# non-null count of the constructed target.

# %%
# hpc-audit-section: feature-construction
# [CONTRACT — replace with your lab's cell.] Build the candidate's features from
# ONE existing, already-defined family, transformed the SAME production way, and
# aligned to the target with the correct forecast-horizon shift (features at t,
# target at t+h — show the shift; a look-ahead leak is the failure this cell
# exists to expose). The candidate forecast comes from the lab's PINNED
# walk-forward engine, never a hand-rolled loop.
# END STATE: contributes to pred_raw / true_raw per the interface contract.

# %%
# hpc-audit-section: baseline
# [CONTRACT — replace with your lab's cell.] Reproduce the deployed baseline LIVE
# in this run: run the deployed baseline's frozen config through the SAME
# walk-forward engine and CITE that config's commit sha in the cell. A baseline
# number is NEVER quoted from results/ — it is recomputed here or it does not
# count. This is the known-answer check that anchors the whole audit.
# END STATE: pred_raw, true_raw, baseline_pred_raw all defined — three aligned
# 1-D raw-scale arrays (the interface contract) — and the baseline's headline
# metric printed for the known-answer comparison.

# %%
# hpc-audit-section: metrics
# [CONTRACT — replace with your lab's cell.] Compute the claimed units with the
# lab's metrics MODULE, never inline arithmetic — the same functions production
# scores with, so the audit and the deployment measure identically.
#
# DEATH BY CONSTRUCTION: the terminal line below references the three interface
# arrays the VARIABLE sections above must define. An unfilled or half-filled
# draft NameErrors HERE — deliberately — so it can never emit a vacuous report.
# Keep a terminal reference to all three when you replace this cell with your
# metrics call.
_ = (pred_raw, true_raw, baseline_pred_raw)  # noqa: F821 — unfilled draft dies here (NameError by construction)
