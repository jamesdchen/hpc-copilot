"""Greenlight-names-target gate for the sequenced block verbs (design §2).

Before a block that ACTS on the cluster runs (``submit-s2`` stage & canary,
``submit-s3`` main-array launch, ``submit-s4`` harvest, ``aggregate-run`` reduce),
its precondition gate verifies that the human actually greenlit *this* verb:

* the latest journaled decision for the run (``read_decisions``, run scope) has
  ``response == "y"`` (a greenlight, not a nudge), AND
* that decision's ``resolved["next_block"]`` names THIS verb — the machine-computed
  ``next_block`` the predecessor block emitted, which the LLM surfaced and the
  human's ``y`` greenlit.

A mis-sequenced call fails loudly with :class:`errors.SpecInvalid`:

* no journaled record at all → "no journaled greenlight for <verb> — surface the
  <predecessor> brief and record the decision via append-decision";
* the latest exchange was a nudge (``response != "y"``) → the human has not
  greenlit yet;
* the greenlight named a *different* verb → both are named.

"A guard the LLM itself satisfies is not a guard" (engineering-principles.md):
prose never hardcodes the block sequence — the affordance is removed, and the
sequence is enforced HERE from the durable journal. The gate is a pure read; it
never writes.

Scope notes (verified 2026-07-03):

* The campaign driver does **NOT** route through these block verbs — it drives
  ``submit-flow`` / ``campaign_run`` directly (grep over ``meta/campaign`` finds
  no ``submit-s*`` / ``aggregate-run`` call), so the async campaign spine is
  untouched by this gate.
* No in-tree caller needs a bypass: nothing *composes* the block verbs (they are
  human-sequenced entry points), so no wire-visible bypass field is invented
  here. If a legitimate composed caller ever appears, add an explicit,
  wire-visible bypass at that call site — never a silent one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent.state.decision_journal import read_decisions

if TYPE_CHECKING:
    from pathlib import Path

# The sentinel a greenlight decision records to name the block it greenlit. The
# predecessor block computes ``next_block = {"verb": ..., ...}``; the
# append-decision that journals the human's ``y`` stores the greenlit verb under
# ``resolved[_NEXT_BLOCK_KEY]`` (the verb string, or the whole hint dict — both
# are accepted; the verb is extracted).
_NEXT_BLOCK_KEY = "next_block"

# The greenlight sentinel (decision_journal §2: ``y`` vs nudge text).
_GREENLIGHT = "y"


def _journaled_target(resolved: object) -> str | None:
    """Extract the greenlit next-block verb from a decision's ``resolved`` block.

    Accepts either the canonical string form (``resolved["next_block"] ==
    "submit-s2"``) or the whole hint dict (``{"verb": "submit-s2", ...}``) —
    whichever the journaling caller recorded.
    """
    if not isinstance(resolved, dict):
        return None
    target = resolved.get(_NEXT_BLOCK_KEY)
    if isinstance(target, dict):
        target = target.get("verb")
    return target if isinstance(target, str) else None


def assert_greenlit_target(
    experiment_dir: Path,
    *,
    run_id: str,
    verb: str,
    predecessor: str,
) -> None:
    """Refuse *verb* unless the latest run-scoped decision greenlit exactly it.

    *predecessor* is the human label of the brief that must have been surfaced
    and greenlit first (e.g. ``"S1"`` for ``submit-s2``) — it is named in the
    "no journaled greenlight" message so the failure is self-remediating.

    Raises :class:`errors.SpecInvalid` when the journal has no record, the latest
    record is a nudge (not a ``y``), or its greenlight names a different verb.
    """
    records = read_decisions(experiment_dir, "run", run_id)
    if not records:
        raise errors.SpecInvalid(
            f"no journaled greenlight for {verb} — surface the {predecessor} brief "
            f"and record the decision via append-decision (run_id={run_id!r})."
        )
    latest = records[-1]
    response = str(latest.get("response") or "")
    if response != _GREENLIGHT:
        raise errors.SpecInvalid(
            f"{verb}: the latest decision for run {run_id!r} is a nudge, not a "
            f"greenlight (response={response!r}); re-surface the {predecessor} brief "
            "and record the human's `y` via append-decision before acting."
        )
    target = _journaled_target(latest.get("resolved"))
    if target != verb:
        raise errors.SpecInvalid(
            f"{verb}: the latest greenlight for run {run_id!r} names {target!r}, "
            f"not {verb!r} — a mis-sequenced call. Run the block the human "
            f"greenlit, or re-surface the {predecessor} brief and record a "
            f"greenlight naming {verb}."
        )
